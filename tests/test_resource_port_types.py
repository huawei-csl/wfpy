"""A port carries a resource when its type is one, not when its name reads like one.

``_is_resource_type`` decides, for every port, whether a token is a file to
materialize or a value to hand on. It used to end with a search for ``file``
or ``resource`` in the printed type, so a dataclass named ``ProfileRun`` (or
any class of a module named ``resources``) was taken for a file: its token
left a nested workflow, a run or an agent as a path string, and the node after
it failed on a ``str``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Optional, TypedDict, Union

import pytest

from wfpy import File, Port, Resource, action, agent, connect, run, task, workflow
from wfpy._agent_validation_runtime import _is_resource_type


@dataclass(frozen=True)
class ProfileRun:  # "pro-FILE-run"
    job: str


@dataclass(frozen=True)
class Profiler:
    job: str


@dataclass(frozen=True)
class FileSpec:
    job: str


@dataclass(frozen=True)
class ResourceUsage:
    job: str


class UserProfile(TypedDict):
    name: str


class Report(File):
    """A subclass of ``File`` is a file."""


def _class_of_module(module: str) -> type:
    """A plain class, printed as a module of that name would define it."""

    return type("Reading", (), {"__module__": module, "value": 7})


RESOURCES = [
    File,
    Resource,
    Report,
    File(ext=".md"),
    Resource(ext=".bin"),
    "File",
    "Resource",
    " file ",
    "wfpy.File",
    "wfpy.types.Resource",
    Optional[File],
    Optional["File"],
    File | None,
    Union[File, Resource],
    Annotated[File, "the report"],
    Optional[Annotated[Resource, "the report"]],
]

NOT_RESOURCES = [
    # the spelling of a class, or of its module, holds "file" or "resource"
    ProfileRun,
    Profiler,
    FileSpec,
    ResourceUsage,
    UserProfile,
    _class_of_module("resources"),
    _class_of_module("profile_tools"),
    _class_of_module("pipeline.files.model"),
    "ProfileRun",
    "profile",
    "resources.Reading",
    "list[File]",
    Optional[ProfileRun],
    Optional["ProfileRun"],
    Annotated[ProfileRun, "the run"],
    # a container of resources is not one resource
    list[File],
    dict[str, File],
    tuple[File, ...],
    # a port that may hold something else cannot be materialized as a file
    Union[File, str],
    File | int,
    # plain values
    int,
    str,
    dict,
    list,
    list[int],
    Optional[int],
    Any,
    None,
]


@pytest.mark.parametrize("port_type", RESOURCES, ids=str)
def test_a_resource_type_is_a_resource(port_type: Any) -> None:
    assert _is_resource_type(port_type) is True


@pytest.mark.parametrize("port_type", NOT_RESOURCES, ids=str)
def test_a_name_does_not_make_a_resource(port_type: Any) -> None:
    assert _is_resource_type(port_type) is False


def _source(token: Any, port_type: Any) -> Any:
    """A node with no input that emits ``token`` once, on a port of ``port_type``."""

    @task
    class Make:
        class Ports:
            Out = Port[port_type](direction="out")

        class Schedule:
            initial = "armed"
            transitions = [("armed", "make", "spent")]

        @action(consumes={}, produces={"Out": 1})
        def make(self) -> dict:
            return {"Out": token}

    return Make


def test_a_dataclass_leaves_a_nested_workflow_as_itself(tmp_path: Path) -> None:
    """The graph of the report: the token reached the consumer as a path."""

    make_node = _source(ProfileRun(job="j1"), ProfileRun)

    @workflow(outputs={"Profile": ProfileRun})
    def collect() -> None:
        make = make_node()
        connect(make.Out, "Profile")

    @task
    class Use:
        class Ports:
            In = Port[ProfileRun](direction="in")
            Job = Port[str](direction="out")

        @action(consumes={"In": 1}, produces={"Job": 1})
        def use(self, token: ProfileRun) -> str:
            return token.job

    @workflow(outputs={"Job": str})
    def top() -> None:
        child, use = collect(), Use()
        connect(child.Profile, use.In)
        connect(use.Job, "Job")

    assert run(top, out_dir=tmp_path, verbose=False) == {"Job": ["j1"]}


@pytest.mark.parametrize(
    "token",
    [ProfileRun(job="j1"), FileSpec(job="j1"), ResourceUsage(job="j1")],
    ids=lambda token: type(token).__name__,
)
def test_a_dataclass_is_the_result_of_a_run(tmp_path: Path, token: Any) -> None:
    make_node = _source(token, type(token))

    @workflow(outputs={"Profile": type(token)})
    def measure() -> None:
        make = make_node()
        connect(make.Out, "Profile")

    assert run(measure, out_dir=tmp_path, verbose=False) == {"Profile": [token]}


def test_the_module_of_a_type_does_not_decide(tmp_path: Path) -> None:
    reading_type = _class_of_module("resources")
    reading = reading_type()
    make_node = _source(reading, reading_type)

    @workflow(outputs={"Reading": reading_type})
    def measure() -> None:
        make = make_node()
        connect(make.Out, "Reading")

    assert run(measure, out_dir=tmp_path, verbose=False) == {"Reading": [reading]}


def test_an_agent_reply_on_a_typed_dict_port_stays_a_dict(tmp_path: Path) -> None:
    """It was written to a ``.txt`` file, and the path was handed on."""

    @agent(prompt="Who is it?", transport="mock", mock_outputs={"Who": {"name": "ada"}})
    class Describe:
        class Ports:
            Who = Port[UserProfile](direction="out")

    @task
    class Greet:
        class Ports:
            Who = Port[UserProfile](direction="in")
            Text = Port[str](direction="out")

        @action(consumes={"Who": 1}, produces={"Text": 1})
        def greet(self, who: UserProfile) -> str:
            return f"hello {who['name']}"

    @workflow(outputs={"Text": str})
    def flow() -> None:
        describe, greet = Describe(), Greet()
        connect(describe.Who, greet.Who)
        connect(greet.Text, "Text")

    assert run(flow, out_dir=tmp_path, verbose=False) == {"Text": ["hello ada"]}


@pytest.mark.parametrize("port_type", [File, Optional[File], File | None], ids=str)
def test_an_optional_file_port_is_still_a_file(tmp_path: Path, port_type: Any) -> None:
    """The reply of an agent on such a port is the content of a file."""

    @agent(prompt="Write it.", transport="mock", mock_outputs={"Doc": "# hello\n"})
    class Write:
        class Ports:
            Doc = Port[port_type](direction="out", ext=".md")

    @workflow(outputs={"Doc": port_type})
    def flow() -> None:
        write = Write()
        connect(write.Doc, "Doc")

    (doc,) = run(flow, out_dir=tmp_path, verbose=False)["Doc"]
    assert Path(doc).read_text() == "# hello\n"


def test_a_list_of_files_is_a_list(tmp_path: Path) -> None:
    """It was taken for one file, and the run answered a path that did not exist."""

    files = [tmp_path / "a.txt", tmp_path / "b.txt"]
    for file in files:
        file.write_text(file.stem)
    paths = [str(file) for file in files]
    make_node = _source(paths, list[File])

    @workflow(outputs={"Files": list[File]})
    def gather() -> None:
        make = make_node()
        connect(make.Out, "Files")

    assert run(gather, out_dir=tmp_path / "out", verbose=False) == {"Files": [paths]}
