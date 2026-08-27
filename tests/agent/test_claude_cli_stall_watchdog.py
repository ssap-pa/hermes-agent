"""Deterministic regression tests for Claude CLI false-stall detection."""

import json

import pytest

from agent import claude_cli_client as mod


def _event(subtype, task_id="task-1", **extra):
    return {"type": "system", "subtype": subtype, "task_id": task_id, **extra}


def test_default_args_request_partial_stream_events(monkeypatch):
    """Old stream-json args went silent during extended thinking."""
    monkeypatch.delenv("HERMES_CLAUDE_CLI_ARGS", raising=False)
    args = mod._resolve_args()
    assert "--include-partial-messages" in args
    assert args.index("--include-partial-messages") > args.index("stream-json")


def test_tool_idle_override_has_unbypassable_ceiling():
    assert mod._bounded_tool_idle_timeout("9999") == 600.0
    assert mod._bounded_tool_idle_timeout("600") == 600.0
    assert mod._bounded_tool_idle_timeout("bad-value") == 600.0
    assert mod._bounded_tool_idle_timeout("nan") == 600.0
    assert mod._bounded_tool_idle_timeout("inf") == 600.0
    assert mod._bounded_tool_idle_timeout("1") == mod._IDLE_EVENT_TIMEOUT_SECONDS


def test_active_task_gets_larger_but_bounded_idle_window(monkeypatch):
    monkeypatch.setattr(mod, "_TOOL_IDLE_EVENT_TIMEOUT_SECONDS", 100.0)
    active = {"task-1"}
    assert mod._effective_idle_timeout(active, 60.0, 1800.0) == 100.0
    assert mod._effective_idle_timeout(active, 60.0, 80.0) == 80.0
    assert mod._effective_idle_timeout(set(), 60.0, 1800.0) == 60.0


@pytest.mark.parametrize(
    "terminal_event",
    [
        _event("task_completed"),
        _event("task_failed"),
        _event("task_cancelled"),
        _event("task_notification", status="completed"),
        _event("task_updated", patch={"status": "completed"}),
        _event("task_updated", patch={"status": "failed"}),
        _event("task_updated", patch={"status": "cancelled"}),
        _event("task_updated", patch={"status": "killed"}),
        _event("task_updated", status="killed"),
    ],
)
def test_terminal_task_events_clear_active_id(terminal_event):
    active = {"task-1"}
    mod._update_active_tasks(active, terminal_event)
    assert active == set()


def test_nonterminal_task_update_keeps_active_id():
    active = set()
    mod._update_active_tasks(active, _event("task_started"))
    mod._update_active_tasks(active, _event("task_updated", patch={"status": "running"}))
    assert active == {"task-1"}


def test_partial_event_diagnostics_never_persist_thinking_or_text():
    secret = "PRIVATE-THINKING-AND-TEXT"
    line = json.dumps({
        "type": "stream_event",
        "event": {"type": "content_block_delta", "delta": {"text": secret}},
        "message": {"content": [{"type": "thinking", "thinking": secret}]},
    })
    summary = mod._redact_event(line)
    assert secret not in summary
    assert summary == json.dumps({"type": "stream_event"}, ensure_ascii=False)
