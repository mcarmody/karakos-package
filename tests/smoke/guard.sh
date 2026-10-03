#!/usr/bin/env bash
# Safety guard shared by tests/smoke/*.sh (7.3a). Sourced, never run.
#
# smoke_guard refuses (exit 97) unless the compose project name matches
# ^karakos-(smoke|up)- and the work directory is under the system temp
# directory. Every `docker compose ... down -v` in these scripts is preceded by
# a call to it; tests/test_release_tests_meta.py scans for that. Nothing here
# reads HOME, a real compose project or the user's ~/.claude.

smoke_guard() {
  local project="${SMOKE_PROJECT:-}" work="${SMOKE_WORK:-}" tmp real
  if ! [[ "$project" =~ ^karakos-(smoke|up)- ]]; then
    echo "smoke guard: refusing, compose project '$project' does not match ^karakos-(smoke|up)-" >&2
    exit 97
  fi
  tmp="$(realpath "${TMPDIR:-/tmp}" 2>/dev/null || echo /tmp)"
  if [ -z "$work" ] || [ ! -d "$work" ]; then
    echo "smoke guard: refusing, work directory '$work' does not exist" >&2
    exit 97
  fi
  real="$(realpath "$work")"
  case "$real" in
    "$tmp"/?*) ;;
    *) echo "smoke guard: refusing, work directory '$real' is not under the system temp directory" >&2
       exit 97 ;;
  esac
}

# Free localhost TCP port (python is already required by these scripts).
smoke_free_port() {
  python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
}

# Poll `$@` until it succeeds or SMOKE_TIMEOUT (default 60) seconds pass.
smoke_wait() {
  local what="$1" timeout="$2"; shift 2
  local end=$(( $(date +%s) + timeout ))
  until "$@" >/dev/null 2>&1; do
    if [ "$(date +%s)" -ge "$end" ]; then echo "timed out after ${timeout}s waiting for: $what" >&2; return 1; fi
    sleep 2
  done
}
