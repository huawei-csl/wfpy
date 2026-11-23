"""The decorator merge (`mergeDefinitionAnnotationArgs`, an IDE's Save) on the
forms a decorator is written in. Each test holds the exact text written."""

from __future__ import annotations

from pathlib import Path

import libcst as cst
import pytest

from wfpy.rewrite import RewriteEngine, RewriteError


def _merged(tmp_path: Path, source: str, entity: str, updates: dict[str, str],
            removals: list[str] | None = None, annotation: str = "agent") -> str:
    path = tmp_path / "wf.py"
    path.write_text(source, encoding="utf-8")
    engine = RewriteEngine(str(path), cst.parse_module(source), source)
    engine.merge_definition_annotation_args(
        entityType=entity, annotationName=annotation, argUpdates=updates, argRemovals=removals
    )
    engine.save()
    return path.read_text(encoding="utf-8")


# A bare decorator (#82)

BARE = '''
from wfpy import agent, keep


@keep
# why it is an agent
@agent  # note
class A:
    pass
'''


def test_a_bare_decorator_keeps_its_comments_when_it_gets_its_first_arg(tmp_path: Path) -> None:
    """The comment on the decorator's line and the comment lines above it were
    lost: the merge wrote a new decorator in place of the one in the file."""
    assert _merged(tmp_path, BARE, "A", {"model": '"m"'}) == '''
from wfpy import agent, keep


@keep
# why it is an agent
@agent(model="m")  # note
class A:
    pass
'''


def test_a_bare_decorator_is_laid_out_as_an_empty_call_is(tmp_path: Path) -> None:
    """Two args given to `@agent` were written one per line, and on one line
    when given to `@agent()`."""
    updates = {"model": '"m"', "mode": '"plan"'}
    expected = '''
from wfpy import agent, keep


@keep
# why it is an agent
@agent(model="m", mode="plan")  # note
class A:
    pass
'''
    assert _merged(tmp_path, BARE, "A", updates) == expected
    assert _merged(tmp_path, BARE.replace("@agent  #", "@agent()  #"), "A", updates) == expected


def test_a_bare_decorator_in_a_class_keeps_the_lines_above_it(tmp_path: Path) -> None:
    """A blank line and a comment between two decorators stay, in a nested
    class as at the top of the file; a name to remove changes nothing."""
    assert _merged(tmp_path, '''
from wfpy import agent, keep


class Outer:
    @keep

    # indented with its class
    @agent
    class A:
        pass
''', "A", {"useSkill": "true"}, ["mode"]) == '''
from wfpy import agent, keep


class Outer:
    @keep

    # indented with its class
    @agent(useSkill=True)
    class A:
        pass
'''


# A decorator written with its module, and a decorator on a function (#83)

WITH_MODULE = '''
import wfpy


@wfpy.keep
@wfpy.agent(prompt="x", variant="high")  # the agent
class A:
    pass
'''


@pytest.mark.parametrize("annotation", ["agent", "wfpy.agent"])
def test_a_decorator_written_with_its_module_is_found(tmp_path: Path, annotation: str) -> None:
    """`@wfpy.agent(...)` answered "Definition not found". It stays written
    with its module, the file gets no `from wfpy import agent`, and the
    spellings of an arg are one arg on it as on `@agent`."""
    updates = {"model": '"m"', "reasoningEffort": '"max"'}
    assert _merged(tmp_path, WITH_MODULE, "A", updates, annotation=annotation) == '''
import wfpy


@wfpy.keep
@wfpy.agent(prompt="x", variant="max", model="m")  # the agent
class A:
    pass
'''


def test_a_bare_decorator_written_with_its_module_keeps_it(tmp_path: Path) -> None:
    assert _merged(tmp_path, '''
import wfpy


@wfpy.agent  # note
class A:
    pass
''', "A", {"model": '"m"'}) == '''
import wfpy


@wfpy.agent(model="m")  # note
class A:
    pass
'''


