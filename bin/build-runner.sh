#!/usr/bin/env bash
# build-runner.sh -- the remote half of a queued build (spec 3.3). The dispatcher
# pipes this file to `bash -s -- <args>` over ssh; the remote needs only bash,
# git, claude and (builds) gh. Nothing here references a path of the dispatching
# host, and no secret is forwarded: git and gh use the remote's own credentials.
#
# Args: KIND ID REPO TARGET_BRANCH MODEL TIMEOUT_S COST_USD DIR BRANCH_PREFIX REPO_URL WORKDIR
#   DIR holds brief.md and system.md (written by the dispatcher by content).
# Contract:
#   * DIR/run.pid (pid, pgid, then the process start time, one per line) exists
#     BEFORE claude starts. The kill script compares the start time before it
#     signals, so a recycled pid is never signalled.
#   * claude runs in its own process group under `timeout <TIMEOUT_S - 60>`.
#   * A pre-push hook refuses any ref but refs/heads/<prefix>*, the target
#     branch itself, deletions and non-fast-forward pushes.
#   * On TERM/INT/HUP, or when claude ends non-zero, work in an unclean or
#     advanced worktree is committed (explicit identity) and pushed to
#     <prefix>salvage-<id>. A failed salvage push keeps the worktree.
#   * The last stdout line is exactly one JSON object:
#     {"karakos_build_result":{"exit":N,"branch":"..","pushed_sha":"..","pr_url":"..","salvaged":true|false}}
#   * DIR/exit is written last.
# The body is one function and the script ends on a single `main` line, so bash
# -s never reads script text that a child could have consumed from stdin.

main() {
set -u
KIND="${1:-}"; ID="${2:-}"; REPO="${3:-}"; BRANCH="${4:-}"; MODEL="${5:-sonnet}"
TIMEOUT_S="${6:-3600}"; COST="${7:-75}"; DIR="${8:-}"; PREFIX="${9:-}"
REPO_URL="${10:-}"; WORKDIR="${11:-$HOME/karakos-builds}"
[[ -n "$REPO_URL" ]] || REPO_URL='https://github.com/{repo}.git'
DIR="${DIR/#\~/$HOME}"; WORKDIR="${WORKDIR/#\~/$HOME}"
WORK="$DIR/work"
SAFE='^[A-Za-z0-9][A-Za-z0-9_./-]*$'
BRANCH_PUSHED=""; SALVAGED=false; INTERRUPTED=""; IN_SIGNAL=""; CPID=""

result_line() {
    printf '{"karakos_build_result":{"exit":%s,"branch":"%s","pushed_sha":"%s","pr_url":"%s","salvaged":%s}}\n' \
        "$1" "${2:-}" "${3:-}" "${4:-}" "${SALVAGED}"
}
fail() {  # fail EXIT MESSAGE
    echo "build-runner: $2"
    mkdir -p "$DIR" 2>/dev/null; echo "$1" > "$DIR/exit" 2>/dev/null
    result_line "$1" "" "" ""
    exit "$1"
}

[[ "$KIND" == build || "$KIND" == review ]] || fail 2 "bad kind"
[[ "$ID" =~ ^bq-[0-9a-f]+$ ]] || fail 2 "bad id"
[[ -z "$REPO" || "$REPO" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || fail 2 "bad repo"
[[ -z "$BRANCH" || "$BRANCH" =~ $SAFE ]] || fail 2 "bad target branch"
[[ -z "$PREFIX" || "$PREFIX" =~ $SAFE ]] || fail 2 "bad branch prefix"
[[ "$TIMEOUT_S" =~ ^[0-9]+$ && "$COST" =~ ^[0-9.]+$ ]] || fail 2 "bad limit"
[[ -n "$DIR" ]] || fail 2 "no dir"
[[ -f "$DIR/brief.md" && -f "$DIR/system.md" ]] || fail 2 "brief.md or system.md missing in $DIR"

# --- worktree --------------------------------------------------------------
START_SHA=""; WBRANCH=""
if [[ -n "$REPO" ]]; then
    CACHE="$WORKDIR/repos/${REPO//\//-}"
    PAT='{repo}'
    URL="${REPO_URL//"$PAT"/$REPO}"
    mkdir -p "$WORKDIR/repos"
    # one run at a time touches the shared cache clone (clone, fetch, worktree add)
    LOCK="$WORKDIR/repos/.lock-${REPO//\//-}"
    for _i in $(seq 1 1200); do
        mkdir "$LOCK" 2>/dev/null && break
        [[ -n "$(find "$LOCK" -maxdepth 0 -mmin +5 2>/dev/null)" ]] && rmdir "$LOCK" 2>/dev/null
        sleep 0.1
    done
    trap 'rmdir "$LOCK" 2>/dev/null' EXIT
    if [[ ! -e "$CACHE/.git" ]]; then
        git clone --quiet "$URL" "$CACHE" </dev/null || fail 3 "clone failed"
    fi
    git -C "$CACHE" fetch --quiet origin </dev/null || fail 3 "fetch failed"
    WBRANCH="${PREFIX}${ID}"
    git -C "$CACHE" worktree prune </dev/null
    git -C "$CACHE" worktree add --quiet -B "$WBRANCH" "$WORK" "origin/$BRANCH" </dev/null \
        || fail 3 "worktree failed for $BRANCH"
    START_SHA=$(git -C "$WORK" rev-parse HEAD)
    git -C "$CACHE" config extensions.worktreeConfig true
    mkdir -p "$DIR/hooks"
    cat > "$DIR/hooks/pre-push" <<HOOK
#!/bin/bash
zero=0000000000000000000000000000000000000000
while read -r lref lsha rref rsha; do
    case "\$rref" in "refs/heads/${BRANCH}") echo "pre-push: refusing the target branch" >&2; exit 1;; esac
    case "\$rref" in "refs/heads/${PREFIX}"*) ;; *) echo "pre-push: refusing \$rref" >&2; exit 1;; esac
    [[ "\$lsha" == "\$zero" ]] && { echo "pre-push: refusing a delete" >&2; exit 1; }
    if [[ "\$rsha" != "\$zero" ]]; then
        git merge-base --is-ancestor "\$rsha" "\$lsha" 2>/dev/null \
            || { echo "pre-push: refusing a non-fast-forward push" >&2; exit 1; }
    fi
