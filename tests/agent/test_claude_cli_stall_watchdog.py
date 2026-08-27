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


def _snapshot(*, pids=(11,), cpu=5, io=10, states=("S",)):
    return {"pids": pids, "cpu_ticks": cpu, "io_bytes": io, "states": states}


def test_first_live_process_record_is_observed():
    assert mod._classify_child_progress(None, _snapshot()) == "observed"


@pytest.mark.parametrize(
    "current",
    [
        _snapshot(pids=(11, 12)),
        _snapshot(cpu=6),
        _snapshot(io=11),
        _snapshot(states=("R",)),
    ],
)
def test_proc_record_change_is_progress(current):
    assert mod._classify_child_progress(_snapshot(), current) == "progressing"


def test_flat_live_process_record_is_waiting_not_timed_out():
    # 네트워크/자식 wait는 CPU·I/O가 잠시 평평할 수 있다. 시간값 없이 alive_waiting으로 둔다.
    record = _snapshot()
    assert mod._classify_child_progress(record, record) == "alive_waiting"


def test_no_descendant_record_is_not_observed_not_timeout():
    assert mod._classify_child_progress(_snapshot(), _snapshot(pids=(), cpu=0, io=0, states=())) == "not_observed"


def test_active_task_bypasses_even_huge_wall_clock_limits():
    assert mod._time_limit_expired({"task-1"}, elapsed=999999.0, limit=1.0) is False
    assert mod._time_limit_expired(set(), elapsed=2.0, limit=1.0) is True


def test_active_task_duration_is_removed_when_total_clock_resumes():
    # 0~10초 일반 처리, 10~100초 task: active 중과 terminal 후 모두 유효 경과는 10초다.
    assert mod._effective_non_task_elapsed(0.0, 100.0, 0.0, 10.0) == 10.0
    assert mod._effective_non_task_elapsed(0.0, 100.0, 90.0, None) == 10.0
    assert mod._time_limit_expired(set(), elapsed=10.0, limit=60.0) is False


def test_snapshot_builds_cpu_io_state_records_deterministically(monkeypatch):
    monkeypatch.setattr(mod, "_descendant_pids", lambda root: [root, 20, 10])
    monkeypatch.setattr(mod, "_read_proc_cpu_ticks", lambda pid: {10: 3, 20: 7}[pid])
    monkeypatch.setattr(mod, "_read_proc_io_bytes", lambda pid: {10: 11, 20: 13}[pid])
    monkeypatch.setattr(mod, "_read_proc_state", lambda pid: {10: "S", 20: "R"}[pid])
    assert mod._child_progress_snapshot(1) == {
        "pids": (10, 20), "cpu_ticks": 10, "io_bytes": 24, "states": ("S", "R")
    }


def test_proc_records_reflect_live_child():
    import os
    import subprocess
    import sys

    if not os.path.exists(f"/proc/{os.getpid()}/task/{os.getpid()}/children"):
        pytest.skip("kernel without /proc/<pid>/task/*/children")

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        found = False
        for _ in range(20):
            snapshot = mod._child_progress_snapshot(os.getpid())
            if child.pid in snapshot["pids"]:
                found = True
                assert len(snapshot["states"]) == len(snapshot["pids"])
                break
        assert found, "live child subprocess not observed via /proc records"
    finally:
        child.terminate()
        child.wait(timeout=5)


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
    active = {"task-1", "task-2"}
    mod._update_active_tasks(active, terminal_event)
    assert active == {"task-2"}


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
