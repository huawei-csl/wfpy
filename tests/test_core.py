"""Tests for wfpy core decorators."""

import pytest

from wfpy import task, action, guard, workflow, tool, agent, viewer, context, connect, Port
from wfpy.core import TaskMeta, WorkflowDef, _active_wf_config, _WorkflowEnvConfig


class TestTaskDecorator:
    def test_basic_task(self):

        @task
        class Doubler:
            factor: int

            class Ports:
                In = Port[int]()
                Out = Port[int]()

            def action(self, x: int) -> int:
                return x * self.factor

        meta: TaskMeta = Doubler._wfpy_meta
        assert meta.name == "Doubler"
        assert meta.kind == "internal"
        assert "In" in meta.ports
        assert "Out" in meta.ports
        assert "factor" in meta.parameters
        assert len(meta.actions) == 1
        assert meta.actions[0].name == "action"

    def test_task_with_state(self):

        @task
        class Accumulator:
            class Ports:
                In = Port[int]()
                Out = Port[int]()

            sum: int = 0

            def action(self, x: int) -> int:
                self.sum += x
                return self.sum

        meta: TaskMeta = Accumulator._wfpy_meta
        assert "sum" in meta.state_fields
        assert meta.state_fields["sum"] == 0
        assert "sum" not in meta.parameters

    def test_task_instantiation(self):

        @task
        class MyTask:
            factor: int

            class Ports:
                In = Port[int]()
                Out = Port[int]()

            count: int = 0

            def action(self, x: int) -> int:
                self.count += 1
                return x * self.factor

        inst = MyTask(factor=5)
        assert inst.factor == 5
        assert inst.count == 0

    def test_task_missing_param_raises(self):

        @task
        class NeedsParam:
            factor: int

            class Ports:
                In = Port[int]()

            def action(self, x: int) -> int:
                return x

        try:
            NeedsParam()
            assert False, "Should have raised TypeError"
        except TypeError as e:
            assert "factor" in str(e)

    def test_task_unexpected_param_raises(self):

        @task
        class NeedsParam:
            factor: int

            class Ports:
                In = Port[int]()

            def action(self, x: int) -> int:
                return x * self.factor

        try:
            NeedsParam(factor=2, typo=3)
            assert False, "Should have raised TypeError"
        except TypeError as e:
            assert "unexpected" in str(e)
            assert "typo" in str(e)

    def test_guard_on_implicit_action_is_collected(self):

        @task
        class OneShot:
            class Ports:
                Out = Port[int](direction="out")

            _i: int = 0

            @guard(lambda self, *_a, **_k: self._i == 0)
            def action(self) -> int:
                self._i += 1
                return 1

        meta: TaskMeta = OneShot._wfpy_meta
        assert len(meta.actions) == 1
        assert meta.actions[0].guard_fn is not None

        inst = OneShot()
        guard_fn = meta.actions[0].guard_fn
        assert guard_fn is not None
        assert guard_fn(inst) is True
        inst._i = 1
        assert guard_fn(inst) is False


