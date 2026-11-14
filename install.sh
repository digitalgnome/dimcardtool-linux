#!/usr/bin/env bash
# Installs DIMcard Tool for the current user and (with sudo) a udev rule for the CH341A.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"

install -Dm755 "$here/dimcardtool.py" "$HOME/.local/bin/dimcardtool"
install -Dm644 /dev/stdin "$HOME/.local/share/applications/dimcardtool.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=DIMcard Tool
Comment=Check, unlock, back up and restore Digimon DIMcards with a CH341A
Exec=$HOME/.local/bin/dimcardtool
Icon=media-flash
Terminal=false
Categories=Utility;
EOF
echo "Installed ~/.local/bin/dimcardtool and an app-menu entry."

rule=/etc/udev/rules.d/70-ch341a.rules
if [[ -f $rule ]]; then
    echo "udev rule already present: $rule"
else
    echo "Adding udev rule so you can use the CH341A without root (needs sudo):"
    echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="1a86", ATTR{idProduct}=="5512", TAG+="uaccess"' \
        | sudo tee "$rule" >/dev/null
    sudo udevadm control --reload
    sudo udevadm trigger --subsystem-match=usb --attr-match=idVendor=1a86
    echo "Done. Unplug and replug the programmer."
fi
