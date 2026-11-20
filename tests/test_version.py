"""The version is said once, in `pyproject.toml`, and nowhere by hand.

`wfpy.__version__` was a constant: it said `0.1.0` in the 1.1.0 wheel, since a
release changes `pyproject.toml` and nothing told it to change the constant.
What a release does change by hand, the README's install lines and the
CHANGELOG's heading, is held to `pyproject.toml` here.
"""

from __future__ import annotations

import re
import sys
from importlib.metadata import version
from pathlib import Path

import wfpy

if sys.version_info >= (3, 11):
    import tomllib
else:  # Python 3.10: the standard library has no tomllib
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]


def _project_version() -> str:
    return str(tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"])


def test_the_package_says_the_version_that_is_installed():
    assert wfpy.__version__ == version("wfpy")
    assert wfpy.__version__ != "0.1.0"


def test_the_readme_installs_the_version_of_pyproject():
    """Two lines install a release by its number: the wheel on GitHub and the
    tagged source. Both name the version twice between them."""
    readme = (ROOT / "README.md").read_text()
    current = _project_version()

    wheel = re.findall(r"releases/download/v([^/]+)/wfpy-([^-]+)-py3-none-any\.whl", readme)
    tagged = re.findall(r"git\+https://github\.com/huawei-csl/wfpy@v([0-9][^\s\"]*)", readme)

    assert wheel == [(current, current)]
    assert tagged == [current]


def test_the_changelog_has_a_section_for_the_version_of_pyproject():
    changelog = (ROOT / "CHANGELOG.md").read_text()
    headings = re.findall(r"^## \[([^\]]+)\]", changelog, flags=re.MULTILINE)

    assert headings[0] == "Unreleased"
    assert headings[1] == _project_version()
