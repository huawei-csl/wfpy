"""Internal validator registry and execution helpers for the runtime."""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wfpy.types import PortDescriptor

logger = logging.getLogger("wfpy")

ValidationMode = str  # "off" | "warn" | "enforce"

VALIDATOR_DEFAULT_TIMEOUT_MS = 10_000
"""Default timeout (ms) for module/MCP validators."""


@dataclasses.dataclass
class ValidatorSpec:
    """Specification for a single validator."""

    kind: str  # "builtin" | "module" | "mcp"
    module: str | None = None  # For kind=module
    export: str | None = None  # For kind=module (default: "default")
    server: str | None = None  # For kind=mcp
    tool: str | None = None  # For kind=mcp
    timeout_ms: int = VALIDATOR_DEFAULT_TIMEOUT_MS


@dataclasses.dataclass
class ValidatorDiagnostic:
    """A single diagnostic from a validator execution."""

    code: str
    message: str
    severity: str = "error"  # "error" | "warning" | "info"
    location: dict[str, str] | None = None


@dataclasses.dataclass
class ValidationResult:
    """Result of running one validator."""

    ok: bool
    diagnostics: list[ValidatorDiagnostic] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class LoadedValidatorRegistry:
    """In-memory representation of loaded validator configurations."""

    mode: ValidationMode
    source: str | None = None  # path to wf-validators.json
    specs: dict[str, ValidatorSpec] = dataclasses.field(default_factory=dict)
    module_cache: dict[str, Any] = dataclasses.field(default_factory=dict)
    mcp_bridge_cmd: str | None = None
    mcp_bridge_args: list[str] = dataclasses.field(default_factory=list)
    mcp_allowlist: list[str] = dataclasses.field(default_factory=list)


def normalize_validation_mode(options: dict[str, Any] | None = None) -> ValidationMode:
    """Determine the validation mode from options / env.

    Priority: options['validate'] -> WF_PORT_VALIDATION_MODE env -> 'enforce'.
    """

    raw = ""
    if options:
        raw = str(options.get("validate", "")).strip().lower()
    if not raw:
        raw = os.environ.get("WF_PORT_VALIDATION_MODE", "enforce").strip().lower()
    if raw in ("off", "warn", "enforce"):
        return raw
    raise ValueError(f"Invalid validation mode '{raw}'. Expected: off, warn, enforce.")


def _find_nearest_file(start_dir: str, filename: str) -> str | None:
    """Walk up from *start_dir* looking for *filename*."""

    cur = Path(start_dir).resolve()
    while True:
        candidate = cur / filename
        if candidate.is_file():
            return str(candidate)
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return None


def _parse_validator_registry(raw: dict[str, Any]) -> dict[str, ValidatorSpec]:
    """Parse the ``validators`` section of a wf-validators.json file."""

    specs: dict[str, ValidatorSpec] = {}
    validators = raw.get("validators", {})
    if not isinstance(validators, dict):
        return specs
    for vid, vdef in validators.items():
        if not isinstance(vdef, dict):
            continue
        kind = vdef.get("kind", "")
        if kind not in ("builtin", "module", "mcp"):
            continue
        specs[vid] = ValidatorSpec(
            kind=kind,
            module=vdef.get("module"),
            export=vdef.get("export"),
            server=vdef.get("server"),
            tool=vdef.get("tool"),
            timeout_ms=vdef.get("timeoutMs", VALIDATOR_DEFAULT_TIMEOUT_MS),
        )
    return specs


