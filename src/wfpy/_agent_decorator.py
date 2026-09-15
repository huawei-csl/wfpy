"""Internal helpers for @agent decorator configuration parsing."""

from __future__ import annotations

from typing import Any

from wfpy.core import (
    AgentOutputValidator,
    LspServerConfig,
    McpServerInlineConfig,
)


def _normalize_agent_kwargs(
    *,
    claude_agent: str | None,
    use_claude_agent: bool,
    use_prompt: bool,
    use_skill: bool,
    use_skill_hooks: bool,
    ask_user: bool,
    ask_permissions: bool,
    timeout_ms: int,
    fireable_without_input: int,
    context_budget: int,
    truncation_strategy: str,
    cli_tools_mode: str,
    connector: str = "",
    mode: str = "",
    # camelCase aliases
    claudeAgent: str | None,
    useClaudeAgent: bool | None,
    usePrompt: bool | None,
    useSkill: bool | None,
    useSkillHooks: bool | None,
    askUser: bool | None,
    timeoutMs: int | None,
    fireableWithoutInput: int | None,
    contextBudget: int | None,
    truncationStrategy: str | None,
    cliToolsMode: str | None,
) -> dict[str, Any]:
    """Normalize camelCase/snake_case agent kwargs. Returns a dict of normalized values."""
    return {
        "claude_agent": claude_agent if claudeAgent is None else claudeAgent,
        "use_claude_agent": use_claude_agent if useClaudeAgent is None else bool(useClaudeAgent),
        "use_prompt": use_prompt if usePrompt is None else bool(usePrompt),
        "use_skill": use_skill if useSkill is None else bool(useSkill),
        "use_skill_hooks": use_skill_hooks if useSkillHooks is None else bool(useSkillHooks),
        "ask_user": ask_user if askUser is None else bool(askUser),
        "ask_permissions": bool(ask_permissions),
        "timeout_ms": timeout_ms if timeoutMs is None else int(timeoutMs),
        "fireable_without_input": (
            fireable_without_input if fireableWithoutInput is None else int(fireableWithoutInput)
        ),
        "context_budget": context_budget if contextBudget is None else int(contextBudget),
        "truncation_strategy": (
            truncation_strategy if truncationStrategy is None else str(truncationStrategy)
        ),
        "cli_tools_mode": cli_tools_mode if cliToolsMode is None else str(cliToolsMode),
        "connector": str(connector or ""),
        "mode": str(mode or ""),
    }


def _parse_mcp_configs(
    mcp_servers: list[str | dict[str, Any]] | None,
    mcp_server_configs: list[dict[str, Any]] | None,
    mcpServers: list[str | dict[str, Any]] | None,
    mcpServerConfigs: list[dict[str, Any]] | None,
) -> tuple[list[str] | None, list[McpServerInlineConfig]]:
    """Parse MCP server configurations from both snake_case and camelCase params.

    Returns:
        (server_name_filters, inline_configs) — server_name_filters is None when
        no MCP servers were specified at all.
    """
    raw_mcp = mcp_servers if mcpServers is None else mcpServers
    server_name_filters: list[str] | None = None
    inline_configs: list[McpServerInlineConfig] = []

    if raw_mcp is not None:
        server_name_filters = []
        for item in raw_mcp:
            if isinstance(item, str):
                server_name_filters.append(item)
            elif isinstance(item, dict):
                cfg = McpServerInlineConfig(
                    name=item["name"],
                    transport=item.get("transport", "streamable-http"),
                    url=item.get("url", ""),
                    command=item.get("command", ""),
                    args=item.get("args", []),
                    env=item.get("env", {}),
                )
                inline_configs.append(cfg)
                server_name_filters.append(cfg.name)

    # Also accept explicit mcpServerConfigs / mcp_server_configs
    raw_configs = mcp_server_configs if mcpServerConfigs is None else mcpServerConfigs
    if raw_configs:
        if server_name_filters is None:
            server_name_filters = []
        for item in raw_configs:
            cfg = McpServerInlineConfig(
                name=item["name"],
                transport=item.get("transport", "streamable-http"),
                url=item.get("url", ""),
                command=item.get("command", ""),
                args=item.get("args", []),
                env=item.get("env", {}),
            )
            inline_configs.append(cfg)
            if cfg.name not in server_name_filters:
                server_name_filters.append(cfg.name)

    return server_name_filters, inline_configs


