r"""chain_up.py [--loop [SECS]] [--print] - bring the go-ios chain up for every connected iPhone in config.json.
Only starts what is missing (tunnel, image auto, runwda, WDA + MJPEG forwards); never kills anything.
config.json phones: {"name": {"kind": "ios", "udid": "...", "port": 8100, "mjpeg": 9200, "alias": "14", "wda_bundle": "...xctrunner"}}
  wda_bundle is optional: without it the bundle id is read from the phone's last runwda log (logs\wda_<alias>.err).
Logs to C:\phonebot\logs\chain_up.log (never UDIDs or bundle ids). Autostart: install_autostart.ps1 (Task Scheduler, per user)."""
import subprocess, json, re, sys, time, http.client, os, logging
import psutil
B = os.environ.get("PHONEBOT_DIR", r"C:\phonebot")
IOS = os.path.join(B, "ios.exe")
LOGD = os.path.join(B, "logs")
DET = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
NOWIN = 0x08000000
os.makedirs(LOGD, exist_ok=True)
logging.basicConfig(filename=os.path.join(LOGD, "chain_up.log"), level=logging.INFO, format="%(asctime)s %(message)s")


def log(m):
    logging.info(m)
    if "--print" in sys.argv or (sys.stdout and sys.stdout.isatty()):
        print(m, flush=True)


def cfg_phones():
    try:
        ps = json.load(open(os.path.join(B, "config.json"), encoding="utf-8-sig"))["phones"]
    except (OSError, ValueError, KeyError):
        return {}
    return {p["udid"]: dict(p, name=n, tag=str(p.get("alias") or n)) for n, p in ps.items() if p.get("kind") == "ios" and p.get("udid")}


def spawn(args, logname):
    lf = open(os.path.join(LOGD if not os.path.exists(os.path.join(B, logname)) else B, logname), "ab")
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=lf, stderr=lf, creationflags=DET, close_fds=True)


def up(port):
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
        c.request("GET", "/status")
        return c.getresponse().status == 200
    except Exception:
        return False


def ios_procs():
    out = []
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if (p.info["name"] or "").lower() == "ios.exe":
                out.append(" ".join(p.info["cmdline"] or []) + " ")
        except Exception:
            pass
    return out


def devices():
    r = subprocess.run([IOS, "list", "--details"], capture_output=True, text=True, timeout=30, creationflags=NOWIN)
    ls = [l for l in r.stdout.splitlines() if "deviceList" in l]
    return json.loads(ls[-1]).get("deviceList", []) if ls else []


def wda_log(ph):
    for name in ("wda_%s.err" % ph["tag"], "wda%s.err" % ph["tag"]):      # wdaNN.err = older installs
        for d in (LOGD, B):
            if os.path.exists(os.path.join(d, name)):
                return os.path.join(d, name)
    return os.path.join(LOGD, "wda_%s.err" % ph["tag"])


def bundle_for(ph):
    if ph.get("wda_bundle"):
        return ph["wda_bundle"]
    try:
        t = open(wda_log(ph), encoding="utf-8", errors="ignore").read()
    except OSError:
        return None
    if ph["udid"] not in t:
        return None
    m = re.findall(r'"bundleid":"([^"]+)"', t)
    return m[-1] if m else None


def once():
    conf = cfg_phones()
    want = [conf[d["Udid"]] for d in devices() if d.get("Udid") in conf]
    if all(up(ph["port"]) for ph in want):
        return [ph["tag"] for ph in want], []
    pl = ios_procs()
    started = []
    if want and not any(" tunnel start" in l for l in pl):
        spawn([IOS, "tunnel", "start", "--userspace"], "tunnel.log")
        started.append("tunnel")
        time.sleep(4)
    for ph in want:
        name, wp, mp, u = ph["tag"], ph["port"], ph.get("mjpeg"), ph["udid"]
        if up(wp):
            continue
        bid = bundle_for(ph)
        if not bid:
            log("%s: no WDA bundle known (set wda_bundle in config.json or do one manual runwda)" % name)
            continue
        if not any("runwda" in l and u in l for l in pl):
            r = subprocess.run([IOS, "image", "auto", "--basedir=" + os.path.join(os.path.dirname(os.path.abspath(__file__)), "devimages"), "--udid=" + u], capture_output=True, text=True, timeout=180, creationflags=NOWIN)
            log("%s: image auto rc=%s" % (name, r.returncode))
            spawn([IOS, "runwda", "--bundleid=" + bid, "--testrunnerbundleid=" + bid, "--xctestconfig=WebDriverAgentRunner.xctest", "--udid=" + u],
                  os.path.basename(wda_log(ph)))
            started.append(name + " runwda")
        if not any((" forward %d 8100" % wp) in l for l in pl):
            spawn([IOS, "forward", str(wp), "8100", "--udid=" + u], "fwd_%s.log" % name)
            started.append(name + " fwd")
        if mp and not any((" forward %d 9100" % mp) in l for l in pl):
            spawn([IOS, "forward", str(mp), "9100", "--udid=" + u], "fwdm_%s.log" % name)
            started.append(name + " mjpeg")
        for i in range(30):
            if up(wp):
                log("%s: WDA up on %d after %d s" % (name, wp, i * 2))
                break
            time.sleep(2)
        else:
            log("%s: WDA not up after 60 s (phone locked, untrusted, expired signing, or Developer Mode needs a tap?)" % name)
    return [ph["tag"] for ph in want], started


def single_instance():
    lock = os.path.join(LOGD, "chain_up.pid")
    try:
        pid = int(open(lock).read().strip())
        if pid != os.getpid() and psutil.pid_exists(pid) and "chain_up" in " ".join(psutil.Process(pid).cmdline()):
            return False
    except Exception:
        pass
    open(lock, "w").write(str(os.getpid()))
    return True


if __name__ == "__main__":
    if "--loop" in sys.argv:
        i = sys.argv.index("--loop")
        secs = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 and sys.argv[i + 1].isdigit() else 45
        if not single_instance():
            log("loop already running; exit")
            sys.exit(0)
        log("loop start, every %d s" % secs)
        last = None
        while True:
            try:
                names, started = once()
                state = tuple(names)
                if started or state != last:
                    log("phones: %s | started: %s" % (",".join(names) or "none", ",".join(started) or "nothing"))
                last = state
            except Exception as e:
                log("error: %s" % str(e)[:200])
            time.sleep(secs)
    else:
        names, started = once()
        conf = cfg_phones()
        log("phones: %s | started: %s | up: %s" % (",".join(names) or "none", ",".join(started) or "nothing",
                                                    {p["tag"]: up(p["port"]) for p in conf.values()}))
