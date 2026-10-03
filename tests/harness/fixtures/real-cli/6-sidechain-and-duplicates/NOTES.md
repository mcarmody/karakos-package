# Q6: sidechain events and duplicate message ids

CLI: Claude Code 2.1.287, model `claude-haiku-4-5-20251001`, recorded 2026-10-02/03 on Linux (WSL2).
Command: `claude -p --input-format stream-json --output-format stream-json --verbose --model haiku --strict-mcp-config --mcp-config <empty-or-stub> --setting-sources "" --permission-mode bypassPermissions --max-budget-usd 0.25 [--replay-user-messages]`
Run in a fresh temp dir. Files: `stdin.jsonl` (what we wrote, `t` = seconds since spawn), `stdout.jsonl` (every stdout event, `t` = seconds since spawn). Ids, paths and the account are scrubbed (`tools/scrub.py`). Sub-directories are variants with the same file names.

Prompt: "Use the Task tool to launch a subagent whose job is to reply with only PONG, then report what it returned."

Observed:
- **Sidechain assistant events carry `parent_tool_use_id`** equal to the `tool_use.id` of the Task call. Main-thread events have `parent_tool_use_id: null` (assistant and user events alike). In this recording the subagent produced one assistant event.
- **The same `message.id` repeats across events**: one API message is split into one `assistant` event per content block (`thinking`, `text`, `tool_use`), each repeating the id. `usage` is repeated **identically** on each (for example `output_tokens: 3` on all three); **it did not grow** in this capture, and `stop_reason` is `null` on all of them. Variant `duplicate-ids-bash/` (long text, then a Bash call) shows the same: id repeats, `output_tokens` constant. 1.5's rule (dedupe by `message.id`, ignore `parent_tool_use_id != null`) is right; "growing usage" was not reproduced, so take the last event per id but do not rely on growth.
- **Task subagents run in the background by default in this version** (`task_started` with `is_backgrounded: true`, `background_tasks_changed`, `task_updated`, `task_notification`). The main thread ended its first turn with a `result` that only said the agent was launched (the `task_notification` event arrived at t=12.9, just before that `result` at t=14.08, but the main thread had not consumed it), and **the CLI then starts a second turn on its own, with no stdin line** (fresh `system`/`init`, then a second `result`, `result_index: 1`). One user line therefore produced two `result` events. A harness that equates one stdin line with one `result` will mis-count.
