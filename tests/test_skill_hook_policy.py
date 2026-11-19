"""Which skill folder a hook approval names.

A skill name resolves to a folder that depends on the working directory,
the variables and what a cloned repository holds. A policy entry that
approves `review:pre` by name approves the `scripts/pre.sh` of whichever
`review` a run finds, a repository's `.claude/skills/review` included. An
entry may name the folder it approves (`dir`), and `requireDir` makes the
entries that name none count for nothing.
"""

from __future__ import annotations

import json
import os
import types
from pathlib import Path
from typing import Any

import pytest

from wfpy._agent_prompt_runtime import _run_skill_hook
from wfpy._agent_skill_lookup import agent_skill_lookup


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """A home and a working directory of the test's own, and no variable or
    git configuration of the machine's."""

    for name in (
        "WF_SKILLS_ROOT",
        "WF_CLAUDE_AGENTS_ROOT",
        "WF_SKILL_HOOK_AUTH",
        "WF_SKILL_HOOK_POLICY",
        "CODEX_HOME",
        "XDG_CONFIG_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return home


def hooked_skill(root: Path, name: str = "review") -> Path:
    """`<root>/<name>` with a `SKILL.md` and a `scripts/pre.sh`; the folder."""

    skill_dir = root / name
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nhooks:\n  pre: pre\n---\nbody\n", encoding="utf-8")
    (skill_dir / "scripts" / "pre.sh").write_text("echo ran\n", encoding="utf-8")
    return skill_dir


def workflow(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "flow.py"
    source.write_text("", encoding="utf-8")
    return source


def run_pre(source: Path, policy: Any, policy_file: Path) -> dict[str, Any]:
    """Run the `review` skill's `pre` hook for the workflow at `source`
    under `policy`, written to `policy_file`."""

    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text(json.dumps(policy), encoding="utf-8")
    plan = types.SimpleNamespace(
        source_path=str(source),
        work_dir=None,
        options={"skill_hook_auth": "policy", "skill_hook_policy": str(policy_file)},
    )
    return _run_skill_hook(
        skill_name="review",
        hook_name="pre",
        phase="pre",
        actor_name="reviewer",
        payload={},
        response_text=None,
        plan=plan,
        timeout_ms=10_000,
    )


def ran(result: dict[str, Any]) -> bool:
    return bool(result.get("ran")) and result.get("exitCode") == 0


class TestNameOnlyEntries:
    @pytest.mark.parametrize("entry", ["review:pre", "review:pre:sh"])
    def test_a_name_approves_the_hook_wherever_it_resolves(self, entry, tmp_path):
        """As before: the policies written so far keep working."""
        project = tmp_path / "project"
        hooked_skill(project / ".claude" / "skills")

        result = run_pre(workflow(project), {"allow": [entry]}, tmp_path / "policy.json")

        assert ran(result), result

    def test_an_entry_that_names_no_folder_approves_by_name(self, tmp_path):
        project = tmp_path / "project"
        hooked_skill(project / ".claude" / "skills")

        result = run_pre(
            workflow(project),
            {"allow": [{"skill": "review", "hook": "pre"}]},
            tmp_path / "policy.json",
        )

        assert ran(result), result
        assert "by name" in result["authorized"]


class TestFolderEntries:
    def test_an_entry_naming_the_folder_approves_its_hook(self, tmp_path):
        """`dir` is read from the policy file's folder."""
        project = tmp_path / "project"
        hooked_skill(project / ".claude" / "skills")
        entry = {"skill": "review", "hook": "pre", "dir": "../project/.claude/skills/review"}

        result = run_pre(workflow(project), {"allow": [entry]}, tmp_path / "config" / "p.json")

        assert ran(result), result
        assert "by folder" in result["authorized"]

    def test_the_skill_a_repository_adds_is_not_the_one_approved(self, tmp_path, isolated):
        """The user approved the hook of their own `~/.claude/skills/review`;
        a cloned repository with a `.claude/skills/review` of its own takes
        the name, and its `scripts/pre.sh` does not run."""
        approved = hooked_skill(isolated / ".claude" / "skills")
        cloned = tmp_path / "cloned"
        theirs = hooked_skill(cloned / ".claude" / "skills")
        entry = {"skill": "review", "hook": "pre", "dir": str(approved)}

        result = run_pre(workflow(cloned), {"allow": [entry]}, tmp_path / "policy.json")

        assert result["ran"] is False
        assert result["reason"].startswith("denied: ")
        assert str(theirs.resolve()) in result["reason"]
        assert result["skillDir"] == str(theirs.resolve())

    def test_the_approved_folder_runs_where_the_name_resolves_to_it(self, tmp_path, isolated):
        approved = hooked_skill(isolated / ".claude" / "skills")
        entry = {"skill": "review", "hook": "pre", "dir": "~/.claude/skills/review"}

        result = run_pre(workflow(tmp_path / "project"), {"allow": [entry]}, tmp_path / "p.json")

        assert ran(result), result
        assert result["skillDir"] == str(approved.resolve())

    @pytest.mark.parametrize("named", ["link", "target"])
    def test_dir_is_compared_after_resolving(self, named, tmp_path):
        """A skill installed as a link into a checkout is approved by the
        link's path or by the checkout's."""
        checkout = hooked_skill(tmp_path / "checkout")
        project = tmp_path / "project"
        (project / ".claude" / "skills").mkdir(parents=True)
        link = project / ".claude" / "skills" / "review"
        try:
            link.symlink_to(checkout, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"cannot create a symlink here: {error}")
        entry = {
            "skill": "review",
            "hook": "pre",
            "dir": str(link if named == "link" else checkout),
        }

        result = run_pre(workflow(project), {"allow": [entry]}, tmp_path / "policy.json")

        assert ran(result), result

    @pytest.mark.parametrize(("interpreter", "runs"), [("sh", True), ("python", False)])
    def test_an_interpreter_named_must_be_the_scripts(self, interpreter, runs, tmp_path):
        project = tmp_path / "project"
        skill_dir = hooked_skill(project / ".claude" / "skills")
        entry = {
            "skill": "review",
            "hook": "pre",
            "interpreter": interpreter,
            "dir": str(skill_dir),
        }

        result = run_pre(workflow(project), {"allow": [entry]}, tmp_path / "policy.json")

        assert ran(result) is runs, result

    def test_an_entry_for_another_hook_or_skill_approves_nothing(self, tmp_path):
        project = tmp_path / "project"
        skill_dir = hooked_skill(project / ".claude" / "skills")
        entries = [
            {"skill": "review", "hook": "post", "dir": str(skill_dir)},
            {"skill": "other", "hook": "pre", "dir": str(skill_dir)},
        ]

        result = run_pre(workflow(project), {"allow": entries}, tmp_path / "policy.json")

        assert result["ran"] is False


class TestRequireDir:
    def test_entries_that_name_no_folder_count_for_nothing(self, tmp_path):
        project = tmp_path / "project"
        skill_dir = hooked_skill(project / ".claude" / "skills")
        policy = {
            "requireDir": True,
            "allow": ["review:pre", "review:pre:sh", {"skill": "review", "hook": "pre"}],
        }

        result = run_pre(workflow(project), policy, tmp_path / "policy.json")

        assert result["ran"] is False
        assert "requireDir" in result["reason"]
        assert str(skill_dir.resolve()) in result["reason"]

    def test_an_entry_naming_the_folder_still_approves(self, tmp_path):
        project = tmp_path / "project"
        skill_dir = hooked_skill(project / ".claude" / "skills")
        policy = {
            "requireDir": True,
            "allow": [{"skill": "review", "hook": "pre", "dir": str(skill_dir)}],
        }

        result = run_pre(workflow(project), policy, tmp_path / "policy.json")

        assert ran(result), result

    def test_false_is_the_old_rule(self, tmp_path):
        project = tmp_path / "project"
        hooked_skill(project / ".claude" / "skills")

        result = run_pre(
            workflow(project), {"requireDir": False, "allow": ["review:pre"]}, tmp_path / "p.json"
        )

        assert ran(result), result


class TestAnEntryThatCannotBeRead:
    """An entry the runtime cannot read is an error, not an approval by
    name: `{"directory": ...}` misspelt would otherwise approve the hook in
    every folder."""

    @pytest.mark.parametrize(
        "entry",
        [
            {"skill": "review", "hook": "pre", "directory": "/somewhere"},
            {"skill": "review", "dir": "/somewhere"},
            {"hook": "pre"},
            {"skill": "review", "hook": "pre", "dir": 3},
            {"skill": "review", "hook": "pre", "dir": ""},
            {"skill": "review", "hook": "pre", "interpreter": ""},
        ],
    )
    def test_it_is_an_error(self, entry, tmp_path):
        project = tmp_path / "project"
        hooked_skill(project / ".claude" / "skills")

        with pytest.raises(RuntimeError, match="skill hook policy"):
            run_pre(workflow(project), {"allow": [entry]}, tmp_path / "policy.json")

    def test_an_entry_that_is_neither_text_nor_an_object_approves_nothing(self, tmp_path):
        """As it did before object entries were read: it was never a name."""
        project = tmp_path / "project"
        hooked_skill(project / ".claude" / "skills")

        result = run_pre(workflow(project), {"allow": [7, None]}, tmp_path / "policy.json")

        assert result["ran"] is False
        assert "not listed" in result["reason"]

    @pytest.mark.parametrize("policy", [["review:pre"], {"allow": "review:pre"}])
    def test_a_policy_that_is_no_object_or_whose_allow_is_no_list_is_an_error(
        self, policy, tmp_path
    ):
        project = tmp_path / "project"
        hooked_skill(project / ".claude" / "skills")

        with pytest.raises(RuntimeError, match="skill hook policy"):
            run_pre(workflow(project), policy, tmp_path / "policy.json")

    def test_a_require_dir_that_is_not_true_or_false_is_an_error(self, tmp_path):
        project = tmp_path / "project"
        hooked_skill(project / ".claude" / "skills")

        with pytest.raises(RuntimeError, match="requireDir"):
            run_pre(workflow(project), {"requireDir": "yes", "allow": []}, tmp_path / "policy.json")


class TestTheRecordNamesTheFolder:
    def test_a_hook_that_ran_says_which_folder_it_ran_from(self, tmp_path):
        project = tmp_path / "project"
        skill_dir = hooked_skill(project / ".claude" / "skills")

        result = run_pre(workflow(project), {"allow": ["review:pre"]}, tmp_path / "policy.json")

        assert ran(result), result
        assert result["skillDir"] == str(skill_dir.resolve())
        assert str(skill_dir.resolve()) in result["authorized"]

    @pytest.mark.parametrize("mode", ["allow-all", "deny-all"])
    def test_a_mode_says_the_folder_too(self, mode, tmp_path):
        project = tmp_path / "project"
        skill_dir = hooked_skill(project / ".claude" / "skills")
        plan = types.SimpleNamespace(
            source_path=str(workflow(project)), work_dir=None, options={"skill_hook_auth": mode}
        )

        result = _run_skill_hook(
            skill_name="review",
            hook_name="pre",
            phase="pre",
            actor_name="reviewer",
            payload={},
            response_text=None,
            plan=plan,
            timeout_ms=10_000,
        )

        assert result["skillDir"] == str(skill_dir.resolve())
        said = result["authorized"] if mode == "allow-all" else result["reason"]
        assert said.startswith("mode=" if mode == "allow-all" else "denied: mode=")
        assert str(skill_dir.resolve()) in said


class TestTheListGivesTheFolderToApprove:
    def test_the_dir_wfpy_skills_lists_is_the_one_an_approval_names(self, tmp_path):
        """`wfpy skills --json` gives each skill's `dir`, the folder a run
        takes: the `dir` an entry names to approve its hooks."""
        project = tmp_path / "project"
        hooked_skill(project / ".claude" / "skills")
        source = workflow(project)
        [listed] = agent_skill_lookup(str(source))["skills"]
        entry = {"skill": "review", "hook": "pre", "dir": listed["dir"]}

        result = run_pre(source, {"requireDir": True, "allow": [entry]}, tmp_path / "p.json")

        assert ran(result), result


class TestTextEntriesAreReadAsBefore:
    @pytest.mark.parametrize("entry", ["review:pre:", "review:pre:sh:x", "review", "review:"])
    def test_an_entry_that_is_not_the_name_approves_nothing(self, entry, tmp_path):
        project = tmp_path / "project"
        hooked_skill(project / ".claude" / "skills")

        result = run_pre(workflow(project), {"allow": [entry]}, tmp_path / "policy.json")

        assert result["ran"] is False
