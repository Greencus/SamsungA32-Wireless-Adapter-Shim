#!/data/data/com.termux/files/usr/bin/python3
"""bt_timing.py — bisect: does stpbt answer immediately after stop (phase 1)
but go silent after 30 s idle with the stack down (phase 2)?

Phase 1: stop stack -> open -> HCI_Reset -> read (expect instant reply).
Phase 2: (stack stays down) sleep 30 -> open again -> Reset -> read 5 s.
Restore BT at the end no matter what.

Run as root.
"""
import os
import select
import subprocess
import sys
import time

STPBHCI = "/dev/stpbt"
HCI_RESET = bytes((0x01, 0x03, 0x0C, 0x00))


def sh(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as e:
        print("sh failed: %s" % e, flush=True)
        return None


def hal():
    r = sh(["getprop", "init.svc.bluetooth-1-1"])
    return r.stdout.strip() if r else "?"


def stop_stack():
    sh(["svc", "bluetooth", "disable"])
    time.sleep(2)
    sh(["stop", "bluetooth-1-1"])
    sh(["am", "force-stop", "com.android.bluetooth"])
    for _ in range(10):
        if hal() == "stopped":
            return True
        time.sleep(1)
    return False


def attempt(tag):
    try:
        fd = os.open(STPBHCI, os.O_RDWR)
    except OSError as e:
        print("%s: OPEN_FAILED errno=%s" % (tag, e.errno), flush=True)
        return
    try:
        os.write(fd, HCI_RESET)
        end = time.time() + 5
        buf = b""
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], max(0, end - time.time()))
            if not r:
                break
            chunk = os.read(fd, 256)
            if not chunk:
                break
            buf += chunk
            if len(buf) >= 7:
                break
        print("%s: READ %d bytes: %s" % (tag, len(buf), buf.hex() if buf else "(timeout)"), flush=True)
    finally:
        os.close(fd)


def main():
    print("stopping stack ...", flush=True)
    print("stopped" if stop_stack() else "WARN not stopped", flush=True)
    attempt("PHASE1-immediate")
    print("sleeping 30 with stack down ...", flush=True)
    time.sleep(30)
    print("hal still: %s" % hal(), flush=True)
    attempt("PHASE2-after-idle")
    print("restoring ...", flush=True)
    sh(["start", "bluetooth-1-1"])
    sh(["svc", "bluetooth", "enable"])
    time.sleep(5)
    print("final hal: %s" % hal(), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