def test_an_annotation_named_with_its_module_finds_the_plain_decorator(tmp_path: Path) -> None:
    """`annotationName="wfpy.agent"` on a file that writes `@agent(...)`."""
    assert _merged(tmp_path, '''
from wfpy import agent


@agent(prompt="x", variant="high")
class A:
    pass
''', "A", {"reasoningEffort": '"max"'}, ["prompt"], annotation="wfpy.agent") == '''
from wfpy import agent


@agent(variant="max")
class A:
    pass
'''


ON_A_FUNCTION = '''
from wfpy import config, workflow


@config(path=["/opt/tools/bin"])
# what the graph takes and gives
@workflow(outputs={"Out": str})
def flow():
    pass


@workflow
def other():
    pass
'''


def test_a_decorator_on_a_function_is_found(tmp_path: Path) -> None:
    """A function workflow answered "Definition not found": only classes were visited."""
    updates = {"inputs": '{"In": str}'}
    assert _merged(tmp_path, ON_A_FUNCTION, "flow", updates, annotation="workflow") == '''
from wfpy import config, workflow


@config(path=["/opt/tools/bin"])
# what the graph takes and gives
@workflow(outputs={"Out": str}, inputs={"In": str})
def flow():
    pass


@workflow
def other():
    pass
'''


def test_config_on_a_function_workflow_is_merged_into(tmp_path: Path) -> None:
    updates = {"env": '{"TOOLS_HOME": "/opt/tools"}'}
    assert _merged(tmp_path, ON_A_FUNCTION, "flow", updates, ["path"], annotation="config") == '''
from wfpy import config, workflow


@config(env={"TOOLS_HOME": "/opt/tools"})
# what the graph takes and gives
@workflow(outputs={"Out": str})
def flow():
    pass


@workflow
def other():
    pass
'''


def test_a_bare_decorator_on_a_function_gets_its_first_arg(tmp_path: Path) -> None:
    updates = {"max_workers": "4"}
    updated = _merged(tmp_path, ON_A_FUNCTION, "other", updates, annotation="wfpy.workflow")
    assert updated == ON_A_FUNCTION.replace(
        "@workflow\ndef other", "@workflow(max_workers=4)\ndef other"
    )


def _removed(tmp_path: Path, source: str, entity: str, annotation: str) -> str:
    path = tmp_path / "wf.py"
    path.write_text(source, encoding="utf-8")
    engine = RewriteEngine(str(path), cst.parse_module(source), source)
    engine.remove_definition_annotation(entityType=entity, annotationName=annotation)
    engine.save()
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("annotation", ["keep", "wfpy.keep"])
def test_removing_an_annotation_finds_one_written_with_its_module(
    tmp_path: Path, annotation: str
) -> None:
    """`removeDefinitionAnnotation` matched as the merge did, and answered
    "Annotation not found for removal"."""
    updated = _removed(tmp_path, WITH_MODULE, "A", annotation)
    assert updated == WITH_MODULE.replace("@wfpy.keep\n", "")


def test_removing_an_annotation_finds_one_on_a_function(tmp_path: Path) -> None:
    updated = _removed(tmp_path, ON_A_FUNCTION, "flow", "config")
    assert updated == ON_A_FUNCTION.replace('@config(path=["/opt/tools/bin"])\n', "")


METHODS = '''
from wfpy import action, task, workflow


@task
class First:
    @action(consumes={"In": 1})
    def make(self, x):
        return x


@task
class Second:
    @action(consumes={"In": 2})
    def make(self, x):
        return x


@workflow
def make():
    pass
'''


def test_a_method_is_not_found_by_its_name(tmp_path: Path) -> None:
    """`entityType` names no class: the `make` of `First` was edited, and
    removed from, for the `make` of any class."""
    with pytest.raises(RewriteError, match="Definition not found for annotation merge: make"):
        _merged(tmp_path, METHODS, "make", {"produces": '{"Out": 1}'}, annotation="action")
    with pytest.raises(RewriteError, match="Annotation not found for removal"):
        _removed(tmp_path, METHODS, "make", "action")
    assert (tmp_path / "wf.py").read_text(encoding="utf-8") == METHODS


def test_a_function_is_found_beside_the_methods_of_its_name(tmp_path: Path) -> None:
    updated = _merged(tmp_path, METHODS, "make", {"outputs": '{"Out": str}'}, annotation="workflow")
    assert updated == METHODS.replace("@workflow\n", '@workflow(outputs={"Out": str})\n')


