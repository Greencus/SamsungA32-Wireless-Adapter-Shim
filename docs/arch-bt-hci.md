# Architecture: Bluetooth HCI export (Phase 2) — A32 → PC virtual BT adapter

**Goal:** expose the phone's MediaTek MT6631 connac1x Bluetooth radio to the PC as a normal BlueZ controller (`hciX`) over the existing RNDIS USB-Ethernet link, so PC apps (keyboard/mouse, audio, file transfer, etc.) use the phone's radio as their local BT adapter.

**Status:** DESIGN. Device facts below are from the 2026-09-07 read-only audit (`audit/raw/*`) unless marked **⏳ verify live** (items the implementer must confirm with the read-only SSH command shown). No device writes were performed.

## 0. Verified device facts (audit 2026-09-07)

| Fact | Value | Source |
| --- | --- | --- |
| Kernel | `4.14.352-openela` (OpenELA user build, clang 11, signed/released bootloader) | raw/02_kernel.txt |
| `CONFIG_BT` | **not set** — no kernel BT core, no hci chars/sysfs, no btusb/hci_uart/vhci, no `/dev/hci*` | raw/02b:1155; raw/04 |
| `CONFIG_RFKILL` | not set | raw/02b:1177 |
| MTK combo drivers | `CONFIG_MTK_COMBO=y`, `COMBO_CHIP_CONSYS_6768`, `COMBO_BT=y`, `COMBO_WIFI=y`, `COMBO_GPS=y`, `MTK_BTIF=y`, `CONNSYS_DEDICATED_LOG_PATH=y` — all **built-in** (`lsmod` empty) | raw/02b:1661–1719; raw/08 |
| `CONFIG_STP=y` (raw/02b:1035) | **bridge spanning-tree** (net/8021q), NOT MediaTek STP — do not cite as MTK-STP evidence | raw/02b context |
| BT char devs | `/dev/stpbt` c **192,0** `bluetooth:bluetooth` `crw-rw----` · `/dev/fw_log_bt` c 490,0 `bluetooth:bluetooth` · `stpwmt` 190,0 · `wmtWifi` 488,0 · `wmtdetect` 154,0 · `uinput` 10,223 | raw/12_wifi_bt_detail.txt |
| BT stack | `bluetooth-1-1` running (pid 786, HAL `android.hardware.bluetooth@1.1-impl-mediatek`), state ON, name "A32 Server", enabled at SYSTEM_BOOT | props; raw/04 dumpsys |
| FW | ver `t-neptune-mp-soc1_0e1-1950-tc10sp-TALBOT_SOC1_0_E1_ASIC-20240904095302`; NV `/vendor/firmware/soc1_0_ram_bt_1a_1_hdr.bin`; `BT_FW.cfg`; `vendor.thermal.bt_completed=1` | props |
| WMT daemons | `wmt_launcher` running (pid 529); `wmt_loader` **stopped**; boottimes: loader 5.86 s → launcher 5.86 s → HAL 7.48 s (**strict order**); `vendor.connsys.driver.ready=yes`, `formeta.ready=yes` | props |
| autobt | ⏳ not in props (`init.svc.autobt` absent) → BSP appears to use `wmt_loader` for fw download; verify: `getprop init.svc.autobt; ps -A | grep -i autobt` | props |
| Modules | `/vendor/lib/modules/bt_drv_connac1x.ko` present, first in `modules.load`; **NOT loaded** (built-in kernel) — vestigial .ko | raw/08, raw/14 |
| USB gadget | ConfigFS funcs: ACM, SERIAL, **RNDIS**, MASS_STORAGE, **F_FS**, MIDI, **HID**, AUDIO_SRC, ACC, CONN_GADGET, SS_MON; **no NCM/ECM/EEM, no f_bt**; UDC `musb-hdrc` | raw/02b:3928–3965 |
| BT profiles | PAN NAP, HID dev/host, GATT, HFP AG, A2DP all enabled in Android stack | props/raw/04 |
| btsnoop | `persist.bluetooth.btsnoopdefaultmode=disabled` (usable diagnostics knob, see RE-3) | props |
| SELinux | enforcing; root domain `u:r:magisk:s0`; `magiskpolicy` present | raw/07, raw/13 |

**Locked-in implications:**

