#!/usr/bin/env python3
"""a32ctl.py — PC CLI for the A32 shim's WiFi control service.

Usage:
  a32ctl.py status
  a32ctl.py scan                (triggers scan, prints results)
  a32ctl.py list                (cached scan results)
  a32ctl.py networks            (saved networks)
  a32ctl.py connect <ssid> <open|owe|wpa2|wpa3|wep> [passphrase]
  a32ctl.py forget <ssid>
  a32ctl.py enable|disable

After `connect`, wait ~10s: the phone reconnects and a32routerd
re-establishes forwarding automatically. Then re-run `status`.
No root needed (plain TCP to 192.168.50.1:33601).
"""
import json
import socket
import sys

HOST, PORT = "192.168.50.1", 33601


def req(obj):
    s = socket.create_connection((HOST, PORT), timeout=45)
    s.sendall((json.dumps(obj) + "\n").encode())
    data = b""
    while not data.endswith(b"\n"):
        c = s.recv(65536)
        if not c:
            break
        data += c
    s.close()
    try:
        return json.loads(data.decode())
    except (ValueError, UnicodeDecodeError) as e:
        return {"ok": False, "err": "bad response: %s" % e}


def main():
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "help"):
        print(__doc__)
        return 0
    c = a[0]
    if c == "status":
        r = req({"cmd": "status"})
        print(r.get("status", r))
    elif c in ("scan", "list", "networks"):
        r = req({"cmd": c})
        items = r.get("results", r.get("networks", r))
        if isinstance(items, list):
            print("\n".join(items))
        else:
            print(items)
    elif c == "connect" and len(a) >= 3:
        obj = {"cmd": "connect", "ssid": a[1], "sec": a[2]}
        if len(a) >= 4:
            obj["pass"] = a[3]
        r = req(obj)
        print("OK: phone connecting; wait ~10s then run `status`" if r.get("ok")
              else "FAIL: %s" % r)
    elif c == "forget" and len(a) >= 2:
        print(req({"cmd": "forget", "ssid": a[1]}))
    elif c in ("enable", "disable"):
        print(req({"cmd": "enable", "on": c == "enable"}))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
