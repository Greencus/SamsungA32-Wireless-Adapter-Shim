#!/bin/bash
# wifishim-setup.sh — per-boot interface setup for the Wi-Fi shim.
# Run once per boot with sudo:  sudo pc/wifishim-setup.sh
#
# LAYOUT (v2): each role gets its OWN radio. Sharing one phy between the
# NM-managed STA and mon1 fails: NM/wpa_supplicant pins the phy, every
# `iw ... set channel` on mon1 dies EBUSY, hwsim tunes nothing, and all TX
# is silently dropped (socket accepts, tx_packets stays 0). Symptoms of the
# old shared layout: "Device or resource busy (-16)" + "TX self-test STUCK".
#   phy #1 (first hwsim radio):  mon1 alone (injection hops freely)
#   phy #2 (second hwsim radio): wlan1 STA (NM scans freely) + later the AP
#      (hostapd AP + STA share phy2 in later stages: STA connects TO our AP,
#      so same channel by construction — no fight ever).
# Discovers radios dynamically (phy/wlan names shift across boots).
set -u

# SKIP_MODPROBE=1 when the operator manages kernel modules externally
# (e.g. /etc/modules-load.d + a systemd service that must not modprobe).
if [ "${SKIP_MODPROBE:-0}" != "1" ]; then
  modprobe mac80211_hwsim radios=2 2>/dev/null || true
  sleep 1
fi

# --- discover hwsim radios: virtual phys carry the hwsim driver behind
# --- /sys/class/ieee80211/<phy>/device/driver. NOTE: readlink on phy80211
# --- yields a RELATIVE path (../../ieee80211/phyX): compare basenames only.
HWSIM_PHYS=""
for p in /sys/class/ieee80211/*; do
  [ -e "$p/device/driver" ] || continue
  d=$(readlink "$p/device/driver" 2>/dev/null || true)
  case "$d" in
  *mac80211_hwsim*) HWSIM_PHYS="$HWSIM_PHYS $(basename "$p")" ;;
  esac
done
# shellcheck disable=SC2086
set -- $HWSIM_PHYS
P1=${1:-}
P2=${2:-}
[ -n "$P1" ] || {
  echo "FATAL: no hwsim phy found; load the module first:"
  echo "  sudo modprobe mac80211_hwsim radios=2"
  echo "or persist it: sudo pc/systemd/install.sh (unless SKIP_MODPROBE=1)"
  exit 1
}
if [ -z "$P2" ]; then
  echo "warn: single hwsim radio; sharing $P1 (channel fights likely)"
  P2=$P1
fi
echo "hwsim radios: MON=$P1 STA/AP=$P2"

# --- wipe hwsim STA-type ifaces for a clean slate (names shift anyway).
# --- mon1 is (re)created below; skip anything starting with mon/lo here.
for n in /sys/class/net/*; do
  i=$(basename "$n")
  case "$i" in mon* | lo) continue ;; esac
  [ -n "$i" ] || continue
  ph=$(basename "$(readlink "$n/phy80211" 2>/dev/null || true)")
  [ -n "$ph" ] || continue
  case " $HWSIM_PHYS " in
  *" $ph "*) iw dev "$i" del 2>/dev/null && echo "removed stale $i ($ph)" || true ;;
  esac
done

# --- STA for NM on the second radio (keep the familiar wlan1 name so all
# --- docs/commands keep working).
iw phy "$P2" interface add wlan1 type managed 2>/dev/null &&
  echo "created wlan1 (STA) on $P2" || echo "wlan1 already present"

# --- unblock ONLY our virtual radios. Never touch real hardware: a blocked
# --- physical card may reflect user intent (airplane mode etc.); hwsim
# --- radios are fresh every boot and must never start blocked, or mon1 can
# --- never TX (channel sets EBUSY forever). Match by driver path suffix so
# --- phy1 never matches phy10+.
for _p in "$P1" "$P2"; do
  [ -n "$_p" ] || continue
  for _r in /sys/class/rfkill/rfkill*; do
    _d=$(readlink "$_r/device" 2>/dev/null || true)
    case "$_d" in
    *"/ieee80211/$_p")
      _i=${_r##*rfkill}
      rfkill unblock "$_i" 2>/dev/null &&
        echo "unblocked rfkill$_i ($_p)" || true
      ;;
    esac
  done
done
# --- monitor ALONE on the first radio.
iw phy "$P1" interface add mon1 type monitor 2>/dev/null &&
  echo "created mon1 on $P1" || echo "mon1 already present"
# Monitor must be UP for inject/sniff; NM only sees UP devices, so bring it
# up BEFORE telling NM to ignore it.
ip link set mon1 up 2>/dev/null || echo "warn: could not bring mon1 up"
sleep 1
nmcli device set mon1 managed no >/dev/null 2>&1 &&
  echo "mon1 unmanaged in NM" || echo "warn: nmcli mon1 unmanaged failed"

# --- record per-boot state for the later stages
mkdir -p /run/wifishim
echo "$P2" >/run/wifishim/sta_phy
echo "$P2" >/run/wifishim/ap_phy
echo "$P1" >/run/wifishim/mon_phy
echo "setup complete"