def _parse_lsp_configs(
    lsp_servers: list[dict[str, Any]] | None,
    lsp_command: str | None,
    lsp_args: list[str] | None,
    lsp_language_id: str,
    lsp_extra_flags: list[str] | None,
    lsp_severity_threshold: str,
    lsp_max_repair_attempts: int,
    lspServers: list[dict[str, Any]] | None,
    lspCommand: str | None,
    lspArgs: list[str] | None,
    lspLanguageId: str | None,
    lspExtraFlags: list[str] | None,
    lspSeverityThreshold: str | None,
    lspMaxRepairAttempts: int | None,
) -> tuple[list[LspServerConfig] | None, dict[str, Any]]:
    """Parse LSP server configurations from both snake_case and camelCase params.

    Returns:
        (parsed_lsp_servers, normalized_shorthand) — normalized_shorthand contains
        the single-command LSP shorthand fields for agents that don't use full
        lsp_servers config.
    """
    raw_lsp = lsp_servers if lspServers is None else lspServers
    normalized_lsp_command = lsp_command if lspCommand is None else lspCommand
    normalized_lsp_args = lsp_args if lspArgs is None else lspArgs
    normalized_lsp_language_id = lsp_language_id if lspLanguageId is None else lspLanguageId
    normalized_lsp_extra_flags = lsp_extra_flags if lspExtraFlags is None else lspExtraFlags
    normalized_lsp_severity_threshold = (
        lsp_severity_threshold if lspSeverityThreshold is None else lspSeverityThreshold
    )
    normalized_lsp_max_repair_attempts = (
        lsp_max_repair_attempts if lspMaxRepairAttempts is None else int(lspMaxRepairAttempts)
    )

    parsed_lsp_servers: list[LspServerConfig] | None = None
    if raw_lsp:
        parsed_lsp_servers = []
        for item in raw_lsp:
            parsed_lsp_servers.append(
                LspServerConfig(
                    command=item["command"],
                    args=item.get("args", []),
                    language_id=item.get("languageId", item.get("language_id", "cpp")),
                    root_uri=item.get("rootUri", item.get("root_uri")),
                    initialization_options=item.get(
                        "initializationOptions", item.get("initialization_options")
                    ),
                    extra_flags=item.get("extraFlags", item.get("extra_flags")),
                    severity_threshold=item.get(
                        "severityThreshold", item.get("severity_threshold", "error")
                    ),
                    ports=item.get("ports"),
                    max_repair_attempts=item.get(
                        "maxRepairAttempts", item.get("max_repair_attempts", 2)
                    ),
                    timeout_ms=item.get("timeoutMs", item.get("timeout_ms", 30_000)),
                )
            )

    normalized_shorthand = {
        "lsp_command": normalized_lsp_command,
        "lsp_args": normalized_lsp_args,
        "lsp_language_id": normalized_lsp_language_id,
        "lsp_extra_flags": normalized_lsp_extra_flags,
        "lsp_severity_threshold": normalized_lsp_severity_threshold,
        "lsp_max_repair_attempts": normalized_lsp_max_repair_attempts,
    }

    return parsed_lsp_servers, normalized_shorthand


def _parse_output_validators(
    output_validators: list[dict[str, Any]] | None,
    outputValidators: list[dict[str, Any]] | None,
) -> list[AgentOutputValidator] | None:
    """Parse legacy output validator configurations.

    Returns:
        Parsed validator list, or None if no validators specified.
    """
    raw_validators = output_validators if outputValidators is None else outputValidators
    parsed_validators: list[AgentOutputValidator] | None = None
    if raw_validators:
        parsed_validators = []
        for v in raw_validators:
            parsed_validators.append(
                AgentOutputValidator(
                    cmd=v["cmd"],
                    args=v.get("args", []),
                    ports=v.get("ports"),
                    max_repair_attempts=v.get(
                        "maxRepairAttempts", v.get("max_repair_attempts", 2)
                    ),
                    timeout_ms=v.get("timeoutMs", v.get("timeout_ms", 30_000)),
                    kind=v.get("kind", "cmd"),
                    env=v.get("env"),
                    language_id=v.get("languageId", v.get("language_id", "cpp")),
                    root_uri=v.get("rootUri", v.get("root_uri")),
                    initialization_options=v.get(
                        "initializationOptions", v.get("initialization_options")
                    ),
                    extra_flags=v.get("extraFlags", v.get("extra_flags")),
                    severity_threshold=v.get(
                        "severityThreshold", v.get("severity_threshold", "error")
                    ),
                )
            )
    return parsed_validators