def load_validator_registry(
    source_path: str,
    options: dict[str, Any] | None = None,
) -> LoadedValidatorRegistry:
    """Load the validator registry.

    Seeds builtin validators (``json.parse``, ``xml.wellFormed``), then
    merges any ``wf-validators.json`` found by walking up from *source_path*.
    """

    mode = normalize_validation_mode(options)
    specs: dict[str, ValidatorSpec] = {
        "json.parse": ValidatorSpec(kind="builtin"),
        "xml.wellFormed": ValidatorSpec(kind="builtin"),
    }

    registry_path = None
    if options:
        registry_path = options.get("validator_registry_path")
    if not registry_path:
        registry_path = os.environ.get("WF_VALIDATOR_REGISTRY_PATH")

    if not registry_path and source_path:
        source_dir = os.path.dirname(os.path.abspath(source_path))
        registry_path = _find_nearest_file(source_dir, "wf-validators.json")

    source_field = None
    if registry_path and os.path.isfile(registry_path):
        try:
            raw = json.loads(Path(registry_path).read_text())
            user_specs = _parse_validator_registry(raw)
            specs.update(user_specs)
            source_field = registry_path
        except (json.JSONDecodeError, OSError):
            pass

    bridge_cmd = None
    if options:
        bridge_cmd = options.get("validator_mcp_bridge_cmd")
    if not bridge_cmd:
        bridge_cmd = os.environ.get("WF_VALIDATOR_MCP_BRIDGE_CMD")

    bridge_args_str = ""
    if options:
        bridge_args_str = str(options.get("validator_mcp_bridge_args", ""))
    if not bridge_args_str:
        bridge_args_str = os.environ.get("WF_VALIDATOR_MCP_BRIDGE_ARGS", "")
    bridge_args = bridge_args_str.split() if bridge_args_str else []

    allowlist_str = ""
    if options:
        allowlist_str = str(options.get("validator_mcp_allowlist", ""))
    if not allowlist_str:
        allowlist_str = os.environ.get("WF_VALIDATOR_MCP_ALLOWLIST", "")
    allowlist = [s.strip() for s in allowlist_str.split(",") if s.strip()] if allowlist_str else []

    return LoadedValidatorRegistry(
        mode=mode,
        source=source_field,
        specs=specs,
        mcp_bridge_cmd=bridge_cmd or None,
        mcp_bridge_args=bridge_args,
        mcp_allowlist=allowlist,
    )


def _check_xml_well_formed(text: str) -> ValidationResult:
    """Check XML well-formedness using a simple tag-stack parser."""

    tag_re = re.compile(r"<(/?)(\w[\w:\-.]*)([^>]*?)(/?)>")
    stack: list[str] = []
    for match in tag_re.finditer(text):
        is_close = bool(match.group(1))
        tag_name = match.group(2)
        is_self_close = bool(match.group(4))
        if is_self_close:
            continue
        if is_close:
            if not stack:
                return ValidationResult(
                    ok=False,
                    diagnostics=[
                        ValidatorDiagnostic(
                            code="XML_WELL_FORMED",
                            message=(
                                f"Unexpected closing tag </{tag_name}> with no matching open tag."
                            ),
                        )
                    ],
                )
            if stack[-1] != tag_name:
                return ValidationResult(
                    ok=False,
                    diagnostics=[
                        ValidatorDiagnostic(
                            code="XML_WELL_FORMED",
                            message=f"Mismatched tags: expected </{stack[-1]}>, found </{tag_name}>.",
                        )
                    ],
                )
            stack.pop()
        else:
            stack.append(tag_name)
    if stack:
        return ValidationResult(
            ok=False,
            diagnostics=[
                ValidatorDiagnostic(
                    code="XML_WELL_FORMED",
                    message=f"Unclosed tags: {', '.join(stack)}.",
                )
            ],
        )
    return ValidationResult(ok=True)


def _execute_builtin_validator(validator_id: str, file_path: str) -> ValidationResult:
    """Execute a builtin validator against a file."""

    try:
        text = Path(file_path).read_text(errors="replace")
    except OSError as exc:
        return ValidationResult(
            ok=False,
            diagnostics=[
                ValidatorDiagnostic(
                    code="VAL_READ_ERROR",
                    message=f"Cannot read file: {exc}",
                    location={"path": file_path},
                )
            ],
        )

    if validator_id == "json.parse":
        try:
            json.loads(text)
            return ValidationResult(ok=True)
        except (json.JSONDecodeError, ValueError) as exc:
            return ValidationResult(
                ok=False,
                diagnostics=[
                    ValidatorDiagnostic(
                        code="JSON_PARSE",
                        message=str(exc),
                        location={"path": file_path},
                    )
                ],
            )
    if validator_id == "xml.wellFormed":
        result = _check_xml_well_formed(text)
        if not result.ok:
            for diagnostic in result.diagnostics:
                if diagnostic.location is None:
                    diagnostic.location = {"path": file_path}
        return result

    return ValidationResult(
        ok=False,
        diagnostics=[
            ValidatorDiagnostic(
                code="VAL_UNKNOWN_BUILTIN",
                message=f"Unknown builtin validator: '{validator_id}'.",
            )
        ],
    )


