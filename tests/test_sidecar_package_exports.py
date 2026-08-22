"""Creating a node in a package keeps the package's `__init__.py` in step.

The end-to-end half: the sidecar op writes the node, updates the package, and
REPORTS the second file so the host can snapshot it. That report is the whole
reason this is safe — the host makes an edit undoable by snapshotting the file
it knows about, so a second write it never heard of would leave undo restoring
half the change.

Opt-in for the same reason: a host that cannot snapshot a second file never asks
for one, so the two repos can ship in either order.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _run_sidecar(payload: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "wfpy.sidecar"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        cwd=str(Path(__file__).resolve().parents[1]),
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
    )
    out = (proc.stdout or "").strip().splitlines()
    return json.loads(out[-1]) if out else {"status": "error", "message": proc.stderr[:400]}


def _package(tmp_path: Path, init_source: str = "") -> Path:
    pkg = tmp_path / "viewers"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(init_source, encoding="utf-8")
    target = pkg / "html.py"
    target.write_text('"""Viewers."""\n', encoding="utf-8")
    return target


def _create(target: Path, *, update_exports: bool, name: str = "Shown") -> dict:
    args: dict = {"kind": "viewer", "name": name}
    if update_exports:
        args["updatePackageExports"] = True
    return _run_sidecar({"file": str(target), "op": "wfpy.createTaskType", "args": args})


def test_creates_the_node_and_exports_it(tmp_path: Path) -> None:
    target = _package(tmp_path, '__all__ = []\n')
    response = _create(target, update_exports=True)

    assert response["status"] == "ok"
    assert "@viewer" in target.read_text(encoding="utf-8")

    init_text = (target.parent / "__init__.py").read_text(encoding="utf-8")
    assert "from .html import Shown" in init_text
    assert '"Shown"' in init_text


def test_reports_the_second_file_so_undo_can_cover_it(tmp_path: Path) -> None:
    # The load-bearing part: the host snapshots what it is told about.
    target = _package(tmp_path)
    response = _create(target, update_exports=True)

    changed = response.get("changedFiles") or []
    assert [entry["file"] for entry in changed] == [str(target.parent / "__init__.py")]
    assert changed[0]["revision"].startswith("sha256:")


def test_touches_nothing_extra_unless_asked(tmp_path: Path) -> None:
    # A host that cannot snapshot a second file never asks for one, so the two
    # repos can ship in either order without undo ever being half a change.
    target = _package(tmp_path)
    response = _create(target, update_exports=False)

    assert response["status"] == "ok"
    assert response.get("changedFiles") in (None, [])
    assert (target.parent / "__init__.py").read_text(encoding="utf-8") == ""


def test_says_nothing_when_the_file_is_not_in_a_package(tmp_path: Path) -> None:
    target = tmp_path / "standalone.py"
    target.write_text('"""Standalone."""\n', encoding="utf-8")
    response = _create(target, update_exports=True)

    assert response["status"] == "ok"
    assert response.get("changedFiles") in (None, [])


def test_reports_nothing_when_the_export_is_already_there(tmp_path: Path) -> None:
    # Nothing changed, so there is nothing for the host to snapshot — reporting
    # the file anyway would make undo restore a file it never altered.
    target = _package(tmp_path, "from .html import Shown\n")
    response = _create(target, update_exports=True)

    assert response["status"] == "ok"
    assert response.get("changedFiles") in (None, [])


def test_the_created_node_still_wins_if_the_export_fails(tmp_path: Path) -> None:
    # The export runs AFTER the edit the caller asked for. An unwritable
    # __init__.py must not turn a successful creation into an error: the node is
    # real either way, and the missing export is one line to add by hand.
    target = _package(tmp_path)
    init_path = target.parent / "__init__.py"
    init_path.chmod(0o444)
    try:
        response = _create(target, update_exports=True)
        assert response["status"] == "ok"
        assert "@viewer" in target.read_text(encoding="utf-8")
    finally:
        init_path.chmod(0o644)
