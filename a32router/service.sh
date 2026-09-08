#!/system/bin/sh
# service.sh — late_start boot service for a32router. Non-blocking by design.
MODDIR=${0%/*}

# Wait for boot completion (bounded: 5 min), then launch daemon.
i=0
until [ "$(getprop sys.boot_completed)" = "1" ]; do
  sleep 5
  i=$((i + 1))
  [ "$i" -ge 60 ] && exit 0
done

# Kill stale daemon from a previous soft-reboot, then start fresh (idempotent).
# Prefer the pidfile: pkill -f can match the caller's own command line when the
# pattern appears in it (e.g. remote ssh invocations) and kill the caller.
if [ -f "$MODDIR/state/a32routerd.pid" ]; then
  kill "$(cat "$MODDIR/state/a32routerd.pid" 2>/dev/null)" 2>/dev/null
else
  pkill -f "a32routerd" 2>/dev/null
fi
sleep 1

# Daemon owns its log; cap size on each boot.
LOG="$MODDIR/a32router.log"
[ -f "$LOG" ] && [ "$(wc -c <"$LOG" 2>/dev/null)" -gt 204800 ] && mv -f "$LOG" "$LOG.old"

nohup "$MODDIR/system/bin/a32routerd" >>"$LOG" 2>&1 &
pkill -f "a32shim" 2>/dev/null
nohup "$MODDIR/system/bin/a32shim" >>"$LOG" 2>&1 &
exit 0
