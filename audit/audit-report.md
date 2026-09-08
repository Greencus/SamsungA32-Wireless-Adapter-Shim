# Audit Report — Rooted Samsung Galaxy A32 (SM-A325M)

**Date:** 2026-09-07 (device clocks show 2026-09-06 ~20:45)
**Method:** READ-ONLY via SSH `ssh -p 8022 192.168.1.3`, root via Magisk `su -c`. No writes performed on device.
**Raw outputs:** `audit/raw/01…16` (see list at end).

---

## 1. Access & Remote Shell

| Item | Value |
|---|---|
| SSH host | 192.168.1.3:8022 |
| SSH server | **OpenSSH_10.2p1** (+ OpenSSL 3.6.3), running inside **Termux** as uid `u0_a177` (processes: multiple `sshd-session`, `sshd` PID 30541) |
| Login user | `u0_a177` (Termux app, gid 10177; groups inet, everybody, cache, all_a177) |
| Root | **Yes.** `su` = Termux wrapper script → Magisk `su` at `/debug_ramdisk/su` → context `u:r:magisk:s0` |
| ADB listener | `adbd` running **as root** (`init.svc.adb_root=running`, pid 949); USB ADB configured (`sys.usb.config=adb`) |
| Open listeners (main netns, uid 10177) | `0.0.0.0:8022` (SSH), `0.0.0.0:3000`, `0.0.0.0:8898`, `127.0.0.1:5432`, `127.0.0.1:8282` |

## 2. Device Identity & Firmware

| Item | Value |
|---|---|
| Model | **SM-A325M** (`ro.product.model`), codename `a32` / `a32ub` (Samsung), Samsung One-UI partition layout preserved |
| ROM | **LineageOS 22.1 UNOFFICIAL** `22.1-20250214-UNOFFICIAL-a32` (`ro.lineage.version`, build type `user`, flavor `lineage_a32-user`) |
| Android | **15** (SDK 35), build ID `AP4A.250205.002`, vendor security patch `2025-01-01` |
| Fingerprint | **Spoofed Samsung Android 13**: `samsung/a32ub/a32:13/TP1A.220624.014/A325MUBSBDXL1:user/release-keys` |
| Bootloader | `A325MUBSBDXL1`, **unlocked/tripped**: `verifiedbootstate=orange`, `warranty_bit=1` |
| Serial no (redacted) | `XXXXXXXXXXX` (ADB guid `adb-R58R75KGLGV-…`) |

## 3. SoC / CPU / Memory

| Item | Value |
|---|---|
| SoC | **MediaTek MT6769T** (`ro.hardware.chipname=MT6769T`, board platform `mt6768`, SoC model `MT6769V/CT` -> Helio G80 class) |
| CPU | 8× Cortex-A55 (4× `0xd05` + 4× `0xd0a`), aarch64, crypto ext (aes/pmull/sha1/sha2), kernels uvmin 2×2 |
| ABI | `arm64-v8a,armeabi-v7a,armeabi` |
| RAM | 4 GB (`androidboot.ddr_size=4096MB`) |

## 4. Kernel

| Item | Value |
|---|---|
| Version | **4.14.352-openela** — custom "OpenELA" kernel, `#3 SMP PREEMPT`, built 2025-02-15 (clang 11.0.1/LLD), builder `murimi14@justinserver` |
| `/proc/config.gz` | **Available** (36 KB, `CONFIG_IKCONFIG_PROC=y`) — full copy in `raw/02b_kernel_config_full.txt` |
| Cmdline highlights | `root=/dev/ram`, `androidboot.boot_devices=soc/11230000.mmc`, `ramoops` (pstore), `androidboot.verifiedbootstate=orange`, `gpt=1`, `usb2jtag_mode=0` |

Key config options:
- **ConfigFS:** `CONFIG_CONFIGFS_FS=y` (mounted at `/config`, rw)
- **USB gadget:** `CONFIG_USB_GADGET=y`, `USB_LIBCOMPOSITE=y`, `USB_CONFIGFS=y` (+serial, ACM, RNDIS, mass_storage, F_FS, MIDI, HID, AudioSrc, ACC, DM, CONN/Samsung), `USB_F_RNDIS=y`, `CONFIG_USB_ANDROID_SAMSUNG_COMPOSITE=y`; controller **MTK MUSB** (`USB_MTK_HDRC=y`, `USB_MTK_OTG=y`)
- **Netfilter:** full legacy **xtables**: `NETFILTER_XTABLES=y` with 73 target/match options (MARK, CONNMARK, TPROXY, REDIRECT, NFQUEUE, bpf, string, u32, time…); `IP_NF_*` filter/nat/mangle/raw/security; `NF_CONNTRACK*`, `NF_NAT*`; multiple routing tables (`IP_MULTIPLE_TABLES=y`, IPv6 too); `BRIDGE_NETFILTER=m` (module only)
- **NOT enabled:** `NF_TABLES` (**nftables: no**), `BRIDGE_NF_EBTABLES` (**ebtables: no**), `MAC80211` (WiFi driver is cfg80211 fullmac)
- **Misc:** `MODULES=y` + `MODULE_UNLOAD=y`, `/proc/sys/kernel/modules_disabled=0` (module loading allowed); `SWAP=y`, `ZRAM=y`; `BPF=y`/`BPF_SYSCALL=y`/`BPF_JIT_ALWAYS_ON=y`/`CGROUP_BPF=y`; `DUMMY=y`, `TUN=y`, `VETH=y`; **no WireGuard**; `XFRM`/IPsec/VTI/sit/tunnels; `ANDROID_BINDERFS=y`

