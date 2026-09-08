# Magisk Module Notes — Persistent Module for Android 12–15 (API 31–35)

> Source: <https://topjohnwu.github.io/Magisk/> (Developer Guides: Module Guide, Boot Scripts, Magic Mount, SELinux). No phone touched; knowledge cutoff 2026-01-04.

## 1. Module ZIP file structure

```
a32wifiadapter.zip
├── META-INF/com/google/android/
│   ├── update-binary      # Magisk module-installer stub (copy from Magisk repo template)
│   └── updater-script     # dummy, content: `#MAGISK`
├── module.prop            # REQUIRED metadata
├── customize.sh           # OPTIONAL install-time script (runs in Magisk app / recovery)
├── post-fs-data.sh        # OPTIONAL early-boot script (post-fs-data mode)
├── service.sh             # OPTIONAL late-boot script (late_start service mode)
├── uninstall.sh           # OPTIONAL cleanup on removal via Manager
├── sepolicy.rule          # OPTIONAL magiskpolicy statements, one per line
├── system.prop            # OPTIONAL resetprop overrides (prop=value per line)
├── system/                # OPTIONAL overlay, mirrors real `/` (Magic Mount)
│   └── system/...         # e.g. `system/bin/foo`, `system/etc/hosts`
└── zygisk/                # OPTIONAL (native .so injection, not needed here)
    └── arm64-v8a.so
```

Rules:

- `id` (= folder name under `/data/adb/modules/`) must match `^[a-zA-Z][a-zA-Z0-9._-]+$`, no spaces.
- `post-fs-data.sh` / `service.sh` must start `#!/system/bin/sh`, be executable.
- `system/` is Magic Mounted over `/` (overlayfs on modern Magisk, magic-mount fallback). Empty dirs ignored. Use `REPLACE="..."` in `customize.sh` to *replace* (not merge) a directory.
- Keep module self-contained under `$MODPATH`; never write outside `/data/adb/...` at install except via overlay.

## 2. module.prop fields

```ini
id=a32wifiadapter
name=A32 WiFi Adapter
version=v1.0.0
versionCode=1
author=you
description=Persistent WiFi adapter service for A32, Android 12-15
updateJson=https://example.com/a32wifiadapter/update.json  # optional OTA
```

- `id, name, version, versionCode, author, description` required. `versionCode` must be integer, monotonically increased.
- `updateJson` optional: JSON with `version`, `versionCode`, `zipUrl`, `changelog`.

## 3. Install lifecycle (customize.sh)

Run by Magisk installer when user flashes ZIP in Magisk app (preferred, `BOOTMODE=true`) or sideload/recovery (`BOOTMODE=false`). Module extracted to `$MODPATH=/data/adb/modules/<id>`.

Env provided: `MAGISK_VER, MAGISK_VER_CODE, BOOTMODE, MODPATH, TMPDIR, ZIPFILE, ARCH (arm/arm64/x86/x86_64), IS64BIT, API (e.g. 31-35)`.

Minimal `customize.sh`:

```sh
#!/system/bin/sh
SKIPUNZIP=1               # default 0 extracts all; set 1 if you unzip manually
# ui_print "<msg>"        # user-visible log
# abort "<msg>"           # fail install

ui_print "- Extracting"
unzip -o "$ZIPFILE" -x 'META-INF/*' -d $MODPATH >&2
unzip -o "$ZIPFILE" 'system/*' -d $MODPATH >&2  # if SKIPUNZIP=1

set_perm_recursive $MODPATH 0 0 0755 0644
set_perm $MODPATH/service.sh 0 0 0755
set_perm $MODPATH/post-fs-data.sh 0 0 0755

# Android version guard (12-15)
[ "$API" -ge 31 ] && [ "$API" -le 35 ] || abort "! Requires Android 12-15 (API=$API)"

# REPLACE example (wholesale dir replace, rarely needed):
# REPLACE="/system/app/Example"
```

Helpers available: `ui_print, abort, set_perm, set_perm_recursive`. After install, Manager sets SELinux to `system_file`.

Idempotency: installer wipes `$MODPATH` on reinstall **except** it preserves `disable` flag. Always `rm -rf` stale state, overwrite binaries, re-`set_perm`.

## 4. Boot service lifecycle

| Stage | Script | When | Blocking? | /data state | Use for |
| --- | --- | --- | --- | --- | --- |
| `post-fs-data` | `post-fs-data.sh` | post-fs-data, pre-Zygote, before apps | **YES — boot stalls until exit** | Mounted; FBE credential storage may still be locked | `resetprop`, `magiskpolicy --live`, prep dirs. Keep < few sec, **no loops/sleep** |
| `late_start` | `service.sh` | `late_start` service class, after `sys.boot_completed` vicinity | No (background) | Fully decrypted | Daemons, polling loops. **This is where persistent service goes** |
| one-shot dirs | `/data/adb/post-fs-data.d/*.sh`, `/data/adb/service.d/*.sh` | same stages, global (not per-module) | same as above | same | Avoid; prefer in-module scripts |

Patterns:

