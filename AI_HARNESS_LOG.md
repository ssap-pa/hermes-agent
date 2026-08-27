# AI Harness Log — Hermes claude-cli provider wiring / Ollama fallback

## Scope

- Diagnose why Hermes fallback failed after smart routing changes.
- Safe fix 1: align Ollama fallback config with the actually installed local model.
- Safe fix 2: prepare a branch that wires `claude-cli` into Hermes runtime/provider paths.

## Routing / execution evidence

| Role | Tool / provider | Evidence |
|---|---|---|
| Implementation | Hermes local file edits | Branch `fix/claude-cli-provider-wiring-20260709`; changed files shown by `git status --short` |
| Review | Codex CLI `gpt-5.5` | `CODEX_REVIEW_claude_cli_provider.md`; Codex CLI exit code 0; `codex login status` -> `Logged in using ChatGPT` |
| Live Claude smoke | `claude-cli`, model `claude-haiku-4-5-20251001` | `hermes chat -Q --provider claude-cli ...` returned `OK`; session `20260709_015641_091b92`; `~/.hermes/logs/agent.log` lines 18608-18612 |
| Live Ollama smoke | `ollama-llama31` -> runtime `custom`, model `llama3.2:3b` | `hermes chat -Q --provider ollama-llama31 -m llama3.2:3b ...` returned `OK`; session `20260709_015650_566f49`; `~/.hermes/logs/agent.log` lines 18645-18653 |

## Root cause evidence

Previous failing fallback log lines from `~/.hermes/logs/agent.log`:

- Lines 17576-17593: fallback tried `provider=ollama-llama31`, `model=qwen2.5:0.5b`.
- Lines 17580, 17585, 17592, 17593: Ollama returned `HTTP 404: model 'qwen2.5:0.5b' not found`.

Verified current config after safe fallback correction:

```json
{
  "fallback_providers": [
    {
      "provider": "ollama-llama31",
      "model": "llama3.2:3b"
    }
  ],
  "ollama-llama31": {
    "base_url": "http://127.0.0.1:11434/v1",
    "model": "llama3.2:3b"
  }
}
```

## Changed files

```text
agent/agent_init.py
agent/agent_runtime_helpers.py
agent/auxiliary_client.py
agent/conversation_loop.py
hermes_cli/auth.py
hermes_cli/providers.py
hermes_cli/runtime_provider.py
tests/hermes_cli/test_api_key_providers.py
tests/hermes_cli/test_claude_cli_provider.py
CODEX_REVIEW_claude_cli_provider.md
```

Important pre-existing/untracked related files are still visible in `git status`, including `agent/claude_cli_client.py`, `agent/smart_routing.py`, and backup files. They were not hidden.

## Verification commands and results

### Syntax check

```bash
python3 -m py_compile hermes_cli/auth.py hermes_cli/providers.py hermes_cli/runtime_provider.py \
  agent/auxiliary_client.py agent/agent_runtime_helpers.py agent/conversation_loop.py \
  agent/agent_init.py tests/hermes_cli/test_claude_cli_provider.py \
  tests/hermes_cli/test_api_key_providers.py
```

Result: exit code 0.

### Focused tests

```bash
python3 -m pytest tests/hermes_cli/test_claude_cli_provider.py \
  tests/hermes_cli/test_api_key_providers.py -q -o 'addopts='
```

Result: `173 passed in 10.50s` on final rerun.

### Live smoke — Claude CLI provider

```bash
hermes chat -Q --provider claude-cli -m claude-haiku-4-5-20251001 -q 'Say OK only.'
```

Result:

```text
session_id: 20260709_015641_091b92
OK
```

Agent log evidence:

- `~/.hermes/logs/agent.log` line 18608: turn uses `provider=claude-cli`.
- line 18609: `Claude CLI client created ... base_url=claude-cli://print`.
- line 18611: API call finished successfully, `latency=4.0s`.
- line 18612: turn ended with `text_response`.

### Live smoke — Ollama fallback model

```bash
hermes chat -Q --provider ollama-llama31 -m llama3.2:3b -q 'Say OK only.'
```

Result:

```text
session_id: 20260709_015650_566f49
OK
```

Agent log evidence:

