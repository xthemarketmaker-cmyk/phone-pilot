import http.client, json, os, re, subprocess, sys, time
import xml.etree.ElementTree as ET

BASE = os.environ.get("PHONEBOT_DIR", r"C:\phonebot")
LOCKDIR = os.path.join(BASE, "locks")
LOGS = os.path.join(BASE, "logs")
ADB = os.path.join(BASE, "platform-tools", "adb.exe")
STALE = 120


class Locked(Exception):
  pass


def config():
  try:
    return json.load(open(os.path.join(BASE, "config.json"), encoding="utf-8-sig"))
  except (OSError, ValueError):
    return {}


def phones():
  cfg = config().get("phones") or {"iphone": {"kind": "ios", "port": 8100, "mjpeg": 9200}}
  out = {}
  for n, p in cfg.items():
    p = dict(p, name=n)
    p["key"] = re.sub(r"[^\w.-]", "_", str(p["port"] if p["kind"] == "ios" else p.get("serial", n)))
    out[n] = p
  return out


def pick(name=None):
  """name = a phone name from config.json or its short "alias" (e.g. "17"); none = config "default_phone", else the first iPhone."""
  ps = phones()
  if name:
    name = str(name)
    if name in ps:
      return ps[name]
    for p in ps.values():
      if str(p.get("alias", "")) == name:
        return p
    raise KeyError("no phone '%s' in config.json (have: %s)" % (name, ", ".join(ps)))
  d = config().get("default_phone")
  if d in ps:
    return ps[d]
  return next((p for p in ps.values() if p["kind"] == "ios"), next(iter(ps.values())))


def lockfile(ph):
  return os.path.join(LOCKDIR, ph["key"] + ".lock")


def lock_status(ph, me):
  f = lockfile(ph)
  try:
    if time.time() - os.path.getmtime(f) < STALE:
      owner = (open(f).read().split() or ["unknown"])[0]
      return ("mine" if owner == me else "other"), owner
  except OSError:
    pass
  return "free", None


def lock_write(ph, me):
  os.makedirs(LOCKDIR, exist_ok=True)
  with open(lockfile(ph), "w") as f:
    f.write(me)


def lock_release(ph, me):
  try:
    if open(lockfile(ph)).read().split()[0] == me:
      os.remove(lockfile(ph))
  except (OSError, IndexError):
    pass


def wda(port, method, path, body=None, timeout=15):
  c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
  try:
    c.request(method, path, None if body is None else json.dumps(body), {"Content-Type": "application/json"})
    r = c.getresponse()
    d = r.read()
    return r.status, (json.loads(d) if d else {})
  finally:
    c.close()


_sid = {}


def ios_locked(port):
  try:
    return bool(wda(port, "GET", "/wda/locked", timeout=5)[1].get("value"))
  except Exception:
    return True


def ios_session(port, force=False):
  if port in _sid and not force:
    return _sid[port]
  sid = wda(port, "GET", "/status")[1].get("sessionId")
  if not sid:
    r = wda(port, "POST", "/session", {"capabilities": {}})[1]
    sid = r.get("sessionId") or r["value"]["sessionId"]
    wda(port, "POST", "/session/%s/appium/settings" % sid,
      {"settings": {"waitForIdleTimeout": 0, "animationCoolOffTimeout": 0, "snapshotMaxDepth": 50}})
  _sid[port] = sid
  return sid


def ios_call(port, method, path, body=None):
  for attempt in (0, 1):
    code, r = wda(port, method, path.replace("{sid}", ios_session(port, bool(attempt))), body)
    if code == 200:
      return r
    if attempt or "session" not in json.dumps(r).lower():
      break
  raise RuntimeError("wda http %s" % code)


def adb(ph, *a, timeout=15):
  return subprocess.run([ADB, "-s", ph["serial"]] + list(a), capture_output=True, timeout=timeout, creationflags=0x08000000).stdout.decode(errors="ignore")


def locked(ph):
  if ph["kind"] == "ios":
    return ios_locked(ph["port"])
  kg = re.search(r"mDreamingLockscreen=true|isStatusBarKeyguard=true|mShowingLockscreen=true|KeyguardShowing=true", adb(ph, "shell", "dumpsys", "window"))
  return bool(kg) or not re.search(r"mWakefulness=Awake|Display Power: state=ON", adb(ph, "shell", "dumpsys", "power"))