class TestMultiAction:
    def test_multiple_actions_collected(self):

        @task
        class MultiAct:
            class Ports:
                In = Port[int]()
                Out = Port[int]()

            @action(consumes={"In": 2}, produces={"Out": 1})
            @guard(lambda self, a, b: a < b)
            def sum_pair(self, a: int, b: int) -> int:
                return a + b

            @action(consumes={"In": 2}, produces={"Out": 1})
            @guard(lambda self, a, b: a >= b)
            def diff_pair(self, a: int, b: int) -> int:
                return a - b

        meta: TaskMeta = MultiAct._wfpy_meta
        assert len(meta.actions) == 2
        assert meta.actions[0].name == "sum_pair"
        assert meta.actions[1].name == "diff_pair"
        assert meta.actions[0].guard_fn is not None
        assert meta.actions[1].guard_fn is not None
        assert meta.actions[0].consumes == {"In": 2}

    def test_explicit_zero_input_action_preserves_empty_consumes(self):

        @task
        class ZeroInput:
            class Ports:
                Out = Port[int]()

            @action(consumes={}, produces={"Out": 1})
            def emit(self) -> int:
                return 1

        meta: TaskMeta = ZeroInput._wfpy_meta
        assert len(meta.actions) == 1
        assert meta.actions[0].consumes == {}

    def test_implicit_action_keeps_auto_infer_consumes(self):

        @task
        class ImplicitInferred:
            class Ports:
                In = Port[int]()
                Out = Port[int]()

            @action
            def emit(self, value: int) -> int:
                return value + 1

        meta: TaskMeta = ImplicitInferred._wfpy_meta
        assert len(meta.actions) == 1
        assert meta.actions[0].consumes is None
        assert meta.actions[0].produces is None

    def test_subclass_inherits_and_overrides_actions_in_mro_order(self):

        @task
        class BaseTask:
            class Ports:
                In = Port[int]()
                Out = Port[int]()

            @action(consumes={"In": 1}, produces={"Out": 1})
            @guard(lambda self, x: x > 0)
            def choose(self, x: int) -> int:
                return x + 1

            @action(consumes={"In": 1}, produces={"Out": 1})
            def finish(self, x: int) -> int:
                return x * 10

        @task
        class DerivedTask(BaseTask):
            @action(consumes={"In": 1}, produces={"Out": 1})
            @guard(lambda self, x: x < 0)
            def choose(self, x: int) -> int:
                return x - 1

        meta: TaskMeta = DerivedTask._wfpy_meta
        assert [action.name for action in meta.actions] == ["choose", "finish"]

        choose = meta.actions[0]
        finish = meta.actions[1]
        assert choose.guard_fn is not None
        assert choose.guard_fn(None, -1) is True
        assert choose.guard_fn(None, 1) is False
        assert choose.fn(DerivedTask(), 5) == 4
        assert finish.fn(DerivedTask(), 3) == 30

    def test_schedule_and_priority_metadata_collected(self):

        @task
        class Scheduled:
            class Ports:
                In = Port[int]()
                Out = Port[int]()

            class Schedule:
                initial = "start"
                transitions = [
                    ("start", "choose_a", "middle"),
                    ("middle", "choose_b", "done"),
                ]

            class Priority:
                rules = [["choose_b", "choose_a"]]

            @action(consumes={"In": 1}, produces={"Out": 1})
            def choose_a(self, x: int) -> int:
                return x + 1

            @action(consumes={"In": 1}, produces={"Out": 1})
            def choose_b(self, x: int) -> int:
                return x + 2

        meta: TaskMeta = Scheduled._wfpy_meta
        assert meta.schedule is not None
        assert meta.schedule.initial == "start"
        assert meta.schedule.by_state == {
            "start": {"choose_a": "middle"},
            "middle": {"choose_b": "done"},
        }
        assert meta.priority is not None
        assert meta.priority.rank == {"choose_b": (0, 0), "choose_a": (0, 1)}

        inst = Scheduled()
        assert inst._wfpy_schedule_state == "start"

    def test_schedule_rejects_unknown_action(self):
        try:

            @task
            class BadSchedule:
                class Ports:
                    In = Port[int]()
                    Out = Port[int]()

                class Schedule:
                    initial = "start"
                    transitions = [("start", "missing_action", "done")]

                @action(consumes={"In": 1}, produces={"Out": 1})
                def only_action(self, x: int) -> int:
                    return x

            assert False, "Should have raised"
        except TypeError as e:
            assert "unknown action" in str(e)

    def test_guard_on_action(self):

        @task
        class Guarded:
            class Ports:
                In = Port[int]()
                Out = Port[int]()

            @action(consumes={"In": 1}, produces={"Out": 1})
            @guard(lambda self, x: x > 0)
            def positive(self, x: int) -> int:
                return x * 2

        meta: TaskMeta = Guarded._wfpy_meta
        act = meta.actions[0]
        assert act.guard_fn is not None
        # guard should reject negative
        assert not act.guard_fn(None, -1)
        assert act.guard_fn(None, 5)


