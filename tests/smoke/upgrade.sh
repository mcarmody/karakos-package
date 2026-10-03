#!/usr/bin/env bash
# Upgrade smoke on a tagged 1.x image (7.3a): the container path, not 7.1's
# in-process chain. Docker only.
#
#   usage: tests/smoke/upgrade.sh <tag>        (v1.0.0, v1.1.1, v1.3, v1.5.0; v1.4.1 optional)
#
#   KARAKOS_SMOKE_IMAGE_TAR   a `docker save` file of the 2.0 image (else: docker build from HEAD)
#   KARAKOS_SMOKE_ARTIFACTS   directory for logs on failure
#   SMOKE_PROJECT / SMOKE_WORK  overrides (the safety test uses them); the guard validates both
#
# Every volume the migrator touches was created by this script, under a
# `karakos-up-*` compose project. HOME is a temp directory. The guard runs before
# anything else and before every volume-removing teardown.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
# shellcheck source=tests/smoke/guard.sh
. "$HERE/guard.sh"

TAG="${1:?usage: upgrade.sh <tag>}"
TAG_SAFE="$(printf '%s' "$TAG" | tr 'A-Z.' 'a-z-' | tr -c 'a-z0-9_\n-' '-')"
SUFFIX="$(python3 -c 'import secrets; print(secrets.token_hex(4))')"
SMOKE_PROJECT="${SMOKE_PROJECT:-karakos-up-$TAG_SAFE-$SUFFIX}"
export SMOKE_PROJECT
CREATED_WORK=""
if [ -z "${SMOKE_WORK:-}" ]; then SMOKE_WORK="$(mktemp -d "${TMPDIR:-/tmp}/karakos-up.XXXXXX")"; CREATED_WORK=1; fi
export SMOKE_WORK
(smoke_guard) || { [ -z "$CREATED_WORK" ] || rm -rf "$SMOKE_WORK"; exit 97; }
command -v docker >/dev/null || { echo "docker not found" >&2; exit 2; }

NEW_TAG="smoke-$SUFFIX"
NEW_IMAGE="ghcr.io/mcarmody/karakos:$NEW_TAG"
OLD_IMAGE="ghcr.io/mcarmody/karakos:$TAG"
OLD_BUILT=0
OLD="$SMOKE_WORK/install"
BACKUPS="$SMOKE_WORK/backups"
ARTIFACTS="${KARAKOS_SMOKE_ARTIFACTS:-}"
export COMPOSE_PROJECT_NAME="$SMOKE_PROJECT"
export HOME="$SMOKE_WORK/home"
mkdir -p "$OLD" "$BACKUPS" "$HOME/.claude"
echo '{}' > "$HOME/.claude.json"

DC=(docker compose -f config/docker-compose.yml -f config/docker-compose.smoke.yml --env-file config/.env)
EXTRA_PROJECTS=()

open_perms() {   # the container user is not the runner's uid; let it write the bind mounts
  chmod -R a+rwX "$OLD" "$HOME" 2>/dev/null || true
}

cleanup() {
  local rc=$?
  smoke_guard
  cd "$OLD" 2>/dev/null || true
  if [ $rc -ne 0 ] && [ -n "$ARTIFACTS" ]; then
    mkdir -p "$ARTIFACTS"
    "${DC[@]}" logs --no-color > "$ARTIFACTS/compose-logs-$TAG.txt" 2>&1 || true
    "${DC[@]}" ps -a > "$ARTIFACTS/compose-ps-$TAG.txt" 2>&1 || true
  fi
  KARAKOS_VERSION="$NEW_TAG" "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  local p
  for p in ${EXTRA_PROJECTS[@]+"${EXTRA_PROJECTS[@]}"}; do
    SMOKE_PROJECT="$p" smoke_guard
    docker ps -aq --filter "label=com.docker.compose.project=$p" | xargs -r docker rm -f >/dev/null 2>&1 || true
    docker volume ls -q --filter "label=com.docker.compose.project=$p" | xargs -r docker volume rm -f >/dev/null 2>&1 || true
  done
  rm -rf "$SMOKE_WORK" 2>/dev/null || {
    docker run --rm -v "$SMOKE_WORK:/w" --entrypoint sh "$NEW_IMAGE" -c 'rm -rf /w/* /w/.[!.]*' >/dev/null 2>&1 || true
    rm -rf "$SMOKE_WORK" 2>/dev/null || true
  }
  docker rmi -f "$NEW_IMAGE" >/dev/null 2>&1 || true
  if [ "$OLD_BUILT" = 1 ]; then docker rmi -f "$OLD_IMAGE" >/dev/null 2>&1 || true; fi
  if [ $rc -eq 0 ]; then smoke_assert_clean || rc=1; fi
  exit $rc
}
trap cleanup EXIT

