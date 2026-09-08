#!/data/data/com.termux/files/usr/bin/python3
"""bt_watch.py — observe what restarts Android BT after a stop (NO stpbt use).

Stops the stack (svc disable + stop + force-stop + settings 0), then polls
every 2 s for 90 s: HAL state, app process presence, bluetooth_on setting.
Reports what came back and when. Restores BT at the end.

Run as root.
"""
import subprocess
import sys
import time


def sh(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return r.stdout.strip()
    except Exception as e:
        return "ERR:%s" % e


def hal():
    return sh(["getprop", "init.svc.bluetooth-1-1"])


def app():
    out = sh(["ps", "-A"])
    return "yes" if "com.android.bluetooth" in out else "no"


def setting():
    return sh(["settings", "get", "global", "bluetooth_on"])


def main():
    print("t=0 hal=%s app=%s setting=%s" % (hal(), app(), setting()), flush=True)
    sh(["svc", "bluetooth", "disable"])
    time.sleep(5)
    sh(["stop", "bluetooth-1-1"])
    sh(["am", "force-stop", "com.android.bluetooth"])
    sh(["settings", "put", "global", "bluetooth_on", "0"])
    t0 = time.time()
    last = None
    while time.time() - t0 < 90:
        cur = (hal(), app(), setting())
        if cur != last:
            print("t=%d hal=%s app=%s setting=%s" % (int(time.time() - t0), cur[0], cur[1], cur[2]), flush=True)
            last = cur
        time.sleep(2)
    print("restoring ...", flush=True)
    sh(["start", "bluetooth-1-1"])
    sh(["svc", "bluetooth", "enable"])
    sh(["settings", "put", "global", "bluetooth_on", "1"])
    time.sleep(5)
    print("final hal=%s app=%s setting=%s" % (hal(), app(), setting()), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