- `~/.hermes/logs/agent.log` line 18645: turn uses `model=llama3.2:3b`.
- line 18646: OpenAI-compatible Ollama client created at `http://127.0.0.1:11434/v1`.
- line 18652: API call finished successfully, `latency=108.8s`.
- line 18653: turn ended with `text_response`.

## Codex review result

See `CODEX_REVIEW_claude_cli_provider.md`.

Final re-review summary:

```text
High: none
Medium: none
Low: none
Nit: none
```

Codex noted that the prior Medium findings were fixed:

- `claude-cli` now resolves through runtime/auth paths.
- Main client creation now returns `ClaudeCLIClient`.
- `AIAgent` forwards command/args.
- Auxiliary clients resolve `claude-cli`.
- Streaming is disabled for the local subprocess shim.

## Safety notes

- No production/main push was performed.
- Work is isolated on branch `fix/claude-cli-provider-wiring-20260709`.
- Gateway restart/production deploy was not performed in this final step.
- Secrets were not printed; API key fields were redacted in chat/tool summaries.

---

# Session 2026-08-10 — branch `fix/claude-cli-drive-provider-20260810`

## Scope

Complete the `claude-cli` print-mode provider wiring on the current Hermes
source, starting from the installer-applied markers already present in the
working tree. Claude Opus performed the implementation; the outer command reached its timeout after writing the changes, so all claims below were independently re-verified from the filesystem and fresh test output. Fresh Codex review was subsequently run and recorded in `CODEX_REVIEW_claude_cli_provider.md`.

## Starting state

```bash
venv/bin/python -m pytest tests/hermes_cli/test_claude_cli_provider.py -q -o 'addopts='
# 2 failed, 2 passed
```

Failures:

1. `test_auxiliary_client_resolves_claude_cli` — `resolve_provider_client("claude-cli")`
   returned `(None, None)`: `agent/auxiliary_client.py` had a `copilot-acp`-only
   branch inside the `external_process` auth path.
2. `test_aiagent_init_forwards_claude_cli_command_and_args` — `agent/agent_init.py`
   forwarded `command`/`args` into `client_kwargs` only when
   `agent.provider == "copilot-acp"`.

The installer script (`/tmp/claude-cli-provider-src/install_claude_cli_provider.py`)
patches `providers.py`, `auth.py`, `runtime_provider.py`, `agent_runtime_helpers.py`
and `conversation_loop.py`; it never patched `auxiliary_client.py` or
`agent_init.py`, which is why those two paths were missing.

## Changes made this session

```text
agent/auxiliary_client.py   # claude-cli branch in the external_process path -> ClaudeCLIClient
agent/agent_init.py         # forward acp_command/acp_args for claude-cli; exclude claude-cli
                            #   from the chat_completions -> codex_responses upgrade
agent/conversation_loop.py  # also disable streaming on a claude-cli:// base_url
hermes_cli/auth.py          # readiness for claude-cli keys off the resolved binary
                            #   (no remote transport); use DEFAULT_CLAUDE_CLI_BASE_URL
```

`agent/claude_cli_client.py`, `hermes_cli/providers.py` and
`hermes_cli/runtime_provider.py` were left as already present in the tree.
Pre-existing unrelated changes and `*.bak*` files were preserved.

Note on `hermes_cli/auth.py`: the previous edit used a `claude-cli://` "ready"
prefix for both `get_external_process_provider_status` and
`resolve_external_process_provider_credentials`. Since the claude-cli base URL
is *always* `claude-cli://print`, that made the check always true — auth status
reported `logged_in: True` and the `missing_claude_cli` error was unreachable
even with no `claude` binary installed. Readiness now depends on
`shutil.which(command)` for claude-cli; the Copilot ACP `acp+tcp://` remote
transport path is unchanged.

## Permissions

`~/.claude/settings.json` was **not** read, written, or otherwise touched.
`bypassPermissions` was not set, and no Claude Code permission setting was
weakened. The installer's permission step (README step 4) was deliberately
not run.

## Verification

### Syntax

```bash
venv/bin/python -m py_compile agent/agent_init.py agent/auxiliary_client.py \
  agent/agent_runtime_helpers.py agent/conversation_loop.py agent/claude_cli_client.py \
  hermes_cli/auth.py hermes_cli/providers.py hermes_cli/runtime_provider.py
```

Result: exit code 0.

### Targeted tests