assert_eq() { [ "$1" = "$2" ] || { echo "FAIL: $3: got '$1', want '$2'" >&2; exit 1; }; }
step() { echo "== [$TAG] $*"; }

# ---- images ---------------------------------------------------------------
step "images"
if docker pull "$OLD_IMAGE" >/dev/null 2>&1; then
  echo "using published image $OLD_IMAGE"
else
  echo "no published image for $TAG: building it from that tag's own Dockerfile (slower)"
  git -C "$REPO" archive "$TAG" | tar -x -C "$OLD"
  docker build -t "$OLD_IMAGE" "$OLD"
  OLD_BUILT=1
  rm -rf "${OLD:?}"/* "$OLD"/.[!.]* 2>/dev/null || true
fi
if [ -n "${KARAKOS_SMOKE_IMAGE_TAR:-}" ]; then
  loaded="$(docker load -i "$KARAKOS_SMOKE_IMAGE_TAR" | sed -n 's/^Loaded image: //p' | tail -1)"
  [ -n "$loaded" ] || { echo "no image in $KARAKOS_SMOKE_IMAGE_TAR" >&2; exit 1; }
  docker tag "$loaded" "$NEW_IMAGE"
else
  git -C "$REPO" archive HEAD | tar -x -C "$SMOKE_WORK" --one-top-level=head-src
  (cd "$SMOKE_WORK/head-src" && docker build -t "$NEW_IMAGE" .)
fi
claude_path() { docker run --rm --entrypoint sh "$1" -c 'command -v claude'; }
OLD_CLAUDE="$(claude_path "$OLD_IMAGE")"; NEW_CLAUDE="$(claude_path "$NEW_IMAGE")"
[ -n "$OLD_CLAUDE" ] && [ -n "$NEW_CLAUDE" ] || { echo "an image has no claude on PATH" >&2; exit 1; }

# ---- the old stack: the tag's own tree, compose file and image ---------------
step "old stack"
git -C "$REPO" archive "$TAG" | tar -x -C "$OLD"
cd "$OLD"
mkdir -p agents/main agents/helper .karakos config/hooks
export KARAKOS_VERSION="$TAG"
DASH_PORT="$(smoke_free_port)"; SRV_PORT="$(smoke_free_port)"
TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(12))')"
cat > config/.env <<EOF
DASHBOARD_PORT=$DASH_PORT
AGENT_SERVER_PORT=$SRV_PORT
AGENT_SERVER_TOKEN=$TOKEN
SESSION_SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=$PASSWORD
TZ=UTC
FAKE_CLAUDE_LOG_DIR=/tmp/fake-claude-logs
FAKE_CLAUDE_SCRIPT=/workspace/config/fake-claude-script.json
EOF
python3 - <<'PY'
import json
def agent(name):
    return {"model": "sonnet", "max_turns": 200, "timeout": 600,
            "system_prompt": f"agents/{name}/SYSTEM_PROMPT.md",
            "tool_streaming": False, "stream_to_channel": False}
json.dump({"agents": {"main": agent("main"), "helper": agent("helper")}},
          open("config/agents.json", "w"), indent=2)
json.dump({"server_id": "0", "channels": {"general": {"id": "0"}, "signals": {"id": "0"}}},
          open("config/channels.json", "w"), indent=2)
PY
echo "You are main." > agents/main/SYSTEM_PROMPT.md
echo "You are helper." > agents/helper/SYSTEM_PROMPT.md
echo '{"version":"1.0.0","system_name":"smoke","owner_name":"smoke"}' > .karakos/config.json
printf '#!/bin/sh\n# custom hook (upgrade smoke)\nexit 0\n' > config/hooks/custom-smoke.sh
HOOK_SUM="$(sha256sum config/hooks/custom-smoke.sh | cut -d' ' -f1)"
echo '{"default":{"text":"smoke-ok"},"rules":[{"match":"HOLD","step":{"hang":true}}]}' > config/fake-claude-script.json
write_override() {   # <claude path> <fixed container ports? 0|1> <image>
  printf '#!/bin/sh\nexec python3 /opt/fake/fake_claude.py "$@"\n' > config/fake-claude-wrapper.sh
  chmod 755 config/fake-claude-wrapper.sh
  {
    # `image:` is set because the 1.0 to 1.2 compose files have `build:` and no `image:`;
    # with the tag present locally, compose uses it instead of building.
    printf 'services:\n  karakos:\n    image: %s\n    pull_policy: never\n    volumes:\n      - %s/tests/harness:/opt/fake:ro\n      - ./fake-claude-wrapper.sh:%s:ro\n' "$3" "$SMOKE_WORK/fake-src" "$1"
    # A compose file with fixed container ports (1.0 to 1.2) must not see the
    # chosen port numbers from env_file: the server would bind a port the
    # mapping does not forward.
    if [ "$2" = 1 ]; then printf '    environment:\n      - AGENT_SERVER_PORT=18791\n      - DASHBOARD_PORT=3000\n'; fi
  } > config/docker-compose.smoke.yml
}
mkdir -p "$SMOKE_WORK/fake-src"
git -C "$REPO" archive HEAD tests/harness | tar -x -C "$SMOKE_WORK/fake-src"
FIXED=0; grep -q 'AGENT_SERVER_PORT:-18791}:\${AGENT_SERVER_PORT' config/docker-compose.yml || FIXED=1
write_override "$OLD_CLAUDE" "$FIXED" "$OLD_IMAGE"
# Fixed-port layouts publish the container's own ports; the health check and API
# live on the container side of the mapping.
BASE="http://127.0.0.1:$SRV_PORT"
AUTH="Authorization: Bearer $TOKEN"

container_healthy() {
  local cid; cid="$("${DC[@]}" ps -q karakos)"
  [ -n "$cid" ] && [ "$(docker inspect -f '{{.State.Health.Status}}' "$cid")" = healthy ]
}
# Old images are waited on through the authenticated API, not Docker's health status:
# the 1.0 healthcheck is an unauthenticated curl that need not match what the server does.
api_healthy() { curl -fsS -H "$AUTH" "$BASE/health" >/dev/null; }
api() { curl -fsS -H "$AUTH" "$BASE$1"; }
post_message() {   # <agent> <text>
  curl -fsS -H "$AUTH" -H 'Content-Type: application/json' \
    -d "{\"agent\":\"$1\",\"content\":\"$2\",\"channel_id\":\"0\",\"server\":\"local\",\"author\":\"smoke\"}" \
    "$BASE/message" >/dev/null
}
dbq() {   # <sql>: one value from the agent-server db (read-only), inside the running container
  "${DC[@]}" exec -T karakos python3 -c "import sqlite3,sys; c=sqlite3.connect('file:data/memory/agent-server.db?mode=ro',uri=True); print(c.execute(sys.argv[1]).fetchone()[0])" "$1"
}
done_rows() { [ "$(dbq "select count(*) from message_queue where processed=2")" -ge "$WANT" ]; }
wait_done() { WANT="$1"; smoke_wait "$1 COMPLETE rows" 120 done_rows; }
vol_hash() {   # <volume>: hash of every file in a volume (read-only mount)
  docker run --rm -v "$1:/d:ro" --entrypoint sh "$NEW_IMAGE" -c \
    'cd /d && find . -type f -not -name "*-wal" -not -name "*-shm" | sort | xargs -r sha256sum | sha256sum'
}
DATA_VOL="${SMOKE_PROJECT}_karakos-data"

step "start, seed through the old API"
open_perms
"${DC[@]}" up -d --pull never
smoke_wait "old stack answering /health" 180 api_healthy
for i in 1 2 3; do post_message main "seed turn $i"; wait_done "$i"; done
post_message helper "seed helper turn"; wait_done 4
post_message main "HOLD this turn"          # the scripted hang
smoke_wait "held row in progress" 60 bash -c "[ \"\$(curl -fsS -H '$AUTH' '$BASE/agents/main/queue' | grep -c processing)\" -ge 1 ]"
post_message main "queued behind the hold"
# Dashboard login on the old image: its session cookie must survive the upgrade.
HDRS="$SMOKE_WORK/old-login.hdrs"
curl -s -D "$HDRS" -o /dev/null -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$PASSWORD\"}" "http://127.0.0.1:$DASH_PORT/api/auth"
OLD_COOKIE="$(grep -i '^set-cookie: karakos_session=' "$HDRS" | sed -E 's/^[^=]*=([^;]*).*/\1/' | tr -d '\r' || true)"
# The 1.0 to 1.2 compose files bind-mount the whole checkout over /workspace, which
# hides the image's built dashboard (a git checkout has no dashboard/.next), so the
# old dashboard cannot serve there and the cookie proof does not apply to that layout.
BIND_ALL=0; grep -qE '^[[:space:]]*-[[:space:]]*\.\.:/workspace[[:space:]]*$' config/docker-compose.yml && BIND_ALL=1
if [ -z "$OLD_COOKIE" ]; then
  if [ "$BIND_ALL" = 1 ]; then
    echo "NOTE: no old session cookie: $TAG bind-mounts the whole checkout, which hides the built dashboard; the cookie compatibility check is skipped for this layout"
  else
    echo "FAIL: the old dashboard set no karakos_session cookie" >&2; cat "$HDRS" >&2; exit 1
  fi
