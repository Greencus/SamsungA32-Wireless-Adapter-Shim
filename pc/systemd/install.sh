#!/bin/bash
# install.sh — install the A32 Wi-Fi shim as systemd services. Run with sudo
# from the repo root:  sudo pc/systemd/install.sh
#
# Installs: scripts -> /usr/local/lib/a32wifishim/, three units, enables
# them, loads kernel modules NOW and persists them for boot (unless
# SKIP_MODPROBE=1, which leaves all module handling to the operator).
set -u

# Resolve everything from THIS script's directory, so all of these work:
#   sudo pc/systemd/install.sh            (from repo root)
#   sudo /path/to/pc/systemd/install.sh   (absolute, any cwd)
#   cd pc/systemd && sudo ./install.sh    (from inside the dir)
# The old logic assumed repo-root-relative invocation and broke otherwise.
SDIR=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)
PCDIR=$(cd "$SDIR/.." && pwd)
DEST=/usr/local/lib/a32wifishim

# Preflight: fail loudly naming the directory instead of cryptic cp errors.
units=("$SDIR"/a32-wifishim-*.service)
[ -e "${units[0]}" ] || {
  echo "FATAL: unit files not found next to $SDIR/install.sh"
  exit 1
}
for f in wifishim-setup.sh wifishim-scan.py wifishim-conn.py a32ctl.py vhci-relay.py; do
  [ -f "$PCDIR/$f" ] || {
    echo "FATAL: missing script: $PCDIR/$f"
    exit 1
  }
done
[ -f "$SDIR/a32-vhci-relay.service" ] || {
  echo "FATAL: missing unit: $SDIR/a32-vhci-relay.service"
  exit 1
}

for bin in python3 iw nmcli hostapd dnsmasq; do
  command -v "$bin" >/dev/null 2>&1 || {
    echo "FATAL: missing prerequisite: $bin"
    exit 1
  }
done
[ "$(id -u)" = "0" ] || {
  echo "FATAL: run as root (sudo pc/systemd/install.sh)"
  exit 1
}

if [ "${SKIP_MODPROBE:-0}" != "1" ]; then
  modprobe mac80211_hwsim radios=2 2>/dev/null ||
    echo "warn: mac80211_hwsim modprobe failed (continuing)"
  modprobe hci_vhci 2>/dev/null ||
    echo "warn: hci_vhci modprobe failed (only needed for BT relay)"
  printf '%s\n' "mac80211_hwsim" "hci_vhci" \
    >/etc/modules-load.d/a32wifishim.conf
  printf '%s\n' "options mac80211_hwsim radios=2" \
    >/etc/modprobe.d/a32wifishim.conf
  echo "modules loaded now + persisted ( SKIP_MODPROBE=1 to opt out )"
else
  echo "SKIP_MODPROBE=1: operator owns module loading;"
  echo "  need at least: modprobe mac80211_hwsim radios=2"
fi

mkdir -p "$DEST"
cp "$PCDIR/wifishim-setup.sh" "$PCDIR/wifishim-scan.py" \
  "$PCDIR/wifishim-conn.py" "$PCDIR/a32ctl.py" \
  "$PCDIR/vhci-relay.py" "$DEST/"
chmod 755 "$DEST"/wifishim-setup.sh "$DEST"/wifishim-scan.py \
  "$DEST"/wifishim-conn.py "$DEST"/a32ctl.py "$DEST"/vhci-relay.py
echo "scripts -> $DEST"
cp "$SDIR"/a32-wifishim-setup.service "$SDIR"/a32-wifishim-scan.service \
  "$SDIR"/a32-wifishim-conn.service "$SDIR"/a32-vhci-relay.service \
  /etc/systemd/system/
echo "units -> /etc/systemd/system/"
cp "$SDIR"/nm-a32wifishim.conf /etc/NetworkManager/conf.d/a32wifishim.conf
echo "nm conf -> /etc/NetworkManager/conf.d/ (hides mon1/ap1 from applet)"
nmcli general reload >/dev/null 2>&1 || true
systemctl daemon-reload
systemctl enable a32-wifishim-setup.service \
  a32-wifishim-scan.service a32-wifishim-conn.service
# The BT relay goes live immediately (enable --now): connecting takes the
# phone radio, so this is the one unit that acts on install. Stop it any
# time with: sudo systemctl stop a32-vhci-relay.service
systemctl enable --now a32-vhci-relay.service
echo "installed. Start now with:"
echo "  sudo systemctl start a32-wifishim-setup.service"
echo "  sudo systemctl start a32-wifishim-scan.service a32-wifishim-conn.service"
echo "Watch: journalctl -u a32-wifishim-scan -u a32-wifishim-conn -f"
