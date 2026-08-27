"""Tests for bin/agent-server.py's routing surface.

The full server is heavy (event loop + sqlite + subprocesses) so we avoid
booting it and read the route table out of the AST instead.

These tests used to be `assert "<literal>" in src` greps. Every assertion they
made was true, so they looked like coverage, but a sabotage pass put five real
defects straight through them and failed one *correct* reformatting:

  - deleting `app.router.add_get("/health", handle_health)` and leaving the
    path in a comment: GREEN, because the grep only wanted the string;
  - dropping the `_AGENT_NAME_RE.match` call out of the register handler while
    the constant and the error message survived elsewhere: GREEN;
  - pointing a route at a handler that does not exist: GREEN, and the server
    would NameError at boot;
  - deleting the `/usage` and `/ask/{ask_id}/answer` routes: GREEN, they were
    never in the list;
  - splitting the register line over two lines the way a formatter would: RED,
    against code that was completely correct.

The rule underneath all six: grepping source text tests the text, not the
behaviour, and a comment is source text too. Read the AST, and assert on the
route table the code actually builds.
"""

import ast
import re
from pathlib import Path

import pytest

from conftest import PACKAGE_ROOT

AGENT_SERVER = PACKAGE_ROOT / "bin" / "agent-server.py"
_SOURCE = AGENT_SERVER.read_text()
_TREE = ast.parse(_SOURCE)

# aiohttp's UrlDispatcher methods we care about: add_get, add_post, add_route...
_ADD_PREFIX = "add_"


def _registered_routes():
    """Every `<something>.router.add_<method>(path, handler)` the module makes.

    Returns [(method, path, handler_name)]. Only literal paths and bare-name
    handlers are collected; anything computed is reported separately by
    ``_dynamic_route_calls`` so it cannot hide from these tests silently.
    """
    routes = []
    for node in ast.walk(_TREE):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or not func.attr.startswith(_ADD_PREFIX):
            continue
        owner = func.value
        if not (isinstance(owner, ast.Attribute) and owner.attr == "router"):
            continue
        method = func.attr[len(_ADD_PREFIX):]
        args = node.args
        # add_route(method, path, handler) vs add_get(path, handler)
        if method == "route" and len(args) >= 3:
            method_node, path_node, handler_node = args[0], args[1], args[2]
            method = (
                method_node.value.lower()
                if isinstance(method_node, ast.Constant)
                else "?"
            )
        elif len(args) >= 2:
            path_node, handler_node = args[0], args[1]
        else:
            continue
        if not isinstance(path_node, ast.Constant) or not isinstance(
            path_node.value, str
        ):
            continue
        handler = handler_node.id if isinstance(handler_node, ast.Name) else None
        routes.append((method, path_node.value, handler))
    return routes


def _dynamic_route_calls():
    """Line numbers of route registrations whose path is not a string literal."""

    def _is_str_literal(node):
        return isinstance(node, ast.Constant) and isinstance(node.value, str)

    dynamic = []
    for node in ast.walk(_TREE):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr.startswith(_ADD_PREFIX)
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "router"
        ):
            continue
        args = node.args
        # add_route(method, path, handler) puts the path second; add_get/add_post
        # and friends put it first.
        path_index = 1 if node.func.attr == "add_route" else 0
        if len(args) <= path_index or not _is_str_literal(args[path_index]):
            dynamic.append(node.lineno)
    return dynamic


def _top_level_functions():
    """{name: node} for module-level def / async def."""
    out = {}
    for node in _TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(node.name, []).append(node)
    return out


def _function(name):
    defs = _top_level_functions().get(name)
    assert defs, f"{name} is not defined at module level in agent-server.py"
    return defs[0]


def _calls_in(node):
    """Names of things called inside a function: `f()`, `obj.f()`, `await f()`."""
    plain, attrs = set(), set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            if isinstance(sub.func, ast.Name):
                plain.add(sub.func.id)
            elif isinstance(sub.func, ast.Attribute):
                attrs.add(sub.func.attr)
                if isinstance(sub.func.value, ast.Name):
                    attrs.add(f"{sub.func.value.id}.{sub.func.attr}")
    return plain, attrs