fi
# Memory: the 1.x memory tool is read-only, so seed memory.db through the tag's own
# bin/memory-maintenance.py init_db() and insert the rows its nightly job would.
"${DC[@]}" exec -T karakos python3 - <<'PY'
import importlib.util, os
os.environ.setdefault("WORKSPACE_ROOT", "/workspace")
spec = importlib.util.spec_from_file_location("mm", "/workspace/bin/memory-maintenance.py")
mm = importlib.util.module_from_spec(spec); spec.loader.exec_module(mm)
con = mm.init_db()
for i in range(5):
    con.execute("INSERT INTO episodes (summary, importance, channel, tags, agents, created_at) "
                "VALUES (?,?,?,?,?, datetime('now'))",
                (f"seed episode {i}", 5.0, "general", '["seed"]', '["main"]'))
for i in range(3):
    con.execute("INSERT INTO facts (subject, content) VALUES (?,?)", (f"subject{i}", f"seed fact {i}"))
con.commit(); con.close()
PY
SEED_ROWS="$(dbq "select count(*) from message_queue")"
SEED_DONE="$(dbq "select count(*) from message_queue where processed=2")"
step "graceful stop; volumes kept"
"${DC[@]}" stop
SEED_HASH="$(vol_hash "$DATA_VOL")"

