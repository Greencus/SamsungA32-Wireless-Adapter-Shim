#!/data/data/com.termux/files/usr/bin/python3
"""bt_probe.py — RE-5 step 1 (SAFE): test exclusive-open of /dev/stpbt.

Does NO I/O: opens O_RDWR|O_NONBLOCK, reports the outcome, closes immediately.
Expected while Android BT stack is up: OSError EBUSY (proves single-client
ownership). EACCES would implicate SELinux (check dmesg avc). Success while
the stack is up would mean non-exclusive access (unexpected).

Run as root:  su -c '/data/data/com.termux/files/usr/bin/python3 bt_probe.py'
"""
import errno
import os
import sys

STPBHCI = "/dev/stpbt"


def main():
    try:
        fd = os.open(STPBHCI, os.O_RDWR | os.O_NONBLOCK)
    except OSError as e:
        eno = e.errno if isinstance(e.errno, int) else -1
        print("OPEN_FAILED errno=%d (%s)" % (eno, errno.errorcode.get(eno, "?")))
        if eno == errno.EBUSY:
            print("=> device held by another client (expected: Android BT stack)")
        elif eno == errno.EACCES:
            print("=> permission/SELinux denial; check dmesg for avc")
        return 0
    print("OPEN_OK (unexpected while stack is up; closing immediately)")
    os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
