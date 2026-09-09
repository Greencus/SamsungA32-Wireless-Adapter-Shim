#!/system/bin/sh
# customize.sh — install-time setup for a32router. Runs in Magisk Manager / recovery.
# Env: MODPATH, ZIPFILE, API, ARCH, BOOTMODE, MAGISK_VER_CODE.

# Require Android 12-15 (API 31-35). Device is LineageOS 22.1 / Android 15 (API 35).
if [ "$API" -lt 31 ] || [ "$API" -gt 35 ]; then
  abort "! a32router requires Android 12-15 (API 31-35), got API=$API"
fi

ui_print "- a32router: routed USB-Ethernet (RNDIS, no NAT) for Galaxy A32"
ui_print "- Target: MT6769 / musb-hdrc / ConfigFS gadget g1"

set_perm_recursive "$MODPATH" 0 0 0755 0644
set_perm "$MODPATH/service.sh" 0 0 0755
set_perm "$MODPATH/uninstall.sh" 0 0 0755
set_perm "$MODPATH/system/bin/a32routerd" 0 0 0755
set_perm "$MODPATH/system/bin/a32diag" 0 0 0755
set_perm "$MODPATH/system/bin/a32shim" 0 0 0755

# Keep user config across upgrades: do not overwrite existing config.
if [ -f "/data/adb/modules/a32router/a32router.conf" ] && [ -z "$FRESH_INSTALL" ]; then
  ui_print "- Preserving existing a32router.conf"
  cp -f "/data/adb/modules/a32router/a32router.conf" "$MODPATH/a32router.conf"
fi

ui_print "- Install complete. Reboot, then connect USB and run 'a32diag'."
