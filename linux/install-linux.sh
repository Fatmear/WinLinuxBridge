#!/usr/bin/env bash
# WinLinuxBridge - Linux side setup. Runs as the login user, no sudo, no system packages.
#
# Creates the delivery folder layout, fixes permissions, normalises CRLF in the
# uploaded scripts (they may come from a Windows checkout) and puts `wl` on PATH
# for interactive shells. Idempotent: safe to run after every upload.
#
# ASCII comments only.

set -euo pipefail

ROOT="${WL_ROOT:-$HOME/WinLinuxBridge}"
PORT="${WL_AGENT_PORT:-18080}"

echo "root: $ROOT"

mkdir -p "$ROOT/config" "$ROOT/logs" \
         "$ROOT/shared/from_win" "$ROOT/shared/to_win" "$ROOT/shared/shared"

# Windows checkouts may carry CRLF; bash and sed both choke on it.
# tr is used instead of sed because GNU sed does not reliably expand \r.
for f in "$ROOT/linux/wl" "$ROOT/tools/verify.sh"; do
    [ -f "$f" ] || continue
    if grep -q "$(printf '\r')" "$f" 2>/dev/null; then
        tr -d '\r' < "$f" > "$f.tmp" && mv "$f.tmp" "$f"
        echo "normalised line endings: $f"
    fi
done
chmod 755 "$ROOT/linux/wl" 2>/dev/null || true
[ -f "$ROOT/tools/verify.sh" ] && chmod 755 "$ROOT/tools/verify.sh" || true

ENV_FILE="$ROOT/config/bridge.env"
if [ ! -f "$ENV_FILE" ]; then
    umask 077
    cat > "$ENV_FILE" <<EOF
WL_AGENT_TOKEN=''
WL_AGENT_PORT='$PORT'
EOF
    echo "created placeholder $ENV_FILE (the Windows installer fills the token)"
fi
chmod 600 "$ENV_FILE"
chmod 700 "$ROOT/config"

# Make `wl` available in interactive shells.
LINE='export PATH="$HOME/WinLinuxBridge/linux:$PATH"'
if ! grep -qF "$LINE" "$HOME/.bashrc" 2>/dev/null; then
    printf '\n# WinLinuxBridge\n%s\n' "$LINE" >> "$HOME/.bashrc"
    echo "added wl to PATH in ~/.bashrc"
fi

TOKEN_STATE="missing"
if grep -q "^WL_AGENT_TOKEN='.\+'" "$ENV_FILE" 2>/dev/null; then
    TOKEN_STATE="present"
fi

echo "python3: $(command -v python3 || echo MISSING) $(python3 --version 2>/dev/null || true)"
echo "curl   : $(command -v curl || echo MISSING)"
echo "token  : $TOKEN_STATE"
echo "lane dirs: $(find "$ROOT/shared" -maxdepth 1 -mindepth 1 -type d | wc -l) created"
"$ROOT/linux/wl" status || true
echo "install-linux.sh: done"
