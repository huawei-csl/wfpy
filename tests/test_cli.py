"""Tests for wfpy CLI argument plumbing."""

from __future__ import annotations

import argparse
import types

import pytest

from wfpy import workflow
from wfpy.cli import (
    _discover_project_root,
    _load_module,        # <-- imported from wfpy.cli
    _module_search_paths,
    cmd_run,
)


class TestCliRun:
    def test_cmd_run_passes_agent_cli_backend_options(self, monkeypatch, tmp_path):
        @workflow(inputs={"Input": str}, outputs={"Output": str})
        def wf():
            return None

        module = types.SimpleNamespace(wf=wf)
        monkeypatch.setattr("wfpy.cli._load_module", lambda _path: module)
        monkeypatch.setattr("wfpy.cli._find_workflows", lambda _module: {"wf": wf})

        captured: dict[str, object] = {}

        def _fake_run(target, **kwargs):
            captured["target"] = target
            captured["kwargs"] = kwargs
            return {"Output": ["ok"]}

        monkeypatch.setattr("wfpy.runner.run", _fake_run)

        args = argparse.Namespace(
            file=str(tmp_path / "workflow.py"),
            workflow="wf",
            input=["Input=hello"],
            out_dir=str(tmp_path / "wf-out"),
            run_id=None,
            work_dir=None,
            keep_work_dir=False,
            verbose=False,
            agent_tool_auth=None,
            agent_tools=False,
            agent_tool_registry=None,
            agent_tool_policy=None,
            agent_tool_timeout_ms=30_000,
            agent_stream=False,
            resume_chat_from=None,
            validate=None,
            queue_trace=True,
            keep_intermediates=False,
            agent_debug=False,
            agent_cli_tools_mode="native",
            agent_cli_opencode_command="opencode-custom",
            agent_cli_opencode_args="--safe-mode",
            agent_cli_opencode_agent="wf-opencode",
            agent_cli_opencode_native_args="--dangerously-skip-permissions",
            agent_cli_claude_command="claude-custom",
            agent_cli_claude_args="--permission-mode plan",
            agent_cli_claude_agent="wf-claude",
            agent_cli_claude_native_args="--allowedTools bash,read",
            agent_cli_codex_command="codex-custom",
            agent_cli_codex_args="--json",
            agent_cli_codex_subcommand="run",
            agent_cli_codex_native_args="--allow-tools",
            skill_hook_auth=None,
            skill_hook_policy=None,
            context_mode=None,
            context_budget=None,
            context_summarize=None,
            resume_context_from=None,
        )

        cmd_run(args)

        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs["inputs"] == {"Input": "hello"}
        assert kwargs["agent_cli_tools_mode"] == "native"
        assert kwargs["agent_cli_opencode_command"] == "opencode-custom"
        assert kwargs["agent_cli_opencode_args"] == "--safe-mode"
        assert kwargs["agent_cli_opencode_agent"] == "wf-opencode"
        assert kwargs["agent_cli_opencode_native_args"] == "--dangerously-skip-permissions"
        assert kwargs["agent_cli_claude_command"] == "claude-custom"
        assert kwargs["agent_cli_claude_args"] == "--permission-mode plan"
        assert kwargs["agent_cli_claude_agent"] == "wf-claude"
        assert kwargs["agent_cli_claude_native_args"] == "--allowedTools bash,read"
        assert kwargs["agent_cli_codex_command"] == "codex-custom"
        assert kwargs["agent_cli_codex_args"] == "--json"
        assert kwargs["agent_cli_codex_subcommand"] == "run"
        assert kwargs["agent_cli_codex_native_args"] == "--allow-tools"


class TestCliModuleSearchPaths:
    def test_load_module_rejects_dynamic_workflow_name_mutation(self, tmp_path, capsys):
        workflow_file = tmp_path / "dynamic_workflow.py"
        workflow_file.write_text(
            '''
from wfpy import workflow


def make_workflow():
    @workflow
    def _inner_workflow():
        pass

    _inner_workflow.__name__ = "inner_workflow"
    return _inner_workflow


inner_workflow = make_workflow()
''',
            encoding="utf-8",
        )

        with pytest.raises(SystemExit) as exc_info:
            _load_module(str(workflow_file))

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Unsupported dynamic workflow naming" in captured.err
        assert "_inner_workflow" in captured.err

    def test_module_search_paths_include_package_parent_and_project_root(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "pyproject.toml").write_text("[project]\nname='wfpy-test'\n")

        examples_pkg = repo / "examples"
        streamblocks_pkg = examples_pkg / "streamblocks"
        streamblocks_pkg.mkdir(parents=True)
        (examples_pkg / "__init__.py").write_text("")
        (streamblocks_pkg / "__init__.py").write_text("")

        module_file = streamblocks_pkg / "test_passes_linux.py"
        module_file.write_text("x = 1\n")

        paths = _module_search_paths(module_file)
        assert paths[0] == streamblocks_pkg
        assert paths[1] == examples_pkg
        assert repo in paths

    def test_discover_project_root_prefers_nearest_parent(self, tmp_path):
        outer = tmp_path / "outer"
        outer.mkdir()
        (outer / "pyproject.toml").write_text("[project]\nname='outer'\n")

        inner = outer / "inner"
        inner.mkdir()
        (inner / "pyproject.toml").write_text("[project]\nname='inner'\n")

        nested = inner / "a" / "b"
        nested.mkdir(parents=True)

        assert _discover_project_root(nested) == inner

    def test_module_search_paths_include_ancestor_example_root_with_src(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "pyproject.toml").write_text("[project]\nname='wfpy-test'\n")

        example_root = repo / "examples" / "pto-kernels"
        workflow_dir = example_root / "tests" / "optimizer"
        src_pkg = example_root / "src"
        workflow_dir.mkdir(parents=True)
        src_pkg.mkdir(parents=True)
        (src_pkg / "__init__.py").write_text("")

        module_file = workflow_dir / "wf_test_optimizer.py"
        module_file.write_text("x = 1\n")

        paths = _module_search_paths(module_file)
        assert workflow_dir in paths
        assert example_root in paths
        assert repo in paths