def size(ph):
  if ph["kind"] == "ios":
    v = ios_call(ph["port"], "GET", "/session/{sid}/window/size")["value"]
    return v["width"], v["height"]
  w, h = adb(ph, "shell", "wm", "size").split(":")[-1].strip().split("x")
  return int(w), int(h)


def pointer(port, acts):
  ios_call(port, "POST", "/session/{sid}/actions", {"actions": [{"type": "pointer", "id": "f1", "parameters": {"pointerType": "touch"}, "actions": acts}]})


def edge(ph, x, y):
  if 12 < x < 400 and y < 800:
    return False
  w, h = size(ph)
  return y > h - 40 or x < 12 or x > w - 12


def plan_scroll(ph, x, y, dy):
  """dy = points the CONTENT should move (+ down). WDA flings: a slow stroke (<330 pt/s) moves content ~1:1, a 150 ms stroke
  + 150 ms hold moves it ~2x. Returns the finger stroke centred on (x, y), kept inside the scrollable area (below the nav bar, above the home bar)."""
  w, h = size(ph)
  c, s = abs(dy), (1 if dy > 0 else -1)
  d, ms, hold = (c / .9, int(c / .9 * 3.3), 0) if c <= 130 else (max(120, min(c / 2.05, 380)), 150, 150)
  if ph["kind"] == "android":
    d, ms, hold = min(c * 2.5, h * .5), 300, 0
  y1 = y - s * d / 2
  sh = max(0, h * .14 - min(y1, y1 + s * d)) - max(0, max(y1, y1 + s * d) - h * .88)
  return x, y1 + sh, x, y1 + s * d + sh, ms, hold