- `stpbt` is the *only* host port to the BT core; it is served by the built-in MTK `BTIF`/combo driver, not by a mainline BT subsystem.
- The combo silicon + firmware lifecycle is owned by `wmt_loader` (boot) + `wmt_launcher` (persistent chip/power/coex). **Both must stay running; only the HAL may be stopped.**
- FunctionFS and HID gadget functions are kernel-available today → fallback transports need no kernel work.

## (a) Why direct kernel BT-gadget export is impossible on this kernel

1. **No Bluetooth core at all.** `# CONFIG_BT is not set` → no hci core, no `hci_uart`/`btusb`/`btsdio`, no `/sys/class/bluetooth`, no `/sys/class/hci`, no RFKILL, no local `/dev/vhci`, no hciconfig/bluetoothctl/hciattach. There is no HCI device to export and no host interface a gadget could attach to.
2. **No `f_bt` gadget function.** `CONFIG_USB_CONFIGFS_F_BT` does not exist in this config, and even the old Android `f_bt` (Samsung/Qualcomm 4.4-era) required a kernel BT core — which is absent.
3. **No mainline driver for the radio.** MTK's *modern* parts have upstream drivers (MT7921/2 USB/PCIe, MT7668 hci_uart). The MT6631 connac1x combo has **none**; its host transport is the proprietary WMT → STP → btif path (`stpbt`). There is no `hci_uart`-style line discipline to enable — the framing is not plain H4.
4. Hence "export from kernel" equals *porting the MTK vendor BT driver into a kernel that also enables `CONFIG_BT` + `CONFIG_BT_HCI_VHCI` + a BT gadget* — effectively a **custom kernel** (see §d, last resort).

## (b) Recommended architecture: userspace HCI relay over RNDIS/TCP → PC VHCI

```
Phone (A32)                                                  PC (Linux)
┌────────────────────────────────────┐   USB/RNDIS      ┌──────────────────────────────────┐
│ Android BT stack STOPPED (§e)      │ 192.168.50.0/24  │ vhci-relay (root)                │
│ ┌────────────────────────────────┐ │   TCP :33600     │  modprobe hci_vhci → /dev/vhci   │
│ │ btrelayd (Magisk module)      │ │◄────────────────►│  create hciX (user channel, as   │
│ │  open("/dev/stpbt", O_RDWR)   │ │ [u32 len][u8 type]│  bluez/tools/btvirt.c does)      │
│ │  HCI_Reset → Command-Complete │ │ [payload]         │  AF_BLUETOOTH/HCI_CHANNEL_USER   │
│ └───────────┬───────────────────┘ │                   │  BlueZ bluetoothd → hciX          │
│             │ MTK_BTIF (built-in)  │                   │  hciX = phone's real MT6631 radio│
│ ┌───────────▼───────────────────┐ │                   └──────────────────────────────────┘
│ │ wmt_loader (done @boot)       │ │   ← KEEP RUNNING (never stop; WiFi coex lives here)
│ │ wmt_launcher (persistent)     │ │   ← KEEP RUNNING
│ └───────────────────────────────┘ │
└────────────────────────────────────┘
```

| Decision | Choice | Why |
| --- | --- | --- |
| Who owns BT | Stop Android stack; daemon owns `stpbt` | btif port is a single-client STP port; two writers corrupt the HCI state machine |
| Keep `wmt_launcher`/`wmt_loader` | **Yes** | They manage the combo chip; stopping them kills Wi-Fi (same silicon) |
| Transport | TCP over existing RNDIS link | Reuses phase-1 infra (192.168.50.1/2); zero extra kernel work; ≥400 Mb/s ≫ worst-case HCI need (~2–3 Mb/s A2DP ACL); host-side HCI tolerates ms-level RTT |
| PC injection | VHCI user-channel | Standard, bluez-tools-compatible; no PC kernel build; reference impl ships in BlueZ |
| FW download | none — done at boot by `wmt_loader`; chip kept powered by `wmt_launcher` | Removes the hardest MTK RE problem entirely |
| Vendor init | mirror the HAL's minimal open-time handshake (RE plan §c) | Typical minimum: open/wake + `HCI_Reset` |

**Phone-side daemon `btrelayd` (phase-2 of the a32router module, gated by `BT_EXPORT=1` + `ALLOW_STOP_BT=1`):**

