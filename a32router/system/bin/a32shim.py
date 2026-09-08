#!/usr/bin/env python3
"""a32shim.py — phone-side shim daemon for the a32router Magisk module.

Two services, both bound ONLY to the USB RNDIS address (default 192.168.50.1):

  wifi control  TCP <BIND>:33601  line-delimited JSON, one req -> one resp
  BT HCI relay  TCP <BIND>:33600  binary framed HCI (see docs/arch-bt-hci.md)

WiFi commands (request {"cmd": ...}):
  status                        -> current connection state
  scan                          -> trigger scan, return cached results
  list                          -> latest scan results (no new scan)
  networks                      -> saved networks
  connect {ssid,sec,pass?}      -> cmd wifi connect-network
  forget {ssid}                 -> cmd wifi forget-network
  enable {on:bool}              -> wifi on/off

BT relay (BT_ENABLE=1 and HAL stopped, else port closed):
  [magic u32 BE 0xA3B70101][len u32 BE][type u8][payload]
  type: 0x01 cmd, 0x02 ACL, 0x03 SCO, 0x04 event, 0x05 ISO, 0xfe vendor
  Phone->PC direction: H4 stream from /dev/stpbt is split into HCI packets
  (H4 type byte + header-determined length). If the stream is NOT H4
  (MTK STP mux), packets are forwarded as type 0xfe raw chunks for analysis.

Only stdlib. Logs to stderr (module captures to a32router.log).
"""
import json
import os
import select
import socket
import struct
import subprocess
import sys
import threading
import time