def act(ph, a):
  t = a.get("type")
  if t not in ("tap", "swipe", "scroll", "apps", "type", "home", "key", "launch"):
    raise ValueError("bad type")
  f = lambda k: float(a[k])
  if t == "tap":
    x1, y1 = f("x"), f("y")
  elif t == "swipe":
    x1, y1, x2, y2 = f("x1"), f("y1"), f("x2"), f("y2")
  elif t == "scroll":
    x1, y1, y2 = f("x"), f("y"), f("dy")
  ms = max(100, min(int(a.get("duration_ms", 350)), 1500))
  hold = max(0, min(int(a.get("hold_ms", 0)), 1000))
  text = str(a.get("text", ""))[:200]
  key = str(a.get("key", "HOME")).upper()
  app = str(a.get("app", ""))
  if not re.fullmatch(r"[A-Z_]+", key) or not re.fullmatch(r"[\w.]*", app):
    raise ValueError("bad value")
  if locked(ph):
    raise Locked()
  if t == "scroll":
    x1, y1, x2, y2, ms, hold = plan_scroll(ph, x1, y1, y2)
    t = "swipe"
  elif t == "apps" and ph["kind"] == "ios":  # app switcher = swipe up from the bottom edge to mid-screen and HOLD (5-point path; a 2-point swipe is ignored)
    w, h = size(ph)
    x1, y1, x2, y2, ms, hold, t = w / 2, h - 2, w / 2, h * .6, 400, 500, "swipe"
  if ph["kind"] == "ios":
    p = ph["port"]
    if t == "tap":
      pointer(p, [{"type": "pointerMove", "duration": 0, "x": x1, "y": y1}, {"type": "pointerDown", "button": 0},
            {"type": "pause", "duration": 50}, {"type": "pointerUp", "button": 0}])
    elif t == "swipe":  # edge starts (home bar, side edges) only register with several move points
      k = 5 if edge(ph, x1, y1) else 1
      pointer(p, [{"type": "pointerMove", "duration": 0, "x": x1, "y": y1}, {"type": "pointerDown", "button": 0}]
          + [{"type": "pointerMove", "duration": ms // k, "x": x1 + (x2 - x1) * i / k, "y": y1 + (y2 - y1) * i / k} for i in range(1, k + 1)]
          + ([{"type": "pause", "duration": hold}] if hold else []) + [{"type": "pointerUp", "button": 0}])
    elif t == "type":
      ios_call(p, "POST", "/session/{sid}/wda/keys", {"value": list(text)})
    elif t == "launch":
      ios_call(p, "POST", "/session/{sid}/wda/apps/launch", {"bundleId": app})
    elif t == "home" or key == "HOME":
      wda(p, "POST", "/wda/homescreen", {})
    else:
      raise ValueError("ios supports key HOME only")
    return
  sh = lambda *v: adb(ph, "shell", *v)
  if t == "tap":
    sh("input", "tap", str(int(x1)), str(int(y1)))
  elif t == "swipe":
    sh("input", "swipe", *[str(int(v)) for v in (x1, y1, x2, y2)], str(ms))
  elif t == "type":
    for ch in text:
      if ch in "\b\n":
        sh("input", "keyevent", "KEYCODE_DEL" if ch == "\b" else "KEYCODE_ENTER")
      elif ch == " " or ch.isascii() and ch.isalnum():
        sh("input", "text", "%s" if ch == " " else ch)
  elif t == "launch":
    sh("monkey", "-p", app, "-c", "android.intent.category.LAUNCHER", "1")
  else:
    sh("input", "keyevent", "KEYCODE_" + ("HOME" if t == "home" else "APP_SWITCH" if t == "apps" else key))


def box(label, r, typ=None):
  return {"type": typ, "label": label, "x": r["x"] + r["width"] / 2, "y": r["y"] + r["height"] / 2, "w": r["width"], "h": r["height"]}


def find(ph, q):
  if ph["kind"] == "android":
    adb(ph, "shell", "uiautomator", "dump", "/sdcard/_ui.xml")
    out = []
    for n in ET.fromstring(adb(ph, "exec-out", "cat", "/sdcard/_ui.xml")).iter("node"):
      if q.lower() in " ".join([n.get("text", ""), n.get("content-desc", ""), n.get("resource-id", "")]).lower():
        l, t, r, b = [int(v) for v in re.findall(r"\d+", n.get("bounds", "0,0,0,0"))[:4]]
        out.append(box(n.get("text") or n.get("content-desc"), {"x": l, "y": t, "width": r - l, "height": b - t}))
    return out
  els = ios_call(ph["port"], "POST", "/session/{sid}/elements", {"using": "predicate string", "value": q})["value"]
  return [box(None, ios_call(ph["port"], "GET", "/session/{sid}/element/%s/rect" % (e.get("ELEMENT") or list(e.values())[0]))["value"]) for e in els]


def source_grep(ph, needle):
  hits = []

  def walk(n):
    if needle.lower() in ((n.get("label") or "") + "|" + (n.get("name") or "")).lower() and n.get("rect"):
      hits.append(box(n.get("label"), n["rect"], n.get("type")))
    for c in n.get("children", []):
      walk(c)

  walk(ios_call(ph["port"], "GET", "/session/{sid}/source?format=json")["value"])
  return hits


ARGS = {"tap": ("x", "y"), "swipe": ("x1", "y1", "x2", "y2", "duration_ms", "hold_ms"), "scroll": ("x", "y", "dy"), "apps": (), "type": ("text",), "key": ("key",), "home": (), "launch": ("app",)}


def main(argv):
  me, name = "PhoneBot", None
  while argv and argv[0] in ("--phone", "--owner"):
    if argv[0] == "--phone":
      name = argv[1]
    else:
      me = argv[1]
    argv = argv[2:]
  if not argv:
    sys.exit("usage: pp.py [--phone N] [--owner O] CMD ARGS (see phone-driving)")
  c, a = argv[0], argv[1:]
  ph = pick(name)
  st, holder = lock_status(ph, me)
  if c == "status":
    print(json.dumps(wda(ph["port"], "GET", "/status")[1]) if ph["kind"] == "ios" else adb(ph, "get-state").strip())
  elif c == "locked":
    print(locked(ph))
  elif c == "size":
    print(json.dumps(size(ph)))
  elif c == "find":
    print(json.dumps(find(ph, a[0])))
  elif c == "source-grep":
    print(json.dumps(source_grep(ph, a[0])))
  elif c == "lock" and a and a[0] == "release":
    lock_release(ph, me)
    print("released")
  elif c in ARGS or c == "lock":
    if st == "other":
      print("BUSY: held by " + holder)
      sys.exit(4)
    if c == "lock":
      lock_write(ph, me)
    else:
      try:
        act(ph, dict(zip(ARGS[c], a), type=c))
      except Locked:
        print("LOCKED: the phone is locked, nothing was pressed. Ask the owner to unlock it.")
        sys.exit(3)
    print("ok")
  else:
    sys.exit("unknown command")


if __name__ == "__main__":
  main(sys.argv[1:])
