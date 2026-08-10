"""Regression tests for the local Claude CLI provider wiring."""

from unittest.mock import MagicMock, patch


def test_runtime_provider_resolves_claude_cli(monkeypatch):
    from hermes_cli.runtime_provider import resolve_runtime_provider

    monkeypatch.setattr("hermes_cli.auth.shutil.which", lambda command: f"/usr/local/bin/{command}")

    runtime = resolve_runtime_provider(
        requested="claude-cli",
        target_model="claude-haiku-4-5-20251001",
    )

    assert runtime["provider"] == "claude-cli"
    assert runtime["api_mode"] == "chat_completions"
    assert runtime["base_url"] == "claude-cli://print"
    assert runtime["api_key"] == "claude-cli"
    assert runtime["command"] == "/usr/local/bin/claude"


def test_auxiliary_client_resolves_claude_cli(monkeypatch):
    from agent.auxiliary_client import resolve_provider_client
    from agent.claude_cli_client import ClaudeCLIClient

    monkeypatch.setattr("hermes_cli.auth.shutil.which", lambda command: f"/usr/local/bin/{command}")

    client, model = resolve_provider_client(
        "claude-cli",
        model="claude-haiku-4-5-20251001",
    )

    assert isinstance(client, ClaudeCLIClient)
    assert model == "claude-haiku-4-5-20251001"
    assert client.base_url == "claude-cli://print"


def test_create_openai_client_uses_claude_cli_client(monkeypatch):
    from agent.agent_runtime_helpers import create_openai_client
    from agent.claude_cli_client import ClaudeCLIClient

    agent = MagicMock()
    agent.provider = "claude-cli"
    agent._client_log_context.return_value = "provider=claude-cli"

    client = create_openai_client(
        agent,
        {
            "api_key": "claude-cli",
            "base_url": "claude-cli://print",
        },
        reason="test",
        shared=True,
    )

    assert isinstance(client, ClaudeCLIClient)


def test_aiagent_init_forwards_claude_cli_command_and_args():
    from run_agent import AIAgent

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("agent.claude_cli_client.ClaudeCLIClient") as mock_claude_client,
    ):
        AIAgent(
            model="claude-haiku-4-5-20251001",
            provider="claude-cli",
            api_key="claude-cli",
            base_url="claude-cli://print",
            acp_command="/opt/bin/claude",
            acp_args=["-p", "--output-format", "stream-json"],
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    _, kwargs = mock_claude_client.call_args
    assert kwargs["command"] == "/opt/bin/claude"
    assert kwargs["args"] == ["-p", "--output-format", "stream-json"]