# ---- git pull: HEAD over the old tree, untracked files kept --------------------
step "emulate git pull"
git -C "$REPO" archive HEAD | tar -x -C "$OLD"
export KARAKOS_VERSION="$NEW_TAG"
[ -f config/.env ] && [ -f config/agents.json ] && [ -f config/hooks/custom-smoke.sh ] \
  || { echo "FAIL: the pull emulation lost an untracked file" >&2; exit 1; }
write_override "$NEW_CLAUDE" 0 "$NEW_IMAGE"

# A copy for the refusal path, made now while the data is still 1.x.
REFUSE_PROJECT="$SMOKE_PROJECT-r"
EXTRA_PROJECTS+=("$REFUSE_PROJECT")
cp -a "$OLD" "$SMOKE_WORK/refuse"
REFUSE_VOL="${REFUSE_PROJECT}_karakos-data"
docker volume create --label "com.docker.compose.project=$REFUSE_PROJECT" --label com.docker.compose.volume=karakos-data "$REFUSE_VOL" >/dev/null
docker run --rm -v "$DATA_VOL:/from:ro" -v "$REFUSE_VOL:/to" --entrypoint sh "$NEW_IMAGE" -c 'cp -a /from/. /to/'

# ---- dry run -------------------------------------------------------------------
step "dry run (documented command), writes a report, changes nothing"
BEFORE="$(vol_hash "$DATA_VOL")"
bin/karakos migrate --dry-run --report-to /backups/migration-plan.md --backup-to "$BACKUPS"
[ -s "$BACKUPS/migration-plan.md" ] || { echo "FAIL: the dry run wrote no report" >&2; exit 1; }
assert_eq "$(vol_hash "$DATA_VOL")" "$BEFORE" "volume hash after the dry run"
assert_eq "$BEFORE" "$SEED_HASH" "volume hash before vs after stop"

