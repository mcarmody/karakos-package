# Q2: burst of three user lines within 200 ms at idle

CLI: Claude Code 2.1.287, model `claude-haiku-4-5-20251001`, recorded 2026-10-02/03 on Linux (WSL2).
Command: `claude -p --input-format stream-json --output-format stream-json --verbose --model haiku --strict-mcp-config --mcp-config <empty-or-stub> --setting-sources "" --permission-mode bypassPermissions --max-budget-usd 0.25 [--replay-user-messages]`
Run in a fresh temp dir. Files: `stdin.jsonl` (what we wrote, `t` = seconds since spawn), `stdout.jsonl` (every stdout event, `t` = seconds since spawn). Ids, paths and the account are scrubbed (`tools/scrub.py`). Sub-directories are variants with the same file names.

Stdin: ALPHA at t=0, BRAVO at t=0.1, CHARLIE at t=0.2 (each "reply with only the word X").

Observed: **two turns, two `result`s, not three and not one.** The first line starts a turn immediately; lines 2 and 3 arrive during it, are queued, and are **coalesced into one user message joined with `\n`** (`"Reply ... BRAVO.\nReply ... CHARLIE."`), which runs as the second turn after the first `result`. The replay event for the merged message appears once, with both texts.
- Every turn begins with a fresh `system`/`init` event, including turns started from the queue.
- Variant `after-idle-turn/`: a warm-up turn, then the same three lines 6 s later at idle. Same shape (turn for the first, one coalesced turn for the other two).
- The model answered only CHARLIE in the coalesced turn; that is model behaviour, not CLI behaviour.
