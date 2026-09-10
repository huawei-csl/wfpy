"""wfpy.cli — Command-line interface for running wfpy workflows.

Usage::

    wfpy run my_workflow.py                                # run default workflow
    wfpy run my_workflow.py --workflow DoublePipeline       # run named workflow
    wfpy run my_workflow.py --input Input=42                # provide inputs
    wfpy run my_workflow.py --out-dir ./wf-out              # output directory
    wfpy run my_workflow.py --verbose                       # detailed logging
    wfpy plan my_workflow.py --workflow DoublePipeline       # export plan JSON
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any

from wfpy.workflow_identity import (
    detect_dynamic_workflow_identity_issues,
    format_dynamic_workflow_identity_error,
)

logger = logging.getLogger("wfpy")


def _discover_project_root(start_dir: Path) -> Path | None:
    """Find nearest ancestor that looks like a project root."""

    current = start_dir.resolve()
    while True:
        if (current / "pyproject.toml").is_file() or (current / ".git").exists():
            return current
        if current.parent == current:
            return None
        current = current.parent


def _module_search_paths(path: Path) -> list[Path]:
    """Compute import search paths for a workflow module file."""

    paths: list[Path] = []

    module_dir = path.parent.resolve()
    paths.append(module_dir)

    package_dir = module_dir
    while (package_dir / "__init__.py").is_file():
        parent = package_dir.parent
        if parent not in paths:
            paths.append(parent)
        package_dir = parent

    project_root = _discover_project_root(module_dir)
    if project_root is not None and project_root not in paths:
        paths.append(project_root)

    # Workflows often import a sibling `src` package from an ancestor project
    # root. Include those roots so workflow files nested in sub-directories can
    # still resolve them.
    current = module_dir
    while True:
        if (current / "src" / "__init__.py").is_file() and current not in paths:
            paths.append(current)
        if current.parent == current:
            break
        current = current.parent

    return paths


def _path_is_within_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _search_path_packages(search_paths: list[Path]) -> dict[str, list[Path]]:
    packages: dict[str, list[Path]] = {}
    for search_path in search_paths:
        try:
            entries = list(search_path.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir() or not (entry / "__init__.py").is_file():
                continue
            roots = packages.setdefault(entry.name, [])
            if search_path not in roots:
                roots.append(search_path)
    return packages


def _module_origin_path(module: Any) -> Path | None:
    module_file = getattr(module, "__file__", None)
    if isinstance(module_file, str) and module_file:
        return Path(module_file).resolve()

    spec = getattr(module, "__spec__", None)
    origin = getattr(spec, "origin", None)
    if isinstance(origin, str) and origin and origin not in {"built-in", "frozen"}:
        return Path(origin).resolve()

    module_path = getattr(module, "__path__", None)
    if module_path is not None:
        for entry in module_path:
            if isinstance(entry, str) and entry:
                return Path(entry).resolve()
    return None


def _evict_conflicting_search_path_packages(search_paths: list[Path]) -> None:
    package_roots = _search_path_packages(search_paths)
    if not package_roots:
        return

    for top_level, roots in package_roots.items():
        loaded = sys.modules.get(top_level)
        if loaded is None:
            continue
        origin = _module_origin_path(loaded)
        if origin is not None and any(_path_is_within_root(origin, root) for root in roots):
            continue
        for module_name in list(sys.modules):
            if module_name == top_level or module_name.startswith(f"{top_level}."):
                sys.modules.pop(module_name, None)


def _resolve_module_name_and_package(path: Path) -> tuple[str, str | None]:
    """Resolve module import identity for a file path.

    Returns (module_name, package_name). package_name is None when the file
    does not live inside a regular package (no __init__.py chain).
    """

    package_parts: list[str] = []
    package_dir = path.parent.resolve()
    while (package_dir / "__init__.py").is_file():
        package_parts.insert(0, package_dir.name)
        package_dir = package_dir.parent

    if not package_parts:
        return "__wfpy_user_module__", None

    package_name = ".".join(package_parts)
    if path.name == "__init__.py":
        return package_name, package_name

    return f"{package_name}.{path.stem}", package_name


def _load_module(filepath: str) -> Any:
    """Dynamically load a Python module from a file path."""
    path = Path(filepath).resolve()
    if not path.exists():
        print(f"Error: file not found: {filepath}", file=sys.stderr)
        sys.exit(1)

    source_text = path.read_text(encoding="utf-8")
    issues = detect_dynamic_workflow_identity_issues(source_text)
    if issues:
        print(
            f"Error: {format_dynamic_workflow_identity_error(str(path), issues)}",
            file=sys.stderr,
        )
        sys.exit(1)

    search_paths = _module_search_paths(path)
    _evict_conflicting_search_path_packages(search_paths)
    importlib.invalidate_caches()

    for search_path in reversed(search_paths):
        search_path_str = str(search_path)
        if search_path_str and search_path_str not in sys.path:
            sys.path.insert(0, search_path_str)

    module_name, package_name = _resolve_module_name_and_package(path)
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        print(f"Error: cannot load module from: {filepath}", file=sys.stderr)
        sys.exit(1)

    module = importlib.util.module_from_spec(spec)
    if package_name:
        module.__package__ = package_name

    sys.modules.pop(module_name, None)
    if module_name != "__wfpy_user_module__":
        sys.modules.pop("__wfpy_user_module__", None)

    sys.modules[module_name] = module
    # Keep backwards compatibility with any internal references to the legacy alias.
    sys.modules["__wfpy_user_module__"] = module
    spec.loader.exec_module(module)
    return module


def _find_workflows(module: Any) -> dict[str, Any]:
    """Find all @workflow-decorated objects in a module."""
    workflows: dict[str, Any] = {}
    for name in dir(module):
        obj = getattr(module, name)
        if hasattr(obj, "_wfpy_workflow"):
            workflows[name] = obj
    return workflows


def _parse_input_value(raw: str) -> Any:
    """Parse a CLI --input value.  Supports int, float, bool, JSON, or plain string."""
    # Try int
    try:
        return int(raw)
    except ValueError:
        pass
    # Try float
    try:
        return float(raw)
    except ValueError:
        pass
    # Try JSON
    if raw.startswith(("{", "[", '"')):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    # Check for file path
    if Path(raw).exists():
        return str(Path(raw).resolve())
    # Plain string
    return raw


def cmd_run(args: argparse.Namespace) -> None:
    """Execute the 'run' sub-command."""
    from wfpy.runner import run

    module = _load_module(args.file)
    workflows = _find_workflows(module)

    if not workflows:
        print(f"Error: no @workflow found in {args.file}", file=sys.stderr)
        sys.exit(1)

    # Pick the workflow
    if args.workflow:
        target = workflows.get(args.workflow)
        if target is None:
            available = ", ".join(workflows.keys())
            print(
                f"Error: workflow {args.workflow!r} not found. Available: {available}",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        if len(workflows) > 1:
            available = ", ".join(workflows.keys())
            print(
                f"Multiple workflows found: {available}. Use --workflow to select one.",
                file=sys.stderr,
            )
            sys.exit(1)
        target = next(iter(workflows.values()))

    # Parse inputs
    inputs: dict[str, Any] = {}
    for inp in args.input or []:
        if "=" not in inp:
            print(f"Error: --input must be key=value, got: {inp!r}", file=sys.stderr)
            sys.exit(1)
        key, _, val = inp.partition("=")
        inputs[key] = _parse_input_value(val)

    # Run
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s: %(message)s")
    else:
        logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")

    if getattr(args, "agent_tools", False) and getattr(args, "agent_tool_auth", None) is None:
        args.agent_tool_auth = "allow-all"

    # Auto-discover agent-tools.json from source directory when --agent-tools is used
    agent_tool_registry = getattr(args, "agent_tool_registry", None)
    if not agent_tool_registry and getattr(args, "agent_tools", False):
        candidate = Path(args.file).resolve().parent / "agent-tools.json"
        if candidate.is_file():
            agent_tool_registry = str(candidate)

    outputs = run(
        target,
        inputs=inputs or None,
        out_dir=args.out_dir,
        run_id=getattr(args, "run_id", None),
        work_dir=getattr(args, "work_dir", None),
        keep_work_dir=getattr(args, "keep_work_dir", False),
        verbose=args.verbose,
        source_path=str(Path(args.file).resolve()),
        agent_tool_auth=getattr(args, "agent_tool_auth", None),
        agent_tool_policy=getattr(args, "agent_tool_policy", None),
        agent_tool_registry=agent_tool_registry,
        agent_tool_timeout_ms=getattr(args, "agent_tool_timeout_ms", 30_000),
        agent_stream=getattr(args, "agent_stream", False),
        interactive=getattr(args, "interactive", None),
        elicit_timeout_ms=getattr(args, "elicit_timeout_ms", 600_000),
        elicit_default=getattr(args, "elicit_default", None),
        elicit_require=getattr(args, "elicit_require", False),
        resume_chat_from=getattr(args, "resume_chat_from", None),
        validate=getattr(args, "validate", None),
        queue_trace=getattr(args, "queue_trace", True),
        keep_intermediates=getattr(args, "keep_intermediates", False),
        agent_debug=getattr(args, "agent_debug", False),
        agent_cli_tools_mode=getattr(args, "agent_cli_tools_mode", None),
        agent_cli_opencode_command=getattr(args, "agent_cli_opencode_command", None),
        agent_cli_opencode_args=getattr(args, "agent_cli_opencode_args", None),
        agent_cli_opencode_agent=getattr(args, "agent_cli_opencode_agent", None),
        agent_cli_opencode_native_args=getattr(args, "agent_cli_opencode_native_args", None),
        agent_cli_claude_command=getattr(args, "agent_cli_claude_command", None),
        agent_cli_claude_args=getattr(args, "agent_cli_claude_args", None),
        agent_cli_claude_agent=getattr(args, "agent_cli_claude_agent", None),
        agent_cli_claude_native_args=getattr(args, "agent_cli_claude_native_args", None),
        agent_cli_codex_command=getattr(args, "agent_cli_codex_command", None),
        agent_cli_codex_args=getattr(args, "agent_cli_codex_args", None),
        agent_cli_codex_subcommand=getattr(args, "agent_cli_codex_subcommand", None),
        agent_cli_codex_native_args=getattr(args, "agent_cli_codex_native_args", None),
        skill_hook_auth=getattr(args, "skill_hook_auth", None),
        skill_hook_policy=getattr(args, "skill_hook_policy", None),
        context_mode=getattr(args, "context_mode", None),
        context_budget=getattr(args, "context_budget", None),
        context_summarize=getattr(args, "context_summarize", None),
        resume_context_from=getattr(args, "resume_context_from", None),
    )

    # Print outputs
    print("\n=== Workflow outputs ===")
    for port_name, values in outputs.items():
        if values is None:
            print(f"  {port_name}: (no output)")
        elif isinstance(values, list) and len(values) == 1:
            print(f"  {port_name}: {values[0]}")
        else:
            print(f"  {port_name}: {values}")


def cmd_plan(args: argparse.Namespace) -> None:
    """Execute the 'plan' sub-command — export plan JSON."""
    from wfpy.runner import build_plan, export_plan_json, _build_workflow_graph
    from wfpy.graph import export_graph_json
    from wfpy.partial import build_partial_graph

    module = _load_module(args.file)
    workflows = _find_workflows(module)

    # A diagram drill-down into a workflow a factory builds names that
    # workflow, which is no module attribute until the factory is called.
    factory = None
    if (
        getattr(args, "format", "plan") == "graph"
        and getattr(args, "best_effort", False)
        and args.workflow
        and args.workflow not in workflows
    ):
        factory = _find_workflow_factory(module, args.file, args.workflow)

    if not workflows and factory is None:
        print(f"Error: no @workflow found in {args.file}", file=sys.stderr)
        sys.exit(1)

    if factory is not None:
        try:
            wf_def = _elaborate_factory_for_display(factory)._wfpy_workflow
            plan_json = export_graph_json(_build_workflow_graph(wf_def))
        except Exception as exc:
            plan_json = build_partial_graph(args.file, args.workflow, exc)
        _write_plan_json(plan_json, args.output)
        return

    if args.workflow:
        target = workflows.get(args.workflow)
        if target is None:
            print(f"Error: workflow {args.workflow!r} not found.", file=sys.stderr)
            sys.exit(1)
    else:
        target = next(iter(workflows.values()))

    wf_def = target._wfpy_workflow
    if getattr(args, "format", "plan") == "graph":
        try:
            graph = _build_workflow_graph(wf_def)
            plan_json = export_graph_json(graph)
        except Exception as exc:
            if not getattr(args, "best_effort", False):
                raise
            plan_json = build_partial_graph(args.file, args.workflow, exc)
    else:
        graph = _build_workflow_graph(wf_def)
        plan = build_plan(graph, wf_def)
        plan_json = export_plan_json(plan)

    _write_plan_json(plan_json, args.output)


def _write_plan_json(plan_json: Any, out_path: str | None) -> None:
    if out_path:
        Path(out_path).write_text(json.dumps(plan_json, indent=2))
        print(f"Plan written to {out_path}")
    else:
        print(json.dumps(plan_json, indent=2))


def _find_workflow_factory(module: Any, file_path: str, workflow_name: str) -> Any | None:
    """The module-level function whose body defines ``@workflow workflow_name``.

    Found in the source rather than by calling anything: only a function that
    visibly builds that workflow is a factory worth calling.
    """
    import ast
    import inspect

    def is_workflow_decorator(decorator: ast.expr) -> bool:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        return (isinstance(target, ast.Name) and target.id == "workflow") or (
            isinstance(target, ast.Attribute) and target.attr == "workflow"
        )

    tree = ast.parse(Path(file_path).read_text(encoding="utf-8"))
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if (
                node is not fn
                and isinstance(node, ast.FunctionDef)
                and node.name == workflow_name
                and any(is_workflow_decorator(d) for d in node.decorator_list)
            ):
                factory = getattr(module, fn.name, None)
                return factory if inspect.isfunction(factory) else None
    return None


def _elaborate_factory_for_display(factory: Any) -> Any:
    """Call a workflow factory so the workflow it returns can be drawn.

    A diagram drill-down has no arguments to give it: parameters keep their
    defaults and the rest get ``<name>`` placeholders. For ``plan --format
    graph --best-effort`` only -- a workflow built this way is for looking
    at, never for running.
    """
    import inspect

    kwargs: dict[str, Any] = {}
    for param in inspect.signature(factory).parameters.values():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD) or param.default is not param.empty:
            continue
        if param.kind is param.POSITIONAL_ONLY:
            raise TypeError(
                f"{factory.__name__}(): positional-only parameter {param.name!r} has no default"
            )
        kwargs[param.name] = f"<{param.name}>"
    target = factory(**kwargs)
    if not hasattr(target, "_wfpy_workflow"):
        raise TypeError(f"{factory.__name__}() did not return a @workflow")
    return target


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="wfpy",
        description="wfpy — Pythonic agentic workflows on actor-dataflow semantics",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ── run ──
    run_parser = subparsers.add_parser("run", help="Run a workflow")
    run_parser.add_argument("file", help="Python file containing @workflow definitions")
    run_parser.add_argument("--workflow", "-w", help="Name of the workflow to run")
    run_parser.add_argument(
        "--input",
        "-i",
        action="append",
        help="Input values as key=value (can be repeated)",
    )
    run_parser.add_argument("--out-dir", "-o", default="./wf-out", help="Base output directory")
    run_parser.add_argument("--run-id", default=None, help="Override auto-generated run ID")
    run_parser.add_argument(
        "--work-dir", default=None, help="Explicit temp working directory (not cleaned up)"
    )
    run_parser.add_argument(
        "--keep-work-dir", action="store_true", help="Keep auto-created temp dir after run"
    )
    run_parser.add_argument(
        "--agent-tool-auth",
        default=None,
        choices=["deny-all", "allow-all", "policy"],
        help="Agent tool authorization mode",
    )
    run_parser.add_argument("--agent-tools", action="store_true", help="Enable agent tool calling")
    run_parser.add_argument(
        "--agent-tool-registry",
        default=None,
        help="Path to agent-tools.json for MCP tool discovery (auto-discovered from source dir with --agent-tools)",
    )
    run_parser.add_argument(
        "--agent-tool-policy",
        default=None,
        help="Path to agent tool policy JSON file (for --agent-tool-auth=policy)",
    )
    run_parser.add_argument(
        "--agent-tool-timeout-ms",
        type=int,
        default=30_000,
        help="Timeout for agent tool execution (ms)",
    )
    run_parser.add_argument(
        "--agent-stream", action="store_true", help="Enable streaming for agent LLM calls"
    )
    run_parser.add_argument(
        "--interactive",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Allow @agent(ask_user=True) agents to prompt for input at the terminal "
        "(default: autodetect a TTY). Use --no-interactive to force headless behavior.",
    )
    run_parser.add_argument(
        "--elicit-timeout-ms",
        type=int,
        default=600_000,
        help="Max time to wait for a user's reply to an agent question (ms)",
    )
    run_parser.add_argument(
        "--elicit-default",
        default=None,
        help="In non-interactive runs, answer agent questions with this fixed text "
        "instead of degrading gracefully",
    )
    run_parser.add_argument(
        "--elicit-require",
        action="store_true",
        help="Fail the run if an agent asks the user but no answer source is available "
        "(instead of proceeding gracefully)",
    )
    run_parser.add_argument(
        "--resume-chat-from",
        default=None,
        help="Path to prior run.wf-run.json for chat history restoration",
    )
    run_parser.add_argument(
        "--validate",
        default=None,
        choices=["off", "warn", "enforce"],
        help="Port validation mode (default: enforce)",
    )
    run_parser.add_argument(
        "--queue-trace",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Write run.wf-queues.json (default: enabled, use --no-queue-trace to disable)",
    )
    run_parser.add_argument(
        "--keep-intermediates",
        action="store_true",
        help="Copy workDir to runOutDir/work/ for debugging",
    )
    run_parser.add_argument(
        "--agent-debug",
        action="store_true",
        help="Write agent request/response artifacts to agent-debug/",
    )
    run_parser.add_argument(
        "--agent-cli-tools-mode",
        default=None,
        choices=["wfpy-none", "native"],
        help="CLI tools mode for non-http transports",
    )
    run_parser.add_argument(
        "--agent-cli-opencode-command",
        default=None,
        help="Override OpenCode CLI command/executable",
    )
    run_parser.add_argument(
        "--agent-cli-opencode-args", default=None, help="Extra arguments for OpenCode CLI backend"
    )
    run_parser.add_argument(
        "--agent-cli-opencode-agent", default=None, help="OpenCode CLI agent profile name"
    )
    run_parser.add_argument(
        "--agent-cli-opencode-native-args",
        default=None,
        help="Extra native-tool arguments for OpenCode backend",
    )
    run_parser.add_argument(
        "--agent-cli-claude-command", default=None, help="Override Claude CLI command/executable"
    )
    run_parser.add_argument(
        "--agent-cli-claude-args", default=None, help="Extra arguments for Claude CLI backend"
    )
    run_parser.add_argument(
        "--agent-cli-claude-agent", default=None, help="Claude CLI agent profile name"
    )
    run_parser.add_argument(
        "--agent-cli-claude-native-args",
        default=None,
        help="Extra native-tool arguments for Claude backend",
    )
    run_parser.add_argument(
        "--agent-cli-codex-command", default=None, help="Override Codex CLI command/executable"
    )
    run_parser.add_argument(
        "--agent-cli-codex-args", default=None, help="Extra arguments for Codex CLI backend"
    )
    run_parser.add_argument(
        "--agent-cli-codex-subcommand",
        default=None,
        help="Codex CLI subcommand inserted before prompt",
    )
    run_parser.add_argument(
        "--agent-cli-codex-native-args",
        default=None,
        help="Extra native-tool arguments for Codex backend",
    )
    run_parser.add_argument(
        "--skill-hook-auth",
        default=None,
        choices=["deny-all", "allow-all", "policy"],
        help="Skill hook authorization mode (default: policy)",
    )
    run_parser.add_argument(
        "--skill-hook-policy", default=None, help="Path to skill hook policy JSON file"
    )
    run_parser.add_argument(
        "--context-mode",
        default=None,
        choices=["off", "scoped", "full"],
        help="Shared context mode (default: scoped)",
    )
    run_parser.add_argument(
        "--context-budget", type=int, default=None, help="Shared context budget hint"
    )
    run_parser.add_argument(
        "--context-summarize",
        default=None,
        choices=["on", "off"],
        help="Shared context summarization mode",
    )
    run_parser.add_argument(
        "--resume-context-from",
        default=None,
        help="Path to prior run.wf-context.json for context restore",
    )
    run_parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    run_parser.set_defaults(func=cmd_run)

    # ── plan ──
    plan_parser = subparsers.add_parser("plan", help="Export execution plan as JSON")
    plan_parser.add_argument("file", help="Python file containing @workflow definitions")
    plan_parser.add_argument("--workflow", "-w", help="Name of the workflow to export")
    plan_parser.add_argument("--output", "-o", help="Output file path (default: stdout)")
    plan_parser.add_argument(
        "--format",
        choices=["plan", "graph"],
        default="plan",
        help="Export format: execution plan (plan) or diagram graph IR (graph)",
    )
    plan_parser.add_argument(
        "--best-effort",
        action="store_true",
        help="Return a partial graph on errors (graph format only)",
    )
    plan_parser.set_defaults(func=cmd_plan)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