1. Wait for `init.svc.bluetooth-1-1=stopped` before start (or perform §e myself and record what was issued).
2. `open("/dev/stpbt", O_RDWR)` as root (SELinux: add allow rule in `sepolicy.rule`, see RE-1/RE-2).
3. Device-open handshake (any ioctl/wake, see RE-0/RE-4), then send **`HCI_Reset`** = `01 03 0C 00` and read back the Command-Complete event — this single exchange proves the whole path before TCP is up. A cheap "radio alive" probe during dev: `timeout 1 cat /dev/fw_log_bt` (Neptune fw log port).
4. Listen on `192.168.50.1:33600` (the phase-1 RNDIS iface address — never `0.0.0.0`). Accept exactly one PC client (reject a second).
5. Framing (both directions): `[u32 BE len][u8 HCI packet type][payload]` where type ∈ {0x01 cmd, 0x02 ACL, 0x03 SCO, 0x04 event, 0x05 ISO, 0xfe MTK vendor}. Optional 4-byte magic `0xA3B70101` + protocol version for sanity.
6. On TCP EOF/unplug: park and re-advertise (re-listen); re-issue `HCI_Reset` once on reconnect; never touch stpbt mid-session; never stop/reload anything else.

**PC-side daemon `vhci-relay` (C, or Python+ctypes):**

1. `modprobe hci_vhci` (root) → `/dev/vhci` appears.
2. Instantiate the controller exactly like **`bluez/tools/btvirt.c`**: write the one-byte bootstrap packet (`HCI_VENDOR_PKT`) to `/dev/vhci` (hci_vhci then creates `hciX` in **user-channel** mode), then `socket(AF_BLUETOOTH, SOCK_RAW, BTPROTO_HCI)` + `bind` with `hci_channel = HCI_CHANNEL_USER`, `hci_dev = <X>`.
3. `connect()` to `192.168.50.1:33600`; bidirectional pump with the same framing.
4. `hciconfig hci0 up` / `bluetoothctl power on`. BlueZ probes the controller with standard commands (read local version, read BD_ADDR, …) — those are forwarded to the phone and answered by the real radio; the phone's real MAC appears automatically.

Reference code to crib: `bluez/tools/btvirt.c` (VHCI relay), btstack `hci_transport_*` (framing), any "python vhci" tool for socket details.

**Sanity numbers:** HCI carries link-layer data + SCO/ISO voice; A2DP ≈ 345 kb/s/core, worst-case ACL bursts ~2–3 Mb/s — the USB link has orders of magnitude headroom. Frame relay adds ~1–5 ms RTT; the radio's Link Manager ACKs at silicon level, so nothing timing-critical crosses the USB link.

## (c) MTK STP protocol unknowns + reverse-engineering plan

Unknowns (risk order):

1. **stpbt wire format** — WMT "STP" mux framing (per-port seq+len+type; the combo has one physical path for bt/gps/fm/wifi) vs. already-demuxed H4-style (type byte + HCI payload) at the char-dev boundary. MTK's btif historically exposes an "stp bt port"; *likely* the char dev already delivers demuxed HCI-ish frames, but this must be proven.
2. **Open mechanics** — exclusive open? Required ioctls (`BTIF_IOC_*`-style: wake, ack, coex)? The Android HAL opens it `O_RDWR`; driver may gate on a BT-wake/GPIO state.
3. **Vendor init sequence** — post-reset vendor HCI commands (BD_ADDR read, coex/antenna/fem config via `connfem`, Neptune features).
4. **Sleep/power** — with the HAL stopped, does wmt_launcher put BT to sleep? Probably: daemon's first write to stpbt drives the wake line (driver-managed) — verify.
5. **Coexistence** — WiFi + BT share the chip; wmt_launcher coex policy may assume the BT host is alive. Real risk of BT activity disturbing wlan0 → mandatory A/B test (T7).

**Zero-risk RE plan (ordered):**

