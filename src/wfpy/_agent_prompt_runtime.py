"""Internal helpers for agent prompt composition and skill loading."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from wfpy.core import AgentSpec

SKILL_MD_MAX_BYTES = 256_000
"""Maximum SKILL.md size accepted by the runtime loader."""

AGENT_MAX_TOOL_ROUNDS = 6
"""Maximum tool-call continuation rounds per single agent firing."""


def _runtime_root_for_skills(plan: Any) -> Path:
    """Resolve the runtime root used for `.wf/skills` discovery."""

    explicit = os.environ.get("WF_SKILLS_ROOT", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    if plan.source_path:
        try:
            return Path(plan.source_path).resolve().parent
        except Exception:
            pass
    if plan.work_dir:
        try:
            return Path(plan.work_dir).resolve()
        except Exception:
            pass
    return Path.cwd().resolve()


def _runtime_root_for_claude_agents(plan: Any) -> Path:
    """Resolve runtime root used for `.claude/agents` discovery."""

    explicit = os.environ.get("WF_CLAUDE_AGENTS_ROOT", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    return _runtime_root_for_skills(plan)


def _skill_search_roots(plan: Any) -> list[Path]:
    """Return ordered directories that can contain skill folders.

    Supported roots:
    - `<runtime-root>/.wf/skills`
    - `<runtime-root>/.claude/skills`
    - `<cwd>/.wf/skills`
    - `<cwd>/.claude/skills`
    - `~/.claude/skills`

    If `WF_SKILLS_ROOT` is set, it is treated as a highest-priority
    directory that directly contains skill folders.
    """

    roots: list[Path] = []
    seen: set[str] = set()

    def add_root(path: Path) -> None:
        key = str(path.resolve())
        if key in seen:
            return
        seen.add(key)
        roots.append(path.resolve())

    explicit = os.environ.get("WF_SKILLS_ROOT", "").strip()
    if explicit:
        add_root(Path(explicit).expanduser())

    runtime_root = _runtime_root_for_skills(plan)
    current = runtime_root
    while True:
        add_root(current / ".wf" / "skills")
        add_root(current / ".claude" / "skills")
        if current.parent == current:
            break
        current = current.parent

    cwd_root = Path.cwd().resolve()
    add_root(cwd_root / ".wf" / "skills")
    add_root(cwd_root / ".claude" / "skills")

    home_root = Path.home().resolve()
    add_root(home_root / ".claude" / "skills")

    return roots


def _claude_agent_search_roots(plan: Any) -> list[Path]:
    """Return ordered directories that can contain Claude agent markdown files."""

    roots: list[Path] = []
    seen: set[str] = set()

    def add_root(path: Path) -> None:
        key = str(path.resolve())
        if key in seen:
            return
        seen.add(key)
        roots.append(path.resolve())

    explicit = os.environ.get("WF_CLAUDE_AGENTS_ROOT", "").strip()
    if explicit:
        add_root(Path(explicit).expanduser())

    runtime_root = _runtime_root_for_claude_agents(plan)
    current = runtime_root
    while True:
        add_root(current / ".claude" / "agents")
        if current.parent == current:
            break
        current = current.parent

    cwd_root = Path.cwd().resolve()
    add_root(cwd_root / ".claude" / "agents")

    home_root = Path.home().resolve()
    add_root(home_root / ".claude" / "agents")

    return roots


def _validate_skill_name(skill_name: str) -> None:
    """Validate user-provided skill names used for filesystem lookup."""

    if not skill_name or not skill_name.strip():
        raise ValueError("Skill name is empty.")
    if "/" in skill_name or "\\" in skill_name:
        raise ValueError("Skill name must not include path separators.")
    if ".." in skill_name:
        raise ValueError("Skill name must not include '..'.")


def _validate_claude_agent_name(agent_name: str) -> None:
    """Validate Claude agent names used for filesystem lookup."""

    _validate_skill_name(agent_name)


def _resolve_skill_dir(skill_name: str, plan: Any) -> Path | None:
    """Resolve the skill directory for a skill name from supported roots."""

    _validate_skill_name(skill_name)
    for root in _skill_search_roots(plan):
        skill_dir = (root / skill_name).resolve()
        try:
            skill_dir.relative_to(root)
        except ValueError:
            continue
        if (skill_dir / "SKILL.md").exists():
            return skill_dir
    return None


def _resolve_skill_md_path(skill_name: str, plan: Any) -> Path:
    """Resolve `SKILL.md` for a skill name from supported roots."""

    skill_dir = _resolve_skill_dir(skill_name, plan)
    if skill_dir is None:
        raise FileNotFoundError(
            f"Skill '{skill_name}' was not found. Searched roots: "
            + ", ".join(str(root) for root in _skill_search_roots(plan))
        )
    return (skill_dir / "SKILL.md").resolve()


def _read_skill_md(skill_name: str, plan: Any) -> tuple[str, Path]:
    """Read and return SKILL.md body text for a skill name."""

    skill_md = _resolve_skill_md_path(skill_name, plan)
    if not skill_md.exists() or not skill_md.is_file():
        raise FileNotFoundError(
            f"Skill '{skill_name}' not found at {skill_md}. "
            "Expected in one of: <runtime>/.wf/skills, <runtime>/.claude/skills, <cwd>/.wf/skills, <cwd>/.claude/skills, ~/.claude/skills"
        )
    size = skill_md.stat().st_size
    if size > SKILL_MD_MAX_BYTES:
        raise ValueError(
            f"Skill '{skill_name}' is too large ({size} bytes). "
            f"Limit is {SKILL_MD_MAX_BYTES} bytes."
        )
    return skill_md.read_text(encoding="utf-8"), skill_md


def _parse_skill_frontmatter(skill_text: str) -> tuple[dict[str, Any], str]:
    """Parse a minimal YAML-like frontmatter block from SKILL.md."""

    if not skill_text.startswith("---\n"):
        return {}, skill_text
    end = skill_text.find("\n---\n", 4)
    if end < 0:
        return {}, skill_text
    header = skill_text[4:end]
    body = skill_text[end + 5 :]
    meta: dict[str, Any] = {}
    lines = header.splitlines()
    in_hooks = False
    hooks: dict[str, str] = {}
    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "hooks:" or stripped == "hooks":
            in_hooks = True
            continue
        if in_hooks:
            if not line.startswith(" ") and not line.startswith("\t"):
                in_hooks = False
            else:
                if ":" in stripped:
                    k, v = stripped.split(":", 1)
                    hooks[k.strip()] = v.strip().strip('"').strip("'")
                continue
        if ":" in stripped:
            k, v = stripped.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    if hooks:
        meta["hooks"] = hooks
    return meta, body


def _split_reference_suffix(ref: str) -> tuple[str, str]:
    """Split a file reference from any trailing query or fragment."""

    for idx, ch in enumerate(ref):
        if ch in "?#":
            return ref[:idx], ref[idx:]
    return ref, ""


def _resolve_skill_reference(ref: str, skill_dir: Path) -> str | None:
    """Resolve an existing relative file reference against a skill directory."""

    stripped = ref.strip()
    if not stripped or any(ch.isspace() for ch in stripped):
        return None
    if "://" in stripped or stripped.startswith(("/", "~")):
        return None

    path_part, suffix = _split_reference_suffix(stripped)
    if not path_part:
        return None

    path_ref = Path(path_part)
    if path_ref.is_absolute():
        return None
    if (
        "/" not in path_part
        and "\\" not in path_part
        and not path_part.startswith(".")
        and path_ref.suffix == ""
    ):
        return None

    resolved = (skill_dir / path_part).resolve()
    if not resolved.exists():
        return None
    return str(resolved) + suffix


def _rewrite_skill_body_paths(skill_body: str, skill_dir: Path) -> str:
    """Rewrite skill-body file references so agents can read them from any cwd."""

    scripts_dir = (skill_dir / "scripts").resolve()
    if scripts_dir.is_dir():
        skill_body = skill_body.replace("./scripts/", str(scripts_dir) + "/")

    def _replace_code_span(match: re.Match[str]) -> str:
        ref = match.group(1)
        resolved = _resolve_skill_reference(ref, skill_dir)
        if resolved is None:
            return match.group(0)
        return f"`{resolved}`"

    skill_body = re.sub(r"`([^`\n]+)`", _replace_code_span, skill_body)

    def _replace_markdown_link(match: re.Match[str]) -> str:
        label = match.group(1)
        target = match.group(2)
        resolved = _resolve_skill_reference(target, skill_dir)
        if resolved is None:
            return match.group(0)
        return f"[{label}]({resolved})"

    return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", _replace_markdown_link, skill_body)


def _parse_frontmatter_markdown(text: str) -> tuple[dict[str, Any], str]:
    """Parse generic markdown frontmatter and return `(meta, body)`.

    This parser is intentionally minimal and supports flat keys, comma lists,
    and one-level nested maps for compatibility with Claude agent docs.
    """

    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    header = text[4:end]
    body = text[end + 5 :]

    meta: dict[str, Any] = {}
    lines = header.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip("\n")
        stripped = line.strip()
        i += 1
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue

        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()

        if value == "":
            nested: dict[str, Any] = {}
            list_items: list[str] = []
            while i < len(lines):
                child = lines[i]
                child_stripped = child.strip()
                if not child_stripped:
                    i += 1
                    continue
                if not child.startswith(" ") and not child.startswith("\t"):
                    break
                i += 1
                child_clean = child_stripped
                if child_clean.startswith("-"):
                    list_items.append(child_clean[1:].strip().strip('"').strip("'"))
                    continue
                if ":" in child_clean:
                    ck, cv = child_clean.split(":", 1)
                    nested[ck.strip()] = cv.strip().strip('"').strip("'")
            if nested:
                meta[key] = nested
            elif list_items:
                meta[key] = [item for item in list_items if item]
            else:
                meta[key] = ""
            continue

        normalized = value.strip().strip('"').strip("'")
        if "," in normalized:
            items = [part.strip() for part in normalized.split(",") if part.strip()]
            meta[key] = items
        else:
            meta[key] = normalized

    return meta, body


def _claude_agent_frontmatter_warnings(meta: dict[str, Any]) -> list[str]:
    """Return compatibility warnings for unsupported Claude agent fields."""

    warnings: list[str] = []
    ignored_fields = [
        "tools",
        "disallowedTools",
        "permissionMode",
        "maxTurns",
        "mcpServers",
        "hooks",
        "memory",
        "background",
        "effort",
        "isolation",
    ]
    for field in ignored_fields:
        if field in meta:
            warnings.append(
                f"claude-agent field '{field}' is currently ignored in wfpy compatibility mode"
            )
    if "model" in meta:
        warnings.append(
            "claude-agent field 'model' is ignored; wfpy keeps current model/provider selection"
        )
    return warnings


def _resolve_claude_agent_path(agent_name: str, plan: Any) -> Path:
    """Resolve Claude agent markdown by name from supported roots."""

    _validate_claude_agent_name(agent_name)
    for root in _claude_agent_search_roots(plan):
        direct = (root / f"{agent_name}.md").resolve()
        nested = (root / agent_name / "AGENT.md").resolve()
        for candidate in (direct, nested):
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            if candidate.exists() and candidate.is_file():
                return candidate
    raise FileNotFoundError(
        f"Claude agent '{agent_name}' was not found. Searched roots: "
        + ", ".join(str(root) for root in _claude_agent_search_roots(plan))
    )


def _normalize_frontmatter_string_list(value: Any) -> list[str]:
    """Normalize frontmatter list values that may appear as list or CSV."""

    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        if not value.strip():
            return []
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _load_claude_agent_profile(agent_name: str, plan: Any) -> dict[str, Any]:
    """Load Claude agent markdown profile and compatibility metadata."""

    profile_path = _resolve_claude_agent_path(agent_name, plan)
    raw = profile_path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter_markdown(raw)
    skills = _normalize_frontmatter_string_list(meta.get("skills"))
    warnings = _claude_agent_frontmatter_warnings(meta)
    return {
        "name": agent_name,
        "path": str(profile_path),
        "meta": meta,
        "body": body.strip(),
        "skills": skills,
        "warnings": warnings,
        "description": str(meta.get("description", "")).strip(),
    }


def _claude_agent_max_turns(profile: dict[str, Any]) -> int | None:
    """Extract optional maxTurns from Claude profile frontmatter."""

    meta = profile.get("meta")
    if not isinstance(meta, dict):
        return None
    raw = meta.get("maxTurns")
    if raw is None:
        return None
    try:
        val = int(str(raw).strip())
    except Exception:
        return None
    if val <= 0:
        return None
    return val


def _effective_max_tool_rounds(spec: AgentSpec, profile: dict[str, Any] | None) -> int:
    """Resolve max tool rounds, honoring Claude profile maxTurns when present.

    The value is treated as a compatibility hint and clamped for safety.
    """

    del spec
    rounds = AGENT_MAX_TOOL_ROUNDS
    if profile is not None:
        hint = _claude_agent_max_turns(profile)
        if hint is not None:
            rounds = min(max(hint, 1), 64)
    return rounds


def _normalize_skill_hook_auth_mode(options: dict[str, Any]) -> str:
    """Resolve skill-hook authorization mode from options / env."""

    raw = (
        str(options.get("skill_hook_auth") or os.environ.get("WF_SKILL_HOOK_AUTH", "policy"))
        .strip()
        .lower()
    )
    if raw in ("deny-all", "allow-all", "policy"):
        return raw
    raise ValueError(
        f"Invalid skill hook auth mode '{raw}'. Expected: deny-all, allow-all, policy."
    )


def _load_skill_hook_policy(path_str: str) -> dict[str, Any]:
    """Load the JSON skill-hook policy file."""

    try:
        result: dict[str, Any] = json.loads(Path(path_str).read_text())
        return result
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Cannot load skill hook policy '{path_str}': {exc}") from exc


def _authorize_skill_hook(
    options: dict[str, Any],
    skill_name: str,
    hook_name: str,
    interpreter: str,
) -> tuple[bool, str]:
    """Authorize running a skill hook script under configured policy."""

    mode = _normalize_skill_hook_auth_mode(options)
    if mode == "deny-all":
        return False, "mode=deny-all"
    if mode == "allow-all":
        return True, "mode=allow-all"
    policy_path = options.get("skill_hook_policy") or os.environ.get("WF_SKILL_HOOK_POLICY")
    if not policy_path:
        return False, "mode=policy but no policy path provided"
    policy = _load_skill_hook_policy(policy_path)
    allow = set(str(v) for v in policy.get("allow", []))
    keys = {
        f"{skill_name}:{hook_name}",
        f"{skill_name}:{hook_name}:{interpreter}",
    }
    if any(k in allow for k in keys):
        return True, f"allowed by policy '{policy_path}'"
    return False, f"hook '{skill_name}:{hook_name}' not listed in policy '{policy_path}'"


def _resolve_skill_hook_script(
    skill_name: str, hook_name: str, plan: Any
) -> tuple[Path, str] | None:
    """Resolve a skill hook script by convention under `<skill>/scripts`."""

    _validate_skill_name(skill_name)
    _validate_skill_name(hook_name)
    skill_dir = _resolve_skill_dir(skill_name, plan)
    if skill_dir is None:
        return None
    scripts_root = (skill_dir / "scripts").resolve()
    py = (scripts_root / f"{hook_name}.py").resolve()
    sh = (scripts_root / f"{hook_name}.sh").resolve()
    if py.exists() and py.is_file():
        return py, "python"
    if sh.exists() and sh.is_file():
        return sh, "sh"
    return None


def _run_skill_hook(
    *,
    skill_name: str,
    hook_name: str,
    phase: str,
    actor_name: str,
    payload: dict[str, Any],
    response_text: str | None,
    plan: Any,
    timeout_ms: int,
) -> dict[str, Any]:
    """Execute one skill hook script and return debug metadata."""

    resolved = _resolve_skill_hook_script(skill_name, hook_name, plan)
    if resolved is None:
        return {
            "ran": False,
            "reason": "script-not-found",
            "phase": phase,
            "hook": hook_name,
        }
    script_path, interpreter = resolved
    allowed, reason = _authorize_skill_hook(plan.options, skill_name, hook_name, interpreter)
    if not allowed:
        return {
            "ran": False,
            "reason": f"denied: {reason}",
            "phase": phase,
            "hook": hook_name,
            "script": str(script_path),
            "interpreter": interpreter,
        }

    env = os.environ.copy()
    env.update(
        {
            "WF_SKILL_NAME": skill_name,
            "WF_SKILL_HOOK": hook_name,
            "WF_SKILL_HOOK_PHASE": phase,
            "WF_AGENT_INSTANCE": actor_name,
        }
    )
    cwd = str(_runtime_root_for_skills(plan))
    proc_input = json.dumps(
        {
            "phase": phase,
            "skill": skill_name,
            "hook": hook_name,
            "instance": actor_name,
            "payload": payload,
            "response": response_text,
        },
        default=str,
    )

    cmd = ["python3", str(script_path)] if interpreter == "python" else ["bash", str(script_path)]
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            input=proc_input,
            timeout=timeout_ms / 1000,
        )
        return {
            "ran": True,
            "phase": phase,
            "hook": hook_name,
            "script": str(script_path),
            "interpreter": interpreter,
            "exitCode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "authorized": reason,
        }
    except subprocess.TimeoutExpired:
        return {
            "ran": True,
            "phase": phase,
            "hook": hook_name,
            "script": str(script_path),
            "interpreter": interpreter,
            "timeoutMs": timeout_ms,
            "error": "timeout",
            "authorized": reason,
        }


def _skill_hook_name(skill_meta: dict[str, Any], phase: str) -> str | None:
    """Extract hook name (`pre`/`post`) from parsed SKILL.md metadata."""

    hooks = skill_meta.get("hooks")
    if not isinstance(hooks, dict):
        return None
    value = hooks.get(phase)
    if value is None:
        return None
    name = str(value).strip()
    return name or None


def _build_effective_agent_prompt(spec: AgentSpec, plan: Any) -> tuple[str, dict[str, Any]]:
    """Build final user/system instruction for an agent from skill + prompt toggles."""

    parts: list[str] = []
    debug_meta: dict[str, Any] = {
        "claudeAgentEnabled": bool(spec.use_claude_agent),
        "claudeAgent": spec.claude_agent,
        "skillEnabled": bool(spec.use_skill),
        "promptEnabled": bool(spec.use_prompt),
        "hooksEnabled": bool(spec.use_skill_hooks),
        "skill": spec.skill,
    }

    if spec.use_claude_agent:
        claude_agent_name = (spec.claude_agent or "").strip()
        if not claude_agent_name:
            raise ValueError("Agent has use_claude_agent=true but no claude_agent name configured.")
        profile = _load_claude_agent_profile(claude_agent_name, plan)
        body = str(profile.get("body", "")).strip()
        if body:
            parts.append(body)
        for preloaded_skill in profile.get("skills", []):
            try:
                skill_text, skill_path = _read_skill_md(str(preloaded_skill), plan)
                _skill_meta, skill_body = _parse_skill_frontmatter(skill_text)
                skill_body = _rewrite_skill_body_paths(skill_body, skill_path.parent)
                if skill_body.strip():
                    parts.append(skill_body.strip())
            except Exception as exc:  # noqa: BLE001
                warnings_val = profile.get("warnings")
                warnings: list[Any] = warnings_val if isinstance(warnings_val, list) else []
                warnings.append(f"failed to preload claude-agent skill '{preloaded_skill}': {exc}")
                profile["warnings"] = warnings
        debug_meta["claudeAgentProfile"] = profile

    if spec.use_skill:
        skill_name = (spec.skill or "").strip()
        if not skill_name:
            raise ValueError("Agent has use_skill=true but no skill name configured.")
        skill_text, skill_path = _read_skill_md(skill_name, plan)
        skill_meta, skill_body = _parse_skill_frontmatter(skill_text)
        skill_dir = skill_path.parent
        skill_body = _rewrite_skill_body_paths(skill_body, skill_dir)
        parts.append(skill_body)
        debug_meta["skillPath"] = str(skill_path)
        debug_meta["skillDir"] = str(skill_dir)
        debug_meta["skillMeta"] = skill_meta

    if spec.use_prompt:
        prompt_text = (spec.prompt or "").strip()
        if prompt_text:
            parts.append(prompt_text)

    if not parts:
        raise ValueError(
            "Agent has no enabled instruction sources. "
            "Enable at least one of: use_claude_agent, use_skill, use_prompt."
        )

    return "\n\n".join(parts), debug_meta
