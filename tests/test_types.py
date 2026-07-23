"""Tests for wfpy types module."""

from wfpy.types import (
    Port,
    PortDescriptor,
    PortFactory,
    Resource,
    File,
    Map,
    infer_resource_kind,
    is_url_resource,
    is_http_resource,
    is_folder_resource,
    is_file_resource,
    extract_ports,
)


class TestPort:
    def test_port_subscript_returns_factory(self):
        factory = Port[int]
        assert isinstance(factory, PortFactory)

    def test_port_factory_call_returns_descriptor(self):
        desc = Port[int]("MyPort")
        assert isinstance(desc, PortDescriptor)
        assert desc.name == "MyPort"
        assert desc.port_type is int

    def test_port_factory_call_no_name(self):
        desc = Port[int]()
        assert isinstance(desc, PortDescriptor)
        assert desc.name is None  # will be filled by __set_name__

    def test_port_direction(self):
        desc = Port[int](direction="in")
        assert desc.direction == "in"

    def test_port_ext(self):
        desc = Port[File](direction="out", ext=".md")
        assert desc.ext == ".md"

    def test_port_set_name(self):
        class MyPorts:
            Out = Port[int]()

        desc = MyPorts.Out
        assert isinstance(desc, PortDescriptor)
        assert desc.name == "Out"
        assert desc.attr_name == "Out"


class TestExtractPorts:
    def test_extract_from_inner_class(self):

        class MyTask:
            class Ports:
                In = Port[int]()
                Out = Port[int]()

        ports = extract_ports(MyTask)
        assert "In" in ports
        assert "Out" in ports
        assert ports["In"].name == "In"

    def test_extract_no_ports_class(self):
        class NoPorts:
            pass

        ports = extract_ports(NoPorts)
        assert ports == {}

    def test_bare_port_factory_is_converted(self):
        """Port[int] without () should be auto-converted."""

        class MyTask:
            class Ports:
                In = Port[int]
                Out = Port[int]

        ports = extract_ports(MyTask)
        assert "In" in ports
        assert isinstance(ports["In"], PortDescriptor)


class TestFile:
    def test_file_default(self):
        f = File()
        assert f.path == ""
        assert f.ext == ""

    def test_file_with_ext(self):
        f = File(ext=".md")
        assert f.ext == ".md"

    def test_file_subscript(self):
        # File[...] should just return File
        assert File[str] is File


class TestResource:
    def test_resource_default(self):
        r = Resource()
        assert r.path == ""
        assert r.ext == ""
        assert r.kind == ""

    def test_resource_with_kind_and_ext(self):
        r = Resource(path="https://example.com/data.json", kind="url", ext=".json")
        assert r.path == "https://example.com/data.json"
        assert r.kind == "url"
        assert r.ext == ".json"

    def test_resource_shorthand_ext(self):
        r = Resource(".md")
        assert r.path == ""
        assert r.ext == ".md"

    def test_resource_subscript(self):
        assert Resource[str] is Resource

    def test_infer_kind_http(self):
        assert infer_resource_kind("https://example.com/a") == "http"
        assert is_http_resource("https://example.com/a") is True
        assert is_url_resource("https://example.com/a") is True

    def test_infer_kind_generic_url(self):
        assert infer_resource_kind("s3://bucket/key") == "url"
        assert is_url_resource("s3://bucket/key") is True
        assert is_http_resource("s3://bucket/key") is False

    def test_infer_kind_folder_hint(self):
        assert infer_resource_kind("/tmp/my-folder/") == "folder"
        assert is_folder_resource("/tmp/my-folder/") is True

    def test_infer_kind_from_explicit_resource(self):
        r = Resource(path="opaque://token", kind="folder")
        assert infer_resource_kind(r) == "folder"
        assert is_folder_resource(r) is True

    def test_file_resource_detection(self):
        assert is_file_resource(File(path="/tmp/a.txt")) is True
        assert is_file_resource(Resource(path="/tmp/a.txt", kind="file")) is True


class TestMap:
    def test_map_default(self):
        m = Map()
        assert m.data == {}


class TestPipelineOperator:
    """`>>` wires ports inside a @workflow body (regression: it used to raise
    ImportError because it imported `_current_graph` from the wrong module)."""

    def test_rshift_connects_ports(self):
        from wfpy.core import task, workflow
        from wfpy.runner import _build_workflow_graph

        @task
        class Doubler:
            class Ports:
                In = Port[int](direction="in")
                Out = Port[int](direction="out")

        @workflow
        def pipe():
            a = Doubler()
            b = Doubler()
            a.Out >> b.In

        graph = _build_workflow_graph(pipe._wfpy_workflow)

        assert sorted(graph.actors) == ["a", "b"]
        assert len(graph.connections) == 1
        conn = graph.connections[0]
        assert str(conn.from_port) == "PortInstance(a.Out)"
        assert str(conn.to_port) == "PortInstance(b.In)"

    def test_rshift_outside_workflow_raises_runtime_error(self):
        import pytest

        from wfpy.core import task

        @task
        class Solo:
            class Ports:
                In = Port[int](direction="in")
                Out = Port[int](direction="out")

        with pytest.raises(RuntimeError, match="only be used inside a @workflow"):
            Solo().Out >> Solo().In