MAGIC = 0xA3B70101
BIND = os.environ.get("SHIM_BIND", "192.168.50.1")
def _port(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        return default


WIFI_PORT = _port("SHIM_WIFI_PORT", 33601)
BT_PORT = _port("SHIM_BT_PORT", 33600)
BT_ENABLE = os.environ.get("BT_EXPORT", "0") == "1"
STPHCI = "/dev/stpbt"

HCI_RESET = bytes((0x01, 0x03, 0x0C, 0x00))


def log(msg):
    sys.stderr.write("%s a32shim: %s\n" % (
        time.strftime("%m-%d %H:%M:%S"), msg))
    sys.stderr.flush()


def sh(*args, timeout=25):
    try:
        p = subprocess.run(list(args), capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 99, "exec-fail: %s" % e


# ---------------- WiFi control ----------------

def wifi_status():
    rc, wifi = sh("cmd", "wifi", "status")
    rc2, iface = sh("cmd", "wifi", "get-country-code")
    return {"ok": rc == 0, "status": wifi.strip(), "country": iface.strip()}


def wifi_scan():
    sh("cmd", "wifi", "start-scan", timeout=15)
    # poll for fresh results (scan dwell varies by driver)
    last = {"ok": True, "results": []}
    for _ in range(6):
        time.sleep(2)
        last = wifi_list()
        if len(last.get("results", [])) > 1:
            break
    return last


def wifi_list():
    rc, out = sh("cmd", "wifi", "list-scan-results")
    nets = []
    for line in out.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("BSSID"):
            continue  # table header, not a result
        # Data lines are whitespace-separated (MAC freq rssi age ssid flags)
        # with no commas on this ROM; keep every non-header line.
        nets.append(s)
    return {"ok": rc == 0, "results": nets}


def wifi_networks():
    rc, out = sh("cmd", "wifi", "list-networks")
    return {"ok": rc == 0, "networks": out.strip()}


def wifi_connect(req):
    ssid = req.get("ssid", "")
    sec = req.get("sec", "wpa2")
    pas = req.get("pass")
    if not ssid or sec not in ("open", "owe", "wpa2", "wpa3", "wep"):
        return {"ok": False, "err": "need ssid + sec open|owe|wpa2|wpa3|wep"}
    args = ["cmd", "wifi", "connect-network", ssid, sec]
    if pas:
        args.append(pas)
    rc, out = sh(*args, timeout=30)
    # NOTE: after Android reconnects, a32routerd's tick detects the new
    # upstream (address + default route) and re-establishes forwarding
    # automatically — no PC-side action needed beyond waiting ~10s.
    return {"ok": rc == 0, "out": out.strip()}


def wifi_forget(req):
    ssid = req.get("ssid", "")
    if not ssid:
        return {"ok": False, "err": "need ssid"}
    rc, out = sh("cmd", "wifi", "forget-network", ssid)
    return {"ok": rc == 0, "out": out.strip()}


def wifi_enable(req):
    on = req.get("on", True)
    rc, out = sh("cmd", "wifi", "set-wifi-enabled",
                 "enabled" if on else "disabled")
    return {"ok": rc == 0, "out": out.strip()}


WIFI_CMDS = {
    "status": lambda r: wifi_status(),
    "scan": lambda r: wifi_scan(),
    "list": lambda r: wifi_list(),
    "networks": lambda r: wifi_networks(),
    "connect": wifi_connect,
    "forget": wifi_forget,
    "enable": wifi_enable,
}


def handle_wifi(conn):
    try:
        f = conn.makefile("r", encoding="utf-8", errors="replace")
        line = f.readline(65536)
        if not line:
            return
        try:
            req = json.loads(line)
        except Exception:
            conn.sendall(b'{"ok": false, "err": "bad json"}\n')
            return
        fn = WIFI_CMDS.get(req.get("cmd", ""))
        resp = fn(req) if fn else {"ok": False, "err": "unknown cmd"}
        conn.sendall((json.dumps(resp) + "\n").encode())
    except Exception as e:
        try:
            conn.sendall(('{"ok": false, "err": %s}\n'
                          % json.dumps(str(e))).encode())
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def wifi_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((BIND, WIFI_PORT))
    srv.listen(4)
    log("wifi control on %s:%d" % (BIND, WIFI_PORT))
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle_wifi, args=(conn,),
                         daemon=True).start()


# ---------------- BT HCI relay ----------------

def hal_stopped():
    rc, out = sh("getprop", "init.svc.bluetooth-1-1")
    return out.strip() == "stopped"


def h4_split(buf):
    """Split leading H4 HCI packets from buf. Returns (packets, rest) where
    packets are (type, payload-with-header) tuples."""
    pkts = []
    i = 0
    n = len(buf)
    while i < n:
        t = buf[i]
        if t == 0x04:  # event: type + evt(1) + len(1)
            if i + 3 > n:
                break
            ln = buf[i + 2]
            if i + 3 + ln > n:
                break
            pkts.append((t, bytes(buf[i + 1:i + 3 + ln])))
            i += 3 + ln
        elif t == 0x02 or t == 0x05:  # ACL/ISO: type + handle(2) + len(2)
            if i + 5 > n:
                break
            ln = buf[i + 3] | (buf[i + 4] << 8)
            if i + 5 + ln > n:
                break
            pkts.append((t, bytes(buf[i + 1:i + 5 + ln])))
            i += 5 + ln
        elif t == 0x01 or t == 0x03:  # cmd / SCO (phone->PC unlikely)
            break  # need length semantics; treat rest as raw below
        else:
            break
    return pkts, bytes(buf[i:])


def send_frame(sock, ptype, payload):
    # Wire format (9-byte header): [magic u32 BE][len u32 BE][type u8][payload]
    # with len = len(payload) + 1. Must match pump_net_to_dev's parser.
    sock.sendall(struct.pack(">IIB", MAGIC, len(payload) + 1, ptype) + payload)


def ensure_hal_stopped():
    """Take the radio: stop Android BT so stpbt is free. Bounded waits."""
    if hal_stopped():
        return True
    # NOTE: `svc bluetooth disable` never completes the stop on this ROM
    # (always falls through after 15 s), so fire it without waiting for
    # gracefulness and go straight to the effective stop. Saves ~15 s of
    # BlueZ init timeout on the PC side.
    log("taking radio: svc disable (async) + stop bluetooth-1-1")
    sh("svc", "bluetooth", "disable")
    sh("stop", "bluetooth-1-1")
    sh("am", "force-stop", "com.android.bluetooth")
    # Race guard: the dying app may still hold stpbt for ~1s. Opening before
    # it exits risks a second live fd that steals our Command Complete.
    # Wait until BOTH the HAL is stopped AND the app process is gone.
    for _ in range(15):
        if hal_stopped() and not bt_app_alive():
            return True
        time.sleep(1)
    return hal_stopped() and not bt_app_alive()


def bt_app_alive():
    try:
        _, out = sh("ps", "-A")
    except Exception:
        return True  # unknown: assume alive (safe = do not open yet)
    return "com.android.bluetooth" in (out or "")


def ensure_hal_started():
    """Give the radio back to Android. Bounded wait."""
    sh("start", "bluetooth-1-1")
    sh("svc", "bluetooth", "enable")
    for _ in range(30):
        if not hal_stopped():
            return True
        time.sleep(1)
    return False


def bt_relay_session(sock):
    """Own /dev/stpbt for one PC client. Returns when client disconnects."""
    if not ensure_hal_stopped():
        raise RuntimeError("HAL would not stop; radio busy")
    fd = os.open(STPHCI, os.O_RDWR)
    try:
        want, sent = len(HCI_RESET), 0
        while sent < want:
            n = os.write(fd, HCI_RESET[sent:])
            if n <= 0:
                raise RuntimeError("stpbt write returned %d" % n)
            sent += n
        log("HCI_Reset sent (%d bytes), awaiting Command-Complete" % sent)
    except OSError as e:
        os.close(fd)
        raise RuntimeError("stpbt write failed: %s" % e)
    # Priming read in THIS thread: if the first read only works in the
    # opener thread (driver quirk?), consume + forward it here so the
    # client still gets its Command Complete.
    try:
        r, _, _ = select.select([fd], [], [], 8)
        if r:
            early = os.read(fd, 256)
            log("priming read %d bytes: %s" % (len(early), early.hex()))
            pkts, _ = h4_split(early)
            for t, p in pkts:
                send_frame(sock, t, p)
        else:
            log("priming read timeout (no data in 8 s)")
    except OSError as e:
        log("priming read OSError: %s" % e)
    stop = threading.Event()

    def pump_dev_to_net():
        # NOTE: this driver's read() returns EIO instead of blocking when its
        # rx queue is empty. Never read blind: select() first, read only when
        # readable. Consecutive unexpected EIOs mean the device went away.
        buf = b""
        eios = 0
        log("reader thread started (fd=%d)" % fd)
        try:
            try:
                while not stop.is_set():
                    r, _, _ = select.select([fd], [], [], 1.0)
                    if stop.is_set():
                        break
                    if not r:
                        continue
                    try:
                        chunk = os.read(fd, 4096)
                    except OSError as e:
                        eios += 1
                        log("reader os.read OSError #%d: %s" % (eios, e))
                        if eios >= 10:
                            break
                        time.sleep(0.1)
                        continue
                    eios = 0
                    if not chunk:
                        log("reader EOF")
                        break
                    log("dev read %d bytes: %s" % (len(chunk), chunk[:32].hex()))
                    buf += chunk
                    pkts, buf = h4_split(buf)
                    for t, p in pkts:
                        send_frame(sock, t, p)
                    if len(buf) > 65536:  # non-H4 stream: forward raw for RE
                        send_frame(sock, 0xFE, buf)
                        log("non-H4 bytes forwarded raw (%d)" % len(buf))
                        buf = b""
            except Exception as e:
                log("reader DIED: %r" % e)
        finally:
            stop.set()
            log("reader thread exiting")

    def pump_net_to_dev():
        try:
            while not stop.is_set():
                hdr = b""
                while len(hdr) < 9 and not stop.is_set():
                    c = sock.recv(9 - len(hdr))
                    if not c:
                        return
                    hdr += c
                magic = struct.unpack(">I", hdr[:4])[0]
                ln = struct.unpack(">I", hdr[4:8])[0]
                ptype = hdr[8]
                if magic != MAGIC or ln > 262144:
                    log("bad frame magic/len; dropping client")
                    return
                payload = b""
                while len(payload) < ln - 1:
                    c = sock.recv(ln - 1 - len(payload))
                    if not c:
                        return
                    payload += c
                if ptype in (0x01, 0x02, 0x03):
                    os.write(fd, bytes((ptype,)) + payload)
                else:
                    log("ignoring PC->dev type 0x%02x" % ptype)
        finally:
            stop.set()

    t = threading.Thread(target=pump_dev_to_net, daemon=True)
    t.start()
    try:
        pump_net_to_dev()
    finally:
        stop.set()
        try:
            os.close(fd)
        except OSError:
            pass
        if ensure_hal_started():
            log("BT relay session ended; radio returned to Android")
        else:
            log("BT relay session ended; WARN radio NOT returned (start bluetooth-1-1 manually)")


def bt_server():
    if not BT_ENABLE:
        log("BT relay disabled (BT_EXPORT!=1)")
        return
    # Listen always; each client session takes the radio on connect
    # (ensure_hal_stopped) and returns it on disconnect (ensure_hal_started).
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((BIND, BT_PORT))
    srv.listen(1)
    log("BT HCI relay on %s:%d" % (BIND, BT_PORT))
    while True:
        conn, addr = srv.accept()
        try:
            peer = "%s:%d" % conn.getpeername()
        except OSError:
            peer = str(addr)
        log("BT client connected from %s" % peer)
        try:
            bt_relay_session(conn)
        except Exception as e:
            log("BT session error: %s" % e)
        finally:
            try:
                conn.close()
            except Exception:
                pass


def main():
    # Only bind when the USB address exists (else retry in wrapper loop).
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((BIND, 0))
        s.close()
    except OSError:
        log("bind address %s absent; exiting (wrapper retries)" % BIND)
        return 2
    threading.Thread(target=wifi_server, daemon=True).start()
    threading.Thread(target=bt_server, daemon=True).start()
    log("shim up (wifi:%d bt:%d bt_enable=%d)" %
        (WIFI_PORT, BT_PORT, BT_ENABLE))
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    sys.exit(main() or 0)
