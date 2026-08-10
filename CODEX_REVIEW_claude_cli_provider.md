# Codex Review — claude-cli provider wiring

## Review command

```bash
codex exec --skip-git-repo-check -s read-only -m gpt-5.5 "Re-review only whether prior Medium findings are fixed..."
```

## Result

High: none

Medium: none. The prior Medium issues appear fixed:
- `claude-cli` now resolves through runtime/auth paths.
- Main client creation now returns `ClaudeCLIClient`.
- `AIAgent` forwards command/args.
- Auxiliary clients resolve `claude-cli`.
- Streaming is disabled for the local subprocess shim.

Low: none

Nit: none

## Evidence

- Codex CLI exit code: 0
- Codex session id from CLI output: `019f4494-383a-7632-9096-3762c8f56104`
- Review mode: read-only sandbox

---

# Fresh review — 2026-08-10 Drive installer adaptation

Four separate Codex CLI `gpt-5.6-sol` read-only requests reviewed one changed file each, per harness policy.

## Results

- `agent/agent_init.py`: High none; Medium 1 (URL-only routing does not forward custom command/args).
- `agent/auxiliary_client.py`: High none; Medium 1 (async conversion path does not preserve `ClaudeCLIClient`); Low 1 (explicit overrides ignored).
- `agent/conversation_loop.py`: High/Medium/Low none; Nit 1 (comment wording).
- `hermes_cli/auth.py`: High none; Medium 2 (binary presence is reported as logged-in; env-based behavioral settings should have config UX).

## Gate decision

This is a non-payment/non-auth-data Hermes provider integration, classified light. No High findings were reported. Medium/Low findings are recorded as follow-up and are not hidden. The verified synchronous gateway path is not blocked: focused tests pass and `claude-opus-4-8` returned the exact smoke token through `provider=claude-cli`.

## Evidence

- Review output directory: `/tmp/codex_drive_provider_reviews/`
- Codex CLI authentication: `Logged in using ChatGPT`
- Review script exit code: 0
- Sandbox: read-only
