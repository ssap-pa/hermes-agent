"""OpenAI-compatible shim that forwards Hermes requests to `claude -p`.

스트리밍(stream-json) 모드로 호출한다. 근거·설계는 _resolve_args / _run_prompt 주석 참고.
핵심: claude가 내부에서 멈춰도(hang) 스트림 이벤트가 끊기는 걸로 즉시 감지하고,
매 호출의 rate_limit_event로 429/세션한도 상태를 실제 데이터로 기록한다.
"""

from __future__ import annotations

import json
import os
import selectors
import shlex
from collections import deque
import signal
import subprocess
import threading
import time
from types import SimpleNamespace
from typing import Any

from agent.copilot_acp_client import (
    _build_subprocess_env,
    _extract_tool_calls_from_text,
    _format_messages_as_prompt,
)

CLAUDE_CLI_MARKER_BASE_URL = "claude-cli://print"
_DEFAULT_TIMEOUT_SECONDS = 900.0
# 스트리밍 이벤트가 이 시간(초) 동안 하나도 안 오면 = 진짜 먹통(hang)으로 간주하고 즉시 kill.
# claude가 내부 툴로 여러 턴을 돌아도 턴마다 이벤트가 나오므로 정상 작업은 절대 안 걸린다.
# 이게 "논스트리밍 블라인드 30분 대기"의 근본 대체물이다.
_IDLE_EVENT_TIMEOUT_SECONDS = float(os.getenv("HERMES_CLAUDE_CLI_IDLE_TIMEOUT", "60"))
# 자식작업(빌드·Codex리뷰 등)은 실행시간이나 무출력 시간으로 종료하지 않는다. Claude가 내보내는
# task lifecycle 기록을 권위 출처로 삼고, /proc의 PID·CPU·I/O·상태 변화는 진행 증거로만 기록한다.
# task terminal event 또는 실제 Claude 프로세스 종료 전에는 시간만으로 kill하지 않는다.
_RATE_LIMIT_STATE_PATH = os.path.expanduser("~/.hermes/state/claude_rate_limit.json")
_TIMEOUT_LOG_PATH = os.path.expanduser("~/.hermes/logs/claude_cli_timeout.log")
_PERF_LOG_PATH = os.path.expanduser("~/.hermes/logs/claude_cli_perf.jsonl")
_MAX_STREAM_LINE_BYTES = 1024 * 1024
_MAX_STDERR_BYTES = 64 * 1024


def _redact_event(line: str) -> str:
    """스트림 라인에서 진단에 필요한 구조(type/subtype/task_id)만 남기고 본문(사고·텍스트·툴
    델타 등 잠재적 민감내용)은 로그에 남기지 않는다. 파싱 실패 시 길이만 남긴다."""
    try:
        e = json.loads(line)
        keys = {k: e.get(k) for k in ("type", "subtype", "task_id") if e.get(k) is not None}
        return json.dumps(keys, ensure_ascii=False) if keys else f"<{len(line)}B>"
    except Exception:
        return f"<{len(line)}B non-json>"


def _update_active_tasks(active_tasks: set, event: dict) -> None:
    """Apply one Claude system task lifecycle event to ``active_tasks``."""
    if event.get("type") != "system":
        return
    subtype = event.get("subtype")
    task_id = event.get("task_id")
    if not task_id:
        return
    if subtype == "task_started":
        active_tasks.add(task_id)
        return
    if subtype in {
        "task_completed", "task_failed", "task_cancelled", "task_notification",
    }:
        active_tasks.discard(task_id)
        return
    if subtype == "task_updated":
        patch = event.get("patch") or {}
        status = patch.get("status") if isinstance(patch, dict) else None
        status = status or event.get("status")
        if status in {"completed", "failed", "cancelled", "killed"}:
            active_tasks.discard(task_id)