# ---- the real run --------------------------------------------------------------
step "real run (documented command)"
bin/karakos migrate --auto --backup-to "$BACKUPS" > "$SMOKE_WORK/migrate.out" 2>&1 || { cat "$SMOKE_WORK/migrate.out" >&2; echo "FAIL: migrate exited non-zero" >&2; exit 1; }
cat "$SMOKE_WORK/migrate.out"
BACKUP="$(ls -1d "$BACKUPS"/pre-2.0-* | tail -1)"
[ -f "$BACKUP/MANIFEST.json" ] || { echo "FAIL: no backup manifest" >&2; exit 1; }
grep -q "karakos migrate --restore" "$SMOKE_WORK/migrate.out" || { echo "FAIL: the restore command was not printed" >&2; exit 1; }
[ -f config/docker-compose.yml.pre-2.0 ] || { echo "FAIL: config/docker-compose.yml.pre-2.0 missing" >&2; exit 1; }
assert_eq "$(grep -E '^(DASHBOARD_PORT|AGENT_SERVER_PORT)=' config/.env | sort | tr '\n' ' ')" \
  "AGENT_SERVER_PORT=$SRV_PORT DASHBOARD_PORT=$DASH_PORT " "ports kept in config/.env"

step "grant the fake's variables to each agent (documented env: step), start 2.0"
docker run --rm -v "$OLD/config:/workspace/config" --entrypoint python3 "$NEW_IMAGE" -c "
import yaml
p='/workspace/config/agents.yaml'; d=yaml.safe_load(open(p))
for a in d['agents'].values():
    a.setdefault('env',{}).update({k:'\${%s}'%k for k in ('FAKE_CLAUDE_LOG_DIR','FAKE_CLAUDE_SCRIPT')})
yaml.safe_dump(d, open(p,'w'), sort_keys=False)"
open_perms
"${DC[@]}" up -d --pull never
smoke_wait "2.0 healthy on migrated volumes" 180 container_healthy

step "assert the migrated install"
assert_eq "$("${DC[@]}" exec -T karakos python3 -c "import json; print(json.load(open('data/.schema-version'))['schema'])")" 2 "schema stamp"
api /agents | python3 -c "
import json,sys
names={a['name'] for a in json.load(sys.stdin)['agents']}
assert names>={'main','helper'} and len(names)>=3, names   # the migrated agents plus a monitor"
# Keying (plan rule): every row and session is keyed by an id the server lists.
IDS="$(api /agents | python3 -c "import json,sys; print(','.join(\"'%s'\" % a['name'] for a in json.load(sys.stdin)['agents']))")"
assert_eq "$(dbq "select count(*) from message_queue where agent not in ($IDS)")" 0 "queue rows keyed by agent id"
assert_eq "$(dbq "select count(*) from sessions where agent not in ($IDS)")" 0 "sessions keyed by agent id"
[ "$(dbq "select count(*) from sessions")" -ge 1 ] || { echo "FAIL: no sessions survived" >&2; exit 1; }
[ "$(dbq "select count(*) from message_queue where processed=2 and response='smoke-ok'")" -ge "$SEED_DONE" ] \
  || { echo "FAIL: completed rows lost their responses" >&2; exit 1; }
assert_eq "$(dbq "select count(*) from message_queue where content like 'HOLD%' and processed=3")" 1 "the interrupted row is crashed"
case "$(dbq "select processed from message_queue where content like 'queued behind%'")" in 0|2) ;; *) echo "FAIL: the queued row is neither queued nor run" >&2; exit 1;; esac
"${DC[@]}" exec -T karakos python3 - <<'PY'
import json, os, sqlite3
g = sqlite3.connect("file:data/memory/graph.db?mode=ro", uri=True)
m = json.loads(g.execute("select value from meta where key='migration'").fetchone()[0])
assert m["episodes"] == 5 and m["facts"] == 3, m
assert os.path.exists("data/memory/memory.db.migrated"), "memory.db.migrated missing"
PY
if [ -n "$OLD_COOKIE" ]; then
  assert_eq "$(curl -s -o /dev/null -w '%{http_code}' -H "Cookie: karakos_session=$OLD_COOKIE" "http://127.0.0.1:$DASH_PORT/agents")" 200 "old session cookie on the new dashboard"
fi
assert_eq "$(sha256sum config/hooks/custom-smoke.sh | cut -d' ' -f1)" "$HOOK_SUM" "custom hook preserved"
post_message main "post-upgrade round trip"
wait_done "$((SEED_DONE + 1))"

