"""Tests for wfpy graph module."""

from wfpy import action, task, workflow, connect, if_, loop, Port, agent, viewer
from wfpy.graph import WorkflowGraph, export_graph_json
from wfpy.types import PortInstance


class TestWorkflowGraph:
    def test_register_actor(self):

        @task
        class T:
            class Ports:
                In = Port[int]()

            def action(self, x: int) -> int:
                return x

        graph = WorkflowGraph("test")
        inst = T()
        name = graph.register_actor(inst)
        assert name in graph.actors
        assert inst._wfpy_instance_name == name

    def test_connect_ports(self):

        @task
        class A:
            class Ports:
                Out = Port[int]()

            def action(self) -> int:
                return 1

        @task
        class B:
            class Ports:
                In = Port[int]()

            def action(self, x: int) -> int:
                return x

        graph = WorkflowGraph("test")
        a = A()
        b = B()
        graph.register_actor(a, "a")
        graph.register_actor(b, "b")

        with graph:
            connect(a.Out, b.In)

        assert len(graph.connections) == 1
        conn = graph.connections[0]
        assert isinstance(conn.from_port, PortInstance)
        assert isinstance(conn.to_port, PortInstance)

    def test_connect_string_ports(self):

        @task
        class T:
            class Ports:
                In = Port[int]()
                Out = Port[int]()

            def action(self, x: int) -> int:
                return x

        graph = WorkflowGraph("test")
        t = T()
        graph.register_actor(t, "t")

        with graph:
            connect("Input", t.In)
            connect(t.Out, "Output")

        assert len(graph.connections) == 2
        assert graph.connections[0].from_port == "Input"
        assert graph.connections[1].to_port == "Output"


class TestConnectOutsideGraph:
    def test_connect_without_graph_raises(self):
        try:
            connect("X", "Y")
            assert False, "Should have raised"
        except RuntimeError as e:
            assert "no active WorkflowGraph" in str(e)


class TestControlScopes:
    def test_if_loop_scopes(self):

        @task
        class Source:
            value: int

            class Ports:
                Out = Port[int]()

            _done: bool = False

            def action(self) -> int:
                if self._done:
                    return None  # type: ignore[return-value]
                self._done = True
                return self.value

        @task
        class Sink:
            class Ports:
                In = Port[int]()

            def action(self, x: int) -> int:
                return x

        @workflow(inputs={"Input": int}, outputs={"Output": int})
        def wf():
            src = Source(value=1)
            cond = if_(True)
            with cond.then:
                s1 = Sink()
                connect(src.Out, s1.In)
            with cond.else_:
                s2 = Sink()
                connect(src.Out, s2.In)
            loop_ctx = loop([1, 2, 3])
            with loop_ctx:
                s3 = Sink()
                connect(src.Out, s3.In)
            connect(src.Out, "Output")

        graph = WorkflowGraph("wf")
        with graph:
            wf()

        graph_json = export_graph_json(graph)
        node_kinds = {n["kind"] for n in graph_json["graph"]["nodes"]}
        assert "if" in node_kinds
        assert "loop" in node_kinds


