#!/usr/bin/env python3
"""wifishim-conn.py — Stage 2/3 of the PC Wi-Fi shim: answer association.

Watches NetworkManager for Wi-Fi activations on the hwsim STA interface.
When the user picks a network in the applet, this daemon brings up a REAL
local AP via hostapd (same SSID, spoofed BSSID, same channel) that NM's
supplicant genuinely associates to, serves DHCP (address-only, no gateway
— data keeps flowing over the proven USB route), and tells the PHONE to
join the same network in parallel.

v1 scope: OPEN networks fully; WPA2-PSK plumbing present (hostapd PSK stanza
+ phone pass-through), first live WPA2 test pending. Run as root.

Usage: sudo python3 pc/wifishim-conn.py
Needs: /run/wifishim/{sta_phy,ap_phy} from wifishim-setup.sh, hostapd,
       dnsmasq, phone reachable at 192.168.50.1:33601.
"""
import hashlib
import json
import os
import random
import socket
import subprocess
import sys
import threading
import time

PHONE, PORT = "192.168.50.1", 33601
AP_IF = "ap1"
AP_NET = "192.168.51.0/24"
AP_IP = "192.168.51.1"
AP_DHCP_FIRST = "192.168.51.10"
CTRL_DIR = "/run/wifishim/hostapd"
STATE_DIR = "/run/wifishim"


def log(msg):
    sys.stderr.write("%s wifishim-conn: %s\n" %
                     (time.strftime("%m-%d %H:%M:%S"), msg))
    sys.stderr.flush()


