#!/usr/bin/env bash
# Pre-commit hook for Karakos repository
# Checks staged files against protected paths configuration
# Called automatically by git before each commit

set -euo pipefail

WORKSPACE_ROOT="${WORKSPACE_ROOT:-.}"
PYTHON="${PYTHON:-python3}"

# Optional pre-push coupling check (off by default). Opt in with:
#   system/install-hooks.sh --install-pre-push
if [ "${1:-}" = "--install-pre-push" ]; then
    hook="$WORKSPACE_ROOT/.git/hooks/pre-push"
    printf '#!/usr/bin/env bash\nexec "%s/system/check-coupling.sh"\n' "$WORKSPACE_ROOT" > "$hook"
    chmod +x "$hook"
    echo "installed pre-push coupling check: $hook"
    exit 0
fi

# Run protected paths checker
"$PYTHON" "$WORKSPACE_ROOT/system/check-protected-paths.py" --staged

# Secrets check (forbidden paths + content scan). KARAKOS_SECRET_SCAN=off skips content only.
"$PYTHON" "$WORKSPACE_ROOT/system/check-secrets.py" --staged