done
exit 0
HOOK
    chmod +x "$DIR/hooks/pre-push"
    git -C "$WORK" config --worktree core.hooksPath "$DIR/hooks"
    rmdir "$LOCK" 2>/dev/null
    trap - EXIT
else
    mkdir -p "$WORK"
fi

# --- prompt ----------------------------------------------------------------
BRIEF=$(cat "$DIR/brief.md")
if [[ "$KIND" == build ]]; then
    PROMPT="Build the following specification. Push a branch and file a PR when done.

Target branch: ${BRANCH}
Branch prefix: ${PREFIX}

---
$BRIEF"
    TOOLS="Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,NotebookEdit"
else
    PROMPT="Review the following specification or code change. Apply the review criteria from your system prompt.

---
$BRIEF"
    TOOLS="Bash,Read,Glob,Grep,Write,WebFetch"
fi
SYSTEM=$(cat "$DIR/system.md")
T=$(( TIMEOUT_S - 60 )); (( T < 1 )) && T=1

# --- signals ---------------------------------------------------------------
pstart() {  # process start time: /proc starttime (ticks), else ps lstart; empty when gone
    if [ -r "/proc/$1/stat" ]; then
        sed 's/^.*) //' "/proc/$1/stat" 2>/dev/null | cut -d' ' -f20
    else
        ps -o lstart= -p "$1" 2>/dev/null | tr -s ' ' | sed 's/^ //'
    fi
}
stop_group() {  # TERM the claude group, only when it is provably ours
    if [[ "$CPID" =~ ^[0-9]+$ ]] && (( CPID > 1 )) && kill -0 "$CPID" 2>/dev/null; then
        kill -TERM -- "-$CPID" 2>/dev/null || true
    fi
}
on_signal() {
    INTERRUPTED=1
    [[ -n "$IN_SIGNAL" ]] && return
    IN_SIGNAL=1
    stop_group
    IN_SIGNAL=""
}
trap 'on_signal' TERM INT HUP