step "second migrator run: exit 0, nothing changes"
"${DC[@]}" stop
AFTER_FIRST="$(vol_hash "$DATA_VOL")"
bin/karakos migrate --auto --backup-to "$BACKUPS" 2>&1 | tee "$SMOKE_WORK/migrate2.out"
grep -qi "already at schema 2" "$SMOKE_WORK/migrate2.out" || { echo "FAIL: second run did not report 'already at schema 2'" >&2; exit 1; }
assert_eq "$(vol_hash "$DATA_VOL")" "$AFTER_FIRST" "volume hash after the second run"

# ---- refusal path ----------------------------------------------------------------
step "refusal path: unknown table and unknown channels.json key (on a copy)"
(
  cd "$SMOKE_WORK/refuse"
  export COMPOSE_PROJECT_NAME="$REFUSE_PROJECT" SMOKE_PROJECT="$REFUSE_PROJECT"
  smoke_guard
  docker run --rm -v "$REFUSE_VOL:/workspace/data" --entrypoint python3 "$NEW_IMAGE" -c \
    "import sqlite3; c=sqlite3.connect('/workspace/data/memory/agent-server.db'); c.execute('create table zz_unknown_table (x)'); c.commit()"
  python3 - <<'PY'
import json
p = "config/channels.json"; d = json.load(open(p)); d["zz_unknown_key"] = True; json.dump(d, open(p, "w"))
PY
  rc=0; bin/karakos migrate --auto --backup-to "$SMOKE_WORK/refuse-backups" > "$SMOKE_WORK/refuse.out" 2>&1 || rc=$?
  assert_eq "$rc" 3 "refusal exit code"
  grep -qi "zz_unknown" "$SMOKE_WORK/refuse.out" || { cat "$SMOKE_WORK/refuse.out" >&2; echo "FAIL: the refusal message does not name the unknown data" >&2; exit 1; }
  mkdir -p "$SMOKE_WORK/refuse-backups"
  bin/karakos migrate --dry-run --report-to /backups/refuse-plan.md --backup-to "$SMOKE_WORK/refuse-backups" >/dev/null 2>&1 || true
  [ -s "$SMOKE_WORK/refuse-backups/refuse-plan.md" ] || { echo "FAIL: --dry-run wrote no report on unknown data" >&2; exit 1; }
  bin/karakos migrate --auto --force --backup-to "$SMOKE_WORK/refuse-backups" >/dev/null 2>&1 \
    || { echo "FAIL: --force did not proceed" >&2; exit 1; }
)

# ---- restore ---------------------------------------------------------------------
step "restore (documented command), then the OLD image on the restored volumes"
"${DC[@]}" down
bin/karakos migrate --restore "$BACKUP"
# The volume tree must equal the manifest: every data/ file hashes as listed, nothing extra.
docker run --rm -v "$DATA_VOL:/workspace/data:ro" -v "$BACKUP:/b:ro" --entrypoint python3 "$NEW_IMAGE" -c "
import hashlib, json, os
m = json.load(open('/b/MANIFEST.json'))
want = {e['path'][5:]: e['sha256'] for e in m['files'] if e['path'].startswith('data/')}
have = {}
for root, _, files in os.walk('/workspace/data'):
    for f in files:
        p = os.path.join(root, f); have[os.path.relpath(p, '/workspace/data')] = hashlib.sha256(open(p,'rb').read()).hexdigest()
extra = set(have) - set(want); missing = set(want) - set(have)
bad = [k for k in want if k in have and have[k] != want[k]]
assert not extra and not missing and not bad, (sorted(extra)[:5], sorted(missing)[:5], bad[:5])
"
# Roll the image back as UPGRADING says: match the checkout to the pin.
git -C "$REPO" archive "$TAG" | tar -x -C "$OLD"
export KARAKOS_VERSION="$TAG"
write_override "$OLD_CLAUDE" "$FIXED"
open_perms
"${DC[@]}" up -d --pull never
smoke_wait "old image answering /health on restored volumes" 180 api_healthy
assert_eq "$(dbq "select count(*) from message_queue")" "$SEED_ROWS" "seeded rows are back"
[ "$("${DC[@]}" exec -T karakos sh -c 'test -e data/.schema-version && echo yes || echo no')" = no ] \
  || { echo "FAIL: a schema stamp survived the restore" >&2; exit 1; }

# ---- cleanup is the EXIT trap: guard, then `down -v` scoped to the project -----------
echo "upgrade smoke [$TAG]: PASS"