- **RE-0 (static, off-device, best ROI):** MTK 4.14 vendor kernel sources are public on GitHub (mt6768/mt6769 trees). Read `drivers/misc/mediatek/connectivity/…`: `btif/` (stpbt creation, ioctls, wake), `common/` (`stp_*` framing), `wmt/` (launcher interface). Answers unknowns 1–4 precisely, no device risk.
- **RE-1 (live, read-only):** `ls -l /vendor/lib/hw /vendor/lib64/hw | grep -i -e bluetooth -e bt` — then `strings -a <hal so> | grep -i -e stp -e '/dev/' -e hci -e btif -e "0x"` and same on any `libbt*`/`libbluetooth*` vendor libs. Look for `"/dev/stpbt"`, ioctl magic, STP tokens.
- **RE-2 (live, read-only):** `ls -lZ /dev/stpbt /dev/fw_log_bt` (SELinux type → concrete `sepolicy.rule`: e.g. `allow magisk stpbt_device chr_file { open read write getattr ioctl };` — adjust to actual type). Init deps: `grep -rn -e bluetooth-1-1 -e wmt_launcher -e wmt_loader -e autobt /vendor/etc/init /system/etc/init /init.rc 2>/dev/null | head -60` (service `class`, `disabled`, prop gates).
- **RE-3 (diagnostic capture):** during one boot, `setprop persist.bluetooth.btsnooplogmode full`, `setprop persist.bluetooth.btsnooppath /data/misc/bluetooth/logs/btsnoop_hci.log` (revert after). btsnoop is the *host-side view of the exact stream stpbt carries* — gives the byte-exact minimal handshake (reset, vendor cmds, BD_ADDR reads) the daemon must reproduce.
- **RE-4 (trace, read-only):** `strace -p 786 -f -e trace=openat,ioctl,read,write,close` streamed to terminal → precise open flags, ioctl numbers, first write sequence. (ptrace from `u:r:magisk:s0` usually works here; if avc-denied, fall back to RE-0/RE-3.)
- **RE-5 (controlled runtime experiment, reversible by reboot):** with fw already loaded (normal boot), stop the HAL (§e), run a stub that opens stpbt, sends only `HCI_Reset`, dumps the first event. If a Command-Complete comes back, the relay needs **no vendor init beyond open+wake** and the rest is plumbing.

## (d) Honest limitations + fallback options

**Hard limitations**

- Only one HCI master at a time: the phone's own Android BT apps lose the radio while `btrelayd` owns stpbt (acceptable — phone is headless in this project's use).
- Undocumented MTK internals are the main project risk; the 80/20 bet is that framing+open+wake+Reset is all that's needed (RE-0/RE-3 settle it early).
- Sleep/wake may need the wake-write; coex may need tuning; no encryption on the TCP relay (USB point-to-point; add a token if desired).
- If stpbt framing proves non-H4 and absent from public trees, effort grows from days to weeks — that is the decision gate before committing to this path.

**Fallback A — USB-HID gadget bridge (works today, no VHCI, no kernel, opposite ownership):** keep the Android BT stack **running**; a small daemon (BT HID host role) connects to one real BT HID peripheral (keyboard/mouse/gamepad) and re-emits its reports on `functions/hid.*` (`CONFIG_USB_CONFIGFS_F_HID=y`, confirmed). The PC sees a plain USB keyboard — no BlueZ, no stpbt access, no RE. Limits: HID-class devices only, one-ish peripheral, peripheral→PC direction; RNDIS + HID can coexist in one gadget config.

**Fallback B — PAN NAP stopgap (zero code, available today):** Android stack has PAN NAP enabled and BT is ON. From the PC (needs its own BT adapter): `bluetoothctl pair` with the phone, connect NAP → `bnep0` on the PC, phone NATs to WiFi. Works now, over the **radio** (low bandwidth, NAT caveat like USB tethering); use as coexistence sanity check while §b is built.

**Fallback C — partial-HAL reuse (documented, not recommended):** if stpbt direct access proves intractable, mirror HAL HCI via btsnoop→TCP; but a mirror cannot *answer* BlueZ commands, so this degrades to "keep Android stack, use Fallback B". Not a real push-through.

**Last resort — custom kernel (flagged):** enable `CONFIG_BT`, `CONFIG_RFKILL`, `CONFIG_BT_HCI_VHCI` (PC side), `CONFIG_BT_HCI_UART` + a **ported MTK STP line discipline** (connac1x has no mainline driver — this is a port of the proprietary `MTK_BTIF`/STP stack into OpenELA 4.14), plus an `f_bt`-style gadget function if kernel-side HCI-over-USB is required. Weeks-to-months, custom toolchain, bootloader implications. **Do not attempt unless the userspace relay is proven impossible** — and note it still needs the RNDIS/VHCI leg afterward, so the kernel path only removes phone-side RE risk, not the transport work.