```sh
#!/system/bin/sh
MODDIR=${0%/*}
# service.sh — wait for boot, then daemonize, idempotent restart
until [ "$(getprop sys.boot_completed)" = "1" ]; do sleep 2; done
# kill stale instance from previous soft-reboot
pkill -f "$MODDIR/system/bin/a32daemon" 2>/dev/null
nohup "$MODDIR/system/bin/a32daemon" >/dev/null 2>&1 &
```

- `MODDIR=${0%/*}` is canonical way to locate self (equals `/data/adb/modules/<id>`).
- `post-fs-data.sh` runs even in Safe Mode? No — modules skipped in Safe Mode / after `magisk --remove-modules`.
- Logs: scripts' stdout/stderr → Magisk log (`/data/adb/magisk_log*` / `logcat | grep Magisk`). Always `exec >>$MODDIR/service.log 2>&1` for own log during dev.
- Reboot persistence is automatic: scripts re-run every boot. No `init.rc` edits needed.

## 5. SELinux handling (Android 12–15 strict)

- Scripts run as `u:r:magisk:s0` (root, permissive-ish domain) — do **not** `setenforce 0`; breaks SafetyNet/Play Integrity and is denied on new kernels.
- Custom daemon needs allow rules. Preferred: static `sepolicy.rule` in ZIP root:

```
# sepolicy.rule — magiskpolicy syntax, one rule per line
allow magisk_file vendor_file file { read open execute execute_no_trans };
allow mydaemon_data mydaemon_data file { create read write open getattr };
type a32daemon_exec, exec_type, file_type;
```

  Applied automatically each boot before `post-fs-data`. Test live first: `magiskpolicy --live "allow ..."`, verify with `dmesg | grep avc | audit2allow`.

- Label module files as `system_file` so Magic Mount inherits sane context:

```sh
chcon -R u:object_r:system_file:s0 "$MODPATH/system"
```

- Never `chcon` to `magisk_file` for executables served to system; never add `permissive` domains in production.
- On Android 13+ (`sepolicy` split, `system_ext`), prefer placing binaries under `system/bin` or `system/xbin`, not `/vendor` (would need `vendor_file` + extra rules).

## 6. Storage paths

| Path | Purpose |
| --- | --- |
| `/data/adb/modules/<id>/` (`$MODPATH`/`$MODDIR`) | Module live dir. Your scripts, binaries, state |
| `/data/adb/modules/<id>/disable` | If exists (empty file), module skipped next boot. `touch` to disable, `rm` to enable |
| `/data/adb/modules/<id>/remove` | If exists, module uninstalled next reboot |
| `/data/adb/modules/<id>/update` | Marker used by Manager during update |
| `/data/adb/post-fs-data.d/`, `/data/adb/service.d/` | Global scripts (avoid for modules) |
| `/data/adb/magisk/` | Internal Magisk db/config — do not touch |
| Module state (your own) | Keep under `$MODDIR/state/` or `/data/adb/a32wifiadapter/`; clean in `uninstall.sh` |

Disable/remove without UI (adb, bootloop recovery):

```sh
touch /data/adb/modules/a32wifiadapter/disable   # safe-disable
rm /data/adb/modules/a32wifiadapter/disable      # re-enable
adb wait-for-device shell magisk --remove-modules # nuke all modules (bootloop rescue)
```

Safe Mode (hold Vol-Down during boot) disables all modules/Zygisk for one boot.

## 7. Idempotency & recovery guidance (implementation checklist)

1. **Reinstall-safe `customize.sh`**: handle errors with `abort`. Re-extract cleanly, `rm -f $MODPATH/disable` only if you intend to re-enable on upgrade; otherwise preserve user disable state.
2. **Boot idempotent `service.sh`**: `pkill -f` stale daemon before start; use `flock` or pidfile; cap log size; guard boot-count: if daemon crashes >5x in 10 min, `touch $MODDIR/disable` + `reboot` to avoid bootloop.
3. **`uninstall.sh`** (runs on Manager removal):

```sh
#!/system/bin/sh
MODDIR=${0%/*}
pkill -f "$MODDIR/system/bin/a32daemon" 2>/dev/null
rm -rf /data/adb/a32wifiadapter "$MODDIR/state"
```
1. **Version upgrades**: bump `versionCode`; in `customize.sh` read old `$MODPATH/module.prop` versionCode if present to migrate state.
2. **Bootloop safety**: keep `post-fs-data.sh` trivial or omit; put all risky logic in `service.sh` (non-blocking). Test disable path before shipping. Document `magisk --remove-modules` + Safe Mode in README.
3. **Android 12–15 specifics**: target `API 31–35` guard; use overlayfs-compatible paths; no `/sbin` assumptions (`/debug_ramdisk` on new devices); Zygisk optional — plain `service.sh` daemon is most stable across OEM skins; respect FBE (don't read CE storage in `post-fs-data`).

## Minimal persistent-module skeleton (this project)

- `module.prop` (above) + `customize.sh` (extract + perms + API guard) + `service.sh` (wait boot_completed → start daemon) + `uninstall.sh` (kill + rm state). Skip `post-fs-data.sh` unless props/policy needed.