def _normalize_validation_result(raw: Any) -> ValidationResult:
    """Normalize a raw validator return value into a ValidationResult."""

    if isinstance(raw, ValidationResult):
        return raw
    if isinstance(raw, dict):
        ok = bool(raw.get("ok", False))
        diagnostics: list[ValidatorDiagnostic] = []
        for item in raw.get("diagnostics", []):
            if isinstance(item, dict):
                diagnostics.append(
                    ValidatorDiagnostic(
                        code=item.get("code", "UNKNOWN"),
                        message=item.get("message", ""),
                        severity=item.get("severity", "error"),
                        location=item.get("location"),
                    )
                )
            elif isinstance(item, ValidatorDiagnostic):
                diagnostics.append(item)
        return ValidationResult(ok=ok, diagnostics=diagnostics)
    if isinstance(raw, bool):
        return ValidationResult(ok=raw)
    return ValidationResult(
        ok=False,
        diagnostics=[
            ValidatorDiagnostic(
                code="VAL_NORMALIZE",
                message=f"Cannot normalize validator result of type {type(raw).__name__}.",
            )
        ],
    )


def _execute_module_validator(
    validator_id: str,
    spec: ValidatorSpec,
    file_path: str,
    registry: LoadedValidatorRegistry,
    *,
    port_name: str = "",
    entity_name: str = "",
    workflow_name: str = "",
    parameters: dict[str, Any] | None = None,
) -> ValidationResult:
    """Execute a module validator by dynamically importing a Python module."""

    if not spec.module:
        return ValidationResult(
            ok=False,
            diagnostics=[
                ValidatorDiagnostic(
                    code="VAL_MODULE_MISSING",
                    message=f"Validator '{validator_id}' has kind=module but no 'module' path.",
                )
            ],
        )

    export_name = spec.export or "validate"
    module_obj = registry.module_cache.get(spec.module)

    if module_obj is None:
        try:
            module_spec = importlib.util.spec_from_file_location(
                f"__wfpy_validator_{validator_id}__",
                spec.module,
            )
            if module_spec is None or module_spec.loader is None:
                return ValidationResult(
                    ok=False,
                    diagnostics=[
                        ValidatorDiagnostic(
                            code="VAL_MODULE_LOAD",
                            message=(
                                f"Cannot load module '{spec.module}' for validator '{validator_id}'."
                            ),
                        )
                    ],
                )
            module_obj = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(module_obj)
            registry.module_cache[spec.module] = module_obj
        except Exception as exc:  # noqa: BLE001
            return ValidationResult(
                ok=False,
                diagnostics=[
                    ValidatorDiagnostic(
                        code="VAL_MODULE_LOAD",
                        message=f"Failed to import '{spec.module}': {exc}",
                    )
                ],
            )

    fn = getattr(module_obj, export_name, None)
    if fn is None or not callable(fn):
        return ValidationResult(
            ok=False,
            diagnostics=[
                ValidatorDiagnostic(
                    code="VAL_MODULE_EXPORT",
                    message=f"Module '{spec.module}' has no callable '{export_name}'.",
                )
            ],
        )

    ctx = {
        "filePath": file_path,
        "portName": port_name,
        "entityName": entity_name,
        "workflowName": workflow_name,
        "parameters": parameters or {},
    }

    try:
        raw_result = fn(ctx)
        return _normalize_validation_result(raw_result)
    except Exception as exc:  # noqa: BLE001
        return ValidationResult(
            ok=False,
            diagnostics=[
                ValidatorDiagnostic(
                    code="VAL_MODULE_ERROR",
                    message=f"Validator '{validator_id}' raised: {exc}",
                )
            ],
        )