## (e) Exact stop / restore commands for the Android BT stack

Stop (root; runtime-only — reboot restores everything):

```sh
# 0. autobt check (not seen in props on this BSP; verify first):
#    getprop init.svc.autobt ; ps -A | grep -i autobt
#    if present:  stop autobt

# 1. Framework-level off (BluetoothManagerService; persists to settings)
svc bluetooth disable
#    equivalents:  cmd bluetooth_manager disable
#                  settings put global bluetooth_on 0

# 2. Stop the HAL service (frees /dev/stpbt)
stop bluetooth-1-1

# 3. Confirm clean handoff
getprop init.svc.bluetooth-1-1          # -> stopped
ps -A | grep -i bluetooth                # HAL pid gone

# 4. NEVER stop wmt_launcher / wmt_loader  (WiFi coex dies with them)
```

Restore:

```sh
start bluetooth-1-1                      # init restarts the HAL
svc bluetooth enable                     # or: cmd bluetooth_manager enable
                                         #     settings put global bluetooth_on 1
```

Safety: a plain `reboot` resets everything to stock regardless of experiment state. Module guardrail: `btrelayd` refuses to start while `init.svc.bluetooth-1-1=running` unless `ALLOW_STOP_BT=1`, records exactly which commands it issued in `state/` so `uninstall.sh`/stop replays restore, and never touches wmt services.

## Test plan (each item reversible by reboot)

- **T1** perms/SELinux: HAL stopped → `head -c 16 /dev/stpbt` returns EAGAIN (open OK, no data) — proves root+SELinux+perms.
- **T2** loopback: daemon opens stpbt, sends `HCI_Reset`, prints first event ≤1 s. (Radio-alive probe: `timeout 1 cat /dev/fw_log_bt`.)
- **T3** transport: PC stub `nc 192.168.50.1 33600` sends framed reset → phone returns framed event.
- **T4** VHCI: PC `hciconfig hci0` shows UP, BD_ADDR = phone's real MAC.
- **T5** radio ownership: `bluetoothctl scan on` on PC discovers a test BT keyboard (or second phone).
- **T6** A2DP: PC streams audio to a BT speaker via pipewire/pulseaudio.
- **T7** coex: iperf3 over wlan0 while PC streams A2DP — measure RSSI/throughput delta.
- **T8** restore: §e restore commands → `dumpsys bluetooth_manager` state ON again.

## Open questions to close before build

1. stpbt framing: H4-type-prefixed vs STP mux → decides frame layer (RE-0/RE-3). *(Highest-risk unknown.)*
2. Exclusive vs multi-open → decides graceful-handoff vs stop-HAL-only (RE-0/RE-4).
3. stpbt SELinux type (`ls -Z`) → exact `sepolicy.rule` text (RE-2).
4. autobt presence (RE-2).
5. Coex impact A/B (T7).

## STATUS 2026-09-07 (tested live): relay path PROVEN phone-side

- H4 framing CONFIRMED by live HCI_Reset round-trip (`test/bt_reset.py`);
  boot btsnoop corroborates (`test/btsnooz_hci.log`, "MTK CONNAC #1").
- End-to-end phone relay PROVEN: `test/bt_client.py` gets framed Command
  Complete over TCP (`a32shim.py` BT relay, v0.2.3). Takeover =
  `svc bluetooth disable` -> `stop bluetooth-1-1` + force-stop app (bounded);
  radio auto-returned on disconnect. `wmt_launcher` never touched.
- Driver quirks (bake into any reimplementation): (1) open-while-busy fails
  with EIO (not EBUSY); (2) read() on empty rx queue returns EIO instead of
  blocking — always select() before read. (3) `settings put global
  bluetooth_on` does NOT drive this stack; use svc/stop/start.
- Trust model v1: TCP :33600/:33601 USB-only, unauthenticated. Physical USB
  access = trusted. Revisit before any untrusted-network exposure.
- Ship default stays BT_EXPORT=0 (no radio takeover unless operator enables).
