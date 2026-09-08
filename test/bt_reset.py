#!/data/data/com.termux/files/usr/bin/python3
"""bt_reset.py — RE-5 decisive test: own /dev/stpbt and run HCI_Reset.

Sequence:
  1. svc bluetooth disable (clean framework shutdown), fallback: stop HAL,
     force-stop com.android.bluetooth
  2. wait for init.svc.bluetooth-1-1 == stopped (bounded)
  3. open /dev/stpbt O_RDWR, write H4 HCI_Reset (01 03 0C 00)
  4. read up to 5 s, hexdump everything received
  5. EXPECTED: 04 0E xx 01 03 0C 00 (Command Complete) -> H4 CONFIRMED live
  6. ALWAYS restore: start HAL + svc bluetooth enable, wait for running.

wmt_launcher is never touched (owns firmware/coex).

Run as root: su -c '/data/data/com.termux/files/usr/bin/python3 bt_reset.py'
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
        print("sh %s failed: %s" % (" ".join(cmd), e), flush=True)
        return None


def hal_state():
    r = sh(["getprop", "init.svc.bluetooth-1-1"])
    return r.stdout.strip() if r else "?"


def bt_off():
    print("svc bluetooth disable ...", flush=True)
    sh(["svc", "bluetooth", "disable"])
    for _ in range(15):
        if hal_state() == "stopped":
            print("HAL stopped via svc", flush=True)
            return True
        time.sleep(1)
    print("svc path slow; stop bluetooth-1-1 + force-stop app ...", flush=True)
    sh(["stop", "bluetooth-1-1"])
    sh(["am", "force-stop", "com.android.bluetooth"])
    for _ in range(10):
        if hal_state() == "stopped":
            print("HAL stopped via stop-cmd", flush=True)
            return True
        time.sleep(1)
    return False


def bt_on():
    sh(["start", "bluetooth-1-1"])
    sh(["svc", "bluetooth", "enable"])
    for _ in range(30):
        if hal_state() == "running":
            return True
        time.sleep(1)
    return False


def probe():
    try:
        fd = os.open(STPBHCI, os.O_RDWR)
        print("OPEN_OK", flush=True)
    except OSError as e:
        eno = e.errno if isinstance(e.errno, int) else -1
        print("OPEN_FAILED errno=%d" % eno, flush=True)
        return False
    try:
        n = os.write(fd, HCI_RESET)
        print("WROTE %d bytes: %s" % (n, HCI_RESET.hex()), flush=True)
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
        print("READ %d bytes: %s" % (len(buf), buf.hex() if buf else "(timeout)"), flush=True)
        if buf.startswith(bytes((0x04, 0x0E))) and bytes((0x03, 0x0C)) in buf:
            print("RESULT: H4_COMMAND_COMPLETE_FOR_RESET -> framing CONFIRMED", flush=True)
            return True
        print("RESULT: unexpected response (see hex above)", flush=True)
        return False
    finally:
        os.close(fd)


def main():
    print("HAL state before: %s" % hal_state(), flush=True)
    try:
        if bt_off():
            print("BT stack down; probing", flush=True)
        else:
            print("WARN: HAL still %s; probing anyway" % hal_state(), flush=True)
        probe()
    finally:
        print("restoring Android BT ...", flush=True)
        print("BT restored" if bt_on() else "WARN: BT NOT back (HAL=%s); run: start bluetooth-1-1" % hal_state(), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