def _is_mcp_target_allowed(allowlist: list[str], server: str, tool: str) -> bool:
    """Check if an MCP target (server:tool) is allowed by the allowlist."""

    target = f"{server}:{tool}"
    for entry in allowlist:
        if entry in ("*", "*:*"):
            return True
        if entry == target:
            return True
        if entry == f"{server}:*":
            return True
    return False


def _execute_mcp_validator(
    validator_id: str,
    spec: ValidatorSpec,
    file_path: str,
    registry: LoadedValidatorRegistry,
    *,
    port_name: str = "",
    entity_name: str = "",
    workflow_name: str = "",
    parameters: dict[str, Any] | None = None,
) -> ValidationResult:
    """Execute an MCP validator via the bridge subprocess."""

    if not spec.server or not spec.tool:
        return ValidationResult(
            ok=False,
            diagnostics=[
                ValidatorDiagnostic(
                    code="VAL_MCP_SPEC",
                    message=(
                        f"Validator '{validator_id}' is kind=mcp but missing 'server' or 'tool'."
                    ),
                )
            ],
        )

    if not _is_mcp_target_allowed(registry.mcp_allowlist, spec.server, spec.tool):
        return ValidationResult(
            ok=False,
            diagnostics=[
                ValidatorDiagnostic(
                    code="VAL_MCP_DENIED",
                    message=(
                        f"MCP target '{spec.server}:{spec.tool}' is not in the allowlist. "
                        "Set WF_VALIDATOR_MCP_ALLOWLIST or --validator-mcp-allowlist."
                    ),
                )
            ],
        )

    if not registry.mcp_bridge_cmd:
        return ValidationResult(
            ok=False,
            diagnostics=[
                ValidatorDiagnostic(
                    code="VAL_MCP_BRIDGE_MISSING",
                    message=(
                        "MCP validation requires a bridge command. "
                        "Set WF_VALIDATOR_MCP_BRIDGE_CMD or --validator-mcp-bridge-cmd."
                    ),
                )
            ],
        )

    payload = {
        "validatorId": validator_id,
        "server": spec.server,
        "tool": spec.tool,
        "timeoutMs": spec.timeout_ms,
        "context": {
            "filePath": file_path,
            "portName": port_name,
            "entityName": entity_name,
            "workflowName": workflow_name,
            "parameters": parameters or {},
        },
    }

    try:
        proc = subprocess.run(
            [registry.mcp_bridge_cmd] + registry.mcp_bridge_args,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=spec.timeout_ms / 1000,
        )
        if proc.returncode != 0:
            return ValidationResult(
                ok=False,
                diagnostics=[
                    ValidatorDiagnostic(
                        code="VAL_MCP_BRIDGE_ERROR",
                        message=f"MCP bridge exited with code {proc.returncode}: {proc.stderr}",
                    )
                ],
            )
        parsed = json.loads(proc.stdout)
        if "result" in parsed and isinstance(parsed["result"], dict):
            parsed = parsed["result"]
        return _normalize_validation_result(parsed)
    except subprocess.TimeoutExpired:
        return ValidationResult(
            ok=False,
            diagnostics=[
                ValidatorDiagnostic(
                    code="VAL_MCP_TIMEOUT",
                    message=f"MCP bridge timed out after {spec.timeout_ms}ms.",
                )
            ],
        )
    except Exception as exc:  # noqa: BLE001
        return ValidationResult(
            ok=False,
            diagnostics=[
                ValidatorDiagnostic(
                    code="VAL_MCP_ERROR",
                    message=f"MCP bridge error: {exc}",
                )
            ],
        )


