#!/usr/bin/env bash
# Fresh-install smoke (7.3a): the image and compose file a new user gets come up
# and answer. Docker only. Whole script under 8 minutes.
#
#   KARAKOS_SMOKE_IMAGE_TAR   a `docker save` file to load (else: docker build from the copy,
#                             which needs the dashboard source: vendor/ tarball or the CI credential)
#   KARAKOS_SMOKE_ARTIFACTS   directory for logs on failure
#   SMOKE_PROJECT / SMOKE_WORK  overrides (the safety test uses them); the guard validates both
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
# shellcheck source=tests/smoke/guard.sh
. "$HERE/guard.sh"

SUFFIX="$(python3 -c 'import secrets; print(secrets.token_hex(4))')"
SMOKE_PROJECT="${SMOKE_PROJECT:-karakos-smoke-$SUFFIX}"
export SMOKE_PROJECT
CREATED_WORK=""
if [ -z "${SMOKE_WORK:-}" ]; then SMOKE_WORK="$(mktemp -d "${TMPDIR:-/tmp}/karakos-smoke.XXXXXX")"; CREATED_WORK=1; fi
export SMOKE_WORK
(smoke_guard) || { [ -z "$CREATED_WORK" ] || rm -rf "$SMOKE_WORK"; exit 97; }
command -v docker >/dev/null || { echo "docker not found" >&2; exit 2; }

VERSION_TAG="smoke-$SUFFIX"
IMAGE="ghcr.io/mcarmody/karakos:$VERSION_TAG"
INSTALL="$SMOKE_WORK/install"
ARTIFACTS="${KARAKOS_SMOKE_ARTIFACTS:-}"
export COMPOSE_PROJECT_NAME="$SMOKE_PROJECT"
export KARAKOS_VERSION="$VERSION_TAG"
export HOME="$SMOKE_WORK/home"        # compose binds $HOME/.claude and $HOME/.claude.json

mkdir -p "$INSTALL" "$HOME/.claude"
echo '{}' > "$HOME/.claude.json"

# The tracked tree only; the working tree is never touched.
git -C "$REPO" archive HEAD | tar -x -C "$INSTALL"
cd "$INSTALL"

DC=(docker compose -f config/docker-compose.yml -f config/docker-compose.smoke.yml --env-file config/.env)

open_perms() {   # the container user is not the runner's uid; let it write the bind mounts
  chmod -R a+rwX "$INSTALL" "$HOME" 2>/dev/null || true
}

cleanup() {
  local rc=$?
  smoke_guard
  if [ $rc -ne 0 ] && [ -n "$ARTIFACTS" ]; then
    mkdir -p "$ARTIFACTS"
    "${DC[@]}" logs --no-color > "$ARTIFACTS/compose-logs.txt" 2>&1 || true
    "${DC[@]}" ps -a > "$ARTIFACTS/compose-ps.txt" 2>&1 || true
  fi
  "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$SMOKE_WORK" 2>/dev/null || {
    docker run --rm -v "$SMOKE_WORK:/w" --entrypoint sh "$IMAGE" -c 'rm -rf /w/* /w/.[!.]*' >/dev/null 2>&1 || true
    rm -rf "$SMOKE_WORK" 2>/dev/null || true
  }
  docker rmi -f "$IMAGE" >/dev/null 2>&1 || true
  if [ $rc -eq 0 ]; then smoke_assert_clean || rc=1; fi
  exit $rc
}
trap cleanup EXIT

if [ -n "${KARAKOS_SMOKE_IMAGE_TAR:-}" ]; then
  loaded="$(docker load -i "$KARAKOS_SMOKE_IMAGE_TAR" | sed -n 's/^Loaded image: //p' | tail -1)"
  [ -n "$loaded" ] || { echo "no image in $KARAKOS_SMOKE_IMAGE_TAR" >&2; exit 1; }
  docker tag "$loaded" "$IMAGE"
else
  docker build -t "$IMAGE" .
fi

# The path is resolved per image, not assumed (a Node global CLI usually lands in /usr/bin).
CLAUDE_PATH="$(docker run --rm --entrypoint sh "$IMAGE" -c 'command -v claude')"
[ -n "$CLAUDE_PATH" ] || { echo "image has no claude on PATH" >&2; exit 1; }

DASH_PORT="$(smoke_free_port)"; SRV_PORT="$(smoke_free_port)"
python3 tests/smoke/make_config.py . --dashboard-port "$DASH_PORT" --server-port "$SRV_PORT" \
  --claude-path "$CLAUDE_PATH" > /dev/null
set -a; . config/.env; set +a
AUTH="Authorization: Bearer $AGENT_SERVER_TOKEN"
BASE="http://127.0.0.1:$AGENT_SERVER_PORT"

