#!/usr/bin/env bash
# One-shot Raspberry Pi setup (64-bit Raspberry Pi OS).
# Installs deps, fetches xvf_host, grants USB access, installs the
# systemd service, and starts the headless autotune + web remote.
set -euo pipefail
cd "$(dirname "$0")/.."

sudo apt-get update
sudo apt-get install -y git python3-venv libportaudio2 libusb-1.0-0

# Prebuilt control binary where one exists (aarch64); on 32-bit ARM
# (Pi Zero W etc.) this is skipped and the native USB backend is used.
./get_xvf_host.sh

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# USB permission for the XVF3800 control interface (no sudo needed after this)
echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="2886", ATTRS{idProduct}=="001a", MODE="0666"' \
  | sudo tee /etc/udev/rules.d/99-xvf3800.rules >/dev/null
sudo udevadm control --reload-rules
sudo udevadm trigger

sed "s|@DIR@|$(pwd)|g; s|@USER@|$USER|g" pi/xvf-autotune.service \
  | sudo tee /etc/systemd/system/xvf-autotune.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now xvf-autotune

# On a PipeWire desktop, WirePlumber fights raw-ALSA capture; hand the
# XVF3800 to this app. No-op on headless/non-PipeWire installs.
if command -v wireplumber >/dev/null 2>&1; then
  echo
  echo "PipeWire detected — freeing the XVF3800 for exclusive capture…"
  ./pi/fix-pipewire.sh || echo "  (fix-pipewire had trouble; see its output)"
fi

echo
echo "Done. Web remote: http://$(hostname -I | awk '{print $1}'):8380"
echo "Logs: journalctl -u xvf-autotune -f"
