# Architecture: Wi-Fi → USB Ethernet router (PRELIMINARY — pending full audit)

Device: Galaxy A32 SM-A325M, Android 13, kernel 4.14.352, SoC MT6769, Magisk 30.4, SELinux Enforcing.
Upstream: `wlan0` DHCP client (observed `192.168.1.3/24`, gw `192.168.1.1`).

## 1. Why not stock USB tethering

Stock Android USB tethering (`com.android.networkstack.tethering`) always applies
NAT (`tetherctrl_nat_POSTROUTING` + MASQUERADE) and runs its own DHCP/DNS on
`192.168.x.0/24` with the phone as gateway. The requirement forbids NAT for PC
traffic, so stock tethering is unsuitable. We keep the stock chains untouched and
add our own isolated rules.

## 2. USB Ethernet transport: RNDIS (only option)

Kernel ConfigFS gadget (`/config/usb_gadget/g1`, UDC `musb-hdrc`) supports:

- `CONFIG_USB_CONFIGFS_RNDIS=y` → `functions/rndis.gs0` (verified creatable)
- `CONFIG_USB_CONFIGFS_NCM/ECM/EEM` all **unset** → no NCM/ECM choice
- `CONFIG_USB_CONFIGFS_F_HID=y` → HID available (reserved for BT bridge experiments)

Baseline gadget state (restore target): config `b.1` links only
`functions/ffs.adb` (`f1` symlink), VID `04e8`, PID `685d`, strings
SAMSUNG/SAMSUNG_Android/R58R75KGLGV, UDC `musb-hdrc`.
Full baseline: `audit/raw/03_gadget_baseline.txt`.

Plan: add `functions/rndis.gs0` (+ optional `ffs.adb` keep) to config `b.1`
as `f2`, set RNDIS MACs deterministically, bind UDC. Creating the function
exposes a host-side interface (typically `usb0` on phone, `enx*` on Linux PC
via `rndis_host` driver — verify on PC with `lsusb`/dmesg during testing).
Keep ADB linked so root SSH/ADB over USB still works; control channel during
dev is Wi-Fi SSH anyway.

Samsung's `UsbDeviceManager` may fight manual ConfigFS changes (it owns `g1`
via `sys.usb.config`). Mitigation under test: set `sys.usb.config` to include
`rndis` through the framework (`svc usb setFunctions rndis,adb` or
`setprop sys.usb.config rndis,adb`) so the framework, not us, owns the gadget.
If the framework forces NAT-associated setup, fall back to direct ConfigFS
management guarded by watching `sys.usb.state`. RESOLVED 2026-09-07 (test/01_rndis_experiment.log): framework-owned is BROKEN
on this Lineage build (`rndis,adb` links ffs.adb twice, rndis never bound).
The module manages ConfigFS directly: create `functions/rndis.a32`, link as
`configs/b.1/f_rndis`, UDC unbind/rebind. Daemon reconciles each tick in case
UsbDeviceManager rebuilds the gadget on USB events.

## 3. Addressing (example; configurable)

- USB subnet default: `192.168.50.0/24` (configurable; module refuses subnets
  overlapping the current Wi-Fi LAN).
- Phone USB address: `192.168.50.1/24` (static, deterministic).
- PC: `192.168.50.2/24`, gateway `192.168.50.1` — via static PC config or a
  minimal DHCP server on the phone (`/system/bin/dnsmasq` exists; static-only
  also acceptable — decision: ship static-first, add dnsmasq DHCP only if PC
  UX demands it).
- Upstream router static route: `192.168.50.0/24 via <phone-wifi-ip>`
  (e.g. `via 192.168.1.50`). Documented in README + router config section.

## 4. Routing without NAT (and Android policy routing!)

Android does NOT use the main table default route: default goes via per-net
tables (`wlan0` table, fwmark `ip rule` set, see `audit/raw/*`). Forwarded
packets (not from `lo`) consult the **main** table, so our needs are:

1. `echo 1 > /proc/sys/net/ipv4/ip_forward` (runtime; restore `0` on stop if
   we set it).