def test_a_function_inside_a_function_is_found(tmp_path: Path) -> None:
    source = '''
from wfpy import workflow


def build():
    @workflow
    def inner():
        pass

    return inner
'''
    updated = _merged(tmp_path, source, "inner", {"outputs": '{"Out": str}'}, annotation="workflow")
    assert updated == source.replace("    @workflow\n", '    @workflow(outputs={"Out": str})\n')


# An empty call written over lines (#84)


def _agent(decorator: str) -> str:
    return f"\nfrom wfpy import agent\n\n\n{decorator}\nclass A:\n    pass\n"


def test_an_empty_call_over_two_lines_gets_its_first_arg_on_its_own_line(tmp_path: Path) -> None:
    """`@agent(⏎)` became `@agent(⏎model="m")`: the arg at the decorator's
    column and the closing parenthesis after it."""
    assert _merged(tmp_path, _agent("@agent(\n)"), "A", {"model": '"m"'}) == _agent('''@agent(
    model="m",
)''')


def test_an_empty_call_keeps_the_comment_on_its_opening_line(tmp_path: Path) -> None:
    source = _agent("@agent(  # note\n)")
    updates = {"model": '"m"', "mode": '"plan"'}
    assert _merged(tmp_path, source, "A", updates) == _agent('''@agent(  # note
    model="m",
    mode="plan",
)''')


def test_an_empty_call_keeps_its_comment_lines_above_the_first_arg(tmp_path: Path) -> None:
    """The args are indented as the comment lines are: by four spaces, and in
    the second call by two, in a class indented by four. The blank line and
    the closing parenthesis of that call stay where they were."""
    source = _agent("@agent(\n    # nothing yet\n)")
    assert _merged(tmp_path, source, "A", {"model": '"m"'}) == _agent('''@agent(
    # nothing yet
    model="m",
)''')
    assert _merged(tmp_path, '''
from wfpy import agent


class Outer:
    @agent(  # note

      # nothing yet
      # still nothing
        )
    class A:
        pass
''', "A", {"model": '"m"', "mode": '"plan"'}) == '''
from wfpy import agent


class Outer:
    @agent(  # note

      # nothing yet
      # still nothing
      model="m",
      mode="plan",
        )
    class A:
        pass
'''


def test_an_empty_call_with_a_comment_at_its_own_column_indents_its_args(tmp_path: Path) -> None:
    """A comment line that is not indented gives no indentation to follow:
    four spaces, as with no comment line."""
    source = _agent("@agent(\n# nothing yet\n)")
    assert _merged(tmp_path, source, "A", {"model": '"m"'}) == _agent('''@agent(
# nothing yet
    model="m",
)''')


def test_an_empty_call_over_lines_is_a_call_written_one_arg_per_line_after(tmp_path: Path) -> None:
    """What the first merge writes is merged into as any call written one arg
    per line: the same merge again changes nothing, and the next arg goes on
    a line of its own. With nothing to add, the empty call is left as written."""
    once = _merged(tmp_path, _agent("@agent(\n)"), "A", {"model": '"m"'})
    assert _merged(tmp_path, once, "A", {"model": '"m"'}) == once
    assert _merged(tmp_path, once, "A", {"mode": '"plan"'}) == _agent('''@agent(
    model="m",
    mode="plan",
)''')
    assert _merged(tmp_path, _agent("@agent(\n)"), "A", {}, ["mode"]) == _agent("@agent(\n)")


def test_an_empty_call_over_lines_on_a_function_written_with_its_module(tmp_path: Path) -> None:
    """The three forms together: `@wfpy.workflow(⏎)` on a `def`."""
    assert _merged(tmp_path, '''
import wfpy


@wfpy.workflow(
)
def flow():
    pass
''', "flow", {"inputs": '{"In": str}', "outputs": '{"Out": str}'}, annotation="workflow") == '''
import wfpy


@wfpy.workflow(
    inputs={"In": str},
    outputs={"Out": str},
)
def flow():
    pass
'''