class TestToolDecorator:
    def test_tool_basic(self):

        @tool(cmd="echo", args=["hello"])
        class EchoTool:
            class Ports:
                Out = Port[str](direction="out")

        meta: TaskMeta = EchoTool._wfpy_meta
        assert meta.kind == "external"
        assert meta.tool_spec is not None
        assert meta.tool_spec.cmd == "echo"
        assert meta.tool_spec.args == ["hello"]

    def test_tool_no_cmd_raises(self):
        try:

            @tool
            class Bad:
                pass

            assert False, "Should have raised"
        except TypeError:
            pass

    def test_tool_fn_inherits_workflow_config_env(self):
        """Function-style @tool picks up env/path from _active_wf_config."""
        import subprocess

        @tool(cmd="env", args=[])
        def get_env() -> subprocess.CompletedProcess: ...

        # Set active workflow config (simulates runner context)
        token = _active_wf_config.set(
            _WorkflowEnvConfig(
                env={"MY_CUSTOM_VAR": "hello_from_config"},
                search_paths=["/fake/bin"],
            )
        )
        try:
            result = get_env()
            assert "MY_CUSTOM_VAR=hello_from_config" in result.stdout
            # search_paths should be prepended to PATH
            assert result.stdout.find("/fake/bin") != -1
        finally:
            _active_wf_config.reset(token)

    def test_tool_fn_per_tool_env_overrides_config(self):
        """Per-tool env= takes precedence over workflow @config env."""
        import subprocess

        @tool(cmd="env", args=[], env={"MY_VAR": "from_tool"})
        def get_env() -> subprocess.CompletedProcess: ...

        token = _active_wf_config.set(_WorkflowEnvConfig(env={"MY_VAR": "from_config"}))
        try:
            result = get_env()
            assert "MY_VAR=from_tool" in result.stdout
        finally:
            _active_wf_config.reset(token)

    def test_tool_fn_no_config_still_works(self):
        """Function-style @tool works without active workflow config."""
        import subprocess

        @tool(cmd="echo", args=["hello"])
        def echo_hello() -> subprocess.CompletedProcess: ...

        result = echo_hello()
        assert result.returncode == 0
        assert "hello" in result.stdout


