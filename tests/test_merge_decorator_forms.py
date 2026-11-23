"""The decorator merge (`mergeDefinitionAnnotationArgs`, an IDE's Save) on the
forms a decorator is written in. Each test holds the exact text written."""

from __future__ import annotations

from pathlib import Path

import libcst as cst

from wfpy.rewrite import RewriteEngine


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