```bash
venv/bin/python -m pytest tests/hermes_cli/test_claude_cli_provider.py -q -o 'addopts='
# 4 passed in 1.51s
```

### Regression suites

```bash
env -u HERMES_REAL_HOME venv/bin/python -m pytest \
  tests/run_agent/test_run_agent.py tests/agent/test_credential_pool_routing.py \
  tests/cli/test_fast_command.py tests/tools/test_delegate.py -q
# 640 passed in 41.26s

env -u HERMES_REAL_HOME venv/bin/python -m pytest \
  tests/hermes_cli/test_runtime_provider_resolution.py tests/hermes_cli/test_provider_catalog.py \
  tests/hermes_cli/test_provider_parity.py tests/hermes_cli/test_auth_provider_gate.py \
  tests/hermes_cli/test_claude_cli_provider.py tests/agent/test_copilot_acp_client.py -q
# 193 passed in 5.56s
```

Two pre-existing, unrelated failure sets were observed and confirmed as
baseline (they reproduce with the changes stashed):

- `tests/agent/test_auxiliary_client.py`: `10 failed, 311 passed` both with and
  without this session's `agent/auxiliary_client.py` change (async-marker /
  environment issues, not provider wiring).
- `tests/agent/test_copilot_acp_client.py::test_run_prompt_preserves_real_home_when_profile_home_available`
  fails only when the ambient `HERMES_REAL_HOME` env var is set; passes under
  `env -u HERMES_REAL_HOME`. No file this session touched is involved.

### Non-network coherence check

Resolution chain checked in-process (no model call, no gateway):

```text
get_auth_status('claude-cli')      -> configured/logged_in True,
                                      resolved_command=/home/ubuntu/.local/bin/claude,
                                      args=['-p','--output-format','stream-json','--verbose']
resolve_runtime_provider(...)      -> provider=claude-cli, api_mode=chat_completions,
                                      base_url=claude-cli://print, api_key=claude-cli
AIAgent(provider='claude-cli', ...)-> api_mode=chat_completions,
                                      client=ClaudeCLIClient with the resolved command/args
```

No live inference call, gateway restart, commit, push, or model-configuration
change was performed by the Claude implementation subtask itself.

## Orchestrator verification after implementation

- Fresh focused rerun: `4 passed in 1.73s`.
- Claude CLI primary smoke: session `20260810_092638_b5cfe4` returned `DRIVE_CLAUDE_CLI_OK` using `provider=claude-cli`, `model=claude-opus-4-8`, `base_url=claude-cli://print`; `agent.log` lines 42511-42515.
- Fable control probe: session `20260810_092647_afc195` reached the same `claude-cli` transport but was rejected by Fable 5 safeguards after three attempts; `agent.log` lines 42541-42584.
- Fresh Codex review: four one-file read-only requests; High 0. Medium/Low follow-ups are listed without concealment in `CODEX_REVIEW_claude_cli_provider.md`.

---

# Session 2026-08-27 — Claude CLI false-stall root fix

## Root cause evidence

- `~/.hermes/logs/claude_cli_timeout.log`: 2026-08-27 02:39 UTC had two `reason=idle limit=60s` terminations whose last event was an assistant `thinking` block with empty visible text.
- `~/.hermes/logs/agent.log`: compression first called explicit `openai-codex`, waited 300s for a failed/incomplete stream, then fell back to the main Claude CLI path.
- The fallback Claude process remained alive after fallback and concurrently edited the same file; PID 2573666 was terminated after its read-only Codex child completed and its changes were preserved.

## Changes

- `agent/claude_cli_client.py`
  - default Claude CLI args now include `--include-partial-messages` so thinking deltas reset the idle timer;
  - tracks `task_started` through all observed terminal lifecycle events;
  - active child tasks use a separate idle window hard-clamped to 600 seconds, never the full request timeout;
  - timeout/performance logs retain event metadata only, never partial thinking/text/command arguments;
  - invalid, NaN, infinite, negative, and oversized tool-idle overrides are bounded safely.
- `tests/agent/test_claude_cli_stall_watchdog.py`
  - deterministic, no-sleep/no-network regression coverage for partial streaming, bounded idle, terminal lifecycle, and redaction.