class TestAgentDecorator:
    def test_agent_basic(self):

        @agent(prompt="Summarize this.", model="openai/gpt-4o")
        class Summarizer:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

        meta: TaskMeta = Summarizer._wfpy_meta
        assert meta.kind == "agent"
        assert meta.agent_spec is not None
        assert meta.agent_spec.prompt == "Summarize this."
        assert meta.agent_spec.model == "openai/gpt-4o"
        assert meta.agent_spec.transport == "http"

    def test_agent_stateful(self):

        @agent(prompt="Chat.", stateful=True, context_budget=30)
        class ChatBot:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

        meta: TaskMeta = ChatBot._wfpy_meta
        assert meta.agent_spec is not None
        assert meta.agent_spec.stateful is True
        assert meta.agent_spec.context_budget == 30

    def test_agent_skill_and_toggles(self):

        @agent(
            prompt="Base prompt",
            skill="writer",
            use_prompt=False,
            use_skill=True,
            use_skill_hooks=False,
        )
        class SkilledAgent:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

        meta: TaskMeta = SkilledAgent._wfpy_meta
        assert meta.agent_spec is not None
        assert meta.agent_spec.skill == "writer"
        assert meta.agent_spec.use_prompt is False
        assert meta.agent_spec.use_skill is True
        assert meta.agent_spec.use_skill_hooks is False

    def test_agent_claude_agent_and_aliases(self):

        @agent(
            prompt="Base prompt",
            claudeAgent="code-reviewer",
            useClaudeAgent=True,
            usePrompt=False,
            timeoutMs=45000,
            contextBudget=80,
            truncationStrategy="summarize",
        )
        class ClaudeCompatAgent:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

        meta: TaskMeta = ClaudeCompatAgent._wfpy_meta
        assert meta.agent_spec is not None
        assert meta.agent_spec.claude_agent == "code-reviewer"
        assert meta.agent_spec.use_claude_agent is True
        assert meta.agent_spec.use_prompt is False
        assert meta.agent_spec.timeout_ms == 45000
        assert meta.agent_spec.context_budget == 80
        assert meta.agent_spec.truncation_strategy == "summarize"

    def test_agent_transport(self):

        @agent(prompt="Use opencode.", transport="opencode-cli")
        class OpenCodeAgent:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

        meta: TaskMeta = OpenCodeAgent._wfpy_meta
        assert meta.agent_spec is not None
        assert meta.agent_spec.transport == "opencode-cli"
        assert meta.agent_spec.cli_tools_mode == "wfpy-none"

    def test_agent_cli_tools_mode(self):

        @agent(prompt="Use opencode.", transport="opencode-cli", cli_tools_mode="native")
        class OpenCodeAgentNative:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

        @agent(prompt="Use opencode.", transport="opencode-cli", cliToolsMode="wfpy-none")
        class OpenCodeAgentCompat:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

        meta_native: TaskMeta = OpenCodeAgentNative._wfpy_meta
        meta_compat: TaskMeta = OpenCodeAgentCompat._wfpy_meta
        assert meta_native.agent_spec is not None
        assert meta_compat.agent_spec is not None
        assert meta_native.agent_spec.cli_tools_mode == "native"
        assert meta_compat.agent_spec.cli_tools_mode == "wfpy-none"

    def test_agent_transport_claude_and_codex(self):

        @agent(prompt="Use claude.", transport="claude-cli")
        class ClaudeCliAgent:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

        @agent(prompt="Use codex.", transport="codex-cli")
        class CodexCliAgent:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

        meta_claude: TaskMeta = ClaudeCliAgent._wfpy_meta
        meta_codex: TaskMeta = CodexCliAgent._wfpy_meta
        assert meta_claude.agent_spec is not None
        assert meta_codex.agent_spec is not None
        assert meta_claude.agent_spec.transport == "claude-cli"
        assert meta_codex.agent_spec.transport == "codex-cli"

    def test_agent_ask_user(self):

        @agent(prompt="Ask if unsure.", ask_user=True)
        class AskUserAgent:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

        @agent(prompt="Ask if unsure.", askUser=True)
        class AskUserCamelAgent:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

        @agent(prompt="No questions.")
        class NoAskAgent:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

        meta_snake: TaskMeta = AskUserAgent._wfpy_meta
        meta_camel: TaskMeta = AskUserCamelAgent._wfpy_meta
        meta_default: TaskMeta = NoAskAgent._wfpy_meta
        assert meta_snake.agent_spec is not None
        assert meta_camel.agent_spec is not None
        assert meta_default.agent_spec is not None
        assert meta_snake.agent_spec.ask_user is True
        assert meta_camel.agent_spec.ask_user is True
        assert meta_default.agent_spec.ask_user is False

    def test_agent_ask_user_camel_wins_over_snake(self):

        @agent(prompt="Ask if unsure.", ask_user=False, askUser=True)
        class AskUserConflictAgent:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

        meta: TaskMeta = AskUserConflictAgent._wfpy_meta
        assert meta.agent_spec is not None
        assert meta.agent_spec.ask_user is True