## 5. Network Interfaces (ip)

- **wlan0**: UP, `192.168.1.3/24`, gw `192.168.1.1` (policy-routed via table `wlan0`); per-network tables (`wlan0`, `dummy0`, local)
- **p2p0** (Wi-Fi Direct), **swlan0** (softAP, `ro.vendor.wifi.sap.interface=swlan0`), `ap0` (tethering iface prop) — all DOWN
- 21× **rmnet0–20** (cellular, DOWN), **ccmni-lan** (MTK modem), **epdg0–7** (ePDG tunnels, DOWN), tunnels `ip_vti0/ip6_vti0/sit0/ip6tnl0`, **dummy0** (UP), **ifb0/ifb1** (qdisc bouncing), `lo`

## 6. Wi-Fi

| Item | Value |
|---|---|
| Chipset | **MediaTek MT6631 combo "connsys gen4m"** — `ro.vendor.wlan.gen=gen4m`, WiFi fw ver `persist.vendor.connsys.wifi_fw_ver=240904095302000` |
| Driver | Kernel module **`wlan_drv_gen4m.ko`** (+ `wmt_drv.ko` WMT, `connfem.ko`, `wmt_chrdev_wifi.ko`); platform driver `wlan` (`/sys/bus/platform/drivers/wlan`); char devs `wmtWifi` (488,0), `wmtdetect` |
| Firmware | `/vendor/firmware/WIFI_RAM_CODE_soc1_0_1a_1.bin`, `soc1_0_ram_wifi_1a_1_hdr.bin`, `wifi.cfg`, `WMT_SOC.cfg` |
| Radio | `phy0` via cfg80211 (`/sys/class/ieee80211/phy0`); legacy mode (`wifi_hal_legacy` service running) |
| Stack | `wpa_supplicant` (running; binary is internal/vendor build, `wpa_cli` not on PATH), `wificond`, Samsung `wlan_assistant` running; **no hostapd** (SAP handled by `wlan_assistant`/wifi_hal legacy) |
| Tools | `iw`, `iwconfig`, `wpa_cli`, `hostapd`, `dhcpcd` — **absent**. Net control via `ip`, sysfs, toybox; **dnsmasq present** (`/system/bin/dnsmasq`) |

## 7. Bluetooth

| Item | Value |
|---|---|
| Chipset | **MediaTek connac1x** (`ro.vendor.bt.platform=connac1x`) — same combo silicon as WiFi (MT6631); BT fw `vendor.bluetooth_fw_ver=t-neptune-mp-soc1_0e1-1950-tc10sp-TALBOT_SOC1_0_E1_ASIC-20240904095302`, NV `/vendor/firmware/soc1_0_ram_bt_1a_1_hdr.bin`, `BT_FW.cfg` |
| Driver | Modules **`bt_drv_connac1x.ko`** expected in `/vendor/lib/modules`; platform drivers `mtk_btif`, `mtk-btcvsd-snd`; char devs **`stpbt`** (192,0), `fw_log_bt` |
| HCI visibility | **No `/sys/class/bluetooth`, no `/sys/class/hci`, no `/dev/hci*`** — MTK BT HCI is private/vendor (stpbt); not exposed to classic hciconfig/bluetoothd interface |
| Stack | Android BT HAL `bluetooth-1-1` (running, pid 786); `dumpsys bluetooth_manager`: **enabled, state ON, name "A32 Server"**, address XX:XX:XX:XX:00:01; rich profile set incl. PAN NAP (tethering-capable), SAP, HID, GATT |
| Tools | `hciconfig`, `bluetoothctl`, `hciattach` — **absent** |

## 8. USB Controller & Gadget