container_healthy() {
  local cid; cid="$("${DC[@]}" ps -q karakos)"
  [ -n "$cid" ] && [ "$(docker inspect -f '{{.State.Health.Status}}' "$cid")" = healthy ]
}
api() { curl -fsS -H "$AUTH" "$BASE$1"; }
json() { python3 -c "import json,sys; d=json.load(sys.stdin); $1"; }
stamp_schema() { "${DC[@]}" exec -T karakos python3 -c "import json; print(json.load(open('data/.schema-version'))['schema'])"; }
dbq() {   # <sql>: one value from the agent-server db, read-only, inside the container
  "${DC[@]}" exec -T karakos python3 -c "import sqlite3,sys; c=sqlite3.connect('file:data/memory/agent-server.db?mode=ro',uri=True); print(c.execute(sys.argv[1]).fetchone()[0])" "$1"
}
rows_done() { [ "$(dbq "select count(*) from message_queue where agent='main' and processed=2")" = "$WANT_ROWS" ]; }
send_and_wait() {   # <text> <expected completed-row count>
  curl -fsS -H "$AUTH" -H 'Content-Type: application/json' \
    -d "{\"agent\":\"main\",\"content\":\"$1\",\"channel_id\":\"0\",\"server\":\"local\",\"author\":\"smoke\"}" \
    "$BASE/message" >/dev/null
  WANT_ROWS="$2"
  smoke_wait "$2 COMPLETE row(s) after '$1'" 90 rows_done
}
rows_complete() {
  "${DC[@]}" exec -T karakos python3 -c "import sqlite3; c=sqlite3.connect('file:data/memory/agent-server.db?mode=ro',uri=True); print([r[0] for r in c.execute('select response from message_queue where agent=? and processed=2 order by id',('main',))])"
}
assert_eq() { [ "$1" = "$2" ] || { echo "FAIL: $3: got '$1', want '$2'" >&2; exit 1; }; }

echo "== up"
open_perms
"${DC[@]}" up -d --pull never
smoke_wait "container healthy" 150 container_healthy

echo "== documented smoke commands (docs/QUICKSTART.md, verbatim)"
set -a; . config/.env; set +a
docker compose -f config/docker-compose.yml --env-file config/.env ps
curl -fsS -H "Authorization: Bearer $AGENT_SERVER_TOKEN" "http://localhost:$AGENT_SERVER_PORT/health"
curl -fsS -o /dev/null -w '%{http_code}\n' "http://localhost:$DASHBOARD_PORT/login"
docker compose -f config/docker-compose.yml --env-file config/.env exec karakos cat data/.schema-version

echo "== /health, /agents, stamp"
api /health | json "assert d['status']=='healthy', d; assert isinstance(d.get('outbox'), dict), d"
api /agents | json "a={x['name']:x['state'] for x in d['agents']} if isinstance(d,dict) else {x['name']:x['state'] for x in d}; assert set(a)>={'main','monitor'}, a; assert all(s=='IDLE' for s in a.values()), a"
assert_eq "$(stamp_schema)" 2 "schema stamp"

echo "== dashboard login, no Secure attribute, session loads /agents"
assert_eq "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$DASHBOARD_PORT/login")" 200 "/login"
HDRS="$SMOKE_WORK/login.hdrs"
logged_in=""
for path in /api/auth /api/auth/login /api/login; do
  curl -s -D "$HDRS" -o /dev/null -H 'Content-Type: application/json' \
    -d "{\"username\":\"$DASHBOARD_USER\",\"password\":\"$DASHBOARD_PASSWORD\"}" \
    "http://127.0.0.1:$DASHBOARD_PORT$path" || true
  if grep -qi '^set-cookie: karakos_session=' "$HDRS"; then logged_in=$path; break; fi
done
[ -n "$logged_in" ] || { echo "FAIL: no login route set karakos_session (tried /api/auth /api/auth/login /api/login)" >&2; cat "$HDRS" >&2; exit 1; }
if grep -i '^set-cookie: karakos_session=' "$HDRS" | grep -qi ';[[:space:]]*secure'; then
  echo "FAIL: session cookie has the Secure attribute on plain http (B40)" >&2; exit 1
fi
SESSION="$(grep -i '^set-cookie: karakos_session=' "$HDRS" | sed -E 's/^[^=]*=([^;]*).*/\1/' | tr -d '\r')"
assert_eq "$(curl -s -o /dev/null -w '%{http_code}' -H "Cookie: karakos_session=$SESSION" "http://127.0.0.1:$DASHBOARD_PORT/agents")" 200 "/agents with the session cookie"

echo "== message round trip"
send_and_wait "first smoke message" 1
assert_eq "$(rows_complete)" "['smoke-ok']" "scripted reply"
assert_eq "$(dbq "select count(*) from cost_events where agent='main'")" 1 "cost rows after one message"

echo "== restart: stamp intact, row still COMPLETE, second round trip"
"${DC[@]}" restart karakos
smoke_wait "container healthy after restart" 150 container_healthy
assert_eq "$(stamp_schema)" 2 "stamp after restart"
send_and_wait "second smoke message" 2

echo "== down, up on the same volumes"
"${DC[@]}" down
open_perms
"${DC[@]}" up -d --pull never
smoke_wait "container healthy after down/up" 150 container_healthy
assert_eq "$(stamp_schema)" 2 "stamp after down/up"
assert_eq "$(rows_complete)" "['smoke-ok', 'smoke-ok']" "rows after down/up"
send_and_wait "third smoke message" 3

echo "fresh-install smoke: PASS"
