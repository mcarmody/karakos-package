#!/usr/bin/env bash
# discord-notify.sh — Post a message to a Discord channel
#
# Usage:
#   discord-notify.sh general "System update complete"
#   discord-notify.sh signals --file /path/to/body.txt
#   discord-notify.sh signals --stdin <<'EOF'
#   a body with quotes, (parentheses) and `backticks`
#   EOF
#
# Prose with quotes, parentheses or backticks belongs in --file or --stdin, not argv.
# A body over 1900 characters is posted as several messages, split on line breaks.

set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib/paths.sh"

usage() {
    echo 'Usage: discord-notify.sh CHANNEL_NAME "message" | --file PATH | --stdin' >&2
}

CHANNEL_NAME="${1:-}"
if [[ -z "$CHANNEL_NAME" ]]; then
    usage
    exit 1
fi
shift

MESSAGE=""
case "${1:-}" in
    --file)
        if [[ -z "${2:-}" || ! -r "${2:-}" ]]; then
            echo "discord-notify: --file needs a readable path" >&2
            exit 1
        fi
        MESSAGE="$(cat "$2")"
        ;;
    --stdin)
        MESSAGE="$(cat)"
        ;;
    "")
        ;;
    *)
        MESSAGE="$1"
        ;;
esac
if [[ -z "$MESSAGE" ]]; then
    usage
    exit 1
fi

# Resolve channel name to ID. The name and path are passed as arguments, never
# spliced into the Python source.
CHANNEL_ID="$CHANNEL_NAME"
if [[ -f "$WORKSPACE_ROOT/config/channels.json" ]]; then
    CHANNEL_ID=$(python3 - "$WORKSPACE_ROOT/config/channels.json" "$CHANNEL_NAME" <<'PY' 2>/dev/null || echo "$CHANNEL_NAME"
import json, sys
path, name = sys.argv[1], sys.argv[2]
cfg = json.load(open(path))
print(cfg.get("channels", {}).get(name, {}).get("id", name))
PY
)
fi

# Bot token. DISCORD_NOTIFY_BOT_TOKEN wins when set (used by the tests); otherwise
# the first available token from the agent registry.
BOT_TOKEN="${DISCORD_NOTIFY_BOT_TOKEN:-}"
if [[ -z "$BOT_TOKEN" ]]; then
    REGISTRY_PY="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../lib" && pwd)/registry.py"
    BOT_TOKEN=$(python3 "$REGISTRY_PY" --workspace "$WORKSPACE_ROOT" legacy 2>/dev/null | python3 -c "
import json, os, sys
cfg = json.load(sys.stdin)
for name, info in cfg.get('agents', {}).items():
    env_var = info.get('discord_bot_token_env', '')
    if env_var:
        token = os.environ.get(env_var, '')
        if token:
            print(token)
            break
" 2>/dev/null || echo "")
fi

if [[ -z "$BOT_TOKEN" ]]; then
    echo "Error: no Discord bot token available" >&2
    exit 1
fi

# Split into chunks of at most 1900 characters on line breaks (a single longer
# line is cut at 1900). Written NUL-terminated to a temp file.
CHUNKS_FILE=$(mktemp)
trap 'rm -f "$CHUNKS_FILE"' EXIT
printf '%s' "$MESSAGE" | python3 -c '
import sys
text = sys.stdin.read()
limit = 1900
chunks = []
cur = None
for line in text.split("\n"):
    while len(line) > limit:
        if cur is not None:
            chunks.append(cur)
            cur = None
        chunks.append(line[:limit])
        line = line[limit:]
    if cur is None:
        cur = line
    elif len(cur) + 1 + len(line) > limit:
        chunks.append(cur)
        cur = line
    else:
        cur += "\n" + line
if cur is not None:
    chunks.append(cur)
chunks = [c for c in chunks if c.strip()]
sys.stdout.write("".join(c + "\0" for c in chunks))
' > "$CHUNKS_FILE"

post_chunk() {
    local body http
    body=$(mktemp)
    http=$(curl -s -o "$body" -w '%{http_code}' -X POST \
        "https://discord.com/api/v10/channels/$CHANNEL_ID/messages" \
        -H "Authorization: Bot $BOT_TOKEN" \
        -H "Content-Type: application/json" \
        -d "$(jq -cn --arg content "$1" '{content: $content}')" || true)
    if [[ "$http" != 2* ]]; then
        echo "discord-notify: Discord answered HTTP ${http:-none}: $(head -c 300 "$body")" >&2
        rm -f "$body"
        return 1
    fi
    rm -f "$body"
}

posted=0
while IFS= read -r -d '' chunk; do
    post_chunk "$chunk" || exit 1
    posted=$((posted + 1))
done < "$CHUNKS_FILE"

if [[ "$posted" -eq 0 ]]; then
    echo "discord-notify: message was empty after trimming, nothing posted" >&2
    exit 1
fi

echo "Posted to #$CHANNEL_NAME"