# The routes the rest of the system depends on. Each entry is the contract a
# caller outside this file relies on, so deleting one has to fail here.
#   bin/create-agent.sh          -> POST /agents/{name}/register
#   bin/relay.py, discord relay  -> POST /message, /agents/{name}/{reset,reload}
#   bin/health-monitor.py, /sys  -> GET  /health, /agents
#   bin/cost-report.sh, /sys     -> POST /cost, GET /cost/{agent}, /usage
#   the ask surface (#101/#135)  -> POST /ask, GET /ask/{id}, POST /ask/{id}/answer
#   the relay's control buttons  -> POST /agents/{name}/{interrupt,kill,flush}
#   the dashboard agent modal    -> GET/DELETE /agents/{name}/queue[/{id}]
REQUIRED_ROUTES = {
    ("post", "/message"): "handle_message",
    ("get", "/health"): "handle_health",
    ("get", "/agents"): "handle_agents",
    ("post", "/agents/{name}/reset"): "handle_agent_reset",
    ("post", "/agents/{name}/reload"): "handle_agent_reload",
    ("post", "/agents/{name}/register"): "handle_agent_register",
    ("post", "/agents/{name}/interrupt"): "handle_agent_interrupt",
    ("post", "/agents/{name}/kill"): "handle_agent_kill",
    ("post", "/agents/{name}/flush"): "handle_agent_flush",
    ("get", "/agents/{name}/queue"): "handle_agent_queue",
    ("delete", "/agents/{name}/queue/{queue_id}"): "handle_agent_queue_delete",
    ("post", "/cost"): "handle_cost",
    ("get", "/cost/{agent}"): "handle_cost_get",
    ("get", "/usage"): "handle_usage",
    ("post", "/ask"): "handle_ask_create",
    ("get", "/ask/{ask_id}"): "handle_ask_status",
    ("post", "/ask/{ask_id}/answer"): "handle_ask_answer",
}


def test_agent_server_parses():
    ast.parse(_SOURCE)


@pytest.mark.parametrize(
    "method,path,handler",
    [(m, p, h) for (m, p), h in sorted(REQUIRED_ROUTES.items())],
)
def test_required_route_is_registered(method, path, handler):
    """Each route is registered, with the handler its callers expect.

    Asserts on the parsed route table, so a registration that has been deleted
    or commented out fails even though the path string is still in the file.
    """
    routes = _registered_routes()
    matches = [r for r in routes if r[0] == method and r[1] == path]
    assert matches, (
        f"{method.upper()} {path} is not registered. Registered: "
        + ", ".join(sorted(f"{m.upper()} {p}" for m, p, _ in routes))
    )
    assert matches[0][2] == handler, (
        f"{method.upper()} {path} is wired to {matches[0][2]}, expected {handler}"
    )


def test_every_registered_handler_is_defined():
    """A route pointing at a name that does not exist NameErrors at boot.

    The old grep could not see this: the route line looked perfectly normal.
    """
    defined = _top_level_functions()
    missing = [
        (m, p, h)
        for m, p, h in _registered_routes()
        if h is not None and h not in defined
    ]
    assert not missing, "routes wired to undefined handlers: " + ", ".join(
        f"{m.upper()} {p} -> {h}" for m, p, h in missing
    )


def test_registered_handlers_are_coroutines():
    """aiohttp handlers must be `async def`; a plain def returns a coroutine-less
    object and every request to it 500s."""
    defined = _top_level_functions()
    sync = [
        (p, h)
        for _, p, h in _registered_routes()
        if h in defined and not isinstance(defined[h][0], ast.AsyncFunctionDef)
    ]
    assert not sync, "handlers that are not async def: " + ", ".join(
        f"{p} -> {h}" for p, h in sync
    )


def test_no_duplicate_handler_definitions():
    """Two `async def handle_x` in one module: the later silently wins.

    This is not hypothetical — merging wave 3 turned up two `on_interaction`
    definitions in one class, which would have taken the slash commands dead
    with no traceback. A duplicate here has the same shape and no symptom.
    """
    dupes = {
        name: [d.lineno for d in defs]
        for name, defs in _top_level_functions().items()
        if len(defs) > 1
    }
    assert not dupes, f"duplicate top-level definitions (the later one wins): {dupes}"


def test_no_duplicate_route_registrations():
    """Registering the same method+path twice raises at startup in aiohttp."""
    seen, dupes = set(), []
    for method, path, _ in _registered_routes():
        if (method, path) in seen:
            dupes.append(f"{method.upper()} {path}")
        seen.add((method, path))
    assert not dupes, f"routes registered more than once: {dupes}"


def test_route_paths_are_all_literals():
    """If a registration ever computes its path, these tests go blind to it.

    Fail loudly rather than quietly checking a shrinking subset — that is the
    failure mode that let the old greps look like coverage.
    """
    dynamic = _dynamic_route_calls()
    assert not dynamic, (
        "route registration with a non-literal path at line(s) "
        f"{dynamic} — extend _registered_routes() to cover it"
    )


def test_register_handler_validates_name():
    """Hot-register takes a path-segment name, so it must run it through
    _AGENT_NAME_RE before touching disk.

    Scoped to the handler's own AST: the old test accepted the constant being
    defined anywhere in the file, so deleting the call site left it green.
    """
    node = _function("handle_agent_register")
    _, attrs = _calls_in(node)
    assert "_AGENT_NAME_RE.match" in attrs, (
        "handle_agent_register does not call _AGENT_NAME_RE.match — an agent "
        "name from the URL reaches disk unvalidated"
    )
    messages = {
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert "Invalid agent name" in messages


def test_agent_name_regex_rejects_path_traversal():
    """The pattern itself, applied — not just asserted to exist."""
    import re

    pattern = None
    for node in ast.walk(_TREE):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "_AGENT_NAME_RE"
                for t in node.targets
            )
            and isinstance(node.value, ast.Call)
            and node.value.args
            and isinstance(node.value.args[0], ast.Constant)
        ):
            pattern = node.value.args[0].value
    assert pattern, "_AGENT_NAME_RE is not assigned a literal pattern"
    rx = re.compile(pattern)
    for good in ["amos", "argus", "agent-1", "agent_1", "A9"]:
        assert rx.match(good), f"{good!r} should be a legal agent name"
    for bad in ["../etc", "a/b", "a b", "", ".", "a.b", "a\nb", "a\x00b"]:
        assert not rx.match(bad), f"{bad!r} must be rejected"


def test_register_handler_reloads_config_and_spawns():
    """The handler must re-read agents.json and start the subprocess, or the
    hot-register is a no-op that reports success."""
    node = _function("handle_agent_register")
    plain, _ = _calls_in(node)
    assert "load_config" in plain, "handle_agent_register never calls load_config()"
    assert "start_agent_subprocess" in plain, (
        "handle_agent_register never calls start_agent_subprocess()"
    )


