"""Internal helpers for graph JSON export (diagram IR)."""

from __future__ import annotations

from typing import Any

from wfpy.core import TaskMeta, WorkflowDef
from wfpy.types import infer_resource_kind


def _instance_type_name(instance: Any) -> str:
    """Return the class name of an instance for the diagram IR."""
    return str(getattr(instance, "_wfpy_class_name", type(instance).__name__))


def _display_type(tp: Any) -> str:
    """Return a human-readable type name for port schemas."""
    try:
        if tp is None:
            return "Any"
        if isinstance(tp, type):
            return tp.__name__
        s = str(tp)
        if s.startswith("<class '") and s.endswith("'>"):
            inner = s[len("<class '") : -2]
            return inner.split(".")[-1]
        return s
    except Exception:
        return "Any"


def _upsert_definition_annotation(
    meta_obj: dict[str, Any],
    annotation_name: str,
    annotation_args: list[dict[str, Any]] | None = None,
) -> None:
    """Add or replace a definition annotation entry in meta_obj."""
    ann_list = meta_obj.setdefault("definitionAnnotations", [])
    if not isinstance(ann_list, list):
        ann_list = []
        meta_obj["definitionAnnotations"] = ann_list

    payload: dict[str, Any] = {"name": annotation_name}
    if annotation_args is not None:
        payload["arguments"] = annotation_args

    for idx, existing in enumerate(ann_list):
        if isinstance(existing, dict) and str(existing.get("name", "")) == annotation_name:
            ann_list[idx] = payload
            return
    ann_list.append(payload)


