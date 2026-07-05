#!/usr/bin/env bash
# One-shot Raspberry Pi setup (64-bit Raspberry Pi OS).
# Installs deps, fetches xvf_host, grants USB access, installs the
# systemd service, and starts the headless autotune + web remote.
set -euo pipefail
cd "$(dirname "$0")/.."

sudo apt-get update
sudo apt-get install -y git python3-venv libportaudio2

./get_xvf_host.sh

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install numpy sounddevice

# USB permission for the XVF3800 control interface (no sudo needed after this)
echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="2886", ATTRS{idProduct}=="001a", MODE="0666"' \
  | sudo tee /etc/udev/rules.d/99-xvf3800.rules >/dev/null
sudo udevadm control --reload-rules
sudo udevadm trigger

sed "s|@DIR@|$(pwd)|g; s|@USER@|$USER|g" pi/xvf-autotune.service \
  | sudo tee /etc/systemd/system/xvf-autotune.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now xvf-autotune

echo
echo "Done. Web remote: http://$(hostname -I | awk '{print $1}'):8380"
echo "Logs: journalctl -u xvf-autotune -f"
