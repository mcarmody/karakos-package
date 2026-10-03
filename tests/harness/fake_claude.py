#!/usr/bin/env python3
"""Fake `claude` CLI for the integration harness.

Speaks just enough of Claude Code's stream-json protocol for bin/agent-server.py
to drive its real spawn path: reads one JSON user line per turn from stdin and
answers with `system/init` (once), `assistant` events, and a closing `result`.

Environment:
  FAKE_CLAUDE_LOG_DIR  where <session-id>.argv.json / .in.jsonl are written
  FAKE_CLAUDE_SCRIPT   JSON file: {"default": Step, "rules": [{"match": re,
                       "agent": re (optional), "step": Step}]}. Re-read every
                       turn so a test can change the script between messages.

Step keys (all optional): text, tools [{name, input, usage, message_id,
parent_tool_use_id}], usage, cost, delay_ms, is_error, exit, hang,
parent_tool_use_id (for the text event). Templates in `text`: {{text}} echoes
the user's input, {{env:NAME}} reads an environment variable.
"""

import json
import os
import re
import sys
import time

DEFAULT_USAGE = {
    "input_tokens": 10,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 1000,
    "output_tokens": 5,
}
DEFAULT_COST = 0.001


def parse_argv(argv):
    """Return {flag: [values]} for `--flag value` pairs; bare flags map to []."""
    flags = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                flags.setdefault(a, []).append(argv[i + 1])
                i += 2
                continue
            flags.setdefault(a, [])
        i += 1
    return flags


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def render(template, text):
    def sub(m):
        key = m.group(1).strip()
        if key == "text":
            return text
        if key.startswith("env:"):
            return os.environ.get(key[4:], "")
        return m.group(0)
    return re.sub(r"\{\{(.*?)\}\}", sub, template)


def load_script():
    path = os.environ.get("FAKE_CLAUDE_SCRIPT")
    if not path or not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def pick_step(script, text):
    agent = os.environ.get("KARAKOS_AGENT", "")
    for rule in script.get("rules", []):
        if rule.get("agent") and not re.search(rule["agent"], agent):
            continue
        if re.search(rule["match"], text):
            return rule["step"]
    return script.get("default", {})


def user_text(event):
    content = (event.get("message") or {}).get("content", "")
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return content


def main():
    flags = parse_argv(sys.argv[1:])
    sid = (flags.get("--session-id") or ["no-session"])[0]
    log_dir = os.environ.get("FAKE_CLAUDE_LOG_DIR")
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, f"{sid}.argv.json"), "w") as f:
            json.dump(sys.argv[1:], f)

    emit({"type": "system", "subtype": "init", "session_id": sid,
          "model": (flags.get("--model") or [""])[0], "tools": ["Read", "Bash"]})

    n_msg = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "user":
            continue
        text = user_text(event)
        if log_dir:
            with open(os.path.join(log_dir, f"{sid}.in.jsonl"), "a") as f:
                f.write(json.dumps({"ts": time.time(), "text": text}) + "\n")

        step = pick_step(load_script(), text)
        n_msg += 1
        started = time.time()
        if step.get("hang"):
            while True:
                time.sleep(3600)
        if step.get("delay_ms"):
            time.sleep(step["delay_ms"] / 1000.0)

        usage = step.get("usage") or DEFAULT_USAGE
        reply = render(step.get("text", "ok"), text)
        ptu = step.get("parent_tool_use_id")
        emit({"type": "assistant", "session_id": sid, "parent_tool_use_id": ptu,
              "message": {"id": f"msg_{sid[:8]}_{n_msg}_t", "role": "assistant",
                          "content": [{"type": "text", "text": reply}],
                          "usage": usage}})
        for i, tool in enumerate(step.get("tools") or []):
            emit({"type": "assistant", "session_id": sid,
                  "parent_tool_use_id": tool.get("parent_tool_use_id"),
                  "message": {"id": tool.get("message_id") or f"msg_{sid[:8]}_{n_msg}_{i}",
                              "role": "assistant",
                              "content": [{"type": "tool_use",
                                           "id": f"toolu_{n_msg}_{i}",
                                           "name": tool["name"],
                                           "input": tool.get("input", {})}],
                              "usage": tool.get("usage") or usage}})
        if step.get("exit"):
            sys.exit(1)

        is_error = bool(step.get("is_error"))
        emit({"type": "result", "subtype": "error" if is_error else "success",
              "session_id": sid, "is_error": is_error, "result": reply,
              "usage": usage,
              "total_cost_usd": (step["cost"] if "cost" in step
                                 else DEFAULT_COST * n_msg),
              "duration_ms": int((time.time() - started) * 1000)})


if __name__ == "__main__":
    main()
