"""The README's Dependencies table says what `pyproject.toml` says.

The table states each dependency's version bound a second time, by hand. A pin
that changes in `pyproject.toml` alone leaves the README telling a user to
expect another version, and nothing else would notice.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # Python 3.10: the standard library has no tomllib
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]

# The `dev` tools are listed by name only: their bounds matter to nobody who
# reads the README to install wfpy.
NAMED_ONLY = {"dev"}


def _declared() -> list[tuple[str, str, str]]:
    """`(group, package, bound)` for every requirement of the project: the
    group is `core` or the extra's name, the bound what follows the name up to
    an environment marker (`>=2.0` of `tomli>=2.0; python_version < '3.11'`)."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    groups = {"core": project["dependencies"], **project["optional-dependencies"]}
    declared = []
    for group, requirements in groups.items():
        for requirement in requirements:
            match = re.match(r"\s*([A-Za-z0-9_.-]+)\s*([^;]*)", requirement)
            assert match, requirement
            declared.append((group, match.group(1), match.group(2).strip()))
    return declared


def _table_rows() -> list[str]:
    readme = (ROOT / "README.md").read_text()
    assert "\n## Dependencies\n" in readme, "the README has no Dependencies section"
    section = readme.split("\n## Dependencies\n", 1)[1].split("\n## ", 1)[0]
    return [line for line in section.splitlines() if line.startswith("|")]


def test_the_readme_lists_every_dependency_with_its_bound() -> None:
    rows = _table_rows()
    for group, package, bound in _declared():
        row = next((r for r in rows if f"`{package}`" in r), None)
        assert row is not None, f"{package} ({group}) is missing from the README's table"
        if group not in NAMED_ONLY:
            assert f"`{bound}`" in row, (
                f"the README gives {package} another bound than pyproject.toml's {bound!r}: {row}"
            )


def test_the_readme_lists_no_dependency_the_project_dropped() -> None:
    declared = {package for _, package, _ in _declared()}
    for row in _table_rows()[2:]:  # past the header and its rule
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        for package in re.findall(r"`([^`]+)`", cells[1]):
            assert package in declared, f"the README lists {package}, which pyproject.toml does not"
