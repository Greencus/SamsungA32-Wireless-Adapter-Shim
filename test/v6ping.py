#!/data/data/com.termux/files/usr/bin/python3
"""v6ping.py — minimal ICMPv6 echo for environments without ping6
(Android toybox ping cannot do IPv6 literals). Raw socket, needs root.

Usage: su -c '/data/data/com.termux/files/usr/bin/python3 v6ping.py <dst> [count]'
"""
import socket
import struct
import sys
import time


def cksum(data):
    if len(data) % 2:
        data += b"\x00"
    s = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


def main():
    if len(sys.argv) < 2:
        print("usage: v6ping.py <dst-ipv6> [count]", file=sys.stderr)
        return 2
    dst, count = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 3
    # source address the kernel would use (for the checksum pseudo-header)
    u = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM, 0)
    try:
        u.connect((dst, 0, 0, 0))
        src = u.getsockname()[0]
    except OSError as e:
        print("no route to %s: %s" % (dst, e))
        return 1
    finally:
        u.close()
    s = socket.socket(socket.AF_INET6, socket.SOCK_RAW,
                      socket.IPPROTO_ICMPV6)
    s.settimeout(3)
    ident = 0xA32 & 0xFFFF
    ok = 0
    for seq in range(count):
        body = struct.pack("!BBHHH", 128, 0, 0, ident, seq) + \
            struct.pack("!d", time.time())
        pseudo = (socket.inet_pton(socket.AF_INET6, src) +
                  socket.inet_pton(socket.AF_INET6, dst) +
                  struct.pack("!I3xB", len(body), 58))
        c = cksum(pseudo + body)
        pkt = body[:2] + struct.pack("!H", c) + body[4:]
        t0 = time.time()
        try:
            s.sendto(pkt, (dst, 0, 0, 0))
            data, _ = s.recvfrom(256)
        except OSError as e:
            print("seq=%d error: %s" % (seq, e))
            time.sleep(1)
            continue
        dt = (time.time() - t0) * 1000
        print("seq=%d got type=%d code=%d (%d bytes) from %s" %
              (seq, data[0] if len(data) > 0 else -1,
               data[1] if len(data) > 1 else -1, len(data), _[0]
               if isinstance(_, tuple) else _))
        if len(data) >= 8 and data[0] == 129:
            rtype, rseq = data[0], struct.unpack("!H", data[6:8])[0]
            print("seq=%d reply in %.1f ms" % (seq, dt) if rseq == seq
                  else "seq=%d UNMATCHED reply" % seq)
            ok += 1
        else:
            print("seq=%d non-echo-reply (%d bytes)" % (seq, len(data)))
        time.sleep(1)
    print("%d/%d replies" % (ok, count))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
