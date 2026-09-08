#!/system/bin/sh
# uninstall.sh — runs on Magisk Manager removal of a32router.
# Remove only our own configuration; never flush stock chains.
MODDIR=${0%/*}
# shellcheck disable=SC1091
[ -f "$MODDIR/a32router.conf" ] && . "$MODDIR/a32router.conf"

# Prefer pidfiles: pkill -f can match the caller's own command line when the
# pattern appears in it (e.g. remote ssh invocations) and kill the caller.
if [ -f "$MODDIR/state/a32routerd.pid" ]; then
  kill "$(cat "$MODDIR/state/a32routerd.pid" 2>/dev/null)" 2>/dev/null
else
  pkill -f "a32routerd" 2>/dev/null
fi
pkill -f "a32shim" 2>/dev/null
for _p in "$MODDIR/state/a32radvd.pid" "$MODDIR/system/bin/state/a32radvd.pid"; do
  P=$(cat "$_p" 2>/dev/null)
  [ -n "$P" ] && kill "$P" 2>/dev/null
done

USB_IF="${USB_IF:-rndis0}"
# Remove our iptables jump + chain (ignore errors if absent).
# a32test was the manual-testing chain name; a32fwd is the daemon name.
for _c in a32fwd a32test; do
  iptables -D FORWARD -j "$_c" 2>/dev/null
  iptables -F "$_c" 2>/dev/null
  iptables -X "$_c" 2>/dev/null
done

# Remove our main-table route, USB address, pinned neighbor, policy rules,
# and private routing table.
ip route del "$USB_SUBNET" dev "$USB_IF" table main 2>/dev/null
ip addr del "$USB_IP/$USB_PREFIX" dev "$USB_IF" 2>/dev/null
ip neigh del 192.168.50.2 dev "$USB_IF" 2>/dev/null
ip rule del from "$USB_SUBNET" table 100 priority 5000 2>/dev/null
ip rule del to "$USB_SUBNET" table 100 priority 5001 2>/dev/null
ip route flush table 100 2>/dev/null

# Remove our IPv6 policy rules (5000/5001 verified free in v6 baseline),
# private v6 table, and any proxy-NDP entries we added (format varies, so
# parse defensively; only ours should exist on this box).
for _c in a32fwd6; do
  ip6tables -D FORWARD -j "$_c" 2>/dev/null
  ip6tables -F "$_c" 2>/dev/null
  ip6tables -X "$_c" 2>/dev/null
done
ip -6 rule del priority 5000 2>/dev/null
ip -6 rule del priority 5001 2>/dev/null
ip -6 route flush table 100 2>/dev/null
ip -6 neigh show proxy 2>/dev/null | while read -r _a _x _d _rest; do
  if [ "$_x" = "dev" ] && [ -n "$_d" ]; then
    ip -6 neigh del proxy "$_a" dev "$_d" 2>/dev/null
  fi
done
echo 0 >/proc/sys/net/ipv6/conf/all/forwarding 2>/dev/null

# Restore stock USB composition via init (never touch configfs/UDC directly).
# Stock default on this ROM is adb.
setprop persist.sys.usb.config adb 2>/dev/null
setprop sys.usb.config none 2>/dev/null
sleep 3
setprop sys.usb.config adb 2>/dev/null

# Restore ip_forward default (Android default is 0).
echo 0 >/proc/sys/net/ipv4/ip_forward 2>/dev/null

rm -rf /data/adb/a32router "$MODDIR/state"
exit 0
