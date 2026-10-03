"""buzz and hive_call in mcp/tools-server.py against a stub agent server (step 2.3)."""
import json
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from conftest import PACKAGE_ROOT

SERVER = PACKAGE_ROOT / "mcp" / "tools-server.py"


class Stub:
    """Scripted agent server: `post` maps path -> (status, body); `get` is a list
    of (status, body) answered to successive GET /hive/call/<id> polls."""

    def __init__(self):
        self.post = {}
        self.get = []
        self.requests = []
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                stub.requests.append(("POST", self.path, body, self.headers.get("Authorization")))
                self._send(*stub.post.get(self.path, (404, {})))

            def do_GET(self):
                stub.requests.append(("GET", self.path, None, self.headers.get("Authorization")))
                self._send(*(stub.get.pop(0) if stub.get else (200, {"status": "pending"})))

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.close()


def rpc(tmp_path, calls, url, shard="a", agent="a"):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "WORKSPACE_ROOT": str(tmp_path),
           "AGENT_SERVER_URL": url, "AGENT_SERVER_TOKEN": "tok"}
    if shard:
        env["KARAKOS_SHARD"] = shard
    if agent:
        env["KARAKOS_AGENT"] = agent
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"}]
    for i, (name, args) in enumerate(calls, start=2):
        lines.append({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                      "params": {"name": name, "arguments": args}})
    proc = subprocess.run([sys.executable, str(SERVER)],
                          input="\n".join(json.dumps(l) for l in lines) + "\n",
                          capture_output=True, text=True, timeout=60, env=env,
                          cwd=str(tmp_path))
    return {r["id"]: r for r in map(json.loads, filter(str.strip, proc.stdout.splitlines()))}


def result(resp):
    return json.loads(resp["result"]["content"][0]["text"])


def test_tools_list_has_both_tools_and_the_earlier_ones(tmp_path, stub):
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "WORKSPACE_ROOT": str(tmp_path)}
    out = subprocess.run([sys.executable, str(SERVER)],
                         input="\n".join(json.dumps(l) for l in lines) + "\n",
                         capture_output=True, text=True, timeout=30, env=env).stdout
    names = [t["name"] for t in json.loads(out.splitlines()[1])["result"]["tools"]]
    assert {"buzz", "hive_call", "ask_user", "workspace"} <= set(names)
    assert names.index("ask_user") < names.index("buzz") < names.index("hive_call")


def test_buzz_queued_and_identity_sent(tmp_path, stub):
    stub.post["/hive/buzz"] = (202, {"status": "queued", "to": "b-2", "message_id": "buzz-1"})
    r = rpc(tmp_path, [("buzz", {"to": "b", "message": "hi"})], stub.url, shard="a-2", agent="a")
    assert result(r[2]) == {"status": "queued", "to": "b-2", "message_id": "buzz-1"}
    method, path, body, auth = stub.requests[0]
    assert body == {"from": "a-2", "to": "b", "message": "hi"} and auth == "Bearer tok"


def test_identity_falls_back_to_agent(tmp_path, stub):
    stub.post["/hive/buzz"] = (202, {"to": "b"})
    rpc(tmp_path, [("buzz", {"to": "b", "message": "hi"})], stub.url, shard="", agent="a")
    assert stub.requests[0][2]["from"] == "a"


def test_no_identity(tmp_path, stub):
    r = rpc(tmp_path, [("buzz", {"to": "b", "message": "x"}),
                       ("hive_call", {"to": "b", "question": "x"})], stub.url, shard="", agent="")
    assert result(r[2]) == {"status": "error", "error": "no_identity"}
    assert result(r[3]) == {"status": "error", "error": "no_identity"}
    assert stub.requests == []


def test_refusal_codes_pass_through(tmp_path, stub):
    stub.post["/hive/buzz"] = (429, {"error": "buzz_limit", "detail": "five"})
    stub.post["/hive/call"] = (409, {"error": "deadlock", "detail": "cycle"})
    r = rpc(tmp_path, [("buzz", {"to": "b", "message": "x"}),
                       ("hive_call", {"to": "b", "question": "x"})], stub.url)
    assert result(r[2]) == {"status": "error", "error": "buzz_limit", "detail": "five"}
    assert result(r[3]) == {"status": "error", "error": "deadlock", "detail": "cycle"}


