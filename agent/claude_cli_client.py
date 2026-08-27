"""OpenAI-compatible shim that forwards Hermes requests to `claude -p`.

스트리밍(stream-json) 모드로 호출한다. 근거·설계는 _resolve_args / _run_prompt 주석 참고.
핵심: claude가 내부에서 멈춰도(hang) 스트림 이벤트가 끊기는 걸로 즉시 감지하고,
매 호출의 rate_limit_event로 429/세션한도 상태를 실제 데이터로 기록한다.
"""

from __future__ import annotations

import json
import math
import os
import queue
import shlex
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
# 자식작업(빌드·Codex리뷰 등)이 도는 동안은 부모 스트림이 수 분간 조용한 게 정상이라 유휴감시를
# 이 값까지 완화한다. 단 전체 타임아웃(최대 1800s)까지 무제한 면제하면 task 종료 이벤트를 놓쳐
# id가 새는 경우 작업 후 진짜 hang을 30분간 못 잡으므로, 바운드된 backstop(기본 600s, env조정)으로
# 상한을 둔다. 진짜 hang은 이 상한 안에 잡히고, 정상 자식작업은 이 안에서 종료 이벤트를 낸다.
_MAX_TOOL_IDLE_EVENT_TIMEOUT_SECONDS = 600.0


def _bounded_tool_idle_timeout(raw: str) -> float:
    """Return a positive tool-idle window with an unbypassable 600s ceiling."""
    try:
        requested = float(raw)
    except (TypeError, ValueError):
        requested = _MAX_TOOL_IDLE_EVENT_TIMEOUT_SECONDS
    if not math.isfinite(requested):
        requested = _MAX_TOOL_IDLE_EVENT_TIMEOUT_SECONDS
    return min(max(requested, _IDLE_EVENT_TIMEOUT_SECONDS),
               _MAX_TOOL_IDLE_EVENT_TIMEOUT_SECONDS)


_TOOL_IDLE_EVENT_TIMEOUT_SECONDS = _bounded_tool_idle_timeout(
    os.getenv("HERMES_CLAUDE_CLI_TOOL_IDLE_TIMEOUT", "600")
)
_RATE_LIMIT_STATE_PATH = os.path.expanduser("~/.hermes/state/claude_rate_limit.json")
_TIMEOUT_LOG_PATH = os.path.expanduser("~/.hermes/logs/claude_cli_timeout.log")
_PERF_LOG_PATH = os.path.expanduser("~/.hermes/logs/claude_cli_perf.jsonl")


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


def _effective_idle_timeout(active_tasks: set, normal_idle: float,
                            total_timeout: float) -> float:
    """Use the bounded tool window only while a child task is actually active."""
    if not active_tasks:
        return normal_idle
    return min(_TOOL_IDLE_EVENT_TIMEOUT_SECONDS, total_timeout)


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
        try:
            proc.kill()
        except Exception:
            pass
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

        # stdout 라인을 백그라운드 스레드로 읽어 큐에 넣는다(유휴 타임아웃 감지용).
        # readline()을 쓰는 이유: `for line in proc.stdout`는 파이썬 내부 read-ahead 버퍼링으로
        # 실시간 라인 도착이 지연될 수 있어, 라인 단위 즉시 수신을 보장하려고 readline 루프를 쓴다.
        line_q: "queue.Queue" = queue.Queue()

        def _reader():
            try:
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    line_q.put(line)
            except Exception:
                pass
            finally:
                line_q.put(None)  # EOF 신호

        threading.Thread(target=_reader, daemon=True).start()

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
        idle = min(_IDLE_EVENT_TIMEOUT_SECONDS, timeout_seconds)

        while True:
            if time.time() - start > timeout_seconds:
                self._kill_and_log(proc, model, timeout_seconds, last_event_text, reason="total")
                raise RuntimeError(
                    f"Claude CLI exceeded total timeout {timeout_seconds:.0f}s (killed)"
                )
            # 자식작업(빌드·Codex리뷰 등)이 실행 중이면 부모 스트림이 수 분간 조용한 게 정상.
            # 단, 별도 tool 상한까지만 완화해 task 종료 이벤트 누락/자식 hang도 유한 시간에 잡는다.
            effective_idle = _effective_idle_timeout(
                active_tasks, idle, timeout_seconds
            )
            try:
                line = line_q.get(timeout=min(5.0, idle))
            except queue.Empty:
                if time.time() - last_meaningful_event_at <= effective_idle:
                    continue
                # 유휴 타임아웃: effective_idle초 동안 이벤트가 하나도 안 옴 = 진짜 먹통(hang) → kill.
                _perf_log({"event": "idle_timeout", "call_id": call_id, "elapsed_sec": round(time.time() - t0, 3), "idle_sec": effective_idle, "active_tasks": len(active_tasks), "event_count": event_count, "last_event": last_event_text[:200]})
                self._kill_and_log(proc, model, effective_idle, last_event_text, reason="idle")
                raise RuntimeError(
                    f"Claude CLI stalled: no meaningful stream event for {effective_idle:.0f}s (hang detected, killed early)"
                )
            if line is None:
                break  # EOF
            line = line.strip()
            if not line:
                if time.time() - last_meaningful_event_at > effective_idle:
                    _perf_log({"event": "idle_timeout", "call_id": call_id, "elapsed_sec": round(time.time() - t0, 3), "idle_sec": effective_idle, "active_tasks": len(active_tasks), "event_count": event_count, "last_event": last_event_text[:200], "blank_keepalive": True})
                    self._kill_and_log(proc, model, effective_idle, last_event_text, reason="idle_blank_keepalive")
                    raise RuntimeError(
                        f"Claude CLI stalled: no meaningful stream event for {effective_idle:.0f}s (blank keepalives ignored, killed early)"
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
            _update_active_tasks(active_tasks, evt)
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

        # 프로세스 마무리 + stderr 수거(비정상 종료 진단용).
        try:
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        stderr_text = ""
        try:
            if proc.stderr:
                stderr_text = (proc.stderr.read() or "").strip()
        except Exception:
            pass

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
