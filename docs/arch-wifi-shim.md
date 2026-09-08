# Architecture: PC-side Wi-Fi shim (virtual station backed by the phone)

**Goal:** a NetworkManager-managed Wi-Fi interface (`wlan1`) that browses
real surrounding networks and connects to them, where the phone's radio
does the real RF work and the existing USB route carries the bytes. Same
pattern as the BT relay: genuine local stack on top, phone underneath.

**Status:** PROVEN live 2026-09-07 (browsing + WPA2-PSK association + DHCP;
phone join + routed data). WPA3-SAE and 6 GHz deferred.

## 1. Layout: one radio per role

`mac80211_hwsim` (`radios=2`, names discovered dynamically — they shift
across boots, never hardcode):

- Radio 1: `mon1` alone (monitor, injection hops freely).
- Radio 2: `wlan1` STA (NM-managed, scans freely) + later the `ap1` AP
  (STA connects *to our AP*, so same channel by construction — no fight).

Sharing one phy between an NM STA and monitor fails hard: NM pins the phy,
every `iw ... set channel` dies EBUSY, hwsim tunes nothing, TX is silently
dropped (socket success, `tx_packets` frozen — and note that counter never
counts monitor injection, so it is not a usable health signal).
`pc/wifishim-setup.sh` enforces the split layout idempotently.

## 2. Browsing: beacon + probe-response injection (`wifishim-scan.py`)

Polls phone scans over USB (`a32ctl` protocol), keeps an AP table
(expire >240 s), and transmits each AP as beacons (all its channels,
continuous hop) + answers directed/broadcast probe requests seen on the
current channel. Frames are hand-built (radiotap + 802.11 + RSN-PSK for
PSK networks; SAE-only/EAP/WEP/hidden skipped in v1 with a log line).
Channel hop vs NM dwell rendezvous is probabilistic per scan; repeats
converge. Injected frames report a uniform default signal (cosmetic).

## 3. Connecting: local AP + phone in parallel (`wifishim-conn.py`)

Watches NM active connections on the STA. On a new activation:

1. Resolves BSSID+channel from the phone (cached `list` first, full `scan`
   fallback), **pinning one BSS per SSID** (dual-band ESSes flip-flop
   otherwise, rebuilding hostapd mid-session). Band policy: non-DFS 5 GHz
   first (`WIFISHIM_BAND_PREF=2.4` flips it); DFS channels refused loudly
   (hostapd dies instantly on radar CAC).
2. Starts **hostapd** on `ap1` (freshly deleted + recreated every time —
   reuse dies `Match already configured`; BSSID spoofed to the real one;
   PSK as locally-derived hex via PBKDF2, immune to `#`/quotes/whitespace
   mangling), then **dnsmasq** DHCP Platz (address-only, no gateway; first-
   pool address pinned to the STA MAC — clients never ARP unowned addresses).
3. In parallel, tells the phone to join the same SSID (skipped if already
   there; throttled to 1/SSID/2min — each attempt flaps the uplink).
4. **Honesty guard:** if the phone leg fails (wrong password, vanished AP),
   the AP side is torn down so NM shows DOWN instead of a local-only
   masquerade (a same-key local handshake always "succeeds", meaningless).
5. Serving key = profile UUID + sha256(ssid/keymgmt/psk): NM saves typed
   passwords *after* profile creation, so UUID-only tracking serves stale
   secrets forever. Any secret change rebuilds.

Data plane: unchanged USB routing (table 100 + metric-700 default). The
virtual link carries no bytes that matter; NM's connectivity check passes
through the USB default. No NAT anywhere.

## 4. Persistence

`pc/systemd/`: `a32-wifishim-setup.service` (oneshot, runs setup +
best-effort `nmcli up a32static`), `a32-wifishim-scan.service` and
`a32-wifishim-conn.service` (simple, `Restart=always`, journal logging).
`install.sh` copies scripts to `/usr/local/lib/a32wifishim/` and enables
units. Operator loads `mac80211_hwsim` externally (modprobe deliberately
out of scope). BT relay stays manual (taking the phone radio must remain
an explicit act).

## 5. Limits / not yet

- WPA3-SAE-only and EAP networks: advertised? No — skipped (can't back the
  handshake). Transition (PSK+SAE) networks work via the PSK path.
- 6 GHz: untested (no networks in range during dev).
- Hidden SSIDs: skipped in v1 (can't beacon meaningfully).
- Phone leg moves the uplink: switching networks drops phone internet for
  ~10–30 s by physics; dashboard accordingly.
- `WIFISHIM_BAND_PREF`, per-SSID pins, and phone-join throttle live in
  process memory (restart resets them — acceptable, documented).