def _wf_string_literal(value: str) -> str:
    """Escape a string value for use in wf-lang expressions."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
        .replace('"', '\\"')
    )
    return f'"{escaped}"'


def _wf_string_list_expr(items: list[str]) -> str:
    """Build a wf-lang list expression from string items."""
    return "[" + ", ".join(_wf_string_literal(item) for item in items) + "]"


def _annotation_entries(ann: Any) -> list[dict[str, Any]]:
    """Convert raw annotations dict to structured annotation entries."""
    entries: list[dict[str, Any]] = []
    if isinstance(ann, dict):
        for name, args in ann.items():
            if isinstance(args, dict):
                entries.append(
                    {
                        "name": name,
                        "arguments": [
                            {"name": str(k), "value": str(v)} for k, v in args.items()
                        ],
                    }
                )
            else:
                entries.append({"name": name})
    return entries


def _build_task_meta(
    rec: Any,
    node_id: str,
    ports: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the meta dict for a task actor node."""
    meta: dict[str, Any] = {"taskKind": rec.meta.kind}

    if rec.meta.annotations:
        meta["definitionAnnotations"] = _annotation_entries(rec.meta.annotations)
    if rec.meta.schedule is not None:
        schedule_args: list[dict[str, Any]] = [
            {"name": "initial", "value": _wf_string_literal(rec.meta.schedule.initial)}
        ]
        for idx, transition in enumerate(rec.meta.schedule.transitions):
            schedule_args.append(
                {
                    "name": f"transitions[{idx}]",
                    "value": _wf_string_literal(
                        f"({transition.state}, {transition.action}, {transition.next_state})"
                    ),
                }
            )
        _upsert_definition_annotation(meta, "schedule", schedule_args)
    if rec.meta.priority is not None:
        priority_args: list[dict[str, Any]] = []
        for idx, rule in enumerate(rec.meta.priority.rules):
            priority_args.append(
                {
                    "name": f"rules[{idx}]",
                    "value": _wf_string_list_expr(rule),
                }
            )
        _upsert_definition_annotation(meta, "priority", priority_args)
    # NOT gated on kind == "viewer". The IDE keys the double-click on this
    # annotation, not on the node's kind, and welding the two together meant a
    # node that is openable in an editor had to also be a runtime sink. A
    # StreamBlocks node is openable and emits.
    if rec.meta.annotations.get("viewer"):
        viewer_cfg = rec.meta.annotations.get("viewer")
        viewer_args: list[dict[str, Any]] = []
        if isinstance(viewer_cfg, dict):
            action = viewer_cfg.get("action")
            if action:
                viewer_args.append(
                    {"name": "action", "value": _wf_string_literal(str(action))}
                )
            inputs = viewer_cfg.get("inputs")
            if isinstance(inputs, list):
                viewer_args.append(
                    {
                        "name": "inputs",
                        "value": _wf_string_list_expr([str(i) for i in inputs]),
                    }
                )
            view_type = viewer_cfg.get("viewType")
            if isinstance(view_type, str) and view_type.strip():
                viewer_args.append(
                    {"name": "viewType", "value": _wf_string_literal(view_type)}
                )
            # Where the target comes from, and the target itself when it is a
            # declared path rather than a produced token. Easy to miss, because
            # the GENERIC annotation exporter above already emitted both and
            # this block then replaces its entry wholesale — so anything not
            # repeated here is silently dropped on the way out.
            source = viewer_cfg.get("source")
            if isinstance(source, str) and source.strip():
                viewer_args.append(
                    {"name": "source", "value": _wf_string_literal(source)}
                )
            declared_path = viewer_cfg.get("path")
            if isinstance(declared_path, str) and declared_path.strip():
                viewer_args.append(
                    {"name": "path", "value": _wf_string_literal(declared_path)}
                )
            # A source declares its target as a PARAMETER, so the value is
            # per instance while `viewer_cfg` is the class's own dict, shared
            # by every instance of it. Read the instance and never write back:
            # mutating the annotation here would leak one node's path onto its
            # siblings.
            if rec.meta.kind == "source":
                instance_path = getattr(rec.instance, "path", None)
                locator = "" if instance_path is None else str(instance_path)
                # Both conditions, though the second implies the first: the
                # narrowing has to be on the value that gets passed on, not on
                # the string derived from it.
                if instance_path is not None and locator:
                    viewer_args.append(
                        {"name": "path", "value": _wf_string_literal(locator)}
                    )
                    # What the target IS, settled here because this is where the
                    # filesystem is visible. The diagram cannot tell a folder
                    # from a file by looking at the string.
                    resource_kind = infer_resource_kind(instance_path)
                    # A locator with a scheme is not a local file whatever the
                    # port type says: `File` pins kind="file" as a class
                    # default, so it describes the port, not this value.
                    locator_kind = infer_resource_kind(locator)
                    if locator_kind in {"http", "url"}:
                        resource_kind = locator_kind
                    if resource_kind:
                        viewer_args.append(
                            {"name": "kind", "value": _wf_string_literal(resource_kind)}
                        )
            command = viewer_cfg.get("command")
            if isinstance(command, str) and command.strip():
                viewer_args.append(
                    {"name": "command", "value": _wf_string_literal(command)}
                )
            command_args = viewer_cfg.get("args")
            if isinstance(command_args, list) and command_args:
                viewer_args.append(
                    {
                        "name": "args",
                        "value": _wf_string_list_expr([str(item) for item in command_args]),
                    }
                )
        _upsert_definition_annotation(meta, "viewer", viewer_args)
    meta["parameters"] = [
        {
            "name": name,
            "kind": "value",
            "type": _display_type(tp),
            "value": repr(getattr(rec.instance, name, None)),
        }
        for name, tp in rec.meta.parameters.items()
    ]
    meta["definitionParams"] = [
        {"name": name, "kind": "value", "type": _display_type(tp)}
        for name, tp in rec.meta.parameters.items()
    ]
    if rec.meta.kind == "viewer":
        meta["viewer"] = True
    if rec.meta.kind == "source":
        # Say what this node IS. The double-click is only read on an external
        # node, and a source stands for something outside the graph by
        # definition — the resource it hands in.
        meta["external"] = True
        meta["source"] = dict(rec.meta.annotations.get("source") or {})
    if rec.meta.kind == "streamblocks":
        annotation = rec.meta.annotations.get("streamblocks") or {}
        meta["streamblocks"] = dict(annotation)
        # Say what this node IS, rather than relying on the platform to
        # recognise the word "streamblocks". The platform renders and treats a
        # node as external when the producer says so; a kind list it would have
        # to extend for every product is not neutral. Without this the node
        # draws as an ordinary actor AND cannot be opened by double-click,
        # because the annotation is only read on an external-actor node.
        meta["external"] = True
        # An instance with no network cannot compile, run or open. That is wrong
        # in the source whether or not anything has run, so it goes out as a
        # static diagnostic — which the diagram keeps across run-overlay
        # cleanup, unlike a run marker.
        if annotation.get("facade") == "instance" and not str(annotation.get("network") or "").strip():
            diagnostics = list(meta.get("diagnostics") or [])
            diagnostics.append(
                {
                    "severity": "error",
                    "message": (
                        "StreamBlocks instance has no network= to compile, run or open."
                    ),
                }
            )
            meta["diagnostics"] = diagnostics
    if rec.meta.annotations:
        meta["annotations"] = rec.meta.annotations
    if rec.meta.schedule is not None:
        meta["schedule"] = {
            "initial": rec.meta.schedule.initial,
            "transitions": [
                {
                    "state": t.state,
                    "action": t.action,
                    "nextState": t.next_state,
                }
                for t in rec.meta.schedule.transitions
            ],
        }
    if rec.meta.priority is not None:
        meta["priority"] = {"rules": rec.meta.priority.rules}
    for attr, pd in rec.meta.ports.items():
        port_name = pd.name or attr
        ports.append(
            {
                "id": f"port:{node_id}:{port_name}",
                "name": port_name,
                "direction": pd.direction,
                "type": _display_type(pd.port_type),
                "role": "data",
                "source": {
                    "file": pd.source_file,
                    "line": pd.source_line,
                }
                if pd.source_file and pd.source_line
                else None,
            }
        )
    if rec.meta.tool_spec:
        tool_args: list[dict[str, Any]] = []
        meta["tool"] = {
            "cmd": rec.meta.tool_spec.cmd,
            "args": rec.meta.tool_spec.args,
        }
        if rec.meta.tool_spec.cmd:
            tool_args.append(
                {"name": "cmd", "value": _wf_string_literal(rec.meta.tool_spec.cmd)}
            )
        if rec.meta.tool_spec.args:
            tool_args.append(
                {"name": "args", "value": _wf_string_list_expr(rec.meta.tool_spec.args)}
            )
        tool_args.append(
            {
                "name": "inheritStdio",
                "value": "true" if rec.meta.tool_spec.inherit_stdio else "false",
            }
        )
        _upsert_definition_annotation(meta, "tool", tool_args)
    if rec.meta.agent_spec:
        _build_agent_meta(meta, rec)
    return meta


