#!/usr/bin/env python3
"""vhci-relay.py — PC side of the A32 BT shim. VHCI fd backend: BlueZ manages
the new hciX as a NORMAL controller; this script ferries H4 frames between
/dev/vhci and the phone's TCP relay (192.168.50.1:33600), whose radio
answers. bluetoothd must be RUNNING (it drives all HCI setup/scan).

Framing (TCP): [magic u32 BE 0xA3B70101][len u32 BE][type u8][payload]
  type: 0x01 cmd, 0x02 ACL, 0x03 SCO, 0x04 event, 0x05 ISO, 0xfe vendor-raw
Framing (vhci fd): plain H4 ([type u8][HCI packet]).

Startup order: prove the radio live (framed Reset) BEFORE creating the
kernel device — BlueZ probes within ~1s while radio takeover takes longer.

Usage (as root):  modprobe hci_vhci && ./vhci-relay.py [phone_ip]
"""
import os
import socket
import struct
import sys

MAGIC = 0xA3B70101
HCI_VENDOR_PKT = 0xFF
PHONE = sys.argv[1] if len(sys.argv) > 1 else "192.168.50.1"
PORT = 33600


def h4_split(buf):
    """Split leading H4 HCI packets from buf. Returns (packets, rest) where
    packets are (type, payload-without-type) tuples."""
    pkts = []
    i = 0
    n = len(buf)
    while i < n:
        t = buf[i]
        if t == 0x01:  # cmd: opcode(2) + len(1)
            if i + 4 > n:
                break
            ln = buf[i + 3]
            if i + 4 + ln > n:
                break
            pkts.append((t, bytes(buf[i + 1:i + 4 + ln])))
            i += 4 + ln
        elif t == 0x02 or t == 0x05:  # ACL/ISO: handle(2) + len(2)
            if i + 5 > n:
                break
            ln = buf[i + 3] | (buf[i + 4] << 8)
            if i + 5 + ln > n:
                break
            pkts.append((t, bytes(buf[i + 1:i + 5 + ln])))
            i += 5 + ln
        elif t == 0x03:  # SCO: handle(2) + len(1)
            if i + 4 > n:
                break
            ln = buf[i + 3]
            if i + 4 + ln > n:
                break
            pkts.append((t, bytes(buf[i + 1:i + 4 + ln])))
            i += 4 + ln
        elif t == 0x04:  # event: evt(1) + len(1)
            if i + 3 > n:
                break
            ln = buf[i + 2]
            if i + 3 + ln > n:
                break
            pkts.append((t, bytes(buf[i + 1:i + 3 + ln])))
            i += 3 + ln
        else:
            break
    return pkts, bytes(buf[i:])


def prewarm(ph):
    """Prove the radio chain BEFORE creating the kernel device: send a
    framed HCI_Reset, await the framed Command Complete. Returns True if
    the phone radio answered (covers the whole TCP+stpbt path)."""
    ph.sendall(struct.pack(">IIB", MAGIC, 4, 0x01) +
               bytes((0x03, 0x0C, 0x00)))
    buf = b""
    while len(buf) < 9:
        c = ph.recv(9 - len(buf))
        if not c:
            print("prewarm: phone closed connection", flush=True)
            return False
        buf += c
    magic, ln = struct.unpack(">II", buf[:8])
    ptype = buf[8]
    payload = b""
    while len(payload) < ln - 1:
        c = ph.recv(ln - 1 - len(payload))
        if not c:
            print("prewarm: phone closed mid-frame", flush=True)
            return False
        payload += c
    ok = (magic == MAGIC and ptype == 0x04 and
          payload[:1] == b"\x0e" and bytes((0x03, 0x0C)) in payload)
    print("prewarm: %s (type=0x%02x len=%d %s)" %
          ("RADIO LIVE" if ok else "unexpected reply", ptype,
           len(payload), payload.hex()), flush=True)
    return ok