class TestViewerDecorator:
    def test_viewer_basic(self):

        @viewer(action_name="open", inputs=["In"])
        class MdViewer:
            class Ports:
                In = Port[str](direction="in")

        meta: TaskMeta = MdViewer._wfpy_meta
        assert meta.kind == "viewer"
        assert meta.annotations.get("viewer") == {
            "action": "open",
            "inputs": ["In"],
        }

    def test_viewer_with_view_type(self):

        @viewer(action_name="openWith", inputs=["In"], viewType="vscode.markdown.preview.editor")
        class MdPreviewViewer:
            class Ports:
                In = Port[str](direction="in")

        meta: TaskMeta = MdPreviewViewer._wfpy_meta
        assert meta.kind == "viewer"
        assert meta.annotations.get("viewer") == {
            "action": "openWith",
            "inputs": ["In"],
            "viewType": "vscode.markdown.preview.editor",
        }

    def test_viewer_with_command_aliases(self):

        @viewer(
            action_name="command",
            inputs=["In"],
            command_name="livePreview.start.preview.atFile",
            command_args=["{uri}"],
        )
        class HtmlPreviewViewer:
            class Ports:
                In = Port[str](direction="in")

        meta: TaskMeta = HtmlPreviewViewer._wfpy_meta
        assert meta.kind == "viewer"
        assert meta.annotations.get("viewer") == {
            "action": "command",
            "inputs": ["In"],
            "command": "livePreview.start.preview.atFile",
            "args": ["{uri}"],
        }

    def test_viewer_command_precedence_prefers_canonical_names(self):

        @viewer(
            action_name="command",
            inputs=["In"],
            command="canonical.command",
            command_name="alias.command",
            args=["canonical"],
            command_args=["alias"],
        )
        class PreferredViewer:
            class Ports:
                In = Port[str](direction="in")

        meta: TaskMeta = PreferredViewer._wfpy_meta
        assert meta.annotations.get("viewer") == {
            "action": "command",
            "inputs": ["In"],
            "command": "canonical.command",
            "args": ["canonical"],
        }

    def test_viewer_command_action_requires_command(self):
        with pytest.raises(TypeError, match="requires command"):

            @viewer(action_name="command", inputs=["In"])
            class BrokenViewer:
                class Ports:
                    In = Port[str](direction="in")


class TestContextDecorator:
    def test_context_on_task_decorated_class_does_not_double_wrap(self):

        @task
        class CtxTaskWrapped:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

            def action(self, x: str) -> str:
                return x

        init_before = CtxTaskWrapped.__init__
        CtxTaskWrapped = context(read=["global.goal"], write=["runtime.events"])(CtxTaskWrapped)

        assert CtxTaskWrapped.__init__ is init_before
        meta: TaskMeta = CtxTaskWrapped._wfpy_meta
        assert meta.annotations.get("context") == {
            "read": ["global.goal"],
            "write": ["runtime.events"],
        }

    def test_context_annotation_attached(self):

        @context(read=["global.goal", "agent.chat.*"], write=["artifacts.latest", "runtime.events"])
        class CtxTask:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

            def action(self, x: str) -> str:
                return x

        meta: TaskMeta = CtxTask._wfpy_meta
        assert meta.annotations.get("context") == {
            "read": ["global.goal", "agent.chat.*"],
            "write": ["artifacts.latest", "runtime.events"],
        }

    def test_context_normalizes_invalid_scope_inputs(self):

        @context(read=[" global.goal ", "", "agent.chat.*"], write=None)
        class CtxTaskNorm:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

            def action(self, x: str) -> str:
                return x

        meta: TaskMeta = CtxTaskNorm._wfpy_meta
        assert meta.annotations.get("context") == {
            "read": ["global.goal", "agent.chat.*"],
            "write": [],
        }


class TestWorkflowDecorator:
    def test_function_workflow(self):

        @task
        class D:
            factor: int

            class Ports:
                In = Port[int]()
                Out = Port[int]()

            def action(self, x: int) -> int:
                return x * self.factor

        @workflow(inputs={"Input": int}, outputs={"Output": int})
        def my_wf():
            d = D(factor=2)
            connect("Input", d.In)
            connect(d.Out, "Output")

        wf_def: WorkflowDef = my_wf._wfpy_workflow
        assert wf_def.name == "my_wf"
        assert "Input" in wf_def.input_names
        assert "Output" in wf_def.output_names
