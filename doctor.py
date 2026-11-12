r"""doctor.py [--json] [--fix] - one-command health check for phone control on Windows. Says in plain words what works,
what is broken and the exact fix (a physical step for the user, or a command the bot runs). Never prints UDIDs or IDs.
  python C:\phonebot\live\doctor.py          report only (presses nothing on the phones)
  python C:\phonebot\live\doctor.py --fix    also runs chain_up.py once to start whatever is missing, then re-checks
Signing age: the first time doctor/chain_up sees a WDA install (its SequenceNumber) it notes the time in
logs\wda_sign.json; a free Apple ID signature lasts 7 days, so it warns from day 6 and says how to re-sign."""
import json, os, re, subprocess, sys, time, http.client
B = os.environ.get("PHONEBOT_DIR", r"C:\phonebot")
IOS = os.path.join(B, "ios.exe")
LOGD = os.path.join(B, "logs")
LIVE = os.path.dirname(os.path.abspath(__file__))
SIGN = os.path.join(LOGD, "wda_sign.json")
NOWIN = 0x08000000 if os.name == "nt" else 0
FREE_DAYS = 7
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def ios(*a, timeout=40):
    try:
        r = subprocess.run([IOS, *a], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, creationflags=NOWIN)
        return r.returncode, r.stdout + "\n" + r.stderr
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except OSError as e:
        return -2, str(e)


def jlines(out):
    for l in out.splitlines():
        l = l.strip()
        if l.startswith(("{", "[")):
            try:
                yield json.loads(l)
            except ValueError:
                pass


def wget(port, path, timeout=4):
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        c.request("GET", path)
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}")
    except Exception:
        return None, {}


def procs():
    try:
        import psutil
    except ImportError:
        return None
    out = []
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if (p.info["name"] or "").lower() in ("ios.exe", "ios", "python.exe", "pythonw.exe"):
                out.append(" ".join(p.info["cmdline"] or []) + " ")
        except Exception:
            pass
    return out


