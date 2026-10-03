"""Turn-log redaction (6.4): lib/redact.py and the four surfaces it guards."""
import asyncio
import importlib.util
import json
import random
import sys
import time

import pytest

from conftest import PACKAGE_ROOT

sys.path.insert(0, str(PACKAGE_ROOT / "lib"))
import redact  # noqa: E402

GH = "ghp_" + "A1b2C3d4" * 5                       # 40 chars after the prefix
ANT = "sk-ant-" + "x9Y8z7W6v5U4t3S2r1Q0"
OPENAI = "sk-" + "a1B2c3D4" * 4
SLACK = "xoxb-123456789012-abcdefghij"
DISCORD = "M" + "A" * 23 + "." + "B" * 6 + "." + "C" * 27
AUTH = "Authorization: Bearer abcdefghijklmnop1234"
AWS = "AKIAABCDEFGHIJKLMNOP"
JWT = "eyJhbGciOiJIUzI1NiIs.eyJzdWIiOiIxMjM0NTY3.SflKxwRJSMeKKF2QT4"
PRIVATE = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"


@pytest.fixture(autouse=True)
def _literals():
    redact.clear_literals()
    yield
    redact.clear_literals()


@pytest.mark.parametrize("secret", [GH, ANT, OPENAI, SLACK, DISCORD, AUTH, AWS, JWT])
def test_each_pattern_redacts_a_sample(secret):
    out = redact.redact_text(f"before {secret} after")
    assert secret not in out and "[redacted]" in out
    assert out.startswith("before ") and out.endswith(" after")


def test_private_key_masks_through_the_end_line_or_to_the_end():
    out = redact.redact_text(f"x {PRIVATE} tail")
    assert "MIIEow" not in out and "BEGIN" not in out and out.endswith(" tail")
    cut = redact.redact_text("x -----BEGIN PRIVATE KEY-----\nMIIEow\nnever ends")
    assert cut == "x [redacted]"


def test_keyword_value_keeps_the_keyword():
    assert redact.redact_text("password=hunter2hunter2") == "password=[redacted]"


def test_literal_masks_a_secret_with_no_shape():
    secret = "plain words here 77"
    redact.register_literals({"MY_API_KEY": secret, "SHORT_TOKEN": "abc",
                              "PWD": "/opt/someone-long-enough",
                              "PATH_NOTE": "not-a-secret-name"})
    assert redact.redact_text(f"the value is {secret}!") == "the value is [redacted]!"
    assert redact.redact_text("abc /opt/someone-long-enough not-a-secret-name") == \
        "abc /opt/someone-long-enough not-a-secret-name"


def test_redact_value_recurses_over_leaves_not_keys():
    out = redact.redact_value({GH: [GH, {"k": AWS}], "n": 3, "t": None})
    assert out == {GH: ["[redacted]", {"k": "[redacted]"}], "n": 3, "t": None}


# -- redact_line --------------------------------------------------------------

def test_untouched_line_is_returned_byte_identical():
    line = json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 5}},
                       "text": "héllo ☃"}, ensure_ascii=True).encode() + b"\n"
    assert redact.redact_line(line) is line


def test_unparseable_line_is_masked_and_kept():
    line = f"garbled {GH} {{not json\n".encode()
    out = redact.redact_line(line)
    assert GH.encode() not in out and out.startswith(b"garbled [redacted]") and out.endswith(b"\n")


def test_literal_inside_an_escaped_json_string_is_masked():
    secret = 'quote"inside secret'
    redact.register_literals({"X_SECRET": secret})
    line = json.dumps({"text": f"a {secret} b"}).encode() + b"\n"
    out = redact.redact_line(line)
    assert b"inside secret" not in out
    assert json.loads(out) == {"text": "a [redacted] b"}


def _rand_string(rng):
    parts = []
    for _ in range(rng.randint(1, 6)):
        parts.append(rng.choice([
            GH, ANT, AWS, AUTH, JWT, "plain", 'say "hi"', "back\\slash", "uni é☃\U0001f600",
            "line\nbreak", "token: abcdefgh1234", "a" * rng.randint(1, 40), "\t", "{}[],:"]))
    return " ".join(parts)


def _rand_obj(rng, depth=0):
    kind = rng.randint(0, 3 if depth < 3 else 1)
    if kind == 0:
        return _rand_string(rng)
    if kind == 1:
        return rng.choice([1, 2.5, True, None])
    if kind == 2:
        return [_rand_obj(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    # keys are never redacted (by design), so they carry no secrets here
    return {f"k{i}\"\u00e9": _rand_obj(rng, depth + 1) for i in range(rng.randint(0, 3))}


@pytest.mark.parametrize("ascii_only", [True, False])
def test_every_redacted_line_still_parses(ascii_only):
    rng = random.Random(1234)
    secrets = (GH, ANT, AWS, JWT)
    for _ in range(400):
        obj = {"type": "user", "message": _rand_obj(rng), "k": _rand_obj(rng)}
        line = json.dumps(obj, ensure_ascii=ascii_only).encode() + b"\n"
        out = redact.redact_line(line)
        parsed = json.loads(out)                       # always parses
        assert out.endswith(b"\n") and out.count(b"\n") == 1
        flat = json.dumps(parsed, ensure_ascii=False)
        assert not any(s in flat for s in secrets)


def test_ten_thousand_ordinary_lines_under_a_second():
    lines = [json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": f"working on step {i}"}],
        "usage": {"input_tokens": 100, "output_tokens": i}}}).encode() + b"\n"
        for i in range(10000)]
    t = time.perf_counter()
    for ln in lines:
        redact.redact_line(ln)
    assert time.perf_counter() - t < 1.0


