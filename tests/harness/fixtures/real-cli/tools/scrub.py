"""Convert raw recorder output (record.py) into committed, scrubbed fixtures.

usage: scrub.py <raw-dir> <fixture-dir> [mcp-log]
Writes stdin.jsonl / stdout.jsonl as {"t": secs, "dir": "in"|"out", "event"|"signal": ...} lines.
Scrubbing is deterministic per process: ids map to stable placeholders in order of appearance.
"""
import json, os, re, sys

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_maps = {"uuid": {}, "msg": {}, "toolu": {}, "task": {}}


def _stable(kind, val, fmt):
    m = _maps[kind]
    if val not in m:
        m[val] = fmt % (len(m) + 1)
    return m[val]


def scrub(s):
    s = re.sub(r"/tmp/claude-\d+", "/tmp/claude-UID", s)
    s = re.sub(r"-tmp-spike-[A-Za-z0-9_]+", "-tmp-SCRATCH", s)
    s = re.sub(r"/tmp/spike-[A-Za-z0-9_]+", "/tmp/SCRATCH", s)
    s = re.sub(r"/home/[A-Za-z0-9_.-]+", "/home/USER", s)
    s = re.sub(r"/run/user/\d+/cc-socks/\d+\.sock", "/run/user/UID/cc-socks/PID.sock", s)
    s = UUID.sub(lambda m: _stable("uuid", m.group(0), "00000000-0000-4000-8000-%012d"), s)
    s = re.sub(r"msg_[A-Za-z0-9]+", lambda m: _stable("msg", m.group(0), "msg_SCRUBBED%03d"), s)
    s = re.sub(r"toolu_[A-Za-z0-9]+", lambda m: _stable("toolu", m.group(0), "toolu_SCRUBBED%03d"), s)
    s = re.sub(r'"task_id": ?"[a-z0-9]{10,}"', lambda m: '"task_id": "%s"' % _stable("task", m.group(0), "task%03d"), s)
    # float noise in cost fields (0.01971819999999998) trips the 17-digit "discord-snowflake" coupling check
    s = re.sub(r"(?<![\d.])(\d+)\.(\d{6})\d{6,}", lambda m: str(round(float(m.group(0)), 6)), s)
    return s


def convert(raw, out, direction):
    with open(raw) as f, open(out, "w") as g:
        for line in f:
            r = json.loads(line)
            if r["line"] == "<SIGINT>":
                rec = {"t": r["t"], "dir": direction, "signal": "SIGINT"}
            else:
                rec = {"t": r["t"], "dir": direction, "event": json.loads(scrub(r["line"]))}
            g.write(json.dumps(rec, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    rawdir, outdir = sys.argv[1], sys.argv[2]
    os.makedirs(outdir, exist_ok=True)
    convert(os.path.join(rawdir, "stdin.raw.jsonl"), os.path.join(outdir, "stdin.jsonl"), "in")
    convert(os.path.join(rawdir, "stdout.raw.jsonl"), os.path.join(outdir, "stdout.jsonl"), "out")
    if len(sys.argv) > 3:
        rows = [json.loads(l) for l in open(sys.argv[3])]
        t0 = rows[0]["t"]
        with open(os.path.join(outdir, "mcp-server-io.jsonl"), "w") as g:
            for r in rows:  # dir is from the stub's side: "in" = received from the CLI
                g.write(json.dumps({"t": round(r["t"] - t0, 3), "dir": r["dir"], "event": json.loads(scrub(r["line"]))}, separators=(",", ":")) + "\n")
