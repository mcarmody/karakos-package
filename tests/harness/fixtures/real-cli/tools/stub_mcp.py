import sys, json, time, os
mode, log = sys.argv[1], sys.argv[2]
def L(kind, s):
    with open(log, "a") as f: f.write(json.dumps({"t": round(time.time(),3), "dir": kind, "line": s})+"\n")
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    L("in", line)
    m = json.loads(line)
    if mode == "silent": continue
    if m.get("method") == "initialize":
        r = {"jsonrpc":"2.0","id":m["id"],"result":{"protocolVersion":m["params"]["protocolVersion"],"capabilities":{"tools":{}},"serverInfo":{"name":"stub","version":"0"}}}
    elif m.get("method") == "tools/list":
        r = {"jsonrpc":"2.0","id":m["id"],"result":{"tools":[{"name":"ping","description":"Returns pong","inputSchema":{"type":"object","properties":{}}}]}}
    elif m.get("method") == "tools/call":
        r = {"jsonrpc":"2.0","id":m["id"],"result":{"content":[{"type":"text","text":"pong"}]}}
    elif m.get("method")=="server/discover" and mode=="strict":
        r={"jsonrpc":"2.0","id":m["id"],"error":{"code":-32601,"message":"Method not found"}}
    elif "id" in m:
        r = {"jsonrpc":"2.0","id":m["id"],"result":{}}
    else: continue
    s = json.dumps(r); L("out", s); sys.stdout.write(s+"\n"); sys.stdout.flush()