# -- parity with the commit-time scanner -----------------------------------------

def test_every_check_secrets_pattern_is_in_redact():
    spec = importlib.util.spec_from_file_location(
        "check_secrets", PACKAGE_ROOT / "system" / "check-secrets.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_secrets"] = mod
    spec.loader.exec_module(mod)
    ours = {text for _name, text in redact.PATTERNS}
    missing = [n for n, text in mod.PATTERNS if text not in ours]
    assert not missing, f"check-secrets patterns missing from lib/redact.py: {missing}"


# -- the server surfaces ----------------------------------------------------------

@pytest.fixture
def ags(tmp_path, monkeypatch):
    from conftest import import_script
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "logs" / "agent-streams").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    return import_script("agent-server")


def test_redact_for_log_masks_before_truncating(ags):
    text = "x" * 190 + GH
    out = ags.redact_for_log(text, 200)
    assert GH[:12] not in out and len(out) <= 200


def test_token_split_by_the_detail_truncation_is_still_masked(ags):
    # The token starts 85 characters in; the 90-character cut would leave a
    # 5-character stub that no pattern matches if truncation ran first.
    cmd = "y" * 80 + " " + GH
    line = ags.describe_tool_call("Bash", {"command": cmd})
    assert GH[:8] not in line and "ghp_" not in line
    assert ags.summarize_tool_call("Bash", {"command": cmd}).startswith("-# ⚙ Bash — ")


def test_write_turn_event_stores_masked_text(harness):
    h = harness(agents=["a", "b"])

    async def go():
        async with h:
            for kind in ("thinking", "interstitial", "tool"):
                await h.module.write_turn_event(["m1"], 1, kind, f"{kind} {GH}")
            return h._query("SELECT kind, content FROM turn_events")

    rows = asyncio.run(go())
    assert len(rows) == 3
    assert all(GH not in r["content"] and "[redacted]" in r["content"] for r in rows)


def test_stream_log_tee_is_masked_and_valid(ags, tmp_path):
    ags.STREAM_LOG_DIR = tmp_path / "streams"
    ags.STREAM_LOG_DIR.mkdir()
    ev = json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": f"cat .env -> API_TOKEN={GH}"}]}})
    ags.write_stream_log("zed", ev.encode())
    ags.write_stream_log("zed", f"garbled {ANT}".encode())
    (log,) = list(ags.STREAM_LOG_DIR.glob("zed_*.jsonl"))
    text = log.read_text()
    assert GH not in text and ANT not in text
    first, second = text.splitlines()
    json.loads(first)
    assert second == "garbled [redacted]"


def test_harness_turn_surfaces_masked_and_loop_unchanged(harness):
    h = harness(agents=["a", "b"], steering={"enabled": False})
    cmd = f"curl -H 'Authorization: Bearer {'q' * 30}' https://example.invalid"

    async def scenario():
        async with h:
            h.script(default={"text": f"done {GH}", "tools": [
                {"name": "Bash", "input": {"command": cmd}, "output": f"TOKEN={GH}"}]})
            await h.send("a", "go")
            await h.wait_idle("a")
            rows = h._query("SELECT content FROM turn_events")
            return rows, h.queue_rows("a")

    rows, queue = asyncio.run(scenario())
    assert rows and all("q" * 30 not in r["content"] and GH not in r["content"] for r in rows)
    assert any("Bash" in r["content"] for r in rows)
    tool_lines = [d["content"] for d in h.discord if "⚙ Bash" in d["content"]]
    assert tool_lines and all("q" * 30 not in t for t in tool_lines)
    # the loop acted on the unredacted event: the recorded response is untouched
    assert queue[0]["response"] and GH in queue[0]["response"]
    # and the stream log tee is masked but still one valid JSON event per line
    logs = list(h.module.STREAM_LOG_DIR.glob("a_*.jsonl"))
    assert logs
    body = "".join(p.read_text() for p in logs)
    assert "q" * 30 not in body and GH not in body
    for ln in body.splitlines():
        if ln.strip():
            json.loads(ln)


def test_summarize_session_builds_from_a_redacted_log(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "summarize_session_t", PACKAGE_ROOT / "bin" / "summarize-session.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    d = tmp_path / "streams"
    d.mkdir()
    events = [
        {"type": "user", "message": {"content": f"deploy with {GH} please"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "on it"},
            {"type": "tool_use", "name": "Bash", "input": {"command": f"echo {GH}"}}]}},
    ]
    (d / "zed_2026-10-03.jsonl").write_bytes(
        b"".join(redact.redact_line(json.dumps(e).encode() + b"\n") for e in events)
        + redact.redact_line(f"garbled {GH}\n".encode()))
    monkeypatch.setattr(mod, "STREAM_LOG_DIR", d)
    out = mod.read_recent_stream("zed")
    assert "[USER] deploy with [redacted] please" in out
    assert "[TEXT] on it" in out and "[TOOL] Bash" in out and GH not in out
