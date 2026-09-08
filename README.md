# A32 Wi-Fi Adapter (a32router + wifishim)

Turn a rooted Samsung Galaxy A32 (SM-A325M, LineageOS 22 / Android 15,
MT6769) into a multi-function USB peripheral for a Linux PC, over a single
USB cable — with **no NAT** on PC traffic:

1. **Wi-Fi → USB-Ethernet router** — PC gets routed (not NATed) internet
   through the phone's Wi-Fi via RNDIS (`192.168.50.0/24`).
2. **Bluetooth radio export** — PC apps use the phone's MT6631 radio as a
   local BlueZ controller (`hciX`) via VHCI relay.
3. **Virtual Wi-Fi station** — a genuine NetworkManager-managed `wlan1`
   that browses real surrounding networks and connects to them, with the
   phone's radio doing the RF work and the USB route carrying the bytes. All connections on wlan1 should be set to not autoconnect.
4. **PC Wi-Fi control CLI** — scan/list/connect/forget the phone's Wi-Fi
   directly (`pc/a32ctl.py`).

All four proven live 2026-09-07. See `docs/` for architecture
(`arch-usb-networking`, `arch-bt-hci`, `arch-wifi-shim`, `magisk-notes`)
and `test/` for the full engineering log.

## Requirements

- Phone: Galaxy A32 rooted with Magisk (tested 30.4), Termux + Termux:Boot
  with `sshd` running (control channel), Python 3 in Termux.
- PC: Linux with `rndis_host` + `mac80211_hwsim` drivers (both in-tree),
  NetworkManager + firewalld, Python 3, `hostapd`, `dnsmasq`, `iw`.
  Root/sudo needed for: BT relay, hwsim setup, DHCP/hostapd, systemd units.
- Upstream router: one static route —
  `192.168.50.0/24 via <phone-wifi-ip>` (e.g. `via 192.168.1.3`).
  This is the *only* non-phone, non-PC config in the whole design.

## Install

### 1. Phone module

```bash
# from this directory; phone reachable via adb or Wi-Fi SSH
adb push a32router-v0.2.6.zip /sdcard/
adb shell su -c 'magisk --install-module /sdcard/a32router-v0.2.6.zip'
# or: Magisk app -> Modules -> Install from storage -> pick the zip
```

Reboot (or start services manually per `docs/`). Verify on the phone (root):

```bash
su -c 'cat /data/adb/modules/a32router/system/bin/state/status'
# expect: up rndis0 wlan0
su -c 'getprop sys.usb.state'   # expect: rndis,adb
```

`service.sh` starts `a32routerd` (USB gadget + routing + firewall, 5 s tick)
and `a32shim` (Wi-Fi control `:33601` + BT relay `:33600`, both bound ONLY
to the USB address `192.168.50.1`). Key config in `a32router/a32router.conf`
(`USB_SUBNET`, `USB_IP`, `BT_EXPORT`, `SHIM`, ports).

### 2. PC USB network profile (safe by design)

Create the `a32static` NetworkManager profile (or replicate in your GUI):

- manual `192.168.50.2/24`, gateway `192.168.50.1`
- `ipv4.never-default no`, **`ipv4.route-metric 700`**
  (your Wi-Fi default at 600 always wins while present; the phone becomes
  the automatic backup uplink when Wi-Fi is down — never a hijack),
- `ipv4.dns 8.8.8.8,8.8.4.4`, `connection.zone a32phone`,
  `connection.autoconnect no`, IPv6 disabled.

### 3. Wi-Fi shim: one-time setup, then systemd does the rest

```bash
sudo pc/systemd/install.sh              # installs units, enables them,
                                        # loads mac80211_hwsim (radios=2)
                                        # + hci_vhci NOW and persists both
                                        # for boot (/etc/modules-load.d +
                                        # /etc/modprobe.d); SKIP_MODPROBE=1
                                        # opts out back to manual loading
sudo systemctl start a32-wifishim-setup.service
sudo systemctl start a32-wifishim-scan.service a32-wifishim-conn.service
```

Manual alternative (no installer): `sudo modprobe mac80211_hwsim radios=2`
once per boot, then run `pc/wifishim-setup.sh` + the two daemons by hand.