def jload(p):
    try:
        return json.load(open(p, encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


class Report:
    def __init__(self):
        self.items = []

    def add(self, scope, ok, what, fix=None, who=None):
        self.items.append({"scope": scope, "ok": ok, "what": what, "fix": fix, "who": who})


def check(rep):
    # ---- PC basics
    rep.add("PC", sys.version_info >= (3, 9), "Python %d.%d" % sys.version_info[:2], "install Python 3.12: winget install -e --id Python.Python.3.12", "bot")
    for mod, why in (("psutil", "chain_up.py and doctor"), ("PIL", "the live phone window")):
        try:
            __import__(mod)
            rep.add("PC", True, "%s installed" % mod)
        except ImportError:
            rep.add("PC", False, "%s missing (needed by %s)" % (mod, why), "python -m pip install %s" % ("pillow" if mod == "PIL" else mod), "bot")
    cfg = jload(os.path.join(B, "config.json"))
    phones = {n: p for n, p in (cfg.get("phones") or {}).items() if p.get("kind") == "ios"}
    if not cfg:
        rep.add("PC", False, "config.json missing or not valid JSON in %s" % B, "write it (skill phone-setup-windows, Common)", "bot")
    missing = [f for f in ("act.py", "do.py", "pp.py", "chain_up.py") if not os.path.exists(os.path.join(LIVE, f))]
    rep.add("PC", not missing, "tool files present" if not missing else "missing in live\\: " + ", ".join(missing), "write them from the phone-*-code-pc skills", "bot")
    if not os.path.exists(IOS):
        rep.add("PC", False, "go-ios (ios.exe) not found in %s" % B, "download go-ios-win.zip from github.com/danielpaulus/go-ios/releases and unzip ios.exe there", "bot")
        return
    rc, out = ios("version")
    ver = next((d.get("version") for d in jlines(out) if isinstance(d, dict) and d.get("version")), "?")
    rep.add("PC", rc == 0, "go-ios %s" % ver)
    rc, out = ios("list")
    devs = next((d.get("deviceList") for d in jlines(out) if isinstance(d, dict) and "deviceList" in d), None)
    if devs is None:
        rep.add("PC", False, "go-ios cannot talk to the Apple USB service", "install Apple Devices from the Microsoft Store (or iTunes), then re-plug the phone", "user")
        devs = []
    pl = procs()
    if pl is None:
        pl = []
    tunnel = any(" tunnel start" in l for l in pl)
    tl = []
    if tunnel:
        rc, out = ios("tunnel", "ls")
        tl = [t.get("udid") for d in jlines(out) if isinstance(d, list) for t in d if isinstance(t, dict)]
    rep.add("PC", tunnel or not devs, "iOS 17+ tunnel running" if tunnel else "tunnel not running", "python %s --print  (starts it)" % os.path.join(LIVE, "chain_up.py"), "bot")
    if os.name == "nt":
        r = subprocess.run(["schtasks", "/Query", "/TN", r"\PhonePilot\PhonePilot chain_up"], capture_output=True, text=True, creationflags=NOWIN)
        task = r.returncode == 0
        loop = any("chain_up.py" in l and "--loop" in l for l in pl)
        rep.add("PC", task and loop, "auto-reconnect task %s, loop %s" % ("installed" if task else "NOT installed", "running" if loop else "NOT running"),
                "powershell -ExecutionPolicy Bypass -File %s  (with the owner's yes)" % os.path.join(LIVE, "install_autostart.ps1") if not task
                else "schtasks /Run /TN \"\\PhonePilot\\PhonePilot chain_up\"", "bot")
    sign = jload(SIGN)
    # ---- each iPhone
    for name, ph in phones.items():
        u, port = ph.get("udid"), ph.get("port")
        s = name
        if not u or u.startswith("<"):
            rep.add(s, False, "no udid in config.json", "plug it in, run ios list, put the udid in config.json", "bot")
            continue
        st, val = wget(port, "/status")
        if u not in devs:
            rep.add(s, False, "not connected (not seen over USB)", "plug it in with a data cable (rear port), unlock it, tap Trust if asked", "user")
            continue
        rc, out = ios("devicename", "--udid=" + u)
        if rc != 0 or "devicename" not in out:
            rep.add(s, False, "connected but not trusted / paired", "unlock the phone and tap Trust, enter the passcode; then run doctor again", "user")
            continue
        rep.add(s, True, "connected and trusted")
        rc, out = ios("devmode", "get", "--udid=" + u)
        dm = next((d.get("DeveloperModeEnabled") for d in jlines(out) if isinstance(d, dict) and "DeveloperModeEnabled" in d), None)
        if dm is False:
            rep.add(s, False, "Developer Mode is off", "on the phone: Settings > Privacy & Security > Developer Mode > On, restart, then Turn On", "user")
            continue
        rep.add(s, dm is not False, "Developer Mode on")
        if tunnel and u not in tl:
            rep.add(s, False, "tunnel has no entry for this phone yet", "unlock the phone; chain_up.py --print", "bot")
        rc, out = ios("apps", "--udid=" + u, timeout=60)
        wda = None
        for d in jlines(out):
            for a in (d if isinstance(d, list) else [d]):
                if isinstance(a, dict) and "xctrunner" in str(a.get("CFBundleIdentifier", "")):
                    wda = a
        if not wda:
            rep.add(s, False, "WebDriverAgent is not installed", "sign and install WDA.ipa with Sideloadly (skill phone-setup-windows, iPhone steps 7-9)", "user")
            continue
        if wda.get("ProfileValidated") is False:
            rep.add(s, False, "WebDriverAgent installed but its developer is not trusted", "on the phone: Settings > General > VPN & Device Management > your Apple ID > Trust", "user")
        if ph.get("wda_bundle") and ph["wda_bundle"] != wda.get("CFBundleIdentifier"):
            rep.add(s, False, "config.json wda_bundle does not match the installed WDA (re-signed under a new id?)",
                    "set wda_bundle in config.json to the xctrunner id from: ios apps --udid=<udid>", "bot")
        seq = "%s:%s" % (wda.get("CFBundleIdentifier"), wda.get("SequenceNumber"))
        key = re.sub(r"\W", "_", name)
        rec = sign.get(key)
        if not rec or rec.get("seq") != seq:
            rec = sign[key] = {"seq": seq, "first_seen": time.time(), "noted": "first seen by doctor/chain_up (install time may be earlier)"}
        since = rec["first_seen"]
        if ph.get("signed"):   # "signed": "YYYY-MM-DD" in config.json (the bot writes it right after each Sideloadly install)
            try:
                since = time.mktime(time.strptime(ph["signed"], "%Y-%m-%d"))
            except ValueError:
                pass
        age = (time.time() - since) / 86400
        paid = str(ph.get("apple_id", "free")).lower() == "paid"
        if not paid:
            left = FREE_DAYS - age
            how = "signed" if ph.get("signed") else "install first seen"
            rep.add(s, left > 1, "WDA signature: %s %.1f days ago, about %.1f days left (free Apple ID: 7 days)" % (how, age, max(left, 0)),
                    "re-sign soon: Sideloadly, same Apple ID, WDA.ipa, Start (the user signs in); then trust it again if asked", "user")
        # running pieces
        runwda = any("runwda" in l and u in l for l in pl)
        fwd = any((" forward %s 8100" % port) in l and u in l for l in pl)
        if st == 200:
            stl, lv = wget(port, "/wda/locked")
            locked = bool(lv.get("value")) if stl == 200 else None
            rep.add(s, not locked, "WDA answering on port %s, phone %s" % (port, "LOCKED" if locked else "unlocked"),
                    "unlock the phone (Face ID / passcode); WDA cannot type a passcode" if locked else None, "user" if locked else None)
            lockf = os.path.join(B, "locks", "%s.lock" % port)
            if os.path.exists(lockf) and time.time() - os.path.getmtime(lockf) < 120:
                rep.add(s, True, "currently in use by %s (shared lock); the bot waits its turn" % (open(lockf).read().split() or ["?"])[0])
        else:
            why = []
            if not runwda:
                why.append("WDA runner not started")
            if not fwd:
                why.append("port forward %s not started" % port)
            errs = ""
            for lg in ("wda_%s.err" % ph.get("alias", name), "wda%s.err" % ph.get("alias", name)):
                for d in (LOGD, B):
                    p = os.path.join(d, lg)
                    if os.path.exists(p):
                        errs += open(p, encoding="utf-8", errors="ignore").read()[-4000:]
            low = errs.lower()
            if re.search(r"expired|no longer available|invalid code signature|not been explicitly trusted|could not get pid|launch.*error 2", low):
                rep.add(s, False, "WDA will not launch: signature expired or the developer is not trusted",
                        "re-sign WDA with Sideloadly (free Apple ID lasts 7 days), then Settings > General > VPN & Device Management > Trust", "user")
            elif re.search(r"developer ?disk ?image|ddi|mount", low) and "error" in low:
                rep.add(s, False, "developer disk image not mounted", "python chain_up.py --print (runs ios image auto); keep the phone unlocked", "bot")
            else:
                rep.add(s, False, "WDA not answering on port %s (%s)" % (port, ", ".join(why) or "pieces running but no answer"),
                        "unlock the phone, then: python %s --print  (wait up to 60 s)" % os.path.join(LIVE, "chain_up.py"), "bot")
    for d in devs:
        if d not in [p.get("udid") for p in phones.values()]:
            rep.add("PC", False, "an iPhone is connected that is not in config.json", "add it to config.json (own ports, e.g. 8102/9202)", "bot")
    try:
        os.makedirs(LOGD, exist_ok=True)
        json.dump(sign, open(SIGN, "w"))
    except OSError:
        pass


def main():
    if "--fix" in sys.argv:
        subprocess.run([sys.executable, os.path.join(LIVE, "chain_up.py"), "--print"], creationflags=NOWIN, timeout=400)
    t = time.time()
    rep = Report()
    check(rep)
    bad = [i for i in rep.items if not i["ok"]]
    if "--json" in sys.argv:
        print(json.dumps({"ok": not bad, "items": rep.items, "ms": int((time.time() - t) * 1000)}))
        return
    for i in rep.items:
        print("%s %-10s %s" % ("OK  " if i["ok"] else "FIX ", i["scope"], i["what"]))
        if not i["ok"] and i["fix"]:
            print("     -> %s%s" % ("(user, on the phone/PC) " if i["who"] == "user" else "(bot runs) ", i["fix"]))
    print("\n%s (%d checks, %d to fix, %.1f s)" % ("ALL GOOD - phones ready" if not bad else "NOT READY", len(rep.items), len(bad), time.time() - t))


if __name__ == "__main__":
    main()