| Item | Value |
|---|---|
| Controller/UDC | **`musb-hdrc`** (MediaTek MTK HDRC; `sys.usb.controller=musb-hdrc`, `vendor.usb.controller=musb-hdrc`), `usb2jtag_mode=0` |
| ConfigFS | Mounted **rw at `/config`** (`none /config configfs`); `/sys/kernel/config` empty (mount moved to /config). Gadget **`g1`** preconfigured by ROM: UDC `musb-hdrc`, funcs `ffs.adb`, `ffs.mtp`, `ffs.ptp`, `acm.gs0-2`, `audio_source`, `conn_gadget`, `mass_storage`, `midi`, `accessory`, `dm`, `ss_mon`, `via_atc/ets/modem`; dir owned by `shell` |
| Legacy IEEE gadget | Samsung `android_usb` composite active (`/sys/class/android_usb/android0`, uevents in dumpsys); functions `f_audio_source`, `f_conn_gadget`, `f_midi` visible |
| Gadget binding (from concurrent audit `raw/03_gadget_baseline.txt`) | `g1/configs/b.1/f1 → functions/ffs.adb` (FunctionFS adb bound); strings: `SAMSUNG` / `SAMSUNG_Android` / serial `R58R75KGLGV`; `MaxPower` + `bmAttributes` in b.1 |
| Tethering stack (from `raw/04_tether_usb.txt`) | Android `networkstack.tethering` (uid 1073) active request for INTERNET — USB/hotspot tethering subsystem live |
| Current mode | `sys.usb.config=adb`, `sys.usb.configfs=1`, functionfs mounted `/dev/usb-ffs/{adb,mtp,ptp}`; VID `0x04E8` (Samsung); `dumpsys usb`: connected, configured, no host mode |
| Services | `vendor.usb-hal-1-3` running; `usbd` stopped; ADB running as root |

## 9. Firewall / Netfilter

| Tool | Status |
|---|---|
| **iptables / ip6tables** | **Yes** (`/system/bin/iptables`, both v4/v6; xtables kernel support complete; CONNMARK warning only — no functional issue reported). Chains active: `bw_*` (bandwidth/quota/BPF allowlist-denylist), `fw_*`, `oem_*`, `st_OUTPUT`, `tetherctrl_*`, `connmark_mangle_*`. **Policies ACCEPT** in filter/nat/mangle/raw |
| **nft** | **No** — binary absent and kernel `CONFIG_NF_TABLES` not set |
| **ebtables** | **No** — binary absent, `BRIDGE_NF_EBTABLES` not set (br_netfilter.ko module exists but not loaded) |
| NAT | Full `NF_NAT`, MASQUERADE, REDIRECT, NETMAP targets; tether NAT chains present |

## 10. SELinux

| Item | Value |
|---|---|
| Mode | **Enforcing** (`getenforce`; `/sys/fs/selinux/enforce=1`), policy version 31, selinuxfs mounted |
| Root context | `u:r:magisk:s0`; app context `u:r:untrusted_app_27:s0` |
| Notes | `sestatus` not present (RHEL tool); `selinux.restorecon_recursive=/data/misc_ce/0`; `ro.control_privapp_permissions=enforce` |

## 11. Root / Magisk

| Item | Value |
|---|---|
| Magisk | **30.4** (`su -c 'magisk -v'` → `30.4:MAGISK:R`; binary 412 KB, `magisk32` + `magiskinit` + `magiskpolicy` + `resetprop` in `/debug_ramdisk`) |
| Zygisk | **Disabled** (`magisk --sqlite` → `zygisk=0`, `bootloop=0`) |
| Denylist | **Empty** |
| su wrapper | `/data/data/com.termux/files/usr/bin/su` (root-owned 700 script) → tries `/debug_ramdisk/su` first |
| Magisk busybox | `/data/adb/magisk/busybox` (1.7 MB) |

## 12. Userland Tools

**Present (/system/bin):** `toolbox`, `toybox 0.8.11-android` (ip, nc/netcat are toybox applets: `/system/bin/nc -> toybox`, `/system/bin/netcat -> toybox`), `iptables`, `ip6tables`, `dnsmasq`, `tcpdump`.
**Absent (/system):** busybox, nft, ebtables, hciconfig, bluetoothctl, hciattach, ssh/scp, wpa_cli, hostapd, dhcpcd, udhcpd.
**Termux ($PREFIX=/data/data/com.termux/files/usr, 834 binaries):** bash, python3, node, git, curl, wget(absent), openssl, **OpenSSH sshd 10.2p1** (the SSH server), tar, unzip, ifconfig, route, htop. **Absent in Termux:** iperf3, socat, nmap, hping3, jq, sqlite3, php, perl, ruby, zsh.

## 13. Kernel Modules