def _read_proc_cpu_ticks(pid: int) -> int:
    """Cumulative utime+stime (clock ticks) for one pid; 0 if unreadable/gone."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
        # comm 필드는 괄호 안에 공백·괄호를 포함할 수 있으므로 마지막 ')' 이후만 파싱한다.
        rparen = data.rfind(b")")
        fields = data[rparen + 1:].split()
        # fields[0]=state; 그 뒤로 utime=fields[11], stime=fields[12].
        return int(fields[11]) + int(fields[12])
    except Exception:
        return 0


def _read_proc_io_bytes(pid: int) -> int:
    """Cumulative read_bytes+write_bytes from /proc; 0 if unavailable."""
    try:
        values = {}
        with open(f"/proc/{pid}/io", encoding="utf-8") as f:
            for line in f:
                key, _, value = line.partition(":")
                if key in {"read_bytes", "write_bytes"}:
                    values[key] = int(value.strip())
        return values.get("read_bytes", 0) + values.get("write_bytes", 0)
    except Exception:
        return 0


def _read_proc_state(pid: int) -> str:
    """Linux process state letter, or ``?`` when the record is unavailable."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
        fields = data[data.rfind(b")") + 1:].split()
        return fields[0].decode("ascii", errors="replace")
    except Exception:
        return "?"


def _descendant_pids(root_pid: int) -> list[int]:
    """Best-effort process subtree of ``root_pid`` via /proc/<pid>/task/*/children."""
    seen: list[int] = []
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.append(pid)
        try:
            tids = os.listdir(f"/proc/{pid}/task")
        except Exception:
            continue
        for tid in tids:
            try:
                with open(f"/proc/{pid}/task/{tid}/children") as f:
                    kids = f.read().split()
            except Exception:
                continue
            for k in kids:
                try:
                    stack.append(int(k))
                except ValueError:
                    continue
    return seen


def _child_progress_snapshot(root_pid: int) -> dict:
    """Read child-process progress records without imposing a duration limit."""
    descendants = sorted(p for p in _descendant_pids(root_pid) if p != root_pid)
    return {
        "pids": tuple(descendants),
        "cpu_ticks": sum(_read_proc_cpu_ticks(p) for p in descendants),
        "io_bytes": sum(_read_proc_io_bytes(p) for p in descendants),
        "states": tuple(_read_proc_state(p) for p in descendants),
    }


def _classify_child_progress(previous: dict | None, current: dict) -> str:
    """Classify current records as observed/progressing/alive_waiting/not_observed.

    ``alive_waiting`` is deliberately not a failure: a network or subprocess wait
    can keep /proc counters flat. Only lifecycle/exit records terminate a task.
    """
    if not current["pids"]:
        return "not_observed"
    if previous is None:
        return "observed"
    if any(current[key] != previous.get(key) for key in
           ("pids", "cpu_ticks", "io_bytes", "states")):
        return "progressing"
    return "alive_waiting"


def _time_limit_expired(active_tasks: set, elapsed: float, limit: float) -> bool:
    """Time limits never expire while a task lifecycle record remains active."""
    return not active_tasks and elapsed > limit


def _effective_non_task_elapsed(start: float, now: float, paused_seconds: float,
                                pause_started: float | None) -> float:
    """Elapsed wall time excluding all intervals covered by an active task record."""
    elapsed = now - start - paused_seconds
    if pause_started is not None:
        elapsed -= now - pause_started
    return max(0.0, elapsed)


def _kill_process_group(proc) -> None:
    """Kill the isolated Claude process group so inherited pipes cannot linger."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass


def _perf_log(payload: dict) -> None:
    """Append one JSONL timing event for Claude CLI bottleneck diagnosis.

    Keep this deterministic and local-file only: no secrets, prompt text, or env values are logged.
    """
    try:
        item = dict(payload)
        item["ts"] = time.time()
        os.makedirs(os.path.dirname(_PERF_LOG_PATH), exist_ok=True)
        with open(_PERF_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        pass


def _resolve_command() -> str:
    return (
        os.getenv("HERMES_CLAUDE_CLI_COMMAND", "").strip()
        or os.getenv("CLAUDE_CLI_PATH", "").strip()
        or "claude"
    )


def _resolve_args() -> list[str]:
    raw = os.getenv("HERMES_CLAUDE_CLI_ARGS", "").strip()
    if raw:
        return shlex.split(raw)
    # 스트리밍(stream-json) 모드.
    #  - 근본원인 해결: 논스트리밍(json)은 claude가 끝날 때까지 출력이 0이라, 내부에서 멈춰도
    #    밖에선 "먹통인지 작업중인지" 구분 불가 → 1800초 블라인드 대기(hang)의 근본원인이었다.
    #    stream-json은 내부 턴마다 이벤트를 실시간으로 뱉으므로, "N초간 이벤트 0 = 진짜 먹통"
    #    이라는 유휴 타임아웃으로 몇십 초 만에 hang을 잡아 fallback으로 넘긴다.
    #  - 429/세션한도 실측: stream-json은 매 호출 rate_limit_event(status·resetsAt)를 준다.
    #    이걸 파싱해 한도 상태를 실제 데이터로 기록한다(실측 이중확인).
    #  - --tools "" 는 되돌렸다(=claude 내부 툴 원래 기본값대로 활성화). 툴 차단은 hang의
    #    근본치료가 아니었고, 진짜 해법은 위 스트리밍 조기감지다.
    #  - --include-partial-messages: 확장추론(extended thinking) 중 stream-json이 메시지
    #    단위라 60초+ 침묵 → 유휴감시 오발화(건강한 프로세스 kill)의 근본원인이었다. 부분
    #    청크(thinking_delta 등)를 실시간으로 뱉게 해 생각 중에도 이벤트가 흘러 유휴타이머가
    #    리셋된다 → 스톨 오판 제거. (claude --help 로 플래그 실재 확인)
    return ["-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages"]


def _timeout_to_seconds(timeout: Any) -> float:
    if timeout is None:
        return _DEFAULT_TIMEOUT_SECONDS
    if isinstance(timeout, (int, float)):
        return float(timeout)
    candidates = [getattr(timeout, attr, None) for attr in ("read", "write", "connect", "pool", "timeout")]
    numeric = [float(v) for v in candidates if isinstance(v, (int, float))]
    return max(numeric) if numeric else _DEFAULT_TIMEOUT_SECONDS


def _persist_rate_limit(info: dict) -> None:
    """stream-json의 rate_limit_event를 상태파일에 기록 → 실제 데이터 기반 한도 이중확인용.

    ops/claude_limit_check.py 가 이 파일을 읽어 현재 한도상태·리셋시각을 보고한다.
    매 호출마다 최신값으로 덮어쓰므로, 리셋이 지나면 다음 호출에서 status=allowed로 갱신되어
    낡은 '차단' 상태가 남지 않는다(리셋 후 즉시 사용 가능 보장).
    """
    try:
        payload = dict(info)
        payload["observed_at"] = int(time.time())
        os.makedirs(os.path.dirname(_RATE_LIMIT_STATE_PATH), exist_ok=True)
        tmp = _RATE_LIMIT_STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, _RATE_LIMIT_STATE_PATH)
    except Exception:
        pass


def _normalize_usage(u: dict) -> dict:
    """토큰 오보고 방지.

    claude 내부 멀티턴이면 usage.input_tokens/cache_read 가 턴마다 누적 합산되어
    컨텍스트 최대(fable 100만)를 넘는 비정상값(수백만)이 된다. Hermes는 이 값으로 컨텍스트
    잔량을 판단해 불필요한 자동압축을 유발하므로, iterations가 있으면 '마지막 턴의 실제
    컨텍스트 크기'를 쓴다(=현재 컨텍스트 점유량의 정확한 값). output_tokens는 총생성량이라 합산 유지.
    """
    iters = u.get("iterations")
    if isinstance(iters, list) and iters and isinstance(iters[-1], dict):
        last = iters[-1]
        return {
            "input_tokens": last.get("input_tokens", u.get("input_tokens", 0)) or 0,
            "cache_read_input_tokens": last.get("cache_read_input_tokens", u.get("cache_read_input_tokens", 0)) or 0,
            "output_tokens": u.get("output_tokens", 0) or 0,
        }
    return {
        "input_tokens": u.get("input_tokens", 0) or 0,
        "cache_read_input_tokens": u.get("cache_read_input_tokens", 0) or 0,
        "output_tokens": u.get("output_tokens", 0) or 0,
    }


class _ClaudeCLIChatCompletions:
    def __init__(self, client):
        self._client = client

    def create(self, **kwargs):
        return self._client._create_chat_completion(**kwargs)


class _ClaudeCLIChatNamespace:
    def __init__(self, client):
        self.completions = _ClaudeCLIChatCompletions(client)


class ClaudeCLIClient:
    """Minimal OpenAI-client-compatible facade for Claude Code print mode (streaming)."""

    def __init__(self, *, api_key=None, base_url=None, command=None, args=None, timeout=None, **_):
        self.api_key = api_key or "claude-cli"
        self.base_url = base_url or CLAUDE_CLI_MARKER_BASE_URL
        self._command = command or _resolve_command()
        self._args = list(args or _resolve_args())
        self._default_timeout = _timeout_to_seconds(timeout)
        self.chat = _ClaudeCLIChatNamespace(self)
        self.is_closed = False

    def close(self):
        self.is_closed = True

    def _create_chat_completion(self, *, model=None, messages=None, timeout=None, tools=None, tool_choice=None, **_):
        prompt_text = _format_messages_as_prompt(messages or [], model=model, tools=tools, tool_choice=tool_choice)
        effective_timeout = _timeout_to_seconds(timeout) if timeout is not None else self._default_timeout
        response_text, usage = self._run_prompt(prompt_text, model=model, timeout_seconds=effective_timeout)
        tool_calls, cleaned_text = _extract_tool_calls_from_text(response_text)
        usage_obj = SimpleNamespace(
            prompt_tokens=int(usage.get("input_tokens") or 0),
            completion_tokens=int(usage.get("output_tokens") or 0),
            total_tokens=int((usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)),
            prompt_tokens_details=SimpleNamespace(cached_tokens=int(usage.get("cache_read_input_tokens") or 0)),
        )
        assistant_message = SimpleNamespace(
            content=cleaned_text, tool_calls=tool_calls,
            reasoning=None, reasoning_content=None, reasoning_details=None,
        )
        choice = SimpleNamespace(message=assistant_message, finish_reason="tool_calls" if tool_calls else "stop")
        return SimpleNamespace(choices=[choice], usage=usage_obj, model=model or "claude-cli")

    def _kill_and_log(self, proc, model, secs, last_event_text, *, reason):
        _kill_process_group(proc)
        try:
            import datetime
            with open(_TIMEOUT_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(
                    f"\n===== {datetime.datetime.now().isoformat()} model={model} "
                    f"reason={reason} limit={secs:.0f}s =====\n"
                    f"[last stream event]\n{last_event_text or '(none received)'}\n"
                )
        except Exception:
            pass

    def _run_prompt(self, prompt_text, *, model, timeout_seconds):
        cmd = [self._command] + self._args
        if model and "--model" not in cmd and "-m" not in cmd:
            cmd.extend(["--model", model])
        call_id = f"{int(time.time() * 1000)}-{threading.get_ident()}"
        t0 = time.time()
        _perf_log({
            "event": "start",
            "call_id": call_id,
            "model": model,
            "prompt_chars": len(prompt_text or ""),
            "timeout_seconds": timeout_seconds,
            "cmd0": os.path.basename(cmd[0]) if cmd else "",
            "argc": len(cmd),
        })
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, cwd=os.getcwd(), env=_build_subprocess_env(),
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Could not start Claude CLI command '{self._command}'. "
                "Install Claude Code or set HERMES_CLAUDE_CLI_COMMAND/CLAUDE_CLI_PATH."
            ) from exc
        _perf_log({"event": "popen", "call_id": call_id, "elapsed_sec": round(time.time() - t0, 3), "pid": proc.pid})

        # 프롬프트를 stdin으로 넘기고 닫는다.
        try:
            if proc.stdin:
                tw = time.time()
                proc.stdin.write(prompt_text)
                proc.stdin.close()
                _perf_log({"event": "stdin_closed", "call_id": call_id, "elapsed_sec": round(time.time() - t0, 3), "write_sec": round(time.time() - tw, 3)})
        except Exception as exc:
            _perf_log({"event": "stdin_error", "call_id": call_id, "elapsed_sec": round(time.time() - t0, 3), "error": repr(exc)[:300]})

        # stdout는 selector로 비차단 수집한다. 단일 reader가 pipe와 상태를 함께 소유하므로
        # reader-thread/qsize 레이스가 없고, 64KiB 청크 + 1MiB line 상한으로 메모리가 유한하다.
        stdout_fd = proc.stdout.fileno()
        stderr_fd = proc.stderr.fileno()
        os.set_blocking(stdout_fd, False)
        os.set_blocking(stderr_fd, False)
        stdout_selector = selectors.DefaultSelector()
        stdout_selector.register(stdout_fd, selectors.EVENT_READ, "stdout")
        stdout_selector.register(stderr_fd, selectors.EVENT_READ, "stderr")
        stdout_open = True
        stderr_open = True
        stdout_buffer = b""
        stderr_buffer = bytearray()
        pending_lines = deque()

        def _consume_stdout_chunk(chunk: bytes) -> None:
            nonlocal stdout_buffer
            stdout_buffer += chunk
            parts = stdout_buffer.split(b"\n")
            stdout_buffer = parts.pop()
            if len(stdout_buffer) > _MAX_STREAM_LINE_BYTES:
                _kill_process_group(proc)
                stdout_selector.close()
                try:
                    proc.wait(timeout=1)
                except Exception:
                    pass
                raise RuntimeError(
                    f"Claude CLI emitted a stream line larger than {_MAX_STREAM_LINE_BYTES} bytes"
                )
            pending_lines.extend(part.decode("utf-8", errors="replace") for part in parts)

        def _consume_stderr_chunk(chunk: bytes) -> None:
            stderr_buffer.extend(chunk)
            if len(stderr_buffer) > _MAX_STDERR_BYTES:
                del stderr_buffer[:-_MAX_STDERR_BYTES]

        def _read_stream_once(fd: int, kind: str) -> bool | None:
            """Read one nonblocking chunk. True=data, False=EOF, None=not ready."""
            nonlocal stdout_open, stderr_open
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                return None
            except OSError:
                chunk = b""
            if not chunk:
                if kind == "stdout":
                    stdout_open = False
                else:
                    stderr_open = False
                try:
                    stdout_selector.unregister(fd)
                except Exception:
                    pass
                return False
            if kind == "stdout":
                _consume_stdout_chunk(chunk)
            else:
                _consume_stderr_chunk(chunk)
            return True

        result_text = ""
        usage: dict = {}
        error_status = None
        error_result = None
        last_event_text = ""
        first_event_seen = False
        event_count = 0
        active_tasks: set = set()  # 실행 중인 자식작업(빌드·Codex리뷰 등) id — 유휴감시 예외용
        start = time.time()
        last_meaningful_event_at = start
        child_snapshot = None
        child_record_status = None
        last_child_record_log_at = 0.0
        parent_exit_seen = False
        paused_task_seconds = 0.0
        task_pause_started = None
        idle = min(_IDLE_EVENT_TIMEOUT_SECONDS, timeout_seconds)

        def _observe_child_records(observed_at: float) -> None:
            nonlocal child_snapshot, child_record_status, last_child_record_log_at
            current_snapshot = _child_progress_snapshot(proc.pid)
            current_status = _classify_child_progress(child_snapshot, current_snapshot)
            should_log = (
                current_status != child_record_status
                or observed_at - last_child_record_log_at >= 60.0
            )
            if should_log:
                _perf_log({
                    "event": "child_task_record", "call_id": call_id,
                    "status": current_status, "active_tasks": len(active_tasks),
                    "descendants": len(current_snapshot["pids"]),
                    "cpu_ticks": current_snapshot["cpu_ticks"],
                    "io_bytes": current_snapshot["io_bytes"],
                })
                last_child_record_log_at = observed_at
            child_snapshot = current_snapshot
            child_record_status = current_status

        while True:
            now = time.time()
            parent_rc = proc.poll()
            if parent_rc is not None and not parent_exit_seen:
                # 실제 부모 종료가 확인되면 같은 프로세스 그룹의 pipe-holder를 정리하고, 이 단일
                # reader가 커널 pipe에 이미 도착한 바이트만 비차단으로 끝까지 수거한다.
                _kill_process_group(proc)
                for fd, kind in ((stdout_fd, "stdout"), (stderr_fd, "stderr")):
                    while stdout_open if kind == "stdout" else stderr_open:
                        if _read_stream_once(fd, kind) is not True:
                            break
                if stdout_buffer:
                    pending_lines.append(stdout_buffer.decode("utf-8", errors="replace"))
                    stdout_buffer = b""
                parent_exit_seen = True
                _perf_log({"event": "parent_exit", "call_id": call_id,
                           "returncode": parent_rc, "active_tasks": len(active_tasks),
                           "queued_lines": len(pending_lines)})
            if parent_exit_seen and not pending_lines:
                break
            if not active_tasks:
                child_snapshot = None
                child_record_status = None

            # 일반 호출에는 전체 상한을 유지하되, task_started 후 terminal 기록 전까지는 면제한다.
            effective_elapsed = _effective_non_task_elapsed(
                start, now, paused_task_seconds, task_pause_started
            )
            if (not pending_lines and
                    _time_limit_expired(active_tasks, effective_elapsed, timeout_seconds)):
                stdout_selector.close()
                self._kill_and_log(proc, model, timeout_seconds, last_event_text, reason="total")
                raise RuntimeError(
                    f"Claude CLI exceeded total timeout {timeout_seconds:.0f}s (killed)"
                )

            line = None
            if pending_lines:
                line = pending_lines.popleft()
            elif (stdout_open or stderr_open) and not parent_exit_seen:
                ready = stdout_selector.select(timeout=min(5.0, idle))
                if ready:
                    for key, _mask in ready:
                        _read_stream_once(key.fd, key.data)
                    if not stdout_open and stdout_buffer:
                        pending_lines.append(stdout_buffer.decode("utf-8", errors="replace"))
                        stdout_buffer = b""
                    if pending_lines:
                        line = pending_lines.popleft()
                    else:
                        if not active_tasks and _time_limit_expired(
                                set(), time.time() - last_meaningful_event_at, idle):
                            stdout_selector.close()
                            _perf_log({"event": "idle_timeout", "call_id": call_id,
                                       "elapsed_sec": round(time.time() - t0, 3),
                                       "idle_sec": idle, "active_tasks": 0,
                                       "event_count": event_count,
                                       "last_event": last_event_text[:200]})
                            self._kill_and_log(proc, model, idle, last_event_text,
                                               reason="idle")
                            raise RuntimeError(
                                f"Claude CLI stalled: no meaningful stream event for {idle:.0f}s (hang detected, killed early)"
                            )
                        continue
                else:
                    if active_tasks:
                        # /proc 관측은 5초 selector poll 때만 수행해 chatty stream의 CPU/로그 폭증을 막는다.
                        _observe_child_records(now)
                        continue
                    if not _time_limit_expired(set(), now - last_meaningful_event_at, idle):
                        continue
                    stdout_selector.close()
                    _perf_log({"event": "idle_timeout", "call_id": call_id, "elapsed_sec": round(time.time() - t0, 3), "idle_sec": idle, "active_tasks": 0, "event_count": event_count, "last_event": last_event_text[:200]})
                    self._kill_and_log(proc, model, idle, last_event_text, reason="idle")
                    raise RuntimeError(
                        f"Claude CLI stalled: no meaningful stream event for {idle:.0f}s (hang detected, killed early)"
                    )
            elif active_tasks and proc.poll() is None:
                # stdout가 닫혀도 task lifecycle 기록과 실제 부모 프로세스 상태가 권위 출처다.
                _observe_child_records(now)
                time.sleep(min(5.0, idle))
                continue
            else:
                break

            line = line.strip()
            if not line:
                # 빈 keepalive: 자식작업 중이면 위 진행판정에 맡기고, 아니면 스트림 유휴만 본다.
                if not active_tasks and time.time() - last_meaningful_event_at > idle:
                    _perf_log({"event": "idle_timeout", "call_id": call_id, "elapsed_sec": round(time.time() - t0, 3), "idle_sec": idle, "active_tasks": 0, "event_count": event_count, "last_event": last_event_text[:200], "blank_keepalive": True})
                    self._kill_and_log(proc, model, idle, last_event_text, reason="idle_blank_keepalive")
                    raise RuntimeError(
                        f"Claude CLI stalled: no meaningful stream event for {idle:.0f}s (blank keepalives ignored, killed early)"
                    )
                continue
            last_meaningful_event_at = time.time()
            last_event_text = _redact_event(line)
            event_count += 1
            if not first_event_seen:
                first_event_seen = True
                _perf_log({"event": "first_stream_event", "call_id": call_id, "elapsed_sec": round(time.time() - t0, 3), "line_prefix": _redact_event(line)})
            try:
                evt = json.loads(line)
            except Exception:
                continue
            etype = evt.get("type")
            was_active = bool(active_tasks)
            _update_active_tasks(active_tasks, evt)
            is_active = bool(active_tasks)
            if not was_active and is_active:
                task_pause_started = time.time()
            elif was_active and not is_active and task_pause_started is not None:
                paused_task_seconds += time.time() - task_pause_started
                task_pause_started = None
            if etype == "system" and str(evt.get("subtype") or "").startswith("task_"):
                _perf_log({
                    "event": "child_task_lifecycle", "call_id": call_id,
                    "subtype": evt.get("subtype"),
                    "status": ((evt.get("patch") or {}).get("status")
                               if isinstance(evt.get("patch"), dict) else evt.get("status")),
                    "active_tasks": len(active_tasks),
                })
            if etype == "rate_limit_event":
                info = evt.get("rate_limit_info")
                if isinstance(info, dict):
                    _persist_rate_limit(info)
            elif etype == "result":
                _perf_log({"event": "result_event", "call_id": call_id, "elapsed_sec": round(time.time() - t0, 3), "is_error": bool(evt.get("is_error")), "api_error_status": evt.get("api_error_status"), "num_turns": evt.get("num_turns"), "duration_ms": evt.get("duration_ms"), "duration_api_ms": evt.get("duration_api_ms")})
                error_status = evt.get("api_error_status")
                res = evt.get("result")
                u = evt.get("usage")
                if isinstance(u, dict):
                    usage = _normalize_usage(u)
                if bool(evt.get("is_error")):
                    error_result = res if isinstance(res, str) else (json.dumps(evt)[:400])
                elif isinstance(res, str):
                    result_text = res

        stdout_selector.close()
        # 프로세스 마무리 + stderr 수거(비정상 종료 진단용).
        try:
            proc.wait(timeout=10)
        except Exception:
            try:
                _kill_process_group(proc)
            except Exception:
                pass
        stderr_text = bytes(stderr_buffer).decode("utf-8", errors="replace").strip()

        # 429/세션한도 등 API 에러는 여기서 명확히 raise → error_classifier가 rate_limit로 분류→fallback.
        if error_result or error_status:
            msg = error_result or f"api_error_status={error_status}"
            raise RuntimeError(f"Claude CLI failed: {msg}")
        # result 이벤트도 없고 stderr만 있고 종료코드 비정상이면 원본 에러 노출.
        if not result_text and proc.returncode not in (0, None):
            detail = stderr_text or f"exit code {proc.returncode}"
            _perf_log({"event": "process_error", "call_id": call_id, "elapsed_sec": round(time.time() - t0, 3), "returncode": proc.returncode, "detail": detail[:300]})
            raise RuntimeError(f"Claude CLI failed: {detail[:4000]}")
        _perf_log({"event": "success", "call_id": call_id, "elapsed_sec": round(time.time() - t0, 3), "event_count": event_count, "returncode": proc.returncode, "usage": usage})
        return result_text, usage
