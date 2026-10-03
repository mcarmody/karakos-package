"""Record real claude stream-json I/O. usage: rec.py <outdir> <script.json> [extra claude args...]
script: list of steps: {"at": secs_since_start, "send": <user text>} | {"at":s,"sigint":true} | {"at":s,"raw":{...}}
"""
import json, os, subprocess, sys, tempfile, threading, time, signal
out, script = sys.argv[1], json.load(open(sys.argv[2]))
extra = sys.argv[3:]
cwd = os.environ.get("CWD") or tempfile.mkdtemp(prefix="spike-")
os.makedirs(cwd, exist_ok=True)
mcp = os.path.join(cwd, "mcp.json")
if not any(a == "--mcp-config" for a in extra):
    open(mcp, "w").write('{"mcpServers":{}}'); extra = ["--mcp-config", mcp] + extra
cmd = ["claude","-p","--input-format","stream-json","--output-format","stream-json","--verbose",
       "--model","haiku","--strict-mcp-config","--setting-sources","","--permission-mode","bypassPermissions",
       "--max-budget-usd","0.25"] + extra
os.makedirs(out, exist_ok=True)
t0 = time.time()
p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
sin, sout, serr = [], [], []
def rd(stream, acc):
    for line in stream:
        acc.append({"t": round(time.time()-t0,3), "line": line.rstrip("\n")})
th = [threading.Thread(target=rd, args=(p.stdout,sout)), threading.Thread(target=rd, args=(p.stderr,serr))]
for t in th: t.start()
maxwait = float(os.environ.get("MAXWAIT","90"))
for st in script:
    while time.time()-t0 < st["at"]: time.sleep(0.01)
    if p.poll() is not None: break
    if "sigint" in st:
        sin.append({"t": round(time.time()-t0,3), "line": "<SIGINT>"}); p.send_signal(signal.SIGINT); continue
    msg = st.get("raw") or {"type":"user","message":{"role":"user","content":st["send"]}}
    s = json.dumps(msg); sin.append({"t": round(time.time()-t0,3), "line": s})
    try: p.stdin.write(s+"\n"); p.stdin.flush()
    except BrokenPipeError: break
settle = float(os.environ.get("SETTLE","0"))
if settle: time.sleep(settle)
if os.environ.get("KEEPOPEN")!="1":
    # wait for idle: close stdin after outputs stop or maxwait
    last=len(sout); idle=time.time()
    while time.time()-t0 < maxwait and p.poll() is None:
        time.sleep(0.5)
        if len(sout)!=last: last=len(sout); idle=time.time()
        elif time.time()-idle > float(os.environ.get("IDLE","12")): break
try: p.stdin.close()
except Exception: pass
try: p.wait(timeout=15)
except subprocess.TimeoutExpired: p.kill()
for t in th: t.join(timeout=3)
json.dump({"cmd":cmd,"cwd":cwd,"exit":p.returncode,"elapsed":round(time.time()-t0,2)}, open(os.path.join(out,"meta.json"),"w"))
for name, acc in (("stdin",sin),("stdout",sout),("stderr",serr)):
    with open(os.path.join(out,name+".raw.jsonl"),"w") as f:
        for r in acc: f.write(json.dumps(r)+"\n")
print("exit",p.returncode,"stdout lines",len(sout),"cwd",cwd)