def sh(*args, timeout=20):
    try:
        r = subprocess.run(list(args), capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 99, "exec-fail: %s" % e


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
    t = line.split()
    if len(t) < 5 or t[0].count(":") != 5:
        return None
    try:
        freq = int(t[1])
    except ValueError:
        return None
    if t[-1].startswith("["):
        flags, ssid = t[-1], " ".join(t[4:-1])
    else:
        flags, ssid = "", " ".join(t[4:])
    try:
        rssi = int(t[2])
    except (ValueError, IndexError):
        rssi = -90
    return {"bssid": t[0].lower(), "freq": freq, "ssid": ssid,
            "flags": flags, "rssi": rssi}


def freq_to_chan(f):
    if 2412 <= f <= 2484:
        return 14 if f == 2484 else 1 + (f - 2412) // 5
    if 5000 <= f <= 5900:
        return (f - 5000) // 5
    if 5955 <= f <= 7115:
        return (f - 5955) // 5 + 1
    return None


# Per-SSID pinned BSS: a dual-band ESS advertises several BSSIDs and the
# phone's scan returns whichever was heard last. Flipping between them
# rebuilds hostapd mid-session (proven crash loop in test log). Pin one.
_PINNED = {}
# 5 GHz channels hostapd can actually use (52-144 need radar CAC and die
# instantly on hwsim; 165 is often restricted). DFS/odd always last.
NON_DFS_5 = frozenset([36, 40, 44, 48, 149, 153, 157, 161, 165])
# Band preference, operator-overridable: 5 GHz default (cleaner spectrum;
# e.g. High5 does 180 Mbps on ch153). WIFISHIM_BAND_PREF=2.4 flips it.
# NOTE: a 5 GHz BSS on a DFS channel still loses to 2.4 (hostapd dies on
# DFS) — e.g. SmartRG's 5 GHz BSS on ch52 correctly stays on 2.4 GHz.
BAND_PREF = os.environ.get("WIFISHIM_BAND_PREF", "5g").lower()


def _bss_rank(e):
    ch = e["chan"]
    five_ok = ch in NON_DFS_5
    twofour = ch <= 14
    if BAND_PREF == "2.4":
        band = 0 if twofour else (1 if five_ok else 2)
    else:
        band = 0 if five_ok else (1 if twofour else 2)
    return (band, -e.get("rssi", -90))


def phone_ap_for(ssid):
    """Best-known BSSID+channel for an SSID. Tries the instant cached
    list first (a full scan costs ~15 s, during which NM may time out).
    Pins one BSS per SSID while visible; prefers 2.4 GHz, then non-DFS 5."""
    cands = []
    for cmd in ("list", "scan"):
        try:
            r = phone_req({"cmd": cmd}, timeout=120)
        except Exception as e:
            log("phone %s failed: %s" % (cmd, e))
            continue
        if not r.get("ok"):
            continue
        for line in r.get("results", []):
            e = parse_scan_line(line)
            if not e or e["ssid"] != ssid:
                continue
            ch = freq_to_chan(e["freq"])
            if ch is None:
                continue
            e["chan"] = ch
            cands.append(e)
        if cands:
            break
    if not cands:
        _PINNED.pop(ssid, None)
        return None
    pin = _PINNED.get(ssid)
    if pin:
        for c in cands:
            if c["bssid"] == pin:
                return c
    best = sorted(cands, key=_bss_rank)[0]
    _PINNED[ssid] = best["bssid"]
    if _bss_rank(best)[0] == 2:
        log("WARN: only DFS/odd BSS for %s (ch %d); hostapd may refuse" %
            (ssid, best["chan"]))
    return best


def phone_current_ssid():
    try:
        r = phone_req({"cmd": "status"}, timeout=60)
    except Exception:
        return None
    import re
    m = re.search(r'SSID:\s*"([^"]+)"', r.get("status", ""))
    return m.group(1) if m else None


def phone_ensure(ssid, sec, password):
    """Join the phone to the target network (best-effort, background-safe).
    Skips the work if already there. Returns True on confirmed presence."""
    cur = phone_current_ssid()
    if cur == ssid:
        log("phone already on %s" % ssid)
        PHONE_OK[ssid] = True
        return True
    log("phone joining %s (sec=%s, currently on %s)" % (ssid, sec, cur))
    try:
        obj = {"cmd": "connect", "ssid": ssid, "sec": sec}
        if password:
            obj["pass"] = password
        r = phone_req(obj, timeout=60)
        if not r.get("ok"):
            log("phone connect rejected: %s" % r)
            PHONE_OK[ssid] = False
            return False
    except Exception as e:
        log("phone connect failed: %s" % e)
        PHONE_OK[ssid] = False
        return False
    for _ in range(20):
        time.sleep(2)
        if phone_current_ssid() == ssid:
            log("phone confirmed on %s" % ssid)
            PHONE_OK[ssid] = True
            return True
    log("phone did not confirm %s" % ssid)
    PHONE_OK[ssid] = False
    return False


def nm_active_wifi(sta):
    """(uuid, ssid, key_mgmt, psk) of the active wifi connection on STA."""
    rc, out = sh("nmcli", "-t", "-f", "UUID,TYPE,DEVICE",
                 "connection", "show", "--active")
    if rc != 0:
        return None
    uuid = None
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[-1] == sta and \
                ("wifi" in parts[1] or "802-11" in parts[1]):
            uuid = parts[0]
            break
    if not uuid:
        return None
    rc, out = sh("nmcli", "-s", "connection", "show", uuid)
    if rc != 0:
        return None
    ssid = keymgmt = psk = ""
    for line in out.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if k == "802-11-wireless.ssid":
            ssid = v
        elif k == "802-11-wireless-security.key-mgmt":
            keymgmt = v
        elif k == "802-11-wireless-security.psk":
            psk = v
    if not ssid or ssid == "--":
        return None
    return uuid, ssid, keymgmt, psk


def ap_phy():
    try:
        with open(os.path.join(STATE_DIR, "ap_phy")) as f:
            return f.read().strip()
    except OSError:
        return ""


PROCS = {}
# Phone-leg outcomes by SSID: True = radio confirmed there, False = join
# failed (wrong password, SAE-only, gone). Main loop tears the AP side down
# on False so a local-only session never masquerades as connectivity.
PHONE_OK = {}


def proc_stop(name):
    p = PROCS.pop(name, None)
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()


def bringup(ssid, sec, password, bssid, chan):
    """Start hostapd + dnsmasq for one network. Returns True if STA-facing
    side is up (phone leg runs separately)."""
    phy = ap_phy()
    if not phy:
        log("no ap_phy state; run wifishim-setup.sh")
        return False
    # Fresh AP interface every time: reusing one a previous hostapd touched
    # dies with "Match already configured" + crash loop. Also kill orphans
    # from killed orchestrator runs (bracket patterns never match us).
    sh("pkill", "-f", "hostapd[.]conf")
    sh("pkill", "-f", "dnsmasq.*ap[1]")
    time.sleep(1)
    sh("iw", "dev", AP_IF, "del")
    time.sleep(1)
    sh("iw", "phy", phy, "interface", "add", AP_IF, "type", "__ap")
    time.sleep(1)
    sh("ip", "link", "set", AP_IF, "down")
    if bssid:
        sh("ip", "link", "set", AP_IF, "address", bssid)
    sh("ip", "addr", "flush", "dev", AP_IF)
    sh("ip", "addr", "add", AP_IP + "/24", "dev", AP_IF)
    sh("ip", "link", "set", AP_IF, "up")
    # Keep ap1 out of the applet: NM lists every managed wifi iface and
    # users try to connect through it (it answers nothing — hostapd owns
    # it). Persistent rule is /etc/NetworkManager/conf.d/a32wifishim.conf;
    # this covers manual runs without the installer (best-effort: NM may
    # not have enumerated the fresh interface yet).
    sh("nmcli", "device", "set", AP_IF, "managed", "no")
    try:
        os.makedirs(CTRL_DIR, exist_ok=True)
    except OSError as e:
        log("cannot create %s: %s" % (CTRL_DIR, e))
        return False
    conf = [
        "interface=%s" % AP_IF,
        "driver=nl80211",
        "ssid=%s" % ssid,
        "channel=%d" % chan,
        "hw_mode=%s" % ("g" if chan <= 14 else "a"),
        "auth_algs=1",
        "ctrl_interface=%s" % CTRL_DIR,
        "ctrl_interface_group=0",
    ]
    if sec == "wpa2":
        if not password:
            log("secured network but no PSK available; refusing")
            return False
        # Hex PSK, not wpa_passphrase: passphrases containing '#' (comment
        # char), quotes or edge whitespace get mangled by hostapd's config
        # parser and fail the handshake as "password incorrect".
        # PSK = PBKDF2-HMAC-SHA1(passphrase, ssid, 4096, 32).
        try:
            psk_hex = hashlib.pbkdf2_hmac(
                "sha1", password.encode("utf-8"),
                ssid.encode("utf-8"), 4096, 32).hex()
        except Exception as e:
            log("PSK derive failed: %s" % e)
            return False
        conf += ["wpa=2",
                 "wpa_psk=%s" % psk_hex,
                 "wpa_key_mgmt=WPA-PSK",
                 "rsn_pairwise=CCMP"]
    else:
        conf += ["wpa=0"]
    try:
        with open("/run/wifishim/hostapd.conf", "w") as f:
            f.write("\n".join(conf) + "\n")
    except OSError as e:
        log("cannot write hostapd.conf: %s" % e)
        return False
    log("hostapd: ssid=%s chan=%d sec=%s bssid=%s" %
        (ssid, chan, sec, bssid or "ap-default"))
    try:
        if PROCS.get("hostapd_log"):
            try:
                PROCS["hostapd_log"].close()
            except Exception:
                pass
        PROCS["hostapd_log"] = open("/run/wifishim/hostapd.log", "a")
    except OSError as e:
        log("cannot open hostapd.log: %s" % e)
        return False
    PROCS["hostapd"] = subprocess.Popen(
        ["hostapd", "/run/wifishim/hostapd.conf"],
        stdout=PROCS["hostapd_log"], stderr=subprocess.STDOUT)
    time.sleep(2)
    if PROCS["hostapd"].poll() is not None:
        log("hostapd exited immediately; check config")
        return False
    # Liveness: the ctrl socket proves the AP is actually beaconing, not
    # just a live process. Fail here (retry loop) rather than serving dead.
    alive = False
    for _ in range(5):
        time.sleep(1)
        rc, out = sh("hostapd_cli", "-p", CTRL_DIR, "ping")
        if "PONG" in out:
            alive = True
            break
        if "exec-fail" in out or "not found" in out.lower():
            log("hostapd_cli unavailable; skipping liveness check")
            alive = True
            break
    if not alive:
        log("hostapd ctrl dead after 5s; AP not beaconing")
        return False
    # Address-only DHCP (no gateway): data plane stays on the USB route.
    # First-pool address pinned to the STA MAC (clients never ARP unowned
    # addresses, same lesson as the phone-side dnsmasq).
    PROCS["dnsmasq"] = subprocess.Popen(
        ["dnsmasq", "--keep-in-foreground", "-u", "root",
         "--interface=%s" % AP_IF, "--bind-interfaces", "--port=0",
         "--dhcp-range=%s,%s,12h" % (AP_DHCP_FIRST, "192.168.51.50"),
         "--dhcp-option=6,8.8.8.8",
         "--dhcp-leasefile=/run/wifishim/dnsmasq.leases",
         "--log-facility=/run/wifishim/dnsmasq.log"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    # Pin the first-pool address to the STA MAC: DHCP clients never ARP for
    # a not-yet-owned address, so unicast OFFERs die in the neighbor queue
    # without this (same lesson as the phone-side dnsmasq in test/03).
    try:
        sta_mac = None
        if STA_IF:
            with open("/sys/class/net/%s/address" % STA_IF) as f:
                sta_mac = f.read().strip()
        if sta_mac:
            sh("ip", "neigh", "replace", AP_DHCP_FIRST, "lladdr",
               sta_mac, "dev", AP_IF, "nud", "permanent")
        else:
            log("WARN: no STA MAC for neighbor pin")
    except Exception as e:
        log("WARN: neighbor pin failed: %s" % e)
    return True


def teardown():
    proc_stop("dnsmasq")
    proc_stop("hostapd")
    hlog = PROCS.pop("hostapd_log", None)
    if hlog:
        try:
            hlog.close()
        except Exception:
            pass


def main():
    try:
        with open(os.path.join(STATE_DIR, "sta_phy")) as f:
            sta_phy = f.read().strip()
    except OSError:
        print("missing state; run wifishim-setup.sh first", file=sys.stderr)
        return 1
    sta = None
    try:
        netdevs = os.listdir("/sys/class/net")
    except OSError as e:
        print("cannot list net devices: %s" % e, file=sys.stderr)
        return 1
    for n in netdevs:
        # Never pick our own AP interface (or monitor/loopback) as the STA:
        # ap1 lives on the same phy and NM will never activate on it.
        if n.startswith("mon") or n.startswith("ap") or n == "lo":
            continue
        try:
            if os.path.basename(os.readlink("/sys/class/net/%s/phy80211"
                                            % n)) == sta_phy:
                sta = n
                break
        except OSError:
            continue
    if not sta:
        print("no STA on %s" % sta_phy, file=sys.stderr)
        return 1
    log("watching NM wifi on %s" % sta)
    global STA_IF
    STA_IF = sta
    PHONE_TRY = {}
    serving = None
    suppressed = None
    while True:
        try:
            cur = nm_active_wifi(sta)
            # Key on profile CONTENT (uuid+ssid+keymgmt+psk), not just uuid:
            # NM saves the password after first prompting, so the secrets
            # visible at detection time are often empty/stale. Without this,
            # a corrected password never reaches hostapd (stuck failing).
            if cur is not None:
                _u, _s, _k, _p = cur
                fpr = hashlib.sha256(
                    ("\0".join([_u, _s, _k or "", _p or ""])).encode(
                        "utf-8", "replace")).hexdigest()[:16]
                key = _u + ":" + fpr
            else:
                key = None
            # Honesty guard: phone leg failed this exact config -> drop the
            # AP side so NM shows DOWN instead of a local-only masquerade.
            # suppressed pins it until the config changes or NM leaves.
            if serving is not None and cur is not None and \
                    cur[1] in PHONE_OK and not PHONE_OK[cur[1]]:
                log("phone failed %s; tearing down (honest DOWN state)"
                    % cur[1])
                teardown()
                serving = None
                suppressed = key
                del PHONE_OK[cur[1]]
                key = None
            if key is not None and key == suppressed:
                time.sleep(3)
                continue
            if key != serving:
                suppressed = None
                if serving is not None:
                    log("NM deactivated; tearing down AP side")
                    teardown()
                    serving = None
                if cur is not None:
                    _uuid, ssid, keymgmt, psk = cur
                    sec = "wpa2" if "wpa-psk" in (keymgmt or "") or psk \
                        else "open"
                    log("NM wants %s (sec=%s)" % (ssid, sec))
                    if sec != "open" and not psk:
                        log("secured without PSK visible; waiting")
                        time.sleep(5)
                        continue
                    if sec != "open" and sec != "wpa2":
                        log("auth type %s not backed yet; skipping" % sec)
                        time.sleep(5)
                        continue
                    info = phone_ap_for(ssid)
                    if info:
                        bssid, chan = info["bssid"], info["chan"]
                    else:
                        rb = [0x02] + [random.randrange(256) for _ in range(5)]
                        bssid = ":".join("%02x" % x for x in rb)
                        chan = 6
                        log("SSID unknown to phone; local BSSID %s ch %d" %
                            (bssid, chan))
                    pw = psk if sec == "wpa2" else None
                    # Throttle phone join attempts: each one flaps the phone
                    # uplink, so at most one per SSID per 2 minutes.
                    now_t = time.time()
                    if now_t - PHONE_TRY.get(ssid, 0) > 120:
                        PHONE_TRY[ssid] = now_t
                        t = threading.Thread(target=phone_ensure,
                                             args=(ssid, sec, pw), daemon=True)
                        t.start()
                    else:
                        log("phone join for %s throttled" % ssid)
                    if bringup(ssid, sec, pw, bssid, chan):
                        serving = key
                        log("serving %s; phone leg in background" % ssid)
                    else:
                        log("local bringup failed for %s" % ssid)
            time.sleep(3)
        except Exception as e:
            log("watch error: %s" % e)
            time.sleep(3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
