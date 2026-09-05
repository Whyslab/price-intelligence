#!/usr/bin/env bash
# Put the shelf on an https address, without a domain and without paying.
#
# Telegram will only run a page inside itself over https, and the hearts on the
# shelf exist only there — `pi/webauth.py` verifies a Telegram signature on every
# request and there is no exception for a local address. So until there is a
# domain, this is what makes the shelf openable at all from a phone.
#
# What it costs: the address changes every time cloudflared restarts. That is
# the whole difference from a named tunnel, and it is why this writes the new
# address into .env and restarts the bot rather than expecting anybody to
# remember to. A reader who kept the old link gets nothing; there is no fixing
# that without a domain.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"
PORT="${PI_WEB_PORT:-8000}"
LOG="$(mktemp -t pi-tunnel.XXXXXX.log)"

command -v cloudflared >/dev/null || {
  echo "cloudflared not installed: sudo pacman -S cloudflared" >&2
  exit 1
}

cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:${PORT}" >"$LOG" 2>&1 &
TUNNEL=$!
trap 'kill "$TUNNEL" 2>/dev/null || true' EXIT

# cloudflared prints the address a second or two after it starts, in a banner.
# Waiting for the line rather than sleeping a fixed time: the pause is the
# handshake, and it is not the same length twice.
URL=""
for _ in $(seq 60); do
  URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)"
  [ -n "$URL" ] && break
  sleep 1
done
[ -n "$URL" ] || { echo "no address after 60s; cloudflared said:" >&2; cat "$LOG" >&2; exit 1; }

echo "shelf is at $URL"

# Replace the line if it is there, append it if it is not. Asserting the result
# rather than trusting sed: a pattern that does not match fails silently, and a
# silently unchanged PI_WEB_URL is a bot whose button opens the previous run.
touch "$ENV_FILE"
if grep -q '^PI_WEB_URL=' "$ENV_FILE"; then
  sed -i "s|^PI_WEB_URL=.*|PI_WEB_URL=$URL|" "$ENV_FILE"
else
  printf 'PI_WEB_URL=%s\n' "$URL" >>"$ENV_FILE"
fi
grep -qF "PI_WEB_URL=$URL" "$ENV_FILE" || { echo "failed to write PI_WEB_URL" >&2; exit 1; }

systemctl --user restart price-intelligence-bot.service 2>/dev/null \
  || echo "bot not running under systemd — restart it yourself to pick up the address" >&2

echo "PI_WEB_URL written; the bot's shelf button now opens it as a mini-app."
echo "Ctrl-C ends the tunnel and the address stops working."
wait "$TUNNEL"
