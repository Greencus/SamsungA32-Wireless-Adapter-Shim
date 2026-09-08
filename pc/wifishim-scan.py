#!/usr/bin/env python3
"""wifishim-scan.py — Stage 1 of the PC Wi-Fi shim: make REAL surrounding
networks appear in NetworkManager on the hwsim STA interface.

How: polls phone scans over USB (192.168.50.1:33601), keeps an AP table,
and transmits those APs as 802.11 beacons + directed/broadcast probe
responses on the fake medium via the monitor interface. NM/wpa_supplicant
see genuine-looking BSS entries (real BSSIDs/SSIDs/RSSI/security).

 Association is NOT faked here (Stage 2/3: hostapd answers for real).
 Run as root (monitor iface, channel setting, raw sockets).

Usage: sudo python3 pc/wifishim-scan.py
State: /run/wifishim/sta_phy (written by wifishim-setup.sh).
"""
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time

PHONE, PORT = "192.168.50.1", 33601
MON = "mon1"
SCAN_EVERY = 45
AP_EXPIRE = 240


def log(msg):
    sys.stderr.write("%s wifishim-scan: %s\n" %
                     (time.strftime("%m-%d %H:%M:%S"), msg))
    sys.stderr.flush()


def phone_req(obj, timeout=90):
    s = socket.create_connection((PHONE, PORT), timeout=timeout)
    try:
        s.sendall((json.dumps(obj) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            c = s.recv(65536)
            if not c:
                break
            data += c
        return json.loads(data.decode())
    finally:
        s.close()


def parse_scan_line(line):
    """'BSSID FREQ RSSI AGE SSID... FLAGS' -> dict or None."""
    t = line.split()
    if len(t) < 5:
        return None
    try:
        freq, rssi, age = int(t[1]), int(t[2]), float(t[3])
    except ValueError:
        return None
    if t[0].count(":") != 5:
        return None
    if t[-1].startswith("["):
        flags, ssid = t[-1], " ".join(t[4:-1])
    else:
        flags, ssid = "", " ".join(t[4:])
    return {"bssid": t[0].lower(), "freq": freq, "rssi": rssi,
            "ssid": ssid, "flags": flags}


def freq_to_chan(f):
    if 2412 <= f <= 2484:
        return 14 if f == 2484 else 1 + (f - 2412) // 5
    if 5000 <= f <= 5900:
        return (f - 5000) // 5
    if 5955 <= f <= 7115:
        return (f - 5955) // 5 + 1
    return None


def rsn_ie(flags):
    """RSN IE for WPA2-PSK (incl. PSK+SAE transition, advertised as PSK).
    Returns None for auth types we can't back yet (SAE-only/EAP/WEP)."""
    f = flags.upper()
    if "PSK" not in f:
        return None  # SAE-only, EAP, WEP, OWE-only: skip in v1
    mfpc = "MFPC" in f
    ie = struct.pack("<H", 1)                       # version
    ie += bytes((0x00, 0x0F, 0xAC, 0x04))            # group: CCMP
    ie += struct.pack("<H", 1)                       # pairwise count
    ie += bytes((0x00, 0x0F, 0xAC, 0x04))            # pairwise: CCMP
    ie += struct.pack("<H", 1)                       # akm count
    ie += bytes((0x00, 0x0F, 0xAC, 0x02))            # akm: PSK
    ie += struct.pack("<H", 0x0080 if mfpc else 0)   # caps (+MFPC)
    if mfpc:
        ie += struct.pack("<H", 0)                   # pmkid count
        ie += bytes((0x00, 0x0F, 0xAC, 0x06))        # mgmt: AES-CMAC
    return bytes((0x30, len(ie))) + ie


RATES = bytes((0x82, 0x84, 0x8B, 0x96, 0x0C, 0x12, 0x18, 0x24))


def ies_for(ap, chan):
    ssid = ap["ssid"].encode("utf-8", "replace")[:32]
    out = bytes((0x00, len(ssid))) + ssid
    out += bytes((0x01, len(RATES))) + RATES
    out += bytes((0x03, 0x01, chan))
    out += bytes((0x05, 0x04, 0x00, 0x01, 0x00, 0x00))  # TIM
    if chan <= 14:
        out += bytes((0x2A, 0x01, 0x00))               # ERP
    rsn = rsn_ie(ap["flags"]) if ap["secured"] else None
    if rsn:
        out += rsn
    return out


def beacon(ap, chan, ts, seq):
    bssid = bytes(int(x, 16) for x in ap["bssid"].split(":"))
    caps = 0x0431 if ap["secured"] else 0x0421
    h = struct.pack("<H", 0x0080) + b"\x00\x00" + b"\xff" * 6
    h += bssid + bssid + struct.pack("<H", (seq << 4) & 0xFFF0)
    fixed = struct.pack("<QHH", ts, 100, caps)
    return h + fixed + ies_for(ap, chan)


def probe_resp(ap, chan, ts, seq, prober):
    bssid = bytes(int(x, 16) for x in ap["bssid"].split(":"))
    h = struct.pack("<H", 0x0050) + b"\x00\x00" + prober
    h += bssid + bssid + struct.pack("<H", (seq << 4) & 0xFFF0)
    fixed = struct.pack("<QHH", ts, 100,
                        0x0431 if ap["secured"] else 0x0421)
    return h + fixed + ies_for(ap, chan)


RADIOTAP = bytes((0x00, 0x00, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00))


def classify(flags):
    f = flags.upper()
    if "PSK" in f:
        return True
    if "SAE" in f or "WEP" in f or "EAP" in f:
        return None  # secured but not backable in v1
    return False  # open


class Table:
    def __init__(self):
        self.lock = threading.Lock()
        self.aps = {}

    def update(self, entries):
        now = time.time()
        with self.lock:
            for e in entries:
                if not e["ssid"]:
                    continue  # hidden: skip in v1
                sec = classify(e["flags"])
                if sec is None:
                    log("skip %s %s (auth not backable in v1: %s)" %
                        (e["ssid"], e["bssid"], e["flags"]))
                    continue
                chan = freq_to_chan(e["freq"])
                if chan is None:
                    continue
                e["secured"] = sec
                e["chan"] = chan
                e["seen"] = now
                self.aps[e["bssid"]] = e
            old = [k for k, v in self.aps.items()
                   if now - v["seen"] > AP_EXPIRE]
            for k in old:
                del self.aps[k]

    def snapshot(self):
        with self.lock:
            return dict(self.aps)


def scanner(table):
    while True:
        try:
            r = phone_req({"cmd": "scan"}, timeout=120)
            items = r.get("results", []) if r.get("ok") else []
            entries = [e for e in (parse_scan_line(l) for l in items) if e]
            table.update(entries)
            log("phone scan: %d usable APs" % len(table.snapshot()))
        except Exception as e:
            log("phone scan failed: %s (keeping cache)" % e)
        time.sleep(SCAN_EVERY)


_last_err_log = {}


def err_log(key, msg, every=30):
    now = time.time()
    if now - _last_err_log.get(key, 0) > every:
        _last_err_log[key] = now
        log(msg)


def iw(*args):
    try:
        r = subprocess.run(["iw"] + list(args), capture_output=True,
                           text=True, timeout=10)
        if r.returncode != 0:
            err_log("iw", "iw %s rc=%d: %s" %
                    (" ".join(args), r.returncode, (r.stderr or "").strip()))
            return False
        return True
    except Exception as e:
        err_log("iw", "iw %s failed: %s" % (" ".join(args), e))
        return False


def sta_connected(sta):
    try:
        r = subprocess.run(["iw", "dev", sta, "link"], capture_output=True,
                           text=True, timeout=10)
        return "Connected to" in r.stdout
    except Exception:
        return False


def find_sta(sta_phy):
    try:
        names = os.listdir("/sys/class/net")
    except OSError:
        return None
    for n in names:
        # Never pick our own AP interface (or monitor/loopback) as the STA:
        # ap1 lives on the same phy and NM will never activate on it.
        if n.startswith("mon") or n.startswith("ap") or n == "lo":
            continue
        try:
            ph = os.path.basename(os.readlink("/sys/class/net/%s/phy80211"
                                              % n))
        except OSError:
            continue
        if ph == sta_phy:
            return n
    return None


def main():
    try:
        with open("/run/wifishim/sta_phy") as f:
            sta_phy = f.read().strip()
    except OSError:
        print("missing /run/wifishim/sta_phy; run wifishim-setup.sh first",
              file=sys.stderr)
        return 1
    sta = find_sta(sta_phy)
    if not sta:
        print("no STA iface on %s" % sta_phy, file=sys.stderr)
        return 1
    log("STA=%s mon=%s (phy %s)" % (sta, MON, sta_phy))
    try:
        raw = socket.socket(socket.AF_PACKET, socket.SOCK_RAW,
                            socket.htons(0x0003))
    except OSError as e:
        print("packet socket failed (need root): %s" % e, file=sys.stderr)
        return 1
    raw.bind((MON, socket.htons(0x0003)))
    raw.setblocking(False)
    subprocess.run(["ip", "link", "set", MON, "up"], capture_output=True)
    try:
        with open("/sys/class/net/%s/operstate" % MON) as f:
            st = f.read().strip()
        r = subprocess.run(["iw", "dev", MON, "info"], capture_output=True,
                           text=True, timeout=10)
        log("mon1 operstate=%s info=%s" %
            (st, " ".join(r.stdout.split()[:8]) if r.returncode == 0
             else "iw-info-failed"))
    except Exception as e:
        log("mon1 state check failed: %s" % e)

    # TX self-test: prove frames actually leave (tx counter must move).
    # Catches silent socket/channel breakage that no errno reveals.
    try:
        with open("/sys/class/net/%s/statistics/tx_packets" % MON) as f:
            tx0 = int(f.read().strip())
        iw("dev", MON, "set", "channel", "6")
        tap = {"bssid": "02:00:00:00:00:00", "ssid": "WIFISHIM-TEST",
               "flags": "", "secured": False}
        tts = int(time.time() * 1000000) & 0xFFFFFFFFFFFFFFFF
        for _ in range(3):
            try:
                raw.send(RADIOTAP + beacon(tap, 6, tts, 1))
            except OSError as e:
                log("self-test send failed: %s" % e)
                break
        time.sleep(0.5)
        with open("/sys/class/net/%s/statistics/tx_packets" % MON) as f:
            tx1 = int(f.read().strip())
        # NOTE: tx_packets does NOT count monitor-injected TX on hwsim
        # (proven live: NM lists our APs while the counter stays frozen).
        # Socket errors are the real failure signal, not this counter.
        log("TX self-test: sent 3 beacons, tx counter %d -> %d "
            "(counter ignores monitor TX; silence = handed to driver)"
            % (tx0, tx1))
    except Exception as e:
        log("TX self-test error: %s" % e)

    table = Table()
    threading.Thread(target=scanner, args=(table,), daemon=True).start()

    cur_chan, seq, last_probe = 6, 0, {}
    tx_err_logged = False
    last_stat = 0
    iw("dev", MON, "set", "channel", str(cur_chan))
    while True:
        try:
            now0 = time.time()
            if now0 - last_stat > 60:
                last_stat = now0
                try:
                    with open("/sys/class/net/%s/statistics/tx_packets"
                              % MON) as f:
                        txc = f.read().strip()
                    r = subprocess.run(
                        ["iw", "dev", MON, "info"], capture_output=True,
                        text=True, timeout=10)
                    chline = " ".join(
                        l.strip() for l in r.stdout.splitlines()
                        if "channel" in l.lower()) or "no-channel-line"
                    log("stats60: tx=%s %s" % (txc, chline))
                except Exception as e:
                    log("stats60 failed: %s" % e)
            if sta_connected(sta):
                # STA associated (later: our own hostapd) — never yank its
                # channel. Pause broadcast work; keep answering probes.
                time.sleep(10)
                continue
            aps = table.snapshot()
            chans = {}
            for b, a in aps.items():
                chans.setdefault(a["chan"], []).append((b, a))
            if chans:
                for ch in sorted(chans):
                    if ch != cur_chan:
                        iw("dev", MON, "set", "channel", str(ch))
                        cur_chan = ch
                    for b, a in chans[ch]:
                        seq = (seq + 1) & 0xFFF
                        ts = int(time.time() * 1000000) & 0xFFFFFFFFFFFFFFFF
                        try:
                            raw.send(RADIOTAP + beacon(a, ch, ts, seq))
                        except OSError as e:
                            if not tx_err_logged:
                                log("BEACON SEND FAILED: %s (mon=%s up=%s)" %
                                    (e, MON, os.path.exists("/sys/class/net/" + MON)))
                                tx_err_logged = True
                        time.sleep(0.02)
            # drain RX: answer probe requests seen on current channel
            for _ in range(64):
                try:
                    data = raw.recv(4096)
                except BlockingIOError:
                    break
                except OSError:
                    break
                if len(data) < 26:
                    continue
                rt_len = struct.unpack("<H", data[2:4])[0]
                d = data[rt_len:]
                if len(d) < 26:
                    continue
                fc0 = d[0]
                if (fc0 >> 2) & 0x3 != 0 or (fc0 >> 4) & 0xF != 4:
                    continue  # not a probe request
                prober = d[10:16]
                ies = d[24:]
                if len(ies) < 2 or ies[0] != 0:
                    continue
                sln, ssid = ies[1], ies[2:2 + ies[1]]
                now = time.time()
                for b, a in aps.items():
                    if a["chan"] != cur_chan:
                        continue
                    if sln and a["ssid"].encode() != ssid:
                        continue
                    if now - last_probe.get(b, 0) < 0.3:
                        continue
                    last_probe[b] = now
                    seq = (seq + 1) & 0xFFF
                    ts = int(time.time() * 1000000) & 0xFFFFFFFFFFFFFFFF
                    try:
                        raw.send(RADIOTAP + probe_resp(a, cur_chan, ts, seq,
                                                       prober))
                    except OSError as e:
                        err_log("tx", "probe-resp send failed: %s" % e)
                    log("probe %s from %s -> %s" %
                        (ssid.decode("utf-8", "replace") or "(broadcast)",
                         prober.hex(":"), a["ssid"]))
            time.sleep(0.1)
        except Exception as e:
            log("loop error: %s" % e)
            time.sleep(2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
