#!/usr/bin/env python3
"""bt_client.py — minimal PC-side test client for the phone BT relay.

Connects to <phone>:33600, sends one framed HCI_Reset, prints the framed
response. Proves the phone relay path without needing root/VHCI.

Usage: ./bt_client.py [phone_ip]
"""
import socket
import struct
import sys

MAGIC = 0xA3B70101
PHONE = sys.argv[1] if len(sys.argv) > 1 else "192.168.50.1"
PORT = 33600


def send_frame(s, ptype, payload):
    s.sendall(struct.pack(">IIB", MAGIC, len(payload) + 1, ptype) + payload)


def read_exact(s, n):
    buf = b""
    while len(buf) < n:
        c = s.recv(n - len(buf))
        if not c:
            raise RuntimeError("connection closed")
        buf += c
    return buf


def main():
    s = socket.create_connection((PHONE, PORT), timeout=90)
    try:
        send_frame(s, 0x01, bytes((0x03, 0x0C, 0x00)))
        print("sent framed HCI_Reset; awaiting response ...", flush=True)
        hdr = read_exact(s, 9)
        magic, ln, ptype = struct.unpack(">IIB", hdr)
        payload = read_exact(s, ln - 1)
        print("frame: magic=0x%08X len=%d type=0x%02X payload=%s" % (magic, ln, ptype, payload.hex()))
        if magic == MAGIC and ptype == 0x04 and payload.startswith(bytes((0x0E,))) and bytes((0x03, 0x0C)) in payload:
            print("RESULT: Command Complete for Reset relayed over TCP -> RELAY WORKS")
            return 0
        print("RESULT: unexpected frame")
        return 1
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