2. `ip addr add 192.168.50.1/24 dev <usb_if>` (idempotent: check first).
3. `ip route add 192.168.50.0/24 dev <usb_if> table main` (main-table route
   for the return path `wlan0 → usb`; also add to `wlan0_local`? verify during
   testing whether forwarded wlan0→usb lookups hit main — expected yes).
4. Upstream route for phone-originated PC-destined traffic is covered by (3).
5. **No MASQUERADE/SNAT** for `192.168.50.0/24` in either direction. Verify with
   `iptables -t nat -L` post-test.

Wi-Fi interface name detected dynamically (`ip route show table all` default
dev, or first UP wireless iface) — never hardcode `wlan0` except as fallback.
Wait for usable upstream (address + default route in its table) before
declaring ready; re-check on reconnect.

## 5. Firewall: isolated chains only

Existing filter has `tetherctrl_FORWARD` ending in `DROP`. We insert (once,
idempotent via `iptables -C` checks):

- `iptables -N a32fwd` (own chain), jump from `FORWARD` only if absent.
- `a32fwd`: ACCEPT `usb_if → wlan_if` for subnet src, ACCEPT reverse for
  `ESTABLISHED,RELATED` + new to subnet dst. Nothing else.
- Removal deletes only our jump + chain flush/del. Never flush stock chains.

## 6. Lifecycle (service.sh daemon loop)

`service.sh` (late_start): wait `sys.boot_completed=1`, start monitor loop
(`a32routerd`, pidfile+flock, 5s tick):

- tick: detect USB rndis iface presence (gadget `configured` + iface exists),
  detect Wi-Fi upstream ready, reconcile desired state (addr/routes/rules),
  log state changes only (no logcat spam; own log capped, e.g. 200 lines).
- USB removal: remove addr/routes (rules stay? — keep rules, they match on
  iface name and are harmless; document). Re-add on next insert.
- Wi-Fi down: keep USB L2 up, mark upstream down; re-verify on reconnect.
- Crash guard: never touch gadget on unknown state; `UDC` bind only when our
  function links verified; any failure leaves stock `adb` gadget intact.

## 7. Diagnostics

`a32diag` command prints: gadget state (UDC, linked functions), USB iface
addr/state, Wi-Fi iface + upstream route, `ip_forward`, our iptables rules,
BT/HCI state placeholder, module status, last log lines.

## 8. Open questions for testing

1. Framework-owned (`svc usb setFunctions`) vs direct ConfigFS — test both.
2. PC driver binding for RNDIS on modern kernel (`rndis_host` present?).
3. Forwarded wlan0→usb lookup table behavior — verify with ping both ways.
4. DHCP: static-first; add dnsmasq only if needed.

## CORRECTION 2026-09-07 (tested): main-table route is NOT sufficient

This ROM's policy routing bypasses `main` for our traffic: rule `32000: from
all unreachable` fires before `main` (32766), per-net catch rules shunt
unmarked traffic into the `wlan0` table, and `main` carries no default route.
Result with main-only routing: no replies to the PC, forwarded PC traffic dies
at rule 32000, local phone pings to 192.168.50.0/24 go out `wlan0`.
Implemented fix (`a32routerd` route_ensure, v0.2.2): private table **100** +
`ip rule {from,to} 192.168.50.0/24 lookup 100` at priorities 5000/5001; table
100 holds the connected route plus a default via the live upstream gateway
(parsed each tick from the Wi-Fi interface's own table). `uninstall.sh` removes
both rules and flushes table 100. Verified end-to-end with zero NAT references.

## CORRECTION 2026-09-07 (tested): gadget composition is init-driven, not direct

The "RESOLVED" note in section 2 above is superseded. Direct ConfigFS
mkdir/ln/UDC writes from the module context hit an EPERM kernel guard and each
tick's UDC unbind/rebind flapped host USB. The working path (`a32routerd`
gadget_ensure, v0.2.1+): drive the sanctioned property transition
`setprop sys.usb.config none -> rndis,adb` with bounded waits and let
`init.usb.configfs.rc` create `f1->rndis.gs4` + `f2->ffs.adb` and bind the UDC.
Direct transition `adb -> rndis,adb` fails (symlink EEXIST); the `none` hop is
mandatory. `persist.sys.usb.config` is pinned to `rndis,adb` so the composition
survives reboot/replug (restored to `adb` by `uninstall.sh`).