def _execute_validator_spec(
    validator_id: str,
    spec: ValidatorSpec,
    file_path: str,
    registry: LoadedValidatorRegistry,
    *,
    port_name: str = "",
    entity_name: str = "",
    workflow_name: str = "",
    parameters: dict[str, Any] | None = None,
) -> ValidationResult:
    """Dispatch to the appropriate validator implementation."""

    if spec.kind == "builtin":
        return _execute_builtin_validator(validator_id, file_path)
    if spec.kind == "module":
        return _execute_module_validator(
            validator_id,
            spec,
            file_path,
            registry,
            port_name=port_name,
            entity_name=entity_name,
            workflow_name=workflow_name,
            parameters=parameters,
        )
    if spec.kind == "mcp":
        return _execute_mcp_validator(
            validator_id,
            spec,
            file_path,
            registry,
            port_name=port_name,
            entity_name=entity_name,
            workflow_name=workflow_name,
            parameters=parameters,
        )
    return ValidationResult(
        ok=False,
        diagnostics=[
            ValidatorDiagnostic(
                code="VAL_UNKNOWN_KIND",
                message=f"Unknown validator kind '{spec.kind}' for '{validator_id}'.",
            )
        ],
    )


def _get_validator_ids_from_port(pd: PortDescriptor) -> list[str]:
    """Extract validator IDs from a port descriptor's validate list."""

    return list(pd.validate) if pd.validate else []


def _append_validation_event(
    out_dir: Path,
    actor_name: str,
    port_name: str,
    phase: str,
    validator_id: str,
    result: ValidationResult,
) -> None:
    """Append a validation event to validation-events.jsonl."""
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "actor": actor_name,
        "port": port_name,
        "phase": phase,
        "validator": validator_id,
        "ok": result.ok,
        "diagnostics": [
            {"code": d.code, "message": d.message, "severity": d.severity}
            for d in result.diagnostics
        ],
    }
    try:
        events_path = out_dir / "validation-events.jsonl"
        with open(events_path, "a") as f:
            f.write(json.dumps(event) + "\n")
    except OSError:
        pass


def run_port_validators(
    registry: LoadedValidatorRegistry,
    actor: Any,
    port_name: str,
    pd: PortDescriptor,
    file_path: str,
    phase: str,  # "input" | "output"
    *,
    out_dir: Path | None = None,
    verbose: bool = False,
) -> None:
    """Run all validators declared on a port, respecting the enforcement mode.

    Args:
        registry: The loaded validator configuration.
        actor: The actor instance being validated.
        port_name: The port name.
        pd: The port descriptor (has validate list).
        file_path: The file to validate.
        phase: "input" or "output".
        out_dir: Output directory (for writing validation-events.jsonl).
        verbose: Enable verbose logging.
    """
    if registry.mode == "off":
        return

    validator_ids = _get_validator_ids_from_port(pd)
    if not validator_ids:
        return

    # Only validate File-typed ports with actual file paths
    if not isinstance(file_path, str) or not os.path.isfile(file_path):
        return

    meta = actor.meta

    for vid in validator_ids:
        spec = registry.specs.get(vid)
        if spec is None:
            msg = (
                f"[validation] Unknown validator '{vid}' on "
                f"{actor.name}.{port_name} ({phase}). "
                f"Check your wf-validators.json or port validate= list."
            )
            if registry.mode == "enforce":
                raise RuntimeError(msg)
            logger.warning(msg)
            continue

        result = _execute_validator_spec(
            vid,
            spec,
            file_path,
            registry,
            port_name=port_name,
            entity_name=actor.name,
            workflow_name="",
            parameters=meta.parameters,
        )

        # Log validation event
        if out_dir:
            _append_validation_event(out_dir, actor.name, port_name, phase, vid, result)

        if not result.ok:
            diag_msgs = "; ".join(d.message for d in result.diagnostics)
            msg = (
                f"[validation] {phase.capitalize()} validation failed on "
                f"{actor.name}.{port_name} (validator={vid}): {diag_msgs}"
            )
            if registry.mode == "enforce":
                raise RuntimeError(msg)
            else:
                logger.warning(msg)

        elif verbose:
            logger.info(
                "[validation] %s ok: %s.%s (validator=%s)",
                phase,
                actor.name,
                port_name,
                vid,
            )