def _build_agent_meta(meta: dict[str, Any], rec: Any) -> None:
    """Build agent-specific meta fields and definition annotations."""
    meta["agent"] = {
        "prompt": rec.meta.agent_spec.prompt,
        "model": rec.meta.agent_spec.model,
    }
    agent_args: list[dict[str, Any]] = []
    if rec.meta.agent_spec.prompt:
        agent_args.append(
            {"name": "prompt", "value": _wf_string_literal(rec.meta.agent_spec.prompt)}
        )
    if rec.meta.agent_spec.claude_agent:
        agent_args.append(
            {
                "name": "claudeAgent",
                "value": _wf_string_literal(rec.meta.agent_spec.claude_agent),
            }
        )
    if rec.meta.agent_spec.skill:
        agent_args.append(
            {"name": "skill", "value": _wf_string_literal(rec.meta.agent_spec.skill)}
        )
    if rec.meta.agent_spec.model:
        agent_args.append(
            {"name": "model", "value": _wf_string_literal(rec.meta.agent_spec.model)}
        )
    if rec.meta.agent_spec.provider:
        agent_args.append(
            {
                "name": "provider",
                "value": _wf_string_literal(rec.meta.agent_spec.provider),
            }
        )
    if rec.meta.agent_spec.endpoint:
        agent_args.append(
            {
                "name": "endpoint",
                "value": _wf_string_literal(rec.meta.agent_spec.endpoint),
            }
        )
    if rec.meta.agent_spec.timeout_ms:
        agent_args.append(
            {"name": "timeoutMs", "value": str(rec.meta.agent_spec.timeout_ms)}
        )
    if rec.meta.agent_spec.context_budget:
        agent_args.append(
            {"name": "contextBudget", "value": str(rec.meta.agent_spec.context_budget)}
        )
    if rec.meta.agent_spec.use_claude_agent is not None:
        agent_args.append(
            {
                "name": "useClaudeAgent",
                "value": "true" if rec.meta.agent_spec.use_claude_agent else "false",
            }
        )
    # Always emit useSkill so the GUI reliably reflects the state.
    _skill_active = (
        bool(rec.meta.agent_spec.use_skill)
        if rec.meta.agent_spec.use_skill is not None
        else bool(rec.meta.agent_spec.skill)
    )
    agent_args.append(
        {
            "name": "useSkill",
            "value": "true" if _skill_active else "false",
        }
    )
    # Always emit usePrompt so the GUI reliably reflects the state.
    _prompt_has_text = bool(rec.meta.agent_spec.prompt)
    agent_args.append(
        {
            "name": "usePrompt",
            "value": "true" if _prompt_has_text else "false",
        }
    )
    # Only emit useSkillHooks when it overrides the default (true)
    if (
        rec.meta.agent_spec.use_skill_hooks is not None
        and not rec.meta.agent_spec.use_skill_hooks
    ):
        agent_args.append(
            {
                "name": "useSkillHooks",
                "value": "false",
            }
        )
    agent_args.append(
        {
            "name": "stateful",
            "value": "true" if rec.meta.agent_spec.stateful else "false",
        }
    )
    if getattr(rec.meta.agent_spec, "ask_user", False):
        agent_args.append(
            {
                "name": "askUser",
                "value": "true",
            }
        )
    if rec.meta.agent_spec.truncation_strategy:
        agent_args.append(
            {
                "name": "truncationStrategy",
                "value": _wf_string_literal(rec.meta.agent_spec.truncation_strategy),
            }
        )
    # Invocation backend transport + CLI tooling — emitted when non-default so the GUI
    # can show/edit them (http/wfpy-none default to absent).
    if rec.meta.agent_spec.transport and rec.meta.agent_spec.transport != "http":
        agent_args.append(
            {
                "name": "transport",
                "value": _wf_string_literal(rec.meta.agent_spec.transport),
            }
        )
    if rec.meta.agent_spec.cli_tools_mode and rec.meta.agent_spec.cli_tools_mode != "wfpy-none":
        agent_args.append(
            {
                "name": "cliToolsMode",
                "value": _wf_string_literal(rec.meta.agent_spec.cli_tools_mode),
            }
        )
    if rec.meta.agent_spec.variant:
        agent_args.append(
            {
                "name": "reasoningEffort",
                "value": _wf_string_literal(rec.meta.agent_spec.variant),
            }
        )
    if rec.meta.agent_spec.fireable_without_input:
        agent_args.append(
            {
                "name": "fireableWithoutInput",
                "value": str(rec.meta.agent_spec.fireable_without_input),
            }
        )
    # MCP servers — serialize for GUI display
    if rec.meta.agent_spec.mcp_servers or rec.meta.agent_spec.mcp_server_configs:
        agent_args.append(
            {
                "name": "useMcp",
                "value": "true",
            }
        )
    if rec.meta.agent_spec.mcp_servers:
        agent_args.append(
            {
                "name": "mcpServers",
                "value": _wf_string_list_expr(rec.meta.agent_spec.mcp_servers),
            }
        )
    # Emit inline MCP server configs as JSON for rich GUI display
    if rec.meta.agent_spec.mcp_server_configs:
        import json as _json

        _mcp_cfgs = []
        for _cfg in rec.meta.agent_spec.mcp_server_configs:
            _mcp_cfgs.append(
                {
                    "name": _cfg.name,
                    "transport": _cfg.transport,
                    "url": _cfg.url,
                    "command": _cfg.command,
                    "args": _cfg.args,
                }
            )
        agent_args.append(
            {
                "name": "mcpServerConfigs",
                "value": _json.dumps(_mcp_cfgs),
            }
        )
    # LSP — serialize full config for GUI display
    if rec.meta.agent_spec.lsp_command:
        agent_args.append(
            {
                "name": "lspCommand",
                "value": _wf_string_literal(rec.meta.agent_spec.lsp_command),
            }
        )
        # Shorthand scalars accompanying lsp_command — emitted when non-default so the
        # GUI can show/edit the single-command LSP form.
        if rec.meta.agent_spec.lsp_args:
            agent_args.append(
                {
                    "name": "lspArgs",
                    "value": _wf_string_list_expr(rec.meta.agent_spec.lsp_args),
                }
            )
        if rec.meta.agent_spec.lsp_language_id and rec.meta.agent_spec.lsp_language_id != "cpp":
            agent_args.append(
                {
                    "name": "lspLanguageId",
                    "value": _wf_string_literal(rec.meta.agent_spec.lsp_language_id),
                }
            )
        if rec.meta.agent_spec.lsp_extra_flags:
            agent_args.append(
                {
                    "name": "lspExtraFlags",
                    "value": _wf_string_list_expr(rec.meta.agent_spec.lsp_extra_flags),
                }
            )
        if rec.meta.agent_spec.lsp_severity_threshold and rec.meta.agent_spec.lsp_severity_threshold != "error":
            agent_args.append(
                {
                    "name": "lspSeverityThreshold",
                    "value": _wf_string_literal(rec.meta.agent_spec.lsp_severity_threshold),
                }
            )
        if rec.meta.agent_spec.lsp_max_repair_attempts and rec.meta.agent_spec.lsp_max_repair_attempts != 2:
            agent_args.append(
                {
                    "name": "lspMaxRepairAttempts",
                    "value": str(rec.meta.agent_spec.lsp_max_repair_attempts),
                }
            )
    if rec.meta.agent_spec.lsp_servers:
        agent_args.append(
            {
                "name": "useLsp",
                "value": "true",
            }
        )
        commands = [cfg.command for cfg in rec.meta.agent_spec.lsp_servers]
        agent_args.append(
            {
                "name": "lspServers",
                "value": _wf_string_list_expr(commands),
            }
        )
        # Emit full LSP server configs as JSON for rich GUI display
        import json as _json

        _lsp_cfgs = []
        for _lsp_cfg in rec.meta.agent_spec.lsp_servers:
            _lsp_cfgs.append(
                {
                    "name": _lsp_cfg.command,
                    "command": _lsp_cfg.command,
                    "args": _lsp_cfg.args,
                    "languageId": _lsp_cfg.language_id,
                    "extraFlags": _lsp_cfg.extra_flags or [],
                    "ports": _lsp_cfg.ports or [],
                    "maxRepairAttempts": _lsp_cfg.max_repair_attempts,
                    "severityThreshold": _lsp_cfg.severity_threshold,
                }
            )
        agent_args.append(
            {
                "name": "lspServerConfigs",
                "value": _json.dumps(_lsp_cfgs),
            }
        )
    # Output validators (cmd / lsp mode) — emitted as JSON for the GUI. Unlike the MCP/LSP
    # configs this round-trips to source: validator configs contain no booleans/None, so the
    # JSON is also a valid Python literal the @agent decorator can re-parse.
    if rec.meta.agent_spec.output_validators:
        import json as _json_ov

        _ov_cfgs: list[dict[str, Any]] = []
        for _ov in rec.meta.agent_spec.output_validators:
            _ov_cfg: dict[str, Any] = {
                "kind": _ov.kind,
                "cmd": _ov.cmd,
                "args": _ov.args,
                "maxRepairAttempts": _ov.max_repair_attempts,
                "severityThreshold": _ov.severity_threshold,
            }
            if _ov.ports:
                _ov_cfg["ports"] = _ov.ports
            if _ov.kind == "lsp":
                if _ov.language_id:
                    _ov_cfg["languageId"] = _ov.language_id
                if _ov.extra_flags:
                    _ov_cfg["extraFlags"] = _ov.extra_flags
            _ov_cfgs.append(_ov_cfg)
        agent_args.append(
            {
                "name": "outputValidators",
                "value": _json_ov.dumps(_ov_cfgs),
            }
        )
    _upsert_definition_annotation(meta, "agent", agent_args)


