# Q1: second user line mid-turn

CLI: Claude Code 2.1.287, model `claude-haiku-4-5-20251001`, recorded 2026-10-02/03 on Linux (WSL2).
Command: `claude -p --input-format stream-json --output-format stream-json --verbose --model haiku --strict-mcp-config --mcp-config <empty-or-stub> --setting-sources "" --permission-mode bypassPermissions --max-budget-usd 0.25 [--replay-user-messages]`
Run in a fresh temp dir. Files: `stdin.jsonl` (what we wrote, `t` = seconds since spawn), `stdout.jsonl` (every stdout event, `t` = seconds since spawn). Ids, paths and the account are scrubbed (`tools/scrub.py`). Sub-directories are variants with the same file names.

Stdin: line 1 at t=0 ("run `sleep 20` with Bash, then say DONE1"); line 2 at t=3 ("also reply SECOND2").

Observed: **queued, then merged into the running turn at the next tool boundary. One `result`.**
- Line 2 is not dropped and does not start a second turn. Nothing appears on stdout for it at t=3.
- At t=22.776 the Bash `tool_result` user event is emitted; at t=22.782 the CLI emits the replayed user event for line 2 (`isReplay: true`, only because `--replay-user-messages` is set). Its `timestamp` field is the receipt time (about 3 s after line 1), so stdout emission time and receipt time differ by the length of the tool call.
- The next assistant message (same turn) saw both: text `DONE1\nSECOND2`. One `result` (`num_turns: 2`, `queued_turn_count: 0`).
- Consequence: steering latency is the remaining duration of the in-flight tool call (here ~20 s), not model latency.
- Side note: a long Bash call also emits `system` events `task_started` (t=5.76) and `task_notification` (t=22.77).
