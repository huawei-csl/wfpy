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
import logging
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

    def test_dir_is_read_from_the_folder_of_the_file_a_linked_policy_is(self, tmp_path):
        """A policy kept in a dotfiles checkout and linked into place names
        its folders from the checkout, where it is written."""
        project = tmp_path / "project"
        hooked_skill(project / ".claude" / "skills")
        entry = {"skill": "review", "hook": "pre", "dir": "../project/.claude/skills/review"}
        written = tmp_path / "dotfiles" / "policy.json"
        written.parent.mkdir()
        written.write_text(json.dumps({"allow": [entry]}), encoding="utf-8")
        linked = tmp_path / "config" / "wfpy" / "policy.json"
        linked.parent.mkdir(parents=True)
        try:
            linked.symlink_to(written)
        except OSError as error:
            pytest.skip(f"cannot create a symlink here: {error}")
        plan = types.SimpleNamespace(
            source_path=str(workflow(project)),
            work_dir=None,
            options={"skill_hook_auth": "policy", "skill_hook_policy": str(linked)},
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

        assert ran(result), result

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
    """An object entry the runtime cannot read approves nothing, and the
    refusal says so: read as an entry without `dir`, a misspelt
    `{"directory": ...}` or a `"dir": null` would approve the hook in every
    folder."""

    @pytest.mark.parametrize(
        "entry",
        [
            {"skill": "review", "hook": "pre", "directory": "/somewhere"},
            {"skill": "review", "hook": "pre", "dir": None},
            {"skill": "review", "hook": "pre", "interpreter": None},
            {"skill": "review", "hook": "pre", "interpreter": "bash"},
            {"skill": "review", "hook": "pre", "dir": 3},
            {"skill": "review", "hook": "pre", "dir": ""},
            {"skill": "review", "hook": "pre", "interpreter": ""},
            {"skill": "review", "dir": "/somewhere"},
            {"hook": "pre"},
        ],
    )
    def test_it_approves_nothing_and_the_refusal_says_why(self, entry, tmp_path):
        project = tmp_path / "project"
        hooked_skill(project / ".claude" / "skills")

        result = run_pre(workflow(project), {"allow": [entry]}, tmp_path / "policy.json")

        assert result["ran"] is False
        assert "what it cannot read" in result["reason"]
        assert json.dumps(entry) in result["reason"]

    @pytest.mark.parametrize(
        ("entry", "said"),
        [
            ({"hook": "pre"}, "it has no skill"),
            ({"dir": "x"}, "it has no skill and no hook"),
            ({"skill": "review", "hook": "pre", "directory": "x"}, "it holds directory"),
            ({"skill": "review", "hook": "pre", "dir": None}, "its dir is not text"),
            ({"skill": "review", "hook": "pre", "interpreter": "bash"}, "its interpreter is not"),
        ],
    )
    def test_the_refusal_says_what_it_is_that_cannot_be_read(self, entry, said, tmp_path):
        project = tmp_path / "project"
        hooked_skill(project / ".claude" / "skills")

        result = run_pre(workflow(project), {"allow": [entry]}, tmp_path / "policy.json")

        assert said in result["reason"]

    def test_a_dir_that_names_a_file_is_not_read(self, tmp_path):
        """`SKILL.md` or `scripts/pre.sh` in place of the folder: it would
        match no folder and the refusal would not say why."""
        project = tmp_path / "project"
        folder = hooked_skill(project / ".claude" / "skills")
        entry = {"skill": "review", "hook": "pre", "dir": str(folder / "SKILL.md")}

        result = run_pre(workflow(project), {"allow": [entry]}, tmp_path / "policy.json")

        assert result["ran"] is False
        assert "its dir is no folder" in result["reason"]

    def test_the_spaces_around_a_name_are_not_part_of_it(self, tmp_path):
        """A run takes `skill=" review "` as `review`; so does an entry."""
        project = tmp_path / "project"
        folder = hooked_skill(project / ".claude" / "skills")
        entry = {"skill": " review ", "hook": " pre ", "dir": f" {folder} "}

        result = run_pre(workflow(project), {"allow": [entry]}, tmp_path / "policy.json")

        assert ran(result), result

    def test_the_entries_it_reads_still_approve(self, tmp_path):
        """A note beside the entries (`{"comment": ...}`) stops no run."""
        project = tmp_path / "project"
        hooked_skill(project / ".claude" / "skills")
        policy = {"allow": [{"comment": "the lint hook is ours"}, "review:pre"]}

        result = run_pre(workflow(project), policy, tmp_path / "policy.json")

        assert ran(result), result

    def test_an_entry_that_is_neither_text_nor_an_object_approves_nothing(self, tmp_path):
        """As it did before object entries were read: it was never a name."""
        project = tmp_path / "project"
        hooked_skill(project / ".claude" / "skills")

        result = run_pre(workflow(project), {"allow": [7, None]}, tmp_path / "policy.json")

        assert result["ran"] is False
        assert "not listed" in result["reason"]
        assert "7 (it is neither text nor an object)" in result["reason"]

    def test_an_allow_that_is_an_object_has_its_keys_read_as_before(self, tmp_path):
        """`set(str(v) for v in allow)` took an object's keys."""
        project = tmp_path / "project"
        hooked_skill(project / ".claude" / "skills")

        result = run_pre(
            workflow(project), {"allow": {"review:pre": True}}, tmp_path / "policy.json"
        )

        assert ran(result), result

    def test_what_an_objects_keys_hold_is_said_to_be_unread(self, tmp_path, caplog):
        """A `dir` written there is not read, and the entry approves the
        hook by name, in every folder: only the log can say so, the hook
        having run."""
        project = tmp_path / "project"
        folder = hooked_skill(project / ".claude" / "skills")
        policy = {"allow": {"review:pre": {"dir": str(folder)}}}

        with caplog.at_level(logging.WARNING, logger="wfpy"):
            result = run_pre(workflow(project), policy, tmp_path / "policy.json")

        assert ran(result), result
        assert "allow is an object: its keys are read as entries" in caplog.text

    def test_the_log_says_what_it_cannot_read(self, tmp_path, caplog):
        project = tmp_path / "project"
        hooked_skill(project / ".claude" / "skills")
        policy = {"allow": [{"skill": "review", "hook": "pre", "dir": None}]}

        with caplog.at_level(logging.WARNING, logger="wfpy"):
            run_pre(workflow(project), policy, tmp_path / "policy.json")

        assert "what it cannot read" in caplog.text
        assert "its dir is not text" in caplog.text

    @pytest.mark.parametrize(
        ("policy", "said"),
        [
            (["review:pre"], "the policy is not a JSON object"),
            ({"allow": "review:pre"}, "is not a list"),
            ({"allow": 3}, "is not a list"),
        ],
    )
    def test_a_policy_or_an_allow_of_another_kind_approves_nothing(self, policy, said, tmp_path):
        """It stops the hooks, as it did before, rather than the firing: an
        approval is what a policy gives, and this one gives none."""
        project = tmp_path / "project"
        hooked_skill(project / ".claude" / "skills")

        result = run_pre(workflow(project), policy, tmp_path / "policy.json")

        assert result["ran"] is False
        assert said in result["reason"]

    def test_a_require_dir_that_is_not_true_or_false_is_taken_as_true(self, tmp_path):
        """The reading that approves less: a name-only entry counts for
        nothing, and the refusal says the setting was not read."""
        project = tmp_path / "project"
        hooked_skill(project / ".claude" / "skills")
        policy = {"requireDir": "yes", "allow": ["review:pre"]}

        result = run_pre(workflow(project), policy, tmp_path / "policy.json")

        assert result["ran"] is False
        assert '"yes" is not true or false: taken as true' in result["reason"]

    def test_a_policy_file_that_does_not_load_stops_the_firing(self, tmp_path):
        """As before: a policy that is not JSON is read as approving nothing
        by no one."""
        project = tmp_path / "project"
        hooked_skill(project / ".claude" / "skills")
        written = tmp_path / "policy.json"
        written.write_text("{not json", encoding="utf-8")
        plan = types.SimpleNamespace(
            source_path=str(workflow(project)),
            work_dir=None,
            options={"skill_hook_auth": "policy", "skill_hook_policy": str(written)},
        )

        with pytest.raises(RuntimeError, match="policy"):
            _run_skill_hook(
                skill_name="review",
                hook_name="pre",
                phase="pre",
                actor_name="reviewer",
                payload={},
                response_text=None,
                plan=plan,
                timeout_ms=10_000,
            )


class TestTheRecordNamesTheFolder:
    def test_it_names_it_resolved_as_the_approval_was_checked(self, tmp_path):
        """A firing names the folder it read the skill from; the record and
        the approval must be the same folder, links and all."""
        project = tmp_path / "project"
        folder = hooked_skill(project / ".claude" / "skills")
        linked = tmp_path / "link-to-review"
        try:
            linked.symlink_to(folder)
        except OSError as error:
            pytest.skip(f"cannot create a symlink here: {error}")
        plan = types.SimpleNamespace(
            source_path=str(workflow(project)),
            work_dir=None,
            options={
                "skill_hook_auth": "policy",
                "skill_hook_policy": str(tmp_path / "policy.json"),
            },
        )
        entry = {"skill": "review", "hook": "pre", "dir": str(folder)}
        (tmp_path / "policy.json").write_text(json.dumps({"allow": [entry]}), encoding="utf-8")

        result = _run_skill_hook(
            skill_name="review",
            hook_name="pre",
            phase="pre",
            actor_name="reviewer",
            payload={},
            response_text=None,
            plan=plan,
            timeout_ms=10_000,
            skill_dir=linked,
        )

        assert ran(result), result
        assert result["skillDir"] == str(folder.resolve())

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


class TestTheRefusalSaysWhatThePolicyHolds:
    def test_it_names_the_folders_the_policy_approves_for_the_hook(self, tmp_path):
        """A `dir` misspelt, or no longer the folder the run resolves, is
        seen for what it is only when the refusal names it."""
        project = tmp_path / "project"
        hooked_skill(project / ".claude" / "skills")
        misspelt = str(project / ".claude" / "skils" / "review")
        policy = {"allow": [{"skill": "review", "hook": "pre", "dir": misspelt}]}

        result = run_pre(workflow(project), policy, tmp_path / "policy.json")

        assert result["ran"] is False
        assert f"for other folders only ({misspelt})" in result["reason"]

    def test_by_name_under_require_dir_and_for_another_folder(self, tmp_path, isolated):
        project = tmp_path / "project"
        hooked_skill(project / ".claude" / "skills")
        policy = {
            "requireDir": True,
            "allow": [
                "review:pre",
                {"skill": "review", "hook": "pre", "dir": "~/.claude/skills/review"},
            ],
        }

        result = run_pre(workflow(project), policy, tmp_path / "policy.json")

        assert result["ran"] is False
        assert "by name only, which requireDir makes count for nothing" in result["reason"]
        assert "for other folders only" in result["reason"]


class TestAFiringRunsItsHooksFromOneFolder:
    def test_the_post_hook_runs_from_the_folder_the_skill_was_read_from(
        self, tmp_path, isolated, make_agent_spec, make_task_meta, make_port, monkeypatch
    ):
        """The agent's turn makes `<project>/.claude/skills/review`, where the
        name now resolves first, with a `post.sh` of its own; the post hook
        the user's `review` declared runs from the user's folder."""
        from wfpy.runner import FifoPlan, Queue, RuntimeActor, _step_agent

        marker = tmp_path / "ran.txt"
        users = isolated / ".claude" / "skills" / "review"
        (users / "scripts").mkdir(parents=True)
        (users / "SKILL.md").write_text(
            "---\nhooks:\n  pre: pre\n  post: post\n---\nbody\n", encoding="utf-8"
        )
        for hook in ("pre", "post"):
            (users / "scripts" / f"{hook}.sh").write_text(
                f"echo users-{hook} >> '{marker}'\n", encoding="utf-8"
            )
        project = tmp_path / "project"
        source = workflow(project)

        def turn(agent_spec, payload_text, verbose, **kwargs):
            planted = project / ".claude" / "skills" / "review"
            (planted / "scripts").mkdir(parents=True)
            (planted / "SKILL.md").write_text("planted\n", encoding="utf-8")
            (planted / "scripts" / "post.sh").write_text(
                f"echo planted-post >> '{marker}'\n", encoding="utf-8"
            )
            return "ok", [{"role": "assistant", "content": "ok"}], None, {}

        monkeypatch.setattr("wfpy.runner._invoke_agent", turn)
        spec = make_agent_spec(prompt="p", model="m", skill="review", use_skill=True)
        meta = make_task_meta(
            name="Reviewer",
            kind="agent",
            output_ports={"out": make_port("out", str, "out")},
            agent_spec=spec,
        )
        actor = RuntimeActor(name="reviewer", kind="agent", instance=object(), meta=meta)
        actor.out_queues = {"out": [Queue(queue_id="q")]}
        plan = FifoPlan(name="wf")
        plan.source_path = str(source)
        plan.options = {"skill_hook_auth": "policy", "skill_hook_policy": str(tmp_path / "p.json")}
        (tmp_path / "p.json").write_text(
            json.dumps({"allow": ["review:pre", "review:post"]}), encoding="utf-8"
        )

        assert _step_agent(actor, tmp_path / "out", plan, verbose=False) is True
        assert marker.read_text(encoding="utf-8").split() == ["users-pre", "users-post"]


class TestTextEntriesAreReadAsBefore:
    @pytest.mark.parametrize("entry", ["review:pre:", "review:pre:sh:x", "review", "review:"])
    def test_an_entry_that_is_not_the_name_approves_nothing(self, entry, tmp_path):
        project = tmp_path / "project"
        hooked_skill(project / ".claude" / "skills")

        result = run_pre(workflow(project), {"allow": [entry]}, tmp_path / "policy.json")

        assert result["ran"] is False