class TestExportDefinitionAnnotations:
    def test_agent_definition_annotation_is_single_and_serialized(self):
        @agent(
            prompt="Transform data",
            skill="writer",
            claudeAgent="code-reviewer",
            usePrompt=True,
            useSkill=True,
            useClaudeAgent=True,
            useSkillHooks=False,
            stateful=True,
        )
        class AnnotatedAgent:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

            def action(self, x: str) -> str:
                return x

        @workflow(inputs={"Input": str}, outputs={"Output": str})
        def wf():
            a = AnnotatedAgent()
            connect("Input", a.In)
            connect(a.Out, "Output")

        graph = WorkflowGraph("wf")
        with graph:
            wf()

        graph_json = export_graph_json(graph)
        agent_nodes = [n for n in graph_json["graph"]["nodes"] if n.get("kind") == "agent"]
        assert len(agent_nodes) == 1

        node_meta = agent_nodes[0].get("meta", {})
        def_annotations = node_meta.get("definitionAnnotations", [])
        assert isinstance(def_annotations, list)

        agent_annotations = [a for a in def_annotations if a.get("name") == "agent"]
        assert len(agent_annotations) == 1

        args = {
            str(item.get("name")): str(item.get("value"))
            for item in agent_annotations[0].get("arguments", [])
            if isinstance(item, dict)
        }

        assert args.get("prompt") == '"Transform data"'
        assert args.get("skill") == '"writer"'
        assert args.get("claudeAgent") == '"code-reviewer"'
        assert args.get("usePrompt") == "true"
        assert args.get("useSkill") == "true"
        assert args.get("useClaudeAgent") == "true"
        assert args.get("useSkillHooks") == "false"
        assert args.get("stateful") == "true"

    def test_agent_backend_and_context_params_are_serialized(self):
        # transport / cliToolsMode / reasoningEffort / fireableWithoutInput /
        # truncationStrategy are surfaced to the IDE so the properties panel can edit them.
        @agent(
            prompt="do x",
            transport="opencode-cli",
            cliToolsMode="native",
            reasoningEffort="high",
            fireableWithoutInput=2,
            truncationStrategy="summarize",
        )
        class BackendAgent:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

            def action(self, x: str) -> str:
                return x

        @workflow(inputs={"Input": str}, outputs={"Output": str})
        def wf():
            a = BackendAgent()
            connect("Input", a.In)
            connect(a.Out, "Output")

        graph = WorkflowGraph("wf")
        with graph:
            wf()

        graph_json = export_graph_json(graph)
        agent_nodes = [n for n in graph_json["graph"]["nodes"] if n.get("kind") == "agent"]
        assert len(agent_nodes) == 1

        agent_annotations = [
            a for a in agent_nodes[0].get("meta", {}).get("definitionAnnotations", [])
            if a.get("name") == "agent"
        ]
        assert len(agent_annotations) == 1
        args = {
            str(item.get("name")): str(item.get("value"))
            for item in agent_annotations[0].get("arguments", [])
            if isinstance(item, dict)
        }

        assert args.get("transport") == '"opencode-cli"'
        assert args.get("cliToolsMode") == '"native"'
        assert args.get("reasoningEffort") == '"high"'
        assert args.get("fireableWithoutInput") == "2"
        assert args.get("truncationStrategy") == '"summarize"'

    def test_output_validators_are_serialized_and_round_trip(self):
        import ast

        @agent(
            prompt="x",
            outputValidators=[
                {"kind": "cmd", "cmd": "bisheng", "args": ["-fsyntax-only", "{file}"], "ports": ["Out"]},
                {"kind": "lsp", "cmd": "clangd", "languageId": "cpp", "extraFlags": ["-xcce"]},
            ],
        )
        class ValidatedAgent:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

            def action(self, x: str) -> str:
                return x

        @workflow(inputs={"Input": str}, outputs={"Output": str})
        def wf():
            a = ValidatedAgent()
            connect("Input", a.In)
            connect(a.Out, "Output")

        graph = WorkflowGraph("wf")
        with graph:
            wf()

        graph_json = export_graph_json(graph)
        agent_nodes = [n for n in graph_json["graph"]["nodes"] if n.get("kind") == "agent"]
        agent_annotations = [
            a for a in agent_nodes[0].get("meta", {}).get("definitionAnnotations", [])
            if a.get("name") == "agent"
        ]
        args = {
            str(item.get("name")): str(item.get("value"))
            for item in agent_annotations[0].get("arguments", [])
            if isinstance(item, dict)
        }

        raw = args.get("outputValidators")
        assert raw is not None
        # The emitted value must be a valid Python literal so the @agent decorator can re-parse
        # it after a properties-panel save (validator configs contain no booleans/None).
        parsed = ast.literal_eval(raw)
        assert isinstance(parsed, list) and len(parsed) == 2
        assert parsed[0]["kind"] == "cmd" and parsed[0]["cmd"] == "bisheng"
        assert parsed[0]["ports"] == ["Out"]
        assert "languageId" not in parsed[0]  # cmd-mode omits LSP-only fields
        assert parsed[1]["kind"] == "lsp" and parsed[1]["extraFlags"] == ["-xcce"]

    def test_lsp_command_shorthand_scalars_are_serialized(self):
        @agent(
            prompt="x",
            lsp_command="clangd",
            lsp_args=["--log=error"],
            lsp_language_id="c",
            lsp_extra_flags=["-xcce"],
            lsp_severity_threshold="warning",
            lsp_max_repair_attempts=4,
        )
        class ShorthandAgent:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

            def action(self, x: str) -> str:
                return x

        @workflow(inputs={"Input": str}, outputs={"Output": str})
        def wf():
            a = ShorthandAgent()
            connect("Input", a.In)
            connect(a.Out, "Output")

        graph = WorkflowGraph("wf")
        with graph:
            wf()

        agent_nodes = [n for n in export_graph_json(graph)["graph"]["nodes"] if n.get("kind") == "agent"]
        agent_annotations = [
            a for a in agent_nodes[0].get("meta", {}).get("definitionAnnotations", [])
            if a.get("name") == "agent"
        ]
        args = {
            str(item.get("name")): str(item.get("value"))
            for item in agent_annotations[0].get("arguments", [])
            if isinstance(item, dict)
        }
        assert args.get("lspCommand") == '"clangd"'
        assert args.get("lspArgs") == '["--log=error"]'
        assert args.get("lspLanguageId") == '"c"'
        assert args.get("lspExtraFlags") == '["-xcce"]'
        assert args.get("lspSeverityThreshold") == '"warning"'
        assert args.get("lspMaxRepairAttempts") == "4"

    def test_viewer_definition_annotation_includes_viewType(self):
        @viewer(action_name="openWith", inputs=["In"], viewType="vscode.markdown.preview.editor")
        class MdViewer:
            class Ports:
                In = Port[str](direction="in")

        @workflow(inputs={"Input": str}, outputs={})
        def wf():
            v = MdViewer()
            connect("Input", v.In)

        graph = WorkflowGraph("wf")
        with graph:
            wf()

        graph_json = export_graph_json(graph)
        viewer_nodes = [n for n in graph_json["graph"]["nodes"] if n.get("kind") == "viewer"]
        assert len(viewer_nodes) == 1

        node_meta = viewer_nodes[0].get("meta", {})
        def_annotations = node_meta.get("definitionAnnotations", [])
        viewer_annotations = [a for a in def_annotations if a.get("name") == "viewer"]
        assert len(viewer_annotations) == 1

        args = {
            str(item.get("name")): str(item.get("value"))
            for item in viewer_annotations[0].get("arguments", [])
            if isinstance(item, dict)
        }

        assert args.get("action") == '"openWith"'
        assert args.get("viewType") == '"vscode.markdown.preview.editor"'

    def test_viewer_definition_annotation_includes_command(self):
        @viewer(
            action_name="command",
            inputs=["In"],
            command_name="livePreview.start.preview.atFile",
            command_args=["{uri}"],
        )
        class HtmlViewer:
            class Ports:
                In = Port[str](direction="in")

        @workflow(inputs={"Input": str}, outputs={})
        def wf():
            v = HtmlViewer()
            connect("Input", v.In)

        graph = WorkflowGraph("wf")
        with graph:
            wf()

        graph_json = export_graph_json(graph)
        viewer_nodes = [n for n in graph_json["graph"]["nodes"] if n.get("kind") == "viewer"]
        assert len(viewer_nodes) == 1

        node_meta = viewer_nodes[0].get("meta", {})
        def_annotations = node_meta.get("definitionAnnotations", [])
        viewer_annotations = [a for a in def_annotations if a.get("name") == "viewer"]
        assert len(viewer_annotations) == 1

        args = {
            str(item.get("name")): str(item.get("value"))
            for item in viewer_annotations[0].get("arguments", [])
            if isinstance(item, dict)
        }

        assert args.get("action") == '"command"'
        assert args.get("command") == '"livePreview.start.preview.atFile"'
        assert args.get("args") == '["{uri}"]'

    def test_task_definition_annotations_include_schedule_and_priority(self):
        @task
        class ScheduledTask:
            class Ports:
                In = Port[int](direction="in")
                Out = Port[int](direction="out")

            class Schedule:
                initial = "start"
                transitions = [
                    ("start", "first", "middle"),
                    ("middle", "second", "done"),
                ]

            class Priority:
                rules = [["second", "first"]]

            @action(consumes={"In": 1}, produces={"Out": 1})
            def first(self, x: int) -> int:
                return x + 1

            @action(consumes={"In": 1}, produces={"Out": 1})
            def second(self, x: int) -> int:
                return x + 2

        @workflow(inputs={"Input": int}, outputs={"Output": int})
        def wf():
            s = ScheduledTask()
            connect("Input", s.In)
            connect(s.Out, "Output")

        graph = WorkflowGraph("wf")
        with graph:
            wf()

        graph_json = export_graph_json(graph)
        task_nodes = [
            n
            for n in graph_json["graph"]["nodes"]
            if n.get("kind") == "internal"
            and n.get("meta", {}).get("schedule", {}).get("initial") == "start"
        ]
        assert len(task_nodes) == 1

        node_meta = task_nodes[0].get("meta", {})
        assert node_meta.get("schedule") == {
            "initial": "start",
            "transitions": [
                {"state": "start", "action": "first", "nextState": "middle"},
                {"state": "middle", "action": "second", "nextState": "done"},
            ],
        }
        assert node_meta.get("priority") == {"rules": [["second", "first"]]}

        def_annotations = node_meta.get("definitionAnnotations", [])
        schedule_annotations = [a for a in def_annotations if a.get("name") == "schedule"]
        priority_annotations = [a for a in def_annotations if a.get("name") == "priority"]
        assert len(schedule_annotations) == 1
        assert len(priority_annotations) == 1


class TestWorkflowFactoryParameters:
    def test_export_graph_json_includes_factory_parameters(self):
        def make_scaled(multiplier: int, label: str):
            @workflow(inputs={"Input": int}, outputs={"Output": int})
            def scaled():
                _ = (multiplier, label)
                connect("Input", "Output")

            return scaled

        workflow_def = make_scaled(multiplier=3, label="triple")

        graph = WorkflowGraph("scaled")
        with graph:
            workflow_def()

        graph_json = export_graph_json(graph)
        assert graph_json["graph"]["meta"]["factoryName"] == "make_scaled"
        params = {
            item["name"]: {"value": item["value"], "type": item["type"]}
            for item in graph_json["graph"]["meta"]["parameters"]
        }
        assert params == {
            "multiplier": {"value": 3, "type": "int"},
            "label": {"value": "triple", "type": "str"},
        }
