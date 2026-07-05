#!/usr/bin/env bash
# Fetch the platform-matched xvf_host control binary from Seeed's repo
# into host_control/<platform>/ (vendored; not committed to this repo).
set -euo pipefail
cd "$(dirname "$0")"

case "$(uname -s) $(uname -m)" in
  Linux\ aarch64|Linux\ arm64) SUB=rpi_64bit ;;
  Linux\ x86_64)               SUB=linux_x86_64 ;;
  Darwin\ arm64)               SUB=mac_arm64 ;;
  *) echo "unsupported platform: $(uname -s) $(uname -m)"; exit 1 ;;
esac

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
git clone --depth 1 --filter=blob:none --sparse \
  https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY.git "$TMP"
git -C "$TMP" sparse-checkout set "host_control/$SUB"
mkdir -p host_control
rm -rf "host_control/$SUB"
cp -r "$TMP/host_control/$SUB" host_control/
chmod +x "host_control/$SUB/xvf_host"
echo "installed: host_control/$SUB/xvf_host"