# --- run -------------------------------------------------------------------
: > "$DIR/stream.jsonl"
set -m
(
    while [[ ! -e "$DIR/go" ]]; do sleep 0.05; done
    cd "$WORK" || exit 97
    exec timeout "$T" claude -p "$PROMPT" \
        --model "$MODEL" --max-turns 200 --system-prompt "$SYSTEM" \
        --allowedTools "$TOOLS" --output-format stream-json --verbose \
        --dangerously-skip-permissions --max-budget-usd "$COST"
) </dev/null >"$DIR/stream.jsonl" 2>"$DIR/claude.err" &
CPID=$!
MYPG=$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')
CPG=$(ps -o pgid= -p "$CPID" 2>/dev/null | tr -d ' ')
if [[ "$CPG" != "$CPID" || "$CPG" == "$MYPG" ]]; then
    kill -KILL "$CPID" 2>/dev/null
    fail 4 "claude did not get its own process group"
fi
CSTART=$(pstart "$CPID")
if [[ -z "$CSTART" ]]; then
    kill -KILL "$CPID" 2>/dev/null
    fail 4 "could not read the start time of claude"
fi
printf '%s\n%s\n%s\n' "$CPID" "$CPG" "$CSTART" > "$DIR/run.pid"
: > "$DIR/go"
tail -n +1 -s 0.1 -f --pid="$CPID" "$DIR/stream.jsonl" 2>/dev/null &
TPID=$!
RC=0
while :; do
    wait "$CPID"; RC=$?
    if (( RC >= 128 )) && kill -0 "$CPID" 2>/dev/null; then continue; fi
    break
done
wait "$TPID" 2>/dev/null
trap - TERM INT HUP

# --- salvage ---------------------------------------------------------------
DIRTY=""; AHEAD=0; HEAD_SHA=""
if [[ -n "$START_SHA" && -d "$WORK" ]]; then
    DIRTY=$(git -C "$WORK" status --porcelain 2>/dev/null | head -1)
    AHEAD=$(git -C "$WORK" rev-list --count "$START_SHA..HEAD" 2>/dev/null || echo 0)
fi
OUT_BRANCH=$(git -C "$WORK" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
if [[ "$KIND" == build && ( -n "$INTERRUPTED" || "$RC" != 0 ) && ( -n "$DIRTY" || "${AHEAD:-0}" -gt 0 ) ]]; then
    SB="${PREFIX}salvage-${ID}"
    git -C "$WORK" add -A </dev/null 2>/dev/null
    git -C "$WORK" -c user.name=karakos-salvage -c user.email=salvage@localhost.invalid \
        commit --quiet --no-verify -m "salvage: $ID (interrupted or failed run)" </dev/null 2>/dev/null
    if git -C "$WORK" push --quiet origin "HEAD:refs/heads/$SB" </dev/null 2>"$DIR/salvage.err"; then
        SALVAGED=true; OUT_BRANCH="$SB"
    else
        echo "build-runner: salvage push failed; worktree kept at $WORK"
    fi
fi

# --- report ----------------------------------------------------------------
PUSHED=""
if [[ -n "$START_SHA" && -n "$OUT_BRANCH" && "$OUT_BRANCH" != HEAD ]]; then
    PUSHED=$(git -C "$WORK" ls-remote origin "refs/heads/$OUT_BRANCH" </dev/null 2>/dev/null | cut -f1 | head -1)
fi
PR=$(grep -o 'https://[^[:space:]"\\<>)]*/pull/[0-9]*' "$DIR/stream.jsonl" 2>/dev/null | tail -1)
KEEP=""
if [[ -n "$START_SHA" ]]; then
    DIRTY2=$(git -C "$WORK" status --porcelain 2>/dev/null | head -1)
    HEAD_SHA=$(git -C "$WORK" rev-parse HEAD 2>/dev/null)
    if [[ -n "$DIRTY2" ]] || { [[ "$HEAD_SHA" != "$START_SHA" ]] && [[ "$PUSHED" != "$HEAD_SHA" ]]; }; then
        KEEP=1
    fi
    [[ -z "$KEEP" ]] && git -C "$CACHE" worktree remove --force "$WORK" </dev/null 2>/dev/null
fi
echo "$RC" > "$DIR/exit"
result_line "$RC" "$OUT_BRANCH" "$PUSHED" "$PR"
exit "$RC"
}
main "$@"; exit $?
