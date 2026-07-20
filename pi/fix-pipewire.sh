#!/usr/bin/env bash
# On a PipeWire desktop (Raspberry Pi OS Bookworm etc.), WirePlumber
# auto-manages the XVF3800 and fights any raw-ALSA (hw:X,0) capture,
# producing "Input/output error" and repeated capture stalls. This drops
# in a rule telling WirePlumber to leave the XVF3800 alone, so the app's
# exclusive hw capture works. Run as the normal login user (NOT via sudo)
# so the `systemctl --user` restart targets the right session.
set -euo pipefail

MATCH="XVF3800"

if ! command -v wireplumber >/dev/null 2>&1; then
    echo "WirePlumber not found — nothing to do (not a PipeWire system)."
    exit 0
fi

WP_VER=$(wireplumber --version 2>/dev/null \
         | grep -oE '[0-9]+\.[0-9]+' | head -1)
WP_MAJMIN=${WP_VER:-0.4}
echo "WirePlumber $WP_MAJMIN detected."

# 0.5+ uses SPA-JSON conf drop-ins; 0.4 uses Lua.
if [ "$(printf '%s\n0.5\n' "$WP_MAJMIN" | sort -V | head -1)" = "0.5" ]; then
    DIR=/etc/wireplumber/wireplumber.conf.d
    FILE="$DIR/50-xvf3800-disable.conf"
    sudo mkdir -p "$DIR"
    sudo tee "$FILE" >/dev/null <<EOF
# Let the deadplug app own the XVF3800 via raw ALSA; keep PipeWire off it.
monitor.alsa.rules = [
  {
    matches = [ { device.name = "~alsa_card.*${MATCH}.*" } ]
    actions = { update-props = { device.disabled = true } }
  }
]
EOF
else
    DIR=/etc/wireplumber/main.lua.d
    FILE="$DIR/50-xvf3800-disable.lua"
    sudo mkdir -p "$DIR"
    sudo tee "$FILE" >/dev/null <<EOF
-- Let the deadplug app own the XVF3800 via raw ALSA; keep PipeWire off it.
table.insert(alsa_monitor.rules, {
  matches = {{ { "device.name", "matches", "*${MATCH}*" } }},
  apply_properties = { ["device.disabled"] = true },
})
EOF
fi
echo "Wrote $FILE"

echo "Restarting PipeWire/WirePlumber (user session)…"
systemctl --user restart wireplumber pipewire pipewire-pulse 2>/dev/null || \
    echo "  (couldn't restart --user services; a reboot will apply the rule)"
sleep 3

# Verify raw capture now works: expect a file well past the 44-byte header.
CARD=$(arecord -l 2>/dev/null | sed -n "s/^card \([0-9]*\):.*${MATCH}.*/\1/p" \
       | head -1)
if [ -z "$CARD" ]; then
    echo "Could not find the XVF3800 card in 'arecord -l'; is it plugged in?"
    exit 1
fi
echo "Testing capture on hw:${CARD},0 …"
arecord -D "hw:${CARD},0" -f S16_LE -r 16000 -c 2 -d 2 /tmp/xvf_test.wav \
    >/dev/null 2>&1 || true
SZ=$(stat -c%s /tmp/xvf_test.wav 2>/dev/null || echo 0)
rm -f /tmp/xvf_test.wav
if [ "$SZ" -gt 1000 ]; then
    echo "SUCCESS: raw capture works ($SZ bytes in 2 s). Restarting the app…"
    sudo systemctl restart xvf-autotune 2>/dev/null || true
    echo "Done. Watch: journalctl -u xvf-autotune -f"
else
    echo "STILL FAILING ($SZ bytes). The match rule may not have applied."
    echo "Check the device name:  wpctl status  |  pw-cli ls Device"
    exit 1
fi
