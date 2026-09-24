#!/usr/bin/env bash
# Install the systemd user units in systemd/ into ~/.config/systemd/user.
#
#   scripts/install-units.sh          show what would change, change nothing
#   scripts/install-units.sh --yes    copy, reload systemd, enable the timers,
#                                     the bot and the shelf
#
# The units run this checkout (%h/Projects/price-intelligence), not an
# installed copy. The tunnel unit is copied but never enabled: it rewrites
# PI_WEB_URL, and the shelf is reached through `tailscale serve` instead.
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
dest="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
apply=0
[[ "${1:-}" == "--yes" ]] && apply=1

mkdir -p "$dest"
changed=()
for unit in "$here"/systemd/price-intelligence*.service "$here"/systemd/price-intelligence*.timer; do
    name="$(basename "$unit")"
    if ! cmp -s "$unit" "$dest/$name"; then
        changed+=("$name")
        if [[ -e "$dest/$name" ]]; then
            diff -u "$dest/$name" "$unit" || true
        else
            echo "new: $name"
        fi
    fi
done

if [[ $apply -eq 0 ]]; then
    if [[ ${#changed[@]} -eq 0 ]]; then
        echo "all units are already up to date"
    else
        echo
        echo "${#changed[@]} unit(s) differ: ${changed[*]}"
        echo "run again with --yes to install them"
    fi
    exit 0
fi

for name in "${changed[@]}"; do
    cp "$here/systemd/$name" "$dest/$name"
done
# Reloaded and enabled even when every file was already in place: a run that
# copied them and then failed here (no user bus, say) would otherwise leave a
# second run saying "up to date" and enabling nothing.
systemctl --user daemon-reload
systemctl --user enable \
    price-intelligence.timer price-intelligence-health.timer \
    price-intelligence-prune.timer price-intelligence-backup.timer \
    price-intelligence-digest.timer price-intelligence-subscriptions.timer \
    price-intelligence-bot.service price-intelligence-web.service
if [[ ${#changed[@]} -eq 0 ]]; then
    echo "all units were already up to date; reloaded and enabled"
else
    echo "installed: ${changed[*]}"
    echo "restart what changed and is running, e.g.:"
    echo "  systemctl --user restart price-intelligence-bot price-intelligence-web"
fi