def main():
    import glob
    import time
    # Phone radio FIRST (may take ~30 s for takeover), kernel device SECOND.
    print("connecting to phone %s:%d ..." % (PHONE, PORT), flush=True)
    try:
        ph = socket.create_connection((PHONE, PORT), timeout=120)
    except OSError as e:
        print("phone TCP failed: %s" % e, file=sys.stderr)
        return 1
    ph.settimeout(120)
    print("proving radio (framed Reset; takeover can take ~30 s) ...",
          flush=True)
    if not prewarm(ph):
        print("phone radio did not answer; aborting (no dead hci created)",
              file=sys.stderr)
        ph.close()
        return 1
    ph.settimeout(None)
    # The sysfs dir name IS the index: /sys/class/bluetooth/hciN = index N.
    before_dirs = set(glob.glob("/sys/class/bluetooth/hci*"))
    print("existing controllers: %s" % sorted(before_dirs), flush=True)
    try:
        # Keep the fd open for the whole session (close = device gone).
        vhci = os.open("/dev/vhci", os.O_RDWR)
    except OSError as e:
        print("cannot open /dev/vhci: %s (try: sudo modprobe hci_vhci)" % e,
              file=sys.stderr)
        return 1
    # Bootstrap = exactly [0xFF, opcode]; opcode 0x00 = plain controller
    # (bits: 0x40 external-config, 0x80 raw-device; keep both clear so
    # BlueZ manages it). Anything else (1 byte, 8 bytes) = -EINVAL.
    print("vhci opened; sending 2-byte bootstrap ...", flush=True)
    try:
        os.write(vhci, bytes((HCI_VENDOR_PKT, 0x00)))
    except OSError as e:
        print("vhci bootstrap failed: %s" % e, file=sys.stderr)
        return 1
    print("waiting for new hci device ...", flush=True)
    dev = None
    for _ in range(50):
        now = set(glob.glob("/sys/class/bluetooth/hci*"))
        new = sorted(now - before_dirs)
        if new:
            dev = new[0]
            break
        time.sleep(0.1)
    if not dev:
        print("no hci device appeared", file=sys.stderr)
        return 1
    try:
        idx = int(os.path.basename(dev)[3:])
    except ValueError:
        print("cannot parse index from %s" % dev, file=sys.stderr)
        return 1
    print("using hci%d" % idx, flush=True)

    # VHCI fd backend: BlueZ manages hciN as a NORMAL controller. Frames
    # BlueZ sends DOWN (cmds, outgoing data) are READ from the vhci fd as
    # H4; frames coming UP (events, incoming data) are WRITTEN to the fd
    # as H4. bluetoothd must be RUNNING (it drives all HCI setup).
    print("pumping %s:%d <-> hci%d (Ctrl-C to stop)" % (PHONE, PORT, idx),
          flush=True)

    import threading
    stop = threading.Event()

    import errno
    import glob as _glob

    def dev_gone():
        return not _glob.glob("/sys/class/bluetooth/hci%d" % idx)

    def vhci_to_phone():
        # BlueZ DOWN frames (cmds + outgoing data) arrive as H4 on the fd.
        buf = stream_head
        try:
            while not stop.is_set():
                try:
                    chunk = os.read(vhci, 65536)
                except OSError as e:
                    print("vhci read failed: %s (device gone: %s)" %
                          (e, dev_gone()), flush=True)
                    break
                if not chunk:
                    print("vhci read EOF", flush=True)
                    break
                buf += chunk
                pkts, buf = h4_split(buf)
                for t, p in pkts:
                    print("C->P type=0x%02x len=%d %s" %
                          (t, len(p), p[:16].hex()), flush=True)
                    ph.sendall(struct.pack(">II", MAGIC, len(p) + 1) +
                               bytes((t,)) + p)
                if len(buf) > 65536:
                    print("vhci non-H4 bytes (%d); dropping" % len(buf),
                          flush=True)
                    buf = b""
        finally:
            stop.set()

    def phone_to_vhci():
        # UP frames (events + incoming data) go to the fd as H4.
        try:
            buf = b""
            while not stop.is_set():
                try:
                    chunk = ph.recv(65536)
                except OSError as e:
                    print("phone recv failed: %s" % e, flush=True)
                    break
                if not chunk:
                    print("phone closed TCP (radio returned to Android?)",
                          flush=True)
                    break
                buf += chunk
                while len(buf) >= 9:
                    magic, ln = struct.unpack(">II", buf[:8])
                    if magic != MAGIC or ln > 262144 or ln < 1:
                        print("bad frame; resync", flush=True)
                        buf = buf[1:]
                        continue
                    if len(buf) < 8 + ln:
                        break
                    ptype = buf[8]
                    payload = buf[9:8 + ln]
                    buf = buf[8 + ln:]
                    if ptype in (0x01, 0x02, 0x03, 0x04, 0x05):
                        print("P->C type=0x%02x len=%d %s" %
                              (ptype, len(payload), payload[:16].hex()),
                              flush=True)
                        try:
                            os.write(vhci, bytes((ptype,)) + payload)
                        except OSError as e:
                            print("vhci write failed: %s (device gone: %s)"
                                  % (e, dev_gone()), flush=True)
                            if e.errno in (errno.ENXIO, errno.ENODEV):
                                print("kernel device hci%d is gone; "
                                      "capture `sudo dmesg|tail` now" % idx,
                                      flush=True)
                            break
                    elif ptype == 0xfe:
                        print("P->C vendor-raw %d bytes (dropped)" %
                              len(payload), flush=True)
                    else:
                        print("P->C unknown type 0x%02x (dropped)" % ptype,
                              flush=True)
        finally:
            stop.set()

    import select
    # The driver queues a 4-byte bootstrap announcement, [0xFF, opcode,
    # id_lo, id_hi], for the first read. It is NOT H4 (0xFF never parses),
    # so leaving it in the stream poisons h4_split forever: every BlueZ
    # command piles up behind it unparsed and init times out (-110 storm).
    # Consume exactly those 4 bytes here; anything beyond is live traffic.
    print("draining vhci bootstrap announcement ...", flush=True)
    pending = b""
    t0 = time.time()
    while len(pending) < 4 and time.time() - t0 < 3:
        r, _, _ = select.select([vhci], [], [], max(0, 3 - (time.time() - t0)))
        if not r:
            break
        try:
            c = os.read(vhci, 65536)
        except OSError:
            break
        if not c:
            break
        pending += c
    stream_head = b""
    if pending[:1] == b"\xff" and len(pending) >= 4:
        print("consumed bootstrap echo %s (dropped)" % pending[:4].hex(),
              flush=True)
        stream_head = pending[4:]
    elif pending:
        print("no echo found (%s); keeping bytes" % pending[:4].hex(),
              flush=True)
        stream_head = pending
    if stream_head:
        print("replaying %d live bytes into stream" % len(stream_head),
              flush=True)

    threading.Thread(target=vhci_to_phone, daemon=True).start()
    phone_to_vhci()
    stop.set()
    print("disconnected", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