- `lsmod` → **empty** (no modules currently loaded; WiFi/BT aren't enumerated there — MTK connsys handles them)
- `/vendor/lib/modules/` (13 .ko): `bt_drv_connac1x`, `wmt_drv`, `connfem`, `fmradio_drv_mt6631`, `gps_drv`, `wmt_chrdev_wifi`, `wlan_drv_gen4m`, `fpsgo`, `met`, `udc_lib`, `br_netfilter`, `tcp_htcp`, `tcp_westwood`; `modules.load` lists the 10 driver modules
- Module loading **enabled** (`modules_disabled=0`, `MODULE_UNLOAD=y`) — e.g. `br_netfilter` could be loaded for bridging/netfilter use

## 14. Relevant init Services (state)

running: `adbd`, **`adb_root`** (root adbd), `netd`, `netdagent`, `wpa_supplicant`, `wificond`, `vendor.wifi_hal_legacy`, `wlan_assistant`, `bluetooth-1-1`, `wmt_launcher`, `zygote`/`zygote_secondary` (64+32), `vold`, `ril-daemon`, `ccci_mdinit`, `surfaceflinger`…
stopped/unused: `usbd`, `apexd*`, `bootanim`, `dmesgd`, `statsd` (stopped)…

## 15. Storage / Partitions

- Legacy GPT on eMMC: `mmcblk0p1` efs, `p16/p17` protect, `p26` nvdata, `p30` metadata; system/`/system_ext`/`/vendor` dm-verity read-only ext4 dm-*; `/data` f2fs w/ inlinecrypt + lz4 (dm-38)
- Fastboot/recovery PIDs known via props (`ro.recovery.usb.*` = 18D1)
- `hidepid=2` on /proc; `/sdcard` via FUSE/sdcardfs (legacy `/config/sdcardfs`)

## 16. Notable Observations / Implications

1. **Kernel is user-built** ("OpenELA" 4.14.352); `/proc/config.gz` present → kernel config fully known/saved; GKI-style builds not used.
2. **Full cfg80211 + xtables + routing tables + CONFIGFS + FunctionFS + RNDIS gadget** → very capable for USB networking (RNDIS/ECM/FunctionFS), bridging (module), NAT tethering (iptables), policy routing.
3. **No nftables/ebtables** — firewall automations must stay on legacy iptables (or load br_netfilter for bridge filtering).
4. **BT HCI not exposed on classic char dev** — Host-side control realistically via Android Binder stack (dumpsys/bluetooth_manager) or stpbt vendor path; device named **"A32 Server"** and BT enabled, PAN NAP profile enabled (BT tethering possible).
5. **ADB root** enabled; SSH via Termux OpenSSH on 8022; root shell context `u:r:magisk:s0` in enforcing SELinux (restrictions apply — e.g., sysfs/proc writes still gated by SELinux).
6. **Spoofed Samsung A13 fingerprint** (Lineage 22.1/Android 15) — affects CTS/profile assumptions.
7. Open listening ports besides SSH: 3000, 8898 (0.0.0.0), 5432/8282 (localhost only) — owner uid 10177 (Termux user).

---

## Raw Files (`audit/raw/`)

| File | Content |
|---|---|
| `01_getprop_all.txt` (1591 ln) | Full `getprop` dump |
| `02_kernel.txt` | /proc/version, uname, cmdline, cpuinfo, first 200 cfg lines |
| `02b_kernel_config_full.txt` (5919 ln) | **Complete** `/proc/config.gz` |
| `03_wifi_network.txt` | ip link/addr/route (all tables), /sys/class/net, driver links, ieee80211 |
| `04_bluetooth.txt` | BT sysfs/chars, props, `dumpsys bluetooth_manager` (truncated head) |
| `05_usb_gadget.txt` | configfs, /config/usb_gadget, `dumpsys usb`, sys.usb.* props, UDC |
| `06_firewall.txt` | iptables -L -n -v (filter/nat/mangle), ip6tables, nft/ebtables attempt |
| `07_selinux_magisk.txt` | getenforce, magisk -v, magisk paths |
| `08_tools_modules.txt` | tool availability matrix, toybox ver, lsmod, .ko inventory |
| `09_init_mounts.txt` | init.svc.* props, /proc/mounts, partitions, by-name, fstab |
| `10_identity.txt` | model/SoC/Android/fingerprint/build/security props |
| `11_processes_ports.txt` | ps head, /proc/net/tcp + tcp6 |
| `12_wifi_bt_detail.txt` | platform drivers, /dev BT/WMT chars, module dir, phy0 |
| `13_magisk_detail.txt` | zygisk sqlite, denylist, /debug_ramdisk, resetprop, su wrapper, enforce/policyvers |
| `14_rom_extra.txt` | lineage props, modules.load, /vendor/firmware listing |
| `15_termux_tools.txt` | Termux tool inventory |
| `16_gadget_sshd.txt` | /config/usb_gadget/g1 internals, nc realpath, sshd version |
| `03_gadget_baseline.txt` *(concurrent worker)* | usb_gadget g1 config binding & Samsung strings, usb props |
| `04_tether_usb.txt` *(concurrent worker)* | networkstack tethering request, dumpsys usb log |