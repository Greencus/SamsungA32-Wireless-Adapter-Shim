#!/data/data/com.termux/files/usr/bin/python3
"""a32radvd.py — minimal ICMPv6 Router Advertiser for the USB link.

Announces ONE upstream /64 (given as arg) on the RNDIS interface so the PC
SLAACs an address in it. No DHCPv6, no RDNSS (PC keeps its IPv4-provided
DNS — DNS is transport-independent). Started/stopped per-prefix by
a32routerd; it owns all lifecycle, this tool just advertises.

  a32radvd.py <iface> <prefix/len> [router_lifetime_s]
  a32radvd.py --selftest     # build+verify one RA offline, no sockets

Behavior: multicast RA to ff02::1 every 15 s + burst on start; answers
Router Solicitations immediately. Requires root (raw ICMPv6 socket).
Only stdlib.
"""
import ipaddress
import os
import select
import socket as _s
import struct
import subprocess
import sys
import time

ALL_NODES = "ff02::1"
HOP_LIMIT = 64
VALID_LIFE = 120000       # 33 h
PREFERRED_LIFE = 120000   # 33 h
ADV_INTERVAL = 15


def log(msg):
    sys.stderr.write("%s a32radvd: %s\n" %
                     (time.strftime("%m-%d %H:%M:%S"), msg))
    sys.stderr.flush()


def cksum(data):
    if len(data) % 2:
        data += b"\x00"
    s = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


def mac_of(iface):
    try:
        with open("/sys/class/net/%s/address" % iface) as f:
            parts = f.read().strip().split(":")
    except OSError as e:
        raise RuntimeError("cannot read MAC of %s: %s" % (iface, e))
    if len(parts) != 6:
        raise ValueError("bad MAC on %s" % iface)
    return bytes(int(x, 16) for x in parts)


def lladdr_of(iface):
    out = subprocess.run(["ip", "-6", "addr", "show", "dev", iface,
                          "scope", "link"], capture_output=True, text=True,
                         timeout=10)
    for tok in out.stdout.split():
        if tok.startswith("fe80") and "/" in tok:
            return tok.split("/")[0]
    raise RuntimeError("no link-local on %s" % iface)


def build_ra(src_ll, src_mac, prefix, lifetime):
    pfx = ipaddress.ip_network(prefix, strict=False)
    body = struct.pack("!BBHBBHII", 134, 0, 0, HOP_LIMIT, 0, lifetime, 0, 0)
    body += struct.pack("!BB6s", 1, 1, src_mac)                       # SLLAO
    body += struct.pack("!BBHI", 5, 1, 0, 1500)                       # MTU
    body += struct.pack("!BBBBIII16s", 3, 4, pfx.prefixlen, 0xC0,
                        VALID_LIFE, PREFERRED_LIFE, 0,
                        pfx.network_address.packed)                   # PIO
    pseudo = (ipaddress.ip_address(src_ll).packed +
              ipaddress.ip_address(ALL_NODES).packed +
              struct.pack("!I3xB", len(body), 58))
    c = cksum(pseudo + body)
    return body[:2] + struct.pack("!H", c) + body[4:]


def selftest():
    pkt = build_ra("fe80::1", bytes((0x02, 0, 0, 0, 0, 0x01)),
                   "2604:abcd:1234:5678::/64", 1800)
    ipa = ipaddress.ip_address
    pseudo = (ipa("fe80::1").packed + ipa(ALL_NODES).packed +
              struct.pack("!I3xB", len(pkt), 58))
    if cksum(pseudo + pkt) != 0:
        print("SELFTEST FAIL: bad checksum: %s" % pkt.hex())
        return 1
    if pkt[0] != 134 or pkt[4] != HOP_LIMIT or len(pkt) != 16 + 8 + 8 + 32:
        print("SELFTEST FAIL: bad header/len: %s" % pkt.hex())
        return 1
    checks = [
        (pkt[6:8] == struct.pack("!H", 1800), "lifetime"),
        (pkt[16:18] == b"\x01\x01", "sllao tag"),
        (pkt[18:24] == bytes((0x02, 0, 0, 0, 0, 0x01)), "sllao mac"),
        (pkt[24:26] == b"\x05\x01", "mtu tag"),
        (int.from_bytes(pkt[28:32], "big") == 1500, "mtu value"),
        (pkt[32:34] == b"\x03\x04", "pio tag"),
        (pkt[34] == 64 and pkt[35] == 0xC0, "pio plen/flags"),
        (int.from_bytes(pkt[36:40], "big") == VALID_LIFE, "pio valid"),
        (int.from_bytes(pkt[40:44], "big") == PREFERRED_LIFE, "pio pref"),
        (pkt[48:56] == bytes.fromhex("2604abcd12345678"), "pio prefix"),
    ]
    for ok, name in checks:
        if not ok:
            print("SELFTEST FAIL: %s: %s" % (name, pkt.hex()))
            return 1
    print("SELFTEST OK (%d bytes): %s" % (len(pkt), pkt.hex()))
    return 0


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--selftest":
        return selftest()
    if len(sys.argv) < 3:
        print("usage: a32radvd.py <iface> <prefix/len> [lifetime_s]",
              file=sys.stderr)
        return 2
    iface, prefix = sys.argv[1], sys.argv[2]
    try:
        lifetime = int(sys.argv[3]) if len(sys.argv) > 3 else 1800
    except ValueError:
        print("bad lifetime: %s" % sys.argv[3], file=sys.stderr)
        return 2
    if not 0 <= lifetime <= 9000:
        print("bad lifetime %d: must be 0..9000 (RFC 4861); "
              "use 1800 default" % lifetime, file=sys.stderr)
        return 2
    try:
        ll = lladdr_of(iface)
        mac = mac_of(iface)
        ifindex = _s.if_nametoindex(iface)
    except (OSError, ValueError, RuntimeError) as e:
        log("iface setup failed: %s" % e)
        return 1
    try:
        s = _s.socket(_s.AF_INET6, _s.SOCK_RAW, _s.IPPROTO_ICMPV6)
    except OSError as e:
        log("raw socket failed (need root): %s" % e)
        return 1
    s.setsockopt(_s.IPPROTO_IPV6, _s.IPV6_MULTICAST_IF, ifindex)
    s.setsockopt(_s.IPPROTO_IPV6, _s.IPV6_MULTICAST_HOPS, 255)
    grp = _s.inet_pton(_s.AF_INET6, "ff02::2") + struct.pack("@I", ifindex)
    try:
        s.setsockopt(_s.IPPROTO_IPV6, _s.IPV6_JOIN_GROUP, grp)
    except OSError as e:
        log("WARN join ff02::2 failed (RS answers degraded): %s" % e)
    try:
        pkt = build_ra(ll, mac, prefix, lifetime)
    except ValueError as e:
        log("bad prefix %s: %s" % (prefix, e))
        return 1
    log("advertising %s on %s (lifetime %ds)" % (prefix, iface, lifetime))

    def send_ra():
        try:
            s.sendto(pkt, (ALL_NODES, 0, 0, ifindex))
        except OSError as e:
            log("send failed: %s" % e)

    for _ in range(2):
        send_ra()
        time.sleep(1)
    while True:
        r, _, _ = select.select([s], [], [], ADV_INTERVAL)
        if r:
            try:
                data, _ = s.recvfrom(256)
            except OSError:
                continue
            if data[:1] == b"\x85":      # Router Solicitation
                send_ra()
        else:
            send_ra()
    return 0


if __name__ == "__main__":
    sys.exit(main())