def export_graph_json(graph: Any) -> dict[str, Any]:
    """Export a WorkflowGraph as a JSON-serializable dict (diagram IR)."""
    from wfpy.graph import ControlPortInstance

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    scopes: list[dict[str, Any]] = []
    node_scope_map: dict[str, str] = {}
    edge_scope_map: dict[str, str] = {}
    scope_children: dict[str, list[str]] = {}
    scope_nodes: dict[str, list[str]] = {}
    scope_edges: dict[str, list[str]] = {}

    wf_inputs: set[str] = set()
    wf_outputs: set[str] = set()

    for conn in graph.connections:
        if isinstance(conn.from_port, str):
            wf_inputs.add(conn.from_port)
        if isinstance(conn.to_port, str):
            wf_outputs.add(conn.to_port)

    # Workflow I/O nodes
    for name in sorted(wf_inputs):
        node_id = f"wf:input:{name}"
        node_scope_map[node_id] = "scope:root"
        nodes.append(
            {
                "id": node_id,
                "kind": "wf-input",
                "label": name,
                "scope": "scope:root",
                "ports": [
                    {
                        "id": f"port:{node_id}:out",
                        "name": name,
                        "direction": "out",
                        "type": "any",
                        "role": "data",
                    }
                ],
            }
        )

    for name in sorted(wf_outputs):
        node_id = f"wf:output:{name}"
        node_scope_map[node_id] = "scope:root"
        nodes.append(
            {
                "id": node_id,
                "kind": "wf-output",
                "label": name,
                "scope": "scope:root",
                "ports": [
                    {
                        "id": f"port:{node_id}:in",
                        "name": name,
                        "direction": "in",
                        "type": "any",
                        "role": "data",
                    }
                ],
            }
        )

    # Control nodes
    for record in graph.control_nodes.values():
        if record.kind == "if":
            node_scope_map[record.node_id] = record.parent_scope_id
            nodes.append(
                {
                    "id": record.node_id,
                    "kind": "if",
                    "label": record.name,
                    "scope": record.parent_scope_id,
                    "ports": [
                        {
                            "id": f"port:{record.node_id}:cond",
                            "name": "cond",
                            "direction": "in",
                            "type": "bool",
                            "role": "control",
                        },
                        {
                            "id": f"port:{record.node_id}:out",
                            "name": "out",
                            "direction": "out",
                            "type": "any",
                            "role": "control",
                        },
                    ],
                    "branches": dict(record.scopes),
                    "meta": {
                        "controlKind": "if",
                        "condition": repr(record.condition),
                    },
                }
            )
        elif record.kind == "loop":
            node_scope_map[record.node_id] = record.parent_scope_id
            nodes.append(
                {
                    "id": record.node_id,
                    "kind": "loop",
                    "label": record.name,
                    "scope": record.parent_scope_id,
                    "ports": [
                        {
                            "id": f"port:{record.node_id}:iter",
                            "name": "iter",
                            "direction": "in",
                            "type": "iterable",
                            "role": "control",
                        },
                        {
                            "id": f"port:{record.node_id}:item",
                            "name": "item",
                            "direction": "out",
                            "type": "any",
                            "role": "control",
                        },
                        {
                            "id": f"port:{record.node_id}:out",
                            "name": "out",
                            "direction": "out",
                            "type": "any",
                            "role": "control",
                        },
                    ],
                    "body": record.scopes.get("body"),
                    "meta": {
                        "controlKind": "loop",
                        "iterable": repr(record.iterable),
                    },
                }
            )

    # Actor nodes
    for name, rec in graph.actors.items():
        node_id = f"node:{rec.scope_id}:{name}"
        ports: list[dict[str, Any]] = []
        meta: dict[str, Any] = {}
        source_map: dict[str, Any] | None = None

        if isinstance(rec.meta, TaskMeta):
            meta = _build_task_meta(rec, node_id, ports)
        elif isinstance(rec.meta, WorkflowDef):
            meta["taskKind"] = "workflow"
            for port_name, port_type in rec.meta.input_names.items():
                ports.append(
                    {
                        "id": f"port:{node_id}:{port_name}",
                        "name": port_name,
                        "direction": "in",
                        "type": _display_type(port_type),
                        "role": "data",
                    }
                )
            for port_name, port_type in rec.meta.output_names.items():
                ports.append(
                    {
                        "id": f"port:{node_id}:{port_name}",
                        "name": port_name,
                        "direction": "out",
                        "type": _display_type(port_type),
                        "role": "data",
                    }
                )

        node_scope_map[node_id] = rec.scope_id
        node_entry = {
            "id": node_id,
            "kind": getattr(rec.meta, "kind", "workflow"),
            "label": name,
            "type": _instance_type_name(rec.instance),
            "scope": rec.scope_id,
            "ports": ports,
            "meta": meta,
        }
        if getattr(rec.instance, "_wfpy_source", None):
            source_map = rec.instance._wfpy_source
        if source_map:
            meta["source"] = source_map
        nodes.append(node_entry)

    # Scopes
    for scope in graph.scopes.values():
        scope_children.setdefault(scope.id, [])
        if scope.parent_id:
            scope_children.setdefault(scope.parent_id, []).append(scope.id)
        scope_nodes.setdefault(scope.id, [])
        scope_edges.setdefault(scope.id, [])
        scopes.append(
            {
                "id": scope.id,
                "kind": scope.kind,
                "parent": scope.parent_id,
                "controlNodeId": scope.control_node_id,
            }
        )

    # Edges
    for conn in graph.connections:
        from_node = ""
        to_node = ""
        if isinstance(conn.from_port, str):
            from_node = f"wf:input:{conn.from_port}"
            from_port = f"port:{from_node}:out"
        elif isinstance(conn.from_port, ControlPortInstance):
            from_node = conn.from_port.control_node_id
            from_port = f"port:{conn.from_port.control_node_id}:{conn.from_port.port_name}"
        else:
            inst_name = conn.from_port.actor_instance._wfpy_instance_name
            node_id = f"node:{graph.actors[inst_name].scope_id}:{inst_name}"
            from_node = node_id
            from_port = f"port:{node_id}:{conn.from_port.port_name}"

        if isinstance(conn.to_port, str):
            to_node = f"wf:output:{conn.to_port}"
            to_port = f"port:{to_node}:in"
        elif isinstance(conn.to_port, ControlPortInstance):
            to_node = conn.to_port.control_node_id
            to_port = f"port:{conn.to_port.control_node_id}:{conn.to_port.port_name}"
        else:
            inst_name = conn.to_port.actor_instance._wfpy_instance_name
            node_id = f"node:{graph.actors[inst_name].scope_id}:{inst_name}"
            to_node = node_id
            to_port = f"port:{node_id}:{conn.to_port.port_name}"

        edges.append(
            {
                "id": f"edge:{from_port}->{to_port}",
                "from": from_port,
                "to": to_port,
                "scope": conn.scope_id,
                "fromNode": from_node,
                "toNode": to_node,
                "source": conn.source,
            }
        )
        edge_scope_map[f"edge:{from_port}->{to_port}"] = conn.scope_id

    for node_id, scope_id in node_scope_map.items():
        scope_nodes.setdefault(scope_id, []).append(node_id)

    for edge_id, scope_id in edge_scope_map.items():
        scope_edges.setdefault(scope_id, []).append(edge_id)

    scopes = [
        {
            **scope,
            "children": scope_children.get(scope["id"], []),
            "nodes": scope_nodes.get(scope["id"], []),
            "edges": scope_edges.get(scope["id"], []),
        }
        for scope in scopes
    ]

    graph_payload: dict[str, Any] = {
        "id": f"wf:{graph.name}",
        "nodes": nodes,
        "edges": edges,
        "subgraphs": scopes,
    }
    if graph.factory_name or graph.factory_parameters:
        graph_payload["meta"] = {
            **({"factoryName": graph.factory_name} if graph.factory_name else {}),
            **({"parameters": graph.factory_parameters} if graph.factory_parameters else {}),
        }

    return {
        "version": "1.1",
        "graph": graph_payload,
    }