def test_hive_call_loops_until_answered(tmp_path, stub):
    stub.post["/hive/call"] = (202, {"call_id": "c-1", "to": "b", "depth": 1})
    stub.get = [(200, {"status": "pending"}), (200, {"status": "pending"}),
                (200, {"status": "answered", "answer": "42", "from": "b", "call_id": "c-1"})]
    r = rpc(tmp_path, [("hive_call", {"to": "b", "question": "meaning?", "timeout": 30})], stub.url)
    assert result(r[2]) == {"status": "answered", "answer": "42", "from": "b"}
    gets = [q for q in stub.requests if q[0] == "GET"]
    assert len(gets) == 3 and gets[0][1] == "/hive/call/c-1?wait=20"
    assert stub.requests[0][2] == {"from": "a", "to": "b", "question": "meaning?", "timeout": 30}


@pytest.mark.parametrize("body,expect", [
    ({"status": "timeout"}, {"status": "timeout"}),
    ({"status": "expired", "error": "expired"}, {"status": "expired", "error": "expired"}),
    ({"status": "error", "error": "callee_failed", "detail": "d"},
     {"status": "error", "error": "callee_failed", "detail": "d"}),
])
def test_hive_call_terminal_statuses(tmp_path, stub, body, expect):
    stub.post["/hive/call"] = (202, {"call_id": "c-1"})
    stub.get = [(200, body)]
    out = result(rpc(tmp_path, [("hive_call", {"to": "b", "question": "q"})], stub.url)[2])
    assert {k: out[k] for k in expect} == expect
    if expect["status"] == "timeout":
        assert "did not answer in time" in out["error"]


def test_hive_call_unknown_call_after_restart(tmp_path, stub):
    stub.post["/hive/call"] = (202, {"call_id": "c-1"})
    stub.get = [(404, {"error": "unknown_call"})]
    out = result(rpc(tmp_path, [("hive_call", {"to": "b", "question": "q"})], stub.url)[2])
    assert out["status"] == "error" and out["error"] == "unknown_call"


def test_server_unreachable_after_three_failures(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{s.getsockname()[1]}"
    r = rpc(tmp_path, [("buzz", {"to": "b", "message": "x"}),
                       ("hive_call", {"to": "b", "question": "x"})], url)
    assert result(r[2]) == {"status": "error", "error": "server_unreachable"}
    assert result(r[3]) == {"status": "error", "error": "server_unreachable"}


def test_unreachable_mid_loop_stops_after_three(tmp_path, stub):
    stub.post["/hive/call"] = (202, {"call_id": "c-1"})
    stub.get = [(200, {"status": "pending"})]
    # the stub goes away after the POST: simulate with a handler that drops
    stub_get_count = {"n": 0}
    orig = stub.httpd.RequestHandlerClass.do_GET

    def do_GET(self):
        stub_get_count["n"] += 1
        if stub_get_count["n"] > 1:
            self.connection.close()
            return
        orig(self)

    stub.httpd.RequestHandlerClass.do_GET = do_GET
    out = result(rpc(tmp_path, [("hive_call", {"to": "b", "question": "q"})], stub.url)[2])
    assert out == {"status": "error", "error": "server_unreachable"}
    assert stub_get_count["n"] == 4  # one pending, then three failures


def test_validate_args_rejects_bad_calls(tmp_path, stub):
    big = "x" * 70000
    r = rpc(tmp_path, [("hive_call", {"question": "q"}),
                       ("buzz", {"to": "b"}),
                       ("buzz", {"to": "b", "message": big}),
                       ("hive_call", {"to": "b", "question": "q", "timeout": "soon"})], stub.url)
    assert r[2]["error"]["code"] == -32602 and "to" in r[2]["error"]["message"]
    assert r[3]["error"]["code"] == -32602 and "message" in r[3]["error"]["message"]
    assert r[4]["error"]["code"] == -32602 and "too large" in r[4]["error"]["message"]
    assert r[5]["error"]["code"] == -32602
    assert stub.requests == []


def test_free_text_may_contain_ellipsis(tmp_path, stub):
    stub.post["/hive/buzz"] = (202, {"to": "b"})
    r = rpc(tmp_path, [("buzz", {"to": "b", "message": "later..."})], stub.url)
    assert result(r[2])["status"] == "queued"