What this gives you: `wlan1` (NM-managed virtual Wi-Fi) + `mon1`
(injection, NM-ignored) on separate radios; a scan injector advertising
real surrounding APs; a connection orchestrator (local hostapd + DHCP +
phone join). Logs: `journalctl -u a32-wifishim-scan -u a32-wifishim-conn`.
Band policy: 5 GHz-first default (`WIFISHIM_BAND_PREF=2.4` flips it;
override in the conn unit).

## Usage

### Internet via the phone

Plug in USB, activate the profile (`nmcli connection up a32static`).
With PC Wi-Fi on, nothing changes (metric 600 wins). Turn PC Wi-Fi off and
all traffic routes phone → Wi-Fi → internet, returning via your router's
static route. Verify: `ping 1.1.1.1`, `nslookup google.com 8.8.8.8`.

Guarantees: no MASQUERADE/SNAT anywhere on `192.168.50.0/24` (the daemon
warns if any appear); only the isolated `a32fwd` iptables chain + private
routing table 100 + rules 5000/5001 are touched (see
`docs/arch-usb-networking.md` for why the main table alone can't work on
this ROM). Stock `tetherctrl_*` chains untouched. SELinux stays enforcing.

### IPv6 (routed, no NAT — active only when upstream offers it)

Same-LAN model: the PC SLAACs an address in the upstream `/64`, the phone
answers neighbor discovery for it on the LAN (proxy NDP) and forwards.
Nothing is configured while the upstream has no global prefix (fully
dormant, v4 untouched). PC profile needs (one-time, harmless while dormant):

```bash
nmcli connection modify a32static ipv6.method auto ipv6.addr-gen-mode eui64 \
  ipv6.ip6-privacy 0 ipv6.route-metric 700 ipv6.never-default no
```

That gives a stable EUI-64 address (no privacy churn to track), DNS stays
on IPv4 (works for AAAA too), and the v6 default is backup-only like v4.
The phone runs its own minimal RA daemon (`a32radvd.py`, stdlib-only) since
the bundled dnsmasq is v4-only, and tracks ISP prefix changes automatically.

Known environment gates (not bugs in the mechanism): VPN IPv6-leak
protection REJECTs PC-originated global v6 locally (ProtonVPN does this
when the tunnel is v4-only — pause it for v6 tests); cross-link
link-local ping is inherently ambiguous under policy routing (always test
globals); and v6 internet needs the upstream router to actually offer a
default route (some only advertise a prefix). See `test/07_ipv6.log` for
the full proof matrix and forensics.

### Virtual Wi-Fi (`wlan1`)

Browse and connect in the NM applet exactly like a real card. The daemon
takes the radio leg in the background (phone joins the same SSID; throttled
to avoid uplink flapping) and tears the local session down if the phone
can't join — NM shows honest DOWN, never a fake connection. Supported:
open + WPA2-PSK (incl. PSK+SAE transition via the PSK path). Not yet:
SAE-only, EAP, hidden SSIDs, 6 GHz (untested).

Tips from testing: type passwords carefully on first connect (a saved wrong
secret replays forever until the profile is deleted); leave 60 s after
clicking (phone joins take ~10–30 s); rapid re-clicking restarts every
timer and nothing converges.

### Bluetooth radio for the PC (manual by design)

Taking the phone's radio must stay an explicit act, so no unit does it:

```bash
sudo modprobe hci_vhci
sudo python3 pc/vhci-relay.py     # runs until Ctrl-C (never Ctrl-Z)
```

Connecting takes the radio automatically (Android BT stack stopped,
`wmt_launcher` left running for firmware/coex) and returns it on disconnect.
Then: `bluetoothctl list` shows the phone controller (real MAC),
`bluetoothctl scan on` discovers through the phone's antenna.
Trust model v1: USB-link only, unauthenticated — physical USB access is
the auth boundary.

### Wi-Fi control CLI (no root)

```bash
python3 pc/a32ctl.py status|scan|list|networks
python3 pc/a32ctl.py connect <ssid> <open|owe|wpa2|wpa3|wep> [pass]
python3 pc/a32ctl.py forget <ssid> | enable | disable
```

## Diagnostics