- Live config via official CLI:
  - `auxiliary.compression.provider: openai-codex -> auto`
  - `auxiliary.compression.model: gpt-5.5 -> ''`
  - runtime resolution probe returned `ClaudeCLIClient claude-opus-4-8 claude-cli://print`.
  - backup: `~/.hermes/config.yaml.bak.claude-stall-20260827T031311Z`.

## Verification

- RED before final lifecycle/bound fix: `1 passed, 3 failed`.
- GREEN: `200 passed in 7.75s` across stall watchdog, Claude provider, and context compressor suites.
- `python3 -m py_compile`: exit 0.
- `hermes config check`: config version 33 valid.
- Final strict Codex source review: `CODEX_REVIEW_claude_cli_stall_rootfix_r4.md` — High 0 / Medium 0 / Low 0 / PASS.
- Deterministic test review: `CODEX_REVIEW_claude_cli_stall_tests_r2.md` — High 0 / PASS; one Low NaN case was subsequently fixed and covered with NaN/Inf assertions.

## Safety

- No credential values were printed or committed.
- No payment, auth, database, environment-secret, Cloudflare, or production-data mutation.
- Work isolated on branch `fix/claude-cli-stall-rootfix-20260827`; unrelated pre-existing untracked files remain untouched.

---

# Session 2026-08-27 (follow-up) — compression-path stall: the *real* remaining root cause

## Why the first fix was incomplete (evidence)

- `~/.hermes/logs/agent.log` line 20177: this session hit `Preflight compression: ~138,030 tokens >= 64,000 threshold`.
- line 20183: compression ran on `auxiliary auto (claude-opus-4-8) at claude-cli://print`.
- line 20212 (03:38:03): `Failed to generate context summary: Claude CLI stalled: no meaningful stream event for 60s (hang detected, killed early)` — **after** the gateway had already restarted (03:36:52) onto the first fix's code.
- `~/.hermes/logs/claude_cli_timeout.log` 03:38:03 entry: `[last stream event] {"type":"assistant"}` then 60s silence — a bare assistant-start with no thinking deltas.

## Root cause (source-confirmed)

- `agent/claude_cli_client.py:218` — `self._args = list(args or _resolve_args())`: passed-in `args` win over the default.
- The first fix added `--include-partial-messages` only to `_resolve_args()`.
- `hermes_cli/auth.py:6400` and `:6639` (both inside `if provider_id == "claude-cli":`) hardcoded the OLD list `["-p","--output-format","stream-json","--verbose"]` **without** the flag.
- The auxiliary/compression client (`agent/auxiliary_client.py:5316`) is built with those resolved args → the flag never reached the compression path → extended-thinking deltas didn't stream → the 60s idle watchdog false-killed a legitimately-thinking 138k-token summary.

## Change

- `hermes_cli/auth.py` — add `--include-partial-messages` to both claude-cli default arg lists (copilot-acp path untouched; `HERMES_CLAUDE_CLI_ARGS` override still honored).
- `tests/hermes_cli/test_claude_cli_provider.py` — `test_default_claude_cli_args_include_partial_messages` (hermetic via `monkeypatch` of `shutil.which` + `delenv`).

## Verification

- In-process: `resolve_provider_client('claude-cli', ...)` → `ClaudeCLIClient._args` now contains `--include-partial-messages` (compression path proven).
- Both auth resolution paths assert the flag.
- `python3 -m py_compile`: exit 0.
- Tests: `tests/hermes_cli/test_claude_cli_provider.py` 5 passed; `test_claude_cli_stall_watchdog.py` 18 passed combined.
- Codex: `CODEX_REVIEW_claude_cli_stall_auxargs.md` → Medium (non-hermetic test); fixed → `CODEX_REVIEW_claude_cli_stall_auxargs_r2.md` → High/Medium/Low/Nit 0, PASS.
- Commits on `fix/claude-cli-stall-rootfix-20260827`: `91f35a620`, then hermetic test commit.

## Activation note

- The running gateway (MainPID from 03:36:52 restart) predates this auth.py commit, so it still resolves the OLD compression args. The fix is code-complete but takes effect only on the NEXT gateway restart. A restart briefly drops+auto-restores this chat (as just happened), so it is left for explicit go / next natural restart rather than self-interrupting.

## Safety

- No payment/auth-secret/env/DB/Cloudflare/main mutation. No credential values printed. Branch-isolated.
