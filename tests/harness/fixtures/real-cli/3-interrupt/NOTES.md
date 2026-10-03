# Q3: interrupt

CLI: Claude Code 2.1.287, model `claude-haiku-4-5-20251001`, recorded 2026-10-02/03 on Linux (WSL2).
Command: `claude -p --input-format stream-json --output-format stream-json --verbose --model haiku --strict-mcp-config --mcp-config <empty-or-stub> --setting-sources "" --permission-mode bypassPermissions --max-budget-usd 0.25 [--replay-user-messages]`
Run in a fresh temp dir. Files: `stdin.jsonl` (what we wrote, `t` = seconds since spawn), `stdout.jsonl` (every stdout event, `t` = seconds since spawn). Ids, paths and the account are scrubbed (`tools/scrub.py`). Sub-directories are variants with the same file names.

Stdin (primary, control request): Bash `sleep 20` prompt at t=0; at t=5 `{"type":"control_request","request_id":"req-int-1","request":{"subtype":"interrupt"}}`; at t=8 a new user line ("reply AFTER").

Observed (control request, supported in 2.1.287):
- t=5.004 stdout `control_response` `{"subtype":"success","request_id":"req-int-1","response":{"still_queued":[]}}`.
- Then user events: the Bash `tool_result` ("The user doesn't want to proceed with this tool use...") and a text block `[Request interrupted by user for tool use]`.
- Then `result` with `subtype: "error_during_execution"`, `is_error: true`, `terminal_reason: "aborted_tools"`, `result` field absent.
- **The process stays alive and is reusable**: the t=8 line produced a new `system`/`init`, a normal assistant message and `result` `success` (`AFTER`) in the same session.

Variant `sigint/`: SIGINT sent to the process at t=5 (marker line `{"signal":"SIGINT"}` in stdin.jsonl). Same tool_result rejection events and the same `error_during_execution` result at t=5.025, **then the process exits with code 0**. The t=8 line was not processed. SIGINT is not reusable; use the control request.