def test_create_agent_script_targets_register():
    """The script and the server must agree on the endpoint path.

    Comments are stripped first: a commented-out curl would otherwise satisfy
    this, which is the same defect as greping the server for a route string.
    """
    create_agent = PACKAGE_ROOT / "bin" / "create-agent.sh"
    assert create_agent.exists()
    live = "\n".join(
        line
        for line in create_agent.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "/agents/$AGENT_NAME/register" in live, (
        "create-agent.sh does not POST to the register endpoint outside of a comment"
    )


# ===========================================================================
# The dashboard <-> agent-server path contract (issue #151)
# ===========================================================================
#
# Every one of the four defects in #151 was the same shape: a dashboard API
# route called an agent-server path that agent-server.py does not register,
# got a 404 body back, and rendered it as if it were data. `GET /status`,
# `POST /interrupt`, `GET /queue/{name}` and `DELETE /queue/{name}/{id}` were
# all confidently written and none of them existed.
#
# Nothing could catch that. TypeScript does not know what the Python server
# routes; the Python tests above did not know the dashboard existed; and the
# 404 arrives as a perfectly well-formed JSON object, so `await res.json()`
# succeeds and the failure has no symptom beyond a card full of `undefined`.
#
# These tests join the two halves: they read the paths the dashboard actually
# asks for and check each one against the route table `create_app()` actually
# builds. They are deliberately general -- they do not enumerate the four
# known-bad paths, so the fifth one fails here too.

DASHBOARD_DIR = PACKAGE_ROOT / "dashboard"
# The single chokepoint through which the dashboard talks to agent-server.
# `test_agent_fetch_is_the_only_door` below is what keeps that true; if it
# ever stops being true, this whole scan goes blind and that test says so.
API_HELPER = DASHBOARD_DIR / "lib" / "api.ts"

_TS_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_TS_LINE_COMMENT = re.compile(r"(?<![:\w])//[^\n]*")
_AGENT_FETCH = re.compile(r"\bagentFetch\s*\(")
# A quoted path: "/health", '/agents', or a `/agents/${name}/queue` template.
# A backtick template may span lines (prettier wraps long ones); a '' or ""
# string may not, and allowing newlines there would let the scan run away
# past an unterminated quote.
_PATH_LITERAL = re.compile(r"""`(/[^`]*)`|['"](/[^'"\n]*)['"]""")


def _path_literals(text):
    """Every quoted path in an expression, template literals included."""
    return [m.group(1) if m.group(1) is not None else m.group(2)
            for m in _PATH_LITERAL.finditer(text)]
_METHOD = re.compile(r"""\bmethod\s*:\s*['"](\w+)['"]""")


def _strip_ts_comments(text):
    """Blank out comments, preserving offsets so line numbers stay true.

    The same trap the contract test documents, and it is live here: the fixed
    routes carry comments naming the dead endpoints they used to call, so a
    scan that read prose as code would report the very paths that were just
    removed. The line-comment pattern refuses a `//` preceded by `:` so that
    `http://host` inside a string is not mistaken for a comment.
    """
    def blank(match):
        return re.sub(r"[^\n]", " ", match.group(0))

    return _TS_LINE_COMMENT.sub(blank, _TS_BLOCK_COMMENT.sub(blank, text))


def _balanced_paren(text, open_idx):
    """Text inside the (...) whose opening paren is at `open_idx`."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i]
    return ""


def _split_top_level_commas(text):
    """Split an argument list on commas that are not nested in (), [], {}."""
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _placeholderize(path):
    """`/agents/${encodeURIComponent(name)}/queue` -> `/agents/{}/queue`.

    Brace-balanced so a `${cond ? a : b}` with its own braces is consumed
    whole rather than leaving a tail that looks like a path segment.
    """
    out, i = [], 0
    while i < len(path):
        if path.startswith("${", i):
            depth, j = 0, i + 1
            while j < len(path):
                if path[j] == "{":
                    depth += 1
                elif path[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            out.append("{}")
            i = j + 1
        else:
            out.append(path[i])
            i += 1
    # The query string is not part of the route, and aiohttp never matches on
    # it -- /cost?period=daily is a request to /cost.
    return "".join(out).split("?", 1)[0]


def _dashboard_sources():
    """Every dashboard .ts/.tsx that calls agentFetch, comments stripped."""
    out = []
    if not DASHBOARD_DIR.exists():
        return out
    for path in sorted(DASHBOARD_DIR.rglob("*.ts")):
        if path == API_HELPER or "node_modules" in path.parts:
            continue
        text = _strip_ts_comments(path.read_text())
        if _AGENT_FETCH.search(text):
            out.append((path, text))
    return out


def _requested_paths():
    """[(file, line, method, path)] for every agentFetch call in the dashboard.

    Resolves the one indirection the codebase actually uses: `const path =
    ... ; agentFetch(path)`, as /api/cost does. Anything it cannot resolve is
    returned with a path of None so `test_every_agent_fetch_path_is_readable`
    fails loudly rather than this scan quietly covering less than it looks.
    """
    found = []
    for path_obj, text in _dashboard_sources():
        rel = path_obj.relative_to(PACKAGE_ROOT)
        for m in _AGENT_FETCH.finditer(text):
            line = text[: m.start()].count("\n") + 1
            args = _balanced_paren(text, m.end() - 1)
            pieces = _split_top_level_commas(args)
            first = pieces[0].strip()
            options = ",".join(pieces[1:])
            method = (_METHOD.search(options).group(1).lower()
                      if _METHOD.search(options) else "get")

            literals = _path_literals(first)
            if not literals:
                # `agentFetch(path)` -- resolve the local const by name.
                ident = first.strip()
                if re.fullmatch(r"[A-Za-z_$][\w$]*", ident):
                    for decl in re.finditer(
                        r"\b(?:const|let|var)\s+%s\s*=(.*?);" % re.escape(ident),
                        text,
                        re.DOTALL,
                    ):
                        literals.extend(_path_literals(decl.group(1)))

            if not literals:
                found.append((rel, line, method, None))
            for lit in literals:
                found.append((rel, line, method, _placeholderize(lit)))
    return found


def _route_matches(requested, registered):
    """Does a requested path reach an aiohttp route pattern?

    Segment-wise. `{param}` on the server side matches anything; `{}` on the
    client side is a value interpolated at runtime, which we cannot evaluate,
    so it is allowed to stand in for any single segment. Segment *count* is
    exact, which is what makes /queue/{name} fail against /cost/{agent}.
    """
    req = requested.strip("/").split("/")
    reg = registered.strip("/").split("/")
    if len(req) != len(reg):
        return False
    for r_seg, s_seg in zip(req, reg):
        if s_seg.startswith("{") and s_seg.endswith("}"):
            continue
        if r_seg == "{}":
            continue
        if r_seg != s_seg:
            return False
    return True


def test_the_dashboard_scan_finds_something():
    """A scan that matched nothing would make the check below pass by vacuum
    -- which is exactly how these four bugs survived to begin with."""
    assert DASHBOARD_DIR.exists(), "dashboard/ is missing"
    calls = _requested_paths()
    assert len(calls) >= 5, (
        f"only {len(calls)} agentFetch call(s) found in dashboard/ -- the "
        "scan has probably stopped seeing them"
    )


def test_every_agent_fetch_path_is_readable():
    """Every call site must yield a path this test can check.

    If a route ever computes its path in a way the scan cannot follow, fail
    here rather than silently checking a shrinking subset.
    """
    unreadable = [
        f"{rel}:{line}" for rel, line, _, path in _requested_paths() if path is None
    ]
    assert not unreadable, (
        "agentFetch() called with a path this test cannot resolve at "
        f"{', '.join(unreadable)} -- extend _requested_paths() to cover it"
    )


def test_agent_fetch_is_the_only_door():
    """The scan assumes every agent-server call goes through agentFetch().

    A dashboard file that built the upstream URL itself would be invisible to
    it, so AGENT_SERVER_URL is allowed to appear in exactly one file.
    """
    if not DASHBOARD_DIR.exists():
        pytest.skip("no dashboard/ in this checkout")
    offenders = []
    for path in sorted(DASHBOARD_DIR.rglob("*.ts")):
        if path == API_HELPER or "node_modules" in path.parts:
            continue
        if "AGENT_SERVER_URL" in _strip_ts_comments(path.read_text()):
            offenders.append(str(path.relative_to(PACKAGE_ROOT)))
    assert not offenders, (
        "these files reach the agent server without agentFetch(), so the "
        f"path contract test cannot see them: {offenders}. Route the call "
        "through lib/api.ts."
    )


def test_no_dashboard_route_calls_an_unregistered_agent_server_path():
    """Issue #151 itself, generalised.

    The dashboard asked for /status, /interrupt, /queue/{name} and
    /queue/{name}/{id}; agent-server.py registers none of them. A 404 body
    parses as JSON just fine, so every one of those failures rendered as
    `undefined` rather than as an error.
    """
    registered = _registered_routes()
    assert registered, "no routes parsed out of agent-server.py"

    offenders = []
    for rel, line, method, path in _requested_paths():
        if path is None:
            continue  # reported by test_every_agent_fetch_path_is_readable
        if any(
            m == method and _route_matches(path, p) for m, p, _ in registered
        ):
            continue
        # Distinguish "wrong path" from "right path, wrong verb": the second
        # is a different fix and a confusing message if it says "no route".
        by_path = sorted(
            {m.upper() for m, p, _ in registered if _route_matches(path, p)}
        )
        detail = (
            f"only {'/'.join(by_path)} is registered for it"
            if by_path
            else "no such route on the agent server"
        )
        offenders.append(f"{rel}:{line} {method.upper()} {path} -- {detail}")

    assert not offenders, (
        "dashboard routes calling agent-server paths that do not exist:\n  "
        + "\n  ".join(offenders)
        + "\nThese return a 404 JSON body that the caller renders as data. "
        "Either repoint the client or register the route in "
        "bin/agent-server.py."
    )
