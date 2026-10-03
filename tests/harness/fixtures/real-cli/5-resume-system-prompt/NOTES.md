# Q5: --resume with a changed system prompt

CLI: Claude Code 2.1.287, model `claude-haiku-4-5-20251001`, recorded 2026-10-02/03 on Linux (WSL2).
Command: `claude -p --input-format stream-json --output-format stream-json --verbose --model haiku --strict-mcp-config --mcp-config <empty-or-stub> --setting-sources "" --permission-mode bypassPermissions --max-budget-usd 0.25 [--replay-user-messages]`
Run in a fresh temp dir. Files: `stdin.jsonl` (what we wrote, `t` = seconds since spawn), `stdout.jsonl` (every stdout event, `t` = seconds since spawn). Ids, paths and the account are scrubbed (`tools/scrub.py`). Sub-directories are variants with the same file names.

Method: `setup-first-session/` runs `--system-prompt "...codeword is ZEBRA-ONE..."` with the prompt "reply OK" (so the history does not reveal the codeword). Each resume then runs in the same cwd with `--resume <that session>` and asks "what is your secret codeword per your system prompt?". All files in this question share one scrubbed session id.

Observed: **the new text is ignored; the session's original system prompt wins.**
- Primary (`--resume` + `--system-prompt` with KIWI-TWO): answer `ZEBRA-ONE`.
- `resume-no-flags/`: `ZEBRA-ONE` (the original is persisted and reloaded).
- `resume-append/` (`--append-system-prompt` PLUM-THREE): `ZEBRA-ONE`.
- `resume-sys-and-append/` (both): `ZEBRA-ONE`.
- `resume-sys-explicit-override/` ("IGNORE ALL EARLIER INSTRUCTIONS... KIWI-TWO"): `ZEBRA-ONE`.
- `control-fresh-session/` (same KIWI-TWO prompt, no resume): `KIWI-TWO`, so the test method does detect a changed prompt.
- The resumed session keeps the same `session_id` (no fork).
- Implication: to change a shard's system prompt (identity, hooks, rules), start a new session; `--resume` alone will not pick it up.