- Phone: `a32diag` (root); logs `/data/adb/modules/a32router/a32router.log`,
  state dir `.../system/bin/state/`.
- PC USB: `ip route get 1.1.1.1`, `nmcli device status`,
  `firewall-cmd --get-active-zones`.
- PC shim: `journalctl -u a32-wifishim-scan -u a32-wifishim-conn -f`,
  `nmcli device wifi list ifname wlan1`, `/run/wifishim/` state,
  `/run/wifishim/hostapd.log` + `dnsmasq.log` (EAPOL/DHCP forensics).
- Full engineering history: `test/` logs (`03` USB, `04` BT, `05` wifi CLI,
  `06` virtual STA, `07` IPv6).

## Rollback / uninstall

- PC shim: `sudo systemctl disable --now a32-wifishim-scan.service
  a32-wifishim-conn.service`, `sudo rm -rf /usr/local/lib/a32wifishim
  /etc/systemd/system/a32-wifishim-*.service /etc/modules-load.d/a32wifishim.conf
  /etc/modprobe.d/a32wifishim.conf`, `sudo rmmod mac80211_hwsim hci_vhci`,
  delete the `wlan1` Wi-Fi profiles you created for testing.
- PC USB: `nmcli connection down a32static` (or delete the profile),
  remove the firewalld zone binding, remove the router static route.
- Phone: remove the module in Magisk Manager (or delete
  `/data/adb/modules/a32router` + reboot). `uninstall.sh` removes only its
  own chain/routes/rules/table, unpins the neighbor, restores USB config to
  `adb` via init, resets `ip_forward`, kills daemons. A plain reboot
  restores stock regardless.

## Portability: what transfers to other Android devices

**Largely generic (reuse as-is):** everything PC-side (`vhci-relay.py`,
`wifishim-scan.py`, `wifishim-conn.py`, NM/firewalld/systemd integration),
both wire protocols (9-byte framed HCI; line-JSON Wi-Fi control), the
no-NAT policy-routing *pattern* (private table + high-priority rules —
rule numbers/priorities will differ per ROM, but stock Android's fwmark
policy routing bypasses `main` on most ROMs, so expect the same fight),
and the hard-won lessons (static neighbor pin for DHCP; dnsmasq
foreground-as-root; pidfiles over `pkill -f`; Ctrl-C never Ctrl-Z;
hex-PSK over `wpa_passphrase`; content-keyed serving; honesty teardown).

**Per-device work required:** the Magisk module's gadget section
(`rndis.gs4` naming, init `.rc` composition scripts, UDC name, persist
props — though `setprop`-driven `rndis,adb` composition works on most
ConfigFS-based ROMs, AOSP and vendor alike); the BT backend (`/dev/stpbt`

- H4 + EIO quirks are MediaTek BTIF-specific — Qualcomm uses `hci_uart`
/ serdev BT, Samsung Exynos yet another path, each needing its own RE
pass with the same ladder: snoop → open test → Reset round-trip);
SELinux allow rules (per-ROM policy types); Wi-Fi control verbs
(`cmd wifi` exists AOSP-wide, but output parsing is version-sensitive —
the SSID/BSSID/comma filter bug class will recur);
`wmt_launcher`/coex handling (MTK-only; others have their own chip-pwr
daemons that must likewise stay alive).

**Rule of thumb:** control-plane concepts and all PC code travel;
anything touching `/dev/*`, `/config/*`, init props, or SELinux types
gets re-derived per device in an afternoon following `test/` as a playbook.

## Status & limits (2026-09-07)

- Proven: routed USB internet (both directions, DNS, no NAT), BT HCI relay
  with auto-takeover/return, Wi-Fi CLI, virtual-STA browse + WPA2-PSK
  connect + DHCP with phone join.
- Link is USB 2.0 High-Speed (`musb-hdrc`, ~150–300 Mb/s real-world).
- Phone BT is borrowed while a relay session runs; phone uplink flaps
  ~10–30 s on every network switch (physics — dashboard accordingly).
- Known gotchas live in `test/`; the recurring ones: interface names shift
  across boots (always discover, never hardcode), NM profiles save wrong
  secrets silently (delete + retype), rapid UI clicking outruns radio timers.
