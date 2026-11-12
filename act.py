#!/usr/bin/env python
"""act.py - fast one-shot iPhone verbs over WDA. One call = one JSON line on stdout. See FAST_ACTIONS.md.
  act.py [--phone 14|17] VERB ... [--dry-run] [--home]
Speed: element lookups use predicate strings at snapshotMaxDepth 50 (~0.2-0.3 s each) instead of the full XML source
(3-20 s on big apps). Depth is restored to 1 in a finally block (and atexit)."""
import sys, os, re, json, time, html, atexit, argparse, http.client, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # emoji in labels crashed print() on a cp1252 pipe
except Exception:
    pass
import pp
HERE = os.path.dirname(os.path.abspath(__file__))
CONTACTS = os.path.join(HERE, "contacts.json")   # local only: never copied to the box, never printed
STATE = os.path.join(HERE, "act_state.json")
WAKE_GAP = 180   # seconds idle => assume the phone may have slept => open a fresh WDA session first
TYPEFREQ = int(os.environ.get("ACT_TYPEFREQ", "200"))      # 0 = leave WDA default
TYPEMODE = os.environ.get("ACT_TYPEMODE", "keys")        # keys | elem
LOCK_OWNER = "PhoneBot"   # shared with pp.py / phone_view.py (owner PhoneLive-user): one driver per phone


def jload(path):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}


def jsave(path, d):
    tmp = path + ".tmp"
    json.dump(d, open(tmp, "w", encoding="utf-8"))
    os.replace(tmp, path)

APPS = {"whatsapp": "net.whatsapp.WhatsApp", "instagram": "com.burbn.instagram", "discord": "com.hammerandchisel.discord",
        "x": "com.atebits.Tweetie2", "twitter": "com.atebits.Tweetie2", "mail": "com.apple.mobilemail", "safari": "com.apple.mobilesafari",
        "clock": "com.apple.mobiletimer", "calculator": "com.apple.calculator", "compass": "com.apple.compass"}
# names the owner can use in config.json "blocked_apps" (bundle ids work too)
APP_NAMES = dict(APPS, notes="com.apple.mobilenotes", photos="com.apple.mobileslideshow", messages="com.apple.MobileSMS",
                 contacts="com.apple.MobileAddressBook", phone="com.apple.mobilephone", weather="com.apple.weather",
                 maps="com.apple.Maps", calendar="com.apple.mobilecal", settings="com.apple.Preferences", facetime="com.apple.facetime",
                 wallet="com.apple.Passbook", health="com.apple.Health", files="com.apple.DocumentsApp", reminders="com.apple.reminders",
                 camera="com.apple.camera", music="com.apple.Music", podcasts="com.apple.podcasts", appstore="com.apple.AppStore")
VERB_APP = {"wa_send": "net.whatsapp.WhatsApp", "wa_learn": "net.whatsapp.WhatsApp", "ig_dm": "com.burbn.instagram",
            "discord_dm": "com.hammerandchisel.discord", "read_page": "com.apple.mobilesafari", "close_tab": "com.apple.mobilesafari",
            "battery": "com.apple.Preferences"}


def blocked_apps():
    """bundle ids the owner said the bot must never open: config.json "blocked_apps" (names like "notes" or bundle ids).
    Empty by default; the getting-started skill asks the owner and writes the list."""
    out = set()
    for x in pp.config().get("blocked_apps") or []:
        x = str(x).strip()
        b = APP_NAMES.get(x.lower()) or (x if "." in x else None)
        if b:
            out.add(b)
    return out


def check_allowed(bundle, name=None):
    if bundle and bundle in blocked_apps():
        raise Fail("app not allowed: %s is in config.json blocked_apps (the owner's list); nothing was opened" % (name or bundle))


TAG = re.compile(r"<XCUIElementType(\w+)\s([^>]*?)/?>")
ATTR = re.compile(r'(\w+)="([^"]*)"')
Q = lambda s: s.replace("\\", "\\\\").replace("'", "\\'")


class Fail(Exception):
    pass


class Phone:
    def __init__(self, name):
        ph = pp.pick(str(name) if name else None)       # config.json name or alias; none = default_phone / first iPhone
        self.name, self.port = ph["name"], ph["port"]
        self.alias = str(ph.get("alias") or ph["name"])
        self.ph, self.lock_ts = ph, 0
        st_, who = pp.lock_status(ph, LOCK_OWNER)
        if st_ == "other":   # the live viewer (or another bot) is driving this phone: never fight it
            raise Fail("busy: %s is driving this phone (lock %s), nothing was pressed" % (who, os.path.basename(pp.lockfile(ph))))
        self.lock_refresh(True)
        atexit.register(self.lock_free)
        self.c = None
        self.t0 = self._last = time.time()
        self.steps = {}
        self.depth_now = None
        st = jload(STATE).get(self.name, {})
        self.woke = (time.time() - st.get("ts", 0)) > WAKE_GAP
        self.sid = self._session(self.woke)
        self.typefreq_set = False
        atexit.register(self.restore)

    def lock_refresh(self, force=False):
        if force or time.time() - self.lock_ts > 30:
            pp.lock_write(self.ph, LOCK_OWNER + " " + str(int(time.time())))
            self.lock_ts = time.time()

    def lock_free(self):
        pp.lock_release(self.ph, LOCK_OWNER)

    def mark(self, k):
        n = time.time()
        self.steps[k] = self.steps.get(k, 0) + int((n - self._last) * 1000)
        self._last = n

    def raw(self, method, path, body=None, timeout=20):
        for attempt in (0, 1):
            try:
                if self.c is None:
                    self.c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
                if self.c.sock:
                    self.c.sock.settimeout(timeout)
                self.c.timeout = timeout
                self.c.request(method, path, None if body is None else json.dumps(body), {"Content-Type": "application/json"})
                r = self.c.getresponse()
                d = r.read()
                try:
                    return r.status, (json.loads(d) if d else {})
                except ValueError:
                    return r.status, {"value": d.decode("utf-8", "ignore")}
            except (http.client.HTTPException, OSError):
                try:
                    self.c.close()
                except Exception:
                    pass
                self.c = None
                if attempt:
                    raise

    def _session(self, fresh=False):
        s = None if fresh else self.raw("GET", "/status")[1].get("sessionId")
        if not s:
            r = self.raw("POST", "/session", {"capabilities": {}})[1]
            s = r.get("sessionId") or r["value"]["sessionId"]
            self.raw("POST", "/session/%s/appium/settings" % s, {"settings": {"waitForIdleTimeout": 0, "animationCoolOffTimeout": 0, "snapshotMaxDepth": 1}})
            self.depth_now = 1
        return s

    def call(self, method, path, body=None, timeout=20):
        self.lock_refresh()
        code, r = self.raw(method, "/session/%s%s" % (self.sid, path), body, timeout)
        if code != 200:
            if "invalid session" in json.dumps(r).lower():
                self.sid = self._session()
                code, r = self.raw(method, "/session/%s%s" % (self.sid, path), body, timeout)
            if code != 200:
                raise Fail("wda %s %s -> %s %s" % (method, path, code, json.dumps(r)[:160]))
        return r.get("value") if isinstance(r, dict) else r

    def depth(self, d):
        if self.depth_now != d:
            self.call("POST", "/appium/settings", {"settings": {"snapshotMaxDepth": d}})
            self.depth_now = d

    def save_state(self):
        try:
            st = jload(STATE)
            st[self.name] = {"ts": time.time()}
            jsave(STATE, st)
        except Exception:
            pass

    def restore(self):
        try:
            self.raw("POST", "/session/%s/appium/settings" % self.sid, {"settings": {"snapshotMaxDepth": 1}}, 5)
            self.depth_now = 1
        except Exception:
            pass

    # ---- element lookups (fast path: predicate at depth 50)
    def find(self, pred, timeout=15):
        self.depth(50)
        try:
            r = self.call("POST", "/elements", {"using": "predicate string", "value": pred}, timeout)
        except Fail as e:
            if "stale" in str(e) or "404" in str(e):
                return []
            raise
        return [e.get("ELEMENT") or list(e.values())[0] for e in r]

    def attr(self, eid, name):
        return self.call("GET", "/element/%s/attribute/%s" % (eid, name))

    def click(self, eid):
        self.tapel(eid)

    def until(self, fn, timeout=8.0, every=0.05):
        end = time.time() + timeout
        while True:
            r = fn()
            if r:
                return r
            if time.time() > end:
                return None
            time.sleep(every)

    def keys(self, text, eid=None):
        if TYPEFREQ and not self.typefreq_set:
            self.call("POST", "/appium/settings", {"settings": {"maxTypingFrequency": TYPEFREQ}})
            self.typefreq_set = True
        if TYPEMODE == "elem" and eid and "\b" not in text:
            self.call("POST", "/element/%s/value" % eid, {"value": [text]})
        else:
            self.call("POST", "/wda/keys", {"value": list(text)})

    # ---- raw actions
    def tap(self, x, y):
        self.call("POST", "/actions", {"actions": [{"type": "pointer", "id": "f1", "parameters": {"pointerType": "touch"}, "actions": [
            {"type": "pointerMove", "duration": 0, "x": x, "y": y}, {"type": "pointerDown", "button": 0}, {"type": "pause", "duration": 40}, {"type": "pointerUp", "button": 0}]}]})

    def swipe(self, x1, y1, x2, y2, ms=300):
        k = 5 if (y1 > 890 or x1 < 12) else 1
        mv = [{"type": "pointerMove", "duration": ms // k, "x": x1 + (x2 - x1) * i / k, "y": y1 + (y2 - y1) * i / k} for i in range(1, k + 1)]
        self.call("POST", "/actions", {"actions": [{"type": "pointer", "id": "f1", "parameters": {"pointerType": "touch"}, "actions": [
            {"type": "pointerMove", "duration": 0, "x": x1, "y": y1}, {"type": "pointerDown", "button": 0}] + mv + [{"type": "pointerUp", "button": 0}]}]})

    def home(self):
        self.raw("POST", "/wda/homescreen", {}, 10)

    def launch(self, bundle):
        check_allowed(bundle)
        self.call("POST", "/wda/apps/launch", {"bundleId": bundle})

    def open_url(self, url):
        self.call("POST", "/url", {"url": url})

    def active(self):
        return self.call("GET", "/wda/activeAppInfo").get("bundleId")

    def locked(self):
        return bool(self.raw("GET", "/wda/locked", None, 5)[1].get("value"))

    def source(self, d=50, timeout=40):
        self.depth(d)
        xml = self.call("GET", "/source?format=xml", timeout=timeout)
        out = []
        for m in TAG.finditer(xml):
            a = {k: html.unescape(v) for k, v in ATTR.findall(m.group(2))}
            a["t"] = m.group(1)
            out.append(a)
        return out

    def tapel(self, eid):
        """rect + coordinate tap: much faster than element click inside huge trees (WhatsApp chat list: 2 s vs 5 s)."""
        r = self.call("GET", "/element/%s/rect" % eid)
        self.tap(r["x"] + r["width"] / 2, r["y"] + r["height"] / 2)

    def navbar(self, n, t=5):
        return self.until(lambda: self.find("type == 'XCUIElementTypeNavigationBar' AND name == '%s'" % Q(n)), t)

    def tap_text(self, label, scrolls=5, nav=None):
        """tap the visible StaticText with this label (Settings rows ignore taps on their Button rect), scrolling if needed."""
        for i in range(scrolls + 2):
            c = self.find("type == 'XCUIElementTypeStaticText' AND label == '%s' AND visible == 1" % Q(label))
            if c:
                r = self.call("GET", "/element/%s/rect" % c[0])
                if 120 < r["y"] < 860:
                    self.tap(r["x"] + r["width"] / 2, r["y"] + r["height"] / 2)
                    if nav is None or self.navbar(nav, 2.5):
                        return True
                    continue
            self.swipe(215, 700, 215, 450, 250)
            time.sleep(0.9)
        return False

    # ---- composer helpers
    def field_value(self, field_pred):
        f = self.find(field_pred)
        return (self.attr(f[0], "value") or "") if f else None

    def type_verify(self, field_pred, text, eid=None):
        """focus the field (unless text was prefilled), type, verify with ONE query. Never sends."""
        pred = "%s AND value == '%s'" % (field_pred, Q(text))
        f = [eid] if eid else self.until(lambda: self.find(field_pred), 6)
        if not f:
            raise Fail("input field not found")
        self.click(f[0])
        self.mark("focus")
        if not eid or True:
            # empty the field FIRST (one query), so old text can never be sent along with ours
            empty = "%s AND (value == nil OR value == '' OR value == 'Message...' OR value == 'Compose message' OR value == 'Message')" % field_pred
            if not (text == "" or self.find(empty)):
                n = len(self.field_value(field_pred) or "")
                self.keys("\b" * (n + 2))
                self.mark("preclear")
        self.keys(text, f[0])
        self.mark("type")
        ok = bool(self.find(pred))
        self.mark("verify_typed")
        return ok, text if ok else (self.field_value(field_pred) or "")

    def clear_verify(self, field_pred, n=None, placeholders=("Message...", "Compose message", "Message")):
        """backspace n chars at once (n = typed length + 2), then ONE query that the field is empty/placeholder; up to 2 more passes."""
        empty = "%s AND (value == nil OR value == ''%s)" % (field_pred, "".join(" OR value == '%s'" % x for x in placeholders))
        for i in range(4):
            if i == 0 and n:
                self.keys("\b" * (n + 2))
            elif i == 1:
                # backspaces miss when the field isn't focused or the cursor isn't at the end (a WhatsApp deep link prefills
                # the box without focusing it): clear the element itself
                fl = self.find(field_pred)
                if fl:
                    try:
                        self.call("POST", "/element/%s/clear" % fl[0], {})
                    except Fail:
                        pass
            elif i:
                self.keys("\b" * ((len(self.field_value(field_pred) or "")) + 2))
            if self.find(empty):
                self.mark("clear_verify")
                return True
        self.mark("clear_verify")
        return False


def out(d):
    print(json.dumps(d, ensure_ascii=False, separators=(",", ":")))


def finish(p, res, home):
    if home:
        p.home()
        p.mark("home")
        res["home"] = True
    res["steps_ms"] = dict(p.steps)
    res["phone_ms"] = int((time.time() - p.t0) * 1000)
    res["phone"] = p.name
    return res


def cache_get(name):
    d = jload(CONTACTS)
    return d.get(name.strip().lower(), {}).get("digits")


def cache_put(name, digits):
    d = jload(CONTACTS)
    d[name.strip().lower()] = {"name": name, "digits": digits, "seen": time.strftime("%Y-%m-%d")}
    jsave(CONTACTS, d)


# ======================= verbs =======================
_BIDI = dict.fromkeys(map(ord, "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200b\u200c\u200d\ufeff"))


def wa_norm(s):
    """WhatsApp labels carry bidi marks (U+202A/U+202C/U+200E...) and non-breaking spaces around numbers and some names."""
    s = (s or "").translate(_BIDI)
    s = re.sub(r"[\u00a0\u202f\u2007\u2009]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def wa_digits(s):
    return re.sub(r"\D", "", wa_norm(s))


def wa_is_number(s):
    return bool(re.fullmatch(r"[+\d\s().\-\u00a0\u202f]+", wa_norm(s) or "x")) and len(wa_digits(s)) >= 6


def v_wa_send(p, a):
    try:
        return _wa_send(p, a)
    except Exception:
        try:   # whatever went wrong, never leave our text sitting in a composer
            if p.active() == "net.whatsapp.WhatsApp":
                p.clear_verify("name == 'ChatBar_ComposerTextView'", len(a.text))
        except Exception:
            pass
        raise


def _wa_send(p, a):
    to, text = a.to, a.text
    to_is_num = wa_is_number(to)
    res = {"verb": "wa_send", "to": "<number>" if to_is_num else to, "dry_run": a.dry_run, "sent": False}   # never echo numbers
    CHATS = "net.whatsapp.WhatsApp"
    HDR = "name == 'NavigationBar_HeaderViewButton'"
    COMP = "name == 'ChatBar_ComposerTextView'"
    if to_is_num and a.phone_digits and wa_digits(to) != wa_digits(a.phone_digits):
        raise Fail("--to number and --phone-digits differ; nothing opened")
    a.from_cache = False
    if not a.phone_digits and not to_is_num and not getattr(a, "no_cache", False):
        cd = cache_get(to)
        if cd:
            a.phone_digits, a.from_cache = cd, True
    if to_is_num and not a.phone_digits and not getattr(a, "no_cache", False):
        a.phone_digits = wa_digits(to)   # a number: the deep link is ~4 s; the chat-list route took ~30 s (header still verified)
    want_digits = wa_digits(a.phone_digits) if a.phone_digits else (wa_digits(to) if to_is_num else "")

    def header_matches(h):
        """name: exact after removing bidi marks/NBSP (case-insensitive). Number: digits only. A deep link only counts
        when the header itself proves the chat: the saved name equals --to, or an unsaved number's digits equal the target."""
        hn = wa_norm(h)
        if not hn:
            return False
        if not to_is_num and hn.lower() == wa_norm(to).lower():
            return True
        has_letters = bool(re.search(r"[^\W\d_]", hn))
        return bool(want_digits) and not has_letters and wa_digits(hn) == want_digits

    def find_cell():
        if to_is_num:   # list cells spell numbers out with spaces/commas ('+ 3 5 8,4 1,...'): compare the leading digits
            hits = []
            for c in p.find("type == 'XCUIElementTypeCell' AND name BEGINSWITH '+'")[:30]:
                nm = wa_norm(p.attr(c, "name") or "")
                head = re.split(r"[^\d+ ,]", nm, maxsplit=1)[0]     # stop at the first letter (preview text / time come later)
                if re.sub(r"\D", "", head).startswith(want_digits) and len(re.sub(r"\D", "", head)) - len(want_digits) <= 4:
                    hits.append(c)
            return hits
        cell = p.find("type == 'XCUIElementTypeCell' AND (name == '%s' OR name BEGINSWITH '%s, ')" % (Q(to), Q(to)))
        if cell:
            return cell
        hits = []   # names with hidden marks / NBSP: normalise and compare the part before the first ", "
        for c in p.find("type == 'XCUIElementTypeCell' AND name CONTAINS[c] '%s'" % Q(to.split()[0]))[:30]:
            if wa_norm(p.attr(c, "name") or "").split(", ")[0].lower() == wa_norm(to).lower():
                hits.append(c)
        return hits

    if a.phone_digits:
        p.open_url("whatsapp://send?phone=%s&text=%s" % (want_digits, urllib.parse.quote(text)))
        res["route"] = "deeplink(cache)" if a.from_cache else "deeplink"
    else:
        if p.active() != CHATS:
            p.launch(CHATS)
        res["route"] = "list"
    p.mark("open")
    if a.phone_digits:
        # arrival marker: the deep link prefills the text. Until it shows, the header on screen may still be the chat
        # that was open before (seen live: the old chat's name matched while the composer switched to another chat).
        def arrived():
            try:
                c = p.find(COMP)
                return bool(c) and (p.attr(c[0], "value") or "") == text
            except Fail:
                return False
        res["prefill_seen"] = bool(p.until(arrived, 6, 0.15))
        if not res["prefill_seen"]:
            time.sleep(1.0)   # no prefill (e.g. very long text): let any chat switch finish before reading the header
    header = None
    for _ in range(8):
        h = p.until(lambda: p.find(HDR) or p.find("name == 'TabBarButton_Chats' OR name == 'NavigationBar_NewChatButton'"), 6)
        if not h:
            raise Fail("whatsapp did not settle")
        hd = p.find(HDR)
        if hd:
            header = p.attr(hd[0], "label") or ""
            if a.phone_digits and not header_matches(header):
                # the deep link may still be switching away from the chat that was open: give it a moment
                end = time.time() + 4
                while time.time() < end and not header_matches(header):
                    time.sleep(0.25)
                    hd = p.find(HDR)
                    header = (p.attr(hd[0], "label") or "") if hd else header
            if a.phone_digits or header_matches(header):
                break          # deep link: never navigate away from the chat it opened; verify it below
            b = p.find("name == 'BackButton'")
            if not b:
                raise Fail("cannot leave the open chat")
            p.click(b[0])
            p.until(lambda: not p.find(HDR), 4)
            continue
        cell = find_cell()
        if len(cell) > 1:
            raise Fail("ambiguous: %d chats match %s" % (len(cell), "the number" if to_is_num else to))
        if not cell:
            tab = p.find("name == 'TabBarButton_Chats'")
            if tab and not p.find("name == 'NavigationBar_NewChatButton'"):
                p.click(tab[0])
                continue
            p.swipe(215, 400, 215, 700, 250)  # scroll list to top, retry
            cell = p.until(lambda: find_cell()[:1], 2)
            if not cell:
                raise Fail("chat %s not found in list" % ("for the number" if to_is_num else "'%s'" % to))
        p.tapel(cell[0])
        p.mark("open_chat")
        p.until(lambda: p.find(HDR), 6)
    p.mark("navigate")
    hn = wa_norm(header) if header else None
    res["header"] = hn if hn and re.search(r"[^\W\d_]", hn) else ("<number>" if hn else None)   # never echo phone numbers
    res["header_ok"] = bool(header) and header_matches(header)
    if not res["header_ok"]:
        if a.phone_digits:   # deep link prefilled the text in a chat we can't prove is the target: empty it, send nothing
            try:
                res["cleared_wrong_chat"] = p.clear_verify(COMP, len(text))
            except Exception:
                res["cleared_wrong_chat"] = False
        res.update(ok=False, error="chat header does not match %s; nothing sent" % ("the number" if to_is_num else "'%s'" % to))
        return res
    if a.phone_digits:
        f = p.find(COMP)
        v = (p.attr(f[0], "value") or "") if f else ""
        typed_ok = v == text
        if not typed_ok:     # prefill missing/mangled: type it ourselves (empties the box first, reads back)
            typed_ok, v = p.type_verify(COMP, text)
        p.mark("verify_typed")
    else:
        typed_ok, v = p.type_verify(COMP, text)
    res["typed_ok"] = typed_ok
    send = p.until(lambda: p.find("name == 'ChatBar_SendButton'"), 2)
    res["send_button"] = bool(send)
    hd = p.find(HDR)   # last look right before sending: still the same, verified chat?
    res["header_recheck"] = bool(hd) and header_matches(p.attr(hd[0], "label") or "")
    if not res["header_recheck"]:
        typed_ok = False
    if a.dry_run or not typed_ok or not send:
        res["cleared"] = p.clear_verify(COMP, len(text))
        res["ok"] = typed_ok and bool(send) and res["cleared"] if a.dry_run else False
        if not a.dry_run:
            res["error"] = "typed text mismatch or no send button; nothing sent"
        return res
    p.click(send[0])
    p.mark("send")
    res["cleared"] = bool(p.until(lambda: not p.find("name == 'ChatBar_SendButton'"), 4))
    res["sent"] = res["cleared"]
    p.mark("verify_sent")
    res["ok"] = res["sent"]
    return res


def v_ig_dm(p, a):
    h, text = a.handle.lstrip("@"), a.text
    res = {"verb": "ig_dm", "handle": h, "dry_run": a.dry_run, "sent": False}
    FIELD = "name == 'direct-keyboard-text-view'"
    p.open_url("instagram://user?username=%s" % urllib.parse.quote(h))
    p.mark("open")
    # wait for the profile Message button; never touch the Follow button
    btn = p.until(lambda: p.find("name == 'profile-action-bar-button' AND label == 'Message'") if p.find("name == '%s'" % Q(h)) else None, 10)
    res["profile_ok"] = bool(btn)
    p.mark("profile")
    if not btn:
        raise Fail("profile %s / Message button not on screen" % h)
    p.click(btn[0])
    p.until(lambda: p.find(FIELD), 8)
    title = p.until(lambda: p.find("name == 'direct_thread_title_button' AND label BEGINSWITH '%s'" % Q(h)), 6)
    p.mark("thread")
    res["thread_ok"] = bool(title)
    if not title:
        raise Fail("DM thread for %s not open" % h)
    typed_ok, v = p.type_verify(FIELD, text)
    res["typed_ok"] = typed_ok
    send = p.find("type == 'XCUIElementTypeButton' AND label CONTAINS[c] 'send' AND NOT label CONTAINS[c] 'saved'")
    res["send_button"] = bool(send)
    if a.dry_run or not typed_ok or not send:
        res["cleared"] = p.clear_verify(FIELD, len(text))
        res["ok"] = typed_ok and bool(send) and res["cleared"] if a.dry_run else False
        if not a.dry_run:
            res["error"] = "typed text mismatch or no send button; nothing sent"
        return res
    p.click(send[0])
    p.mark("send")
    f = p.until(lambda: (lambda x: x and (p.attr(x[0], "value") or "") in ("", "Message..."))(p.find(FIELD)), 4)
    res["cleared"] = bool(f)
    res["sent"] = res["cleared"]
    p.mark("verify_sent")
    res["ok"] = res["sent"]
    return res


def v_discord_dm(p, a):
    name, text = a.name, a.text
    res = {"verb": "discord_dm", "to": name, "dry_run": a.dry_run, "sent": False}
    FIELD = "type == 'XCUIElementTypeTextView' AND label BEGINSWITH 'Compose Message, '"
    ROW = "type == 'XCUIElementTypeButton' AND label BEGINSWITH '%s (direct message)'" % Q(name)
    BACK = "type == 'XCUIElementTypeButton' AND label BEGINSWITH 'Back'"
    EXACT = "type == 'XCUIElementTypeTextView' AND label == 'Compose Message, %s'" % Q(name)
    in_discord = p.active() == "com.hammerandchisel.discord"
    p.open_url("discord://-/channels/@me")
    p.mark("open")
    rows, end = [], time.time() + 14
    while time.time() < end:
        # Always look for Back first: Discord resumes inside the last DM, and the DM list rows stay in the tree UNDER
        # the open chat, so a row "found" there taps into the open DM (seen live: asked for one friend, landed in another DM).
        if p.active() != "com.hammerandchisel.discord":
            time.sleep(0.1)
            continue
        probe = (lambda: p.find(BACK), lambda: p.find(ROW))
        for k, fn in enumerate(probe):
            hit = fn()
            if hit:
                break
        if not hit:
            time.sleep(0.1)
            continue
        is_row = (k == 1)
        if is_row:
            rows = hit
            break
        p.click(hit[0])
        p.mark("back")
        in_discord = False
    p.mark("list")
    if len(rows) != 1:
        raise Fail("%d DM rows match %s" % (len(rows), name))
    p.click(rows[0])
    p.mark("open_dm")
    fld = p.until(lambda: p.find(EXACT), 8)
    res["name_ok"] = bool(fld)
    res["other_names"] = not fld
    res["field_label"] = "Compose Message, %s" % name if fld else (p.attr(p.find("type == 'XCUIElementTypeTextView' AND label BEGINSWITH 'Compose Message, '")[0], "label") if p.find("type == 'XCUIElementTypeTextView' AND label BEGINSWITH 'Compose Message, '") else None)
    p.mark("verify_dm")
    if not fld:
        raise Fail("DM field label '%s' is not for %s" % (res["field_label"], name))
    typed_ok, v = p.type_verify(FIELD, text, fld[0])
    res["typed_ok"] = typed_ok
    send = p.find("type == 'XCUIElementTypeButton' AND label == 'Send'")
    res["send_button"] = bool(send)
    if a.dry_run or not typed_ok or not send:
        res["cleared"] = p.clear_verify(FIELD, len(text))
        res["ok"] = typed_ok and bool(send) and res["cleared"] if a.dry_run else False
        if not a.dry_run:
            res["error"] = "typed text mismatch or no send button; nothing sent"
        return res
    p.click(send[0])
    p.mark("send")
    res["cleared"] = bool(p.until(lambda: not p.find("type == 'XCUIElementTypeButton' AND label == 'Send'"), 4))
    res["sent"] = res["cleared"]
    p.mark("verify_sent")
    res["ok"] = res["sent"]
    return res


def v_open_app(p, a):
    key = a.app.lower()
    bid = APPS.get(key)
    if not bid:
        raise Fail("unknown app '%s' (known: %s)" % (a.app, ", ".join(sorted(APPS))))
    res = {"verb": "open_app", "app": key}
    if p.active() != bid:
        p.launch(bid)
    p.mark("launch")
    res["active"] = p.until(lambda: p.active() == bid, 5) is True
    p.mark("settle")
    res["ok"] = res["active"]
    return res


def v_tap(p, a):
    p.tap(a.x, a.y)
    p.mark("tap")
    return {"verb": "tap", "x": a.x, "y": a.y, "ok": True}


def v_swipe(p, a):
    p.swipe(a.x1, a.y1, a.x2, a.y2, a.ms)
    p.mark("swipe")
    return {"verb": "swipe", "ok": True}


def v_home(p, a):
    p.home()
    ok = bool(p.until(lambda: p.active() == "com.apple.springboard", 3, 0.1))
    p.mark("home")
    return {"verb": "home", "ok": ok}


def v_read(p, a):
    if a.mode == "light":
        labs, n = light_labels(p, a.limit, a.maxlen)
        p.mark("light")
        return {"verb": "read", "mode": "light", "app": p.active(), "n_elements": n, "n": len(labs), "labels": labs, "ok": True}
    els = p.source(a.depth)
    p.mark("source")
    labels, seen = [], set()
    for e in els:
        if e.get("visible") == "false" or e["t"] in ("Key", "Keyboard"):
            continue
        l = e.get("label") or e.get("name") or ""
        if l and l not in seen and e["t"] not in ("Application", "Window", "Toolbar"):
            seen.add(l)
            labels.append(l.replace("\u200e", "").replace("\u2068", "").replace("\u2069", "")[:a.maxlen])
    return {"verb": "read", "app": p.active(), "n": len(labels), "labels": labels[:a.limit], "ok": True}


def clean(l):
    return l.replace("\u200e", "").replace("\u2068", "").replace("\u2069", "").replace("\u202a", "").replace("\u202c", "").strip()


def light_labels(p, limit=60, maxlen=80, types=("StaticText", "Button", "Link", "TextField", "TextView", "Cell")):
    """tree-light read: predicate query for visible labelled elements, then label per element (no full XML source)."""
    pred = "(%s) AND visible == 1" % " OR ".join("type == 'XCUIElementType%s'" % t for t in types)
    ids = p.find(pred)
    labs, seen = [], set()
    for i in ids[:limit]:
        try:
            l = clean(p.attr(i, "label") or p.attr(i, "name") or "")
        except Fail:
            continue
        if l and l not in seen:
            seen.add(l)
            labs.append(l[:maxlen])
    return labs, len(ids)


def v_wa_learn(p, a):
    """open the verified chat, open its WhatsApp contact info, store the visible number locally. Never prints the number."""
    to = a.to
    res = {"verb": "wa_learn", "to": to}
    HDR = "name == 'NavigationBar_HeaderViewButton'"
    if p.active() != "net.whatsapp.WhatsApp":
        p.launch("net.whatsapp.WhatsApp")
    p.until(lambda: p.find(HDR) or p.find("name == 'NavigationBar_NewChatButton'"), 8)
    for _ in range(3):
        hd = p.find(HDR)
        if hd:
            if clean(p.attr(hd[0], "label") or "").lower() == to.lower():
                break
            b = p.find("name == 'BackButton'")
            if b:
                p.click(b[0])
                p.until(lambda: not p.find(HDR), 4)
            continue
        cell = p.find("type == 'XCUIElementTypeCell' AND (name == '%s' OR name BEGINSWITH '%s, ')" % (Q(to), Q(to)))
        if len(cell) != 1:
            raise Fail("%d chats match %s" % (len(cell), to))
        p.tapel(cell[0])
        p.until(lambda: p.find(HDR), 6)
    p.mark("chat")
    hd = p.find(HDR)
    header = clean(p.attr(hd[0], "label") or "") if hd else ""
    res["header_ok"] = header.lower() == to.lower()
    if not res["header_ok"]:
        raise Fail("header mismatch")
    p.click(hd[0])
    NUM = "type == 'XCUIElementTypeStaticText' AND label MATCHES '^[+]?[0-9][0-9 ()-]{7,18}$'"
    nums = p.until(lambda: p.find(NUM), 6)
    p.mark("contact_info")
    digits = None
    if nums:
        digits = re.sub(r"\D", "", p.attr(nums[0], "label") or "")
    res["number"] = "found" if digits and len(digits) >= 8 else "not found"
    if digits and len(digits) >= 8:
        cache_put(to, digits)
        res["cached"] = True
    b = p.find("name == 'BackButton' OR (type == 'XCUIElementTypeButton' AND name == 'Back')")
    if b:
        p.click(b[0])
        p.until(lambda: p.find(HDR), 3)
    b = p.find("name == 'BackButton'")
    if b:
        p.click(b[0])
    p.mark("back")
    res["ok"] = bool(res["cached"]) if "cached" in res else False
    return res


def v_battery(p, a):
    res = {"verb": "battery"}
    bi = p.call("GET", "/wda/batteryInfo")
    res["api_level_pct"] = round(float(bi.get("level", -1)) * 100)
    res["api_state"] = {1: "unplugged", 2: "charging", 3: "full"}.get(bi.get("state"), bi.get("state"))
    p.mark("api")
    p.call("POST", "/wda/apps/terminate", {"bundleId": "com.apple.Preferences"})   # start Settings at its root
    p.launch("com.apple.Preferences")
    p.navbar("Settings", 6)
    p.mark("open_settings")
    if not p.tap_text("Battery", nav="Battery"):
        raise Fail("Battery row not opened")
    p.mark("tap_battery")
    PCT = "type == 'XCUIElementTypeStaticText' AND label MATCHES '^[0-9]{1,3} ?%$'"
    got = p.until(lambda: p.find(PCT), 6)   # first % label on the page = the big level (app usage is never read)
    p.mark("read_page")
    res["settings_pct"] = int(re.sub(r"\D", "", p.attr(got[0], "label"))) if got else None
    res["ok"] = res["settings_pct"] is not None
    res["match"] = res["ok"] and abs(res["settings_pct"] - res["api_level_pct"]) <= 2
    b = p.find("type == 'XCUIElementTypeButton' AND label == 'Settings'")
    if b:
        p.call("POST", "/element/%s/click" % b[0])
    p.mark("back")
    return res


# ---- Safari: one tab per job. x-safari-https:// opens a NEW tab; the tool closes the tab it opened (or reuses it),
# and only ever touches the front tab after checking its address is the page the tool itself opened.
SAFARI = "com.apple.mobilesafari"


def host_of(url):
    h = re.sub(r"^[a-z-]*https?://", "", (url or "").strip(), flags=re.I).split("/")[0].split("?")[0].lower()
    return re.sub(r"^www\.", "", h)


def safari_host(p):
    """host shown in the front tab's address bar (compact bar shows the host only), or None."""
    try:   # the bar is rebuilt while a page loads: a stale element just means "not readable yet"
        f = p.find("name == 'TabBarItemTitle'", 3)
        return host_of(wa_norm(p.attr(f[0], "value") or "")) if f else None
    except Fail:
        return None


def safari_tabs_menu(p):
    """long-press the Tabs button: returns (tab_count or None, 'Close This Tab' element id). Dismiss with menu_dismiss()."""
    b = p.find("name == 'TabOverviewButton'", 3)
    if not b:
        raise Fail("Safari Tabs button not found")
    r = p.attr(b[0], "rect")
    x, y = r["x"] + r["width"] / 2, r["y"] + r["height"] / 2
    p.call("POST", "/actions", {"actions": [{"type": "pointer", "id": "f1", "parameters": {"pointerType": "touch"}, "actions": [
        {"type": "pointerMove", "duration": 0, "x": x, "y": y}, {"type": "pointerDown", "button": 0},
        {"type": "pause", "duration": 800}, {"type": "pointerUp", "button": 0}]}]})
    close = p.until(lambda: p.find("type == 'XCUIElementTypeButton' AND label == 'Close This Tab'", 2), 3, 0.2)
    if not close:
        raise Fail("Safari tab menu did not open")
    n = None
    for e in p.find("type == 'XCUIElementTypeButton' AND label MATCHES '^[0-9]+ Tabs?$'", 2):
        n = int(p.attr(e, "label").split()[0])
    return n, close[0]


def safari_close_tab(p, want_host):
    """close the FRONT tab only if its address host is want_host (a page this tool opened). Never 'Close All'."""
    if p.active() != SAFARI:
        return {"closed": False, "why": "Safari not in front"}
    h = safari_host(p)
    if not want_host or h != want_host:
        return {"closed": False, "why": "front tab is not the page this tool opened"}
    n, close = safari_tabs_menu(p)
    if p.attr(close, "label") != "Close This Tab":   # paranoia: never tap anything else in that menu
        raise Fail("unexpected tab menu item")
    p.click(close)
    time.sleep(0.6)
    return {"closed": True, "tabs_before": n}


def safari_goto(p, url, reuse_host=None):
    """open url in Safari. If the front tab is the tool's own tab (host == reuse_host) load it there; else a new tab.
    Returns 'reused' or 'new_tab'. Waits until the address bar shows the target host."""
    want = host_of(url)
    check_allowed(SAFARI, "safari")
    if reuse_host and p.active() == SAFARI and safari_host(p) == reuse_host:
        f = p.find("name == 'TabBarItemTitle'", 3)
        p.click(f[0])
        ed = p.until(lambda: p.find("type == 'XCUIElementTypeTextField' AND name == 'URL'", 2), 3, 0.2)
        if ed:
            p.keys(url + "\n")
            if p.until(lambda: safari_host(p) == want, 10, 0.25):
                return "reused"
    p.open_url(re.sub(r"^https?://", lambda m: "x-safari-" + m.group(0), url))
    if not p.until(lambda: p.active() == SAFARI, 6):
        raise Fail("Safari did not open (front app: %s)" % p.active())
    if not p.until(lambda: safari_host(p) == want, 10, 0.25):
        raise Fail("Safari tab did not reach %s" % want)
    return "new_tab"


def v_close_tab(p, a):
    res = {"verb": "close_tab", "host": host_of(a.url)}
    res.update(safari_close_tab(p, host_of(a.url)))
    res["ok"] = res["closed"]
    return res


def v_read_page(p, a):
    res = {"verb": "read_page", "url": a.url}
    # x-safari-https:// forces Safari (the default browser may be another app) in a NEW tab; the tab is closed after the
    # read (--keep-tab keeps it), so repeated reads never pile up tabs. Other tabs are never touched.
    res["tab"] = safari_goto(p, a.url)
    p.mark("open")
    CHROME = ("URL", "Address", "Tab Bar", "Back", "Forward", "Share", "Bookmarks", "Tabs", "Reload", "Page Menu", "Show Tabs")
    prev, labels = None, []
    t_end = time.time() + 15
    while time.time() < t_end:
        # light dump (no `visible` etc.: 5-10x faster) filtered by the on-screen rect; web content often says visible=false
        p.depth(50)
        xml = p.call("GET", "/source?format=xml&excluded_attributes=visible,accessible,enabled,index,placeholderValue,selected,hittable,traits,focused", timeout=40)
        els = []
        for m in TAG.finditer(xml):
            e = {k: html.unescape(v) for k, v in ATTR.findall(m.group(2))}
            e["t"] = m.group(1)
            try:
                y, h = float(e.get("y", 0)), float(e.get("height", 0))
            except ValueError:
                y, h = 0, 0
            if h > 0 and y + h > 0 and y < 1000:
                els.append(e)
        labels, seen = [], set()
        for e in els:
            if e["t"] in ("StaticText", "Link", "Button", "Heading", "TextView", "Other"):
                l = clean(e.get("label") or e.get("value") or "")
                if len(l) > 2 and l not in seen and not any(l == c or l.startswith(c + ",") for c in CHROME) and e["t"] != "Other":
                    seen.add(l)
                    labels.append(l[:a.maxlen])
        if len(labels) >= 3 and labels == prev:
            break
        prev = labels
        time.sleep(0.25)
    p.mark("read")
    try:   # the web area (a big "Other" element) carries the page title as its label
        res["title"] = next(clean(e.get("label", "")) for e in els if e["t"] == "Other" and e.get("label") and float(e.get("width", 0)) > 300 and float(e.get("height", 0)) > 300)[:a.maxlen]
    except (StopIteration, ValueError):
        res["title"] = None
    res["n"] = len(labels)
    res["labels"] = labels[:a.limit]
    res["ok"] = len(labels) >= 3
    if not a.keep_tab:
        c = safari_close_tab(p, host_of(a.url))
        res["tab_closed"] = c["closed"]
        p.mark("close_tab")
    return res


def main():
    ap = argparse.ArgumentParser(prog="act.py")
    ap.add_argument("--phone", default=None, help="config.json phone name or alias (default: default_phone, else the first iPhone)")
    ap.add_argument("--home", action="store_true", help="press Home after the verb")
    sub = ap.add_subparsers(dest="verb", required=True)
    s = sub.add_parser("wa_send"); s.add_argument("--to", required=True); s.add_argument("--text", required=True); s.add_argument("--phone-digits"); s.add_argument("--no-cache", action="store_true"); s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("ig_dm"); s.add_argument("--handle", required=True); s.add_argument("--text", required=True); s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("discord_dm"); s.add_argument("--name", required=True); s.add_argument("--text", required=True); s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("open_app"); s.add_argument("app")
    s = sub.add_parser("tap"); s.add_argument("x", type=float); s.add_argument("y", type=float)
    s = sub.add_parser("swipe"); [s.add_argument(k, type=float) for k in ("x1", "y1", "x2", "y2")]; s.add_argument("--ms", type=int, default=300)
    sub.add_parser("home")
    s = sub.add_parser("read"); s.add_argument("--depth", type=int, default=50); s.add_argument("--limit", type=int, default=80); s.add_argument("--maxlen", type=int, default=60); s.add_argument("--mode", default="xml", choices=["xml", "light"])
    sub.add_parser("battery")
    s = sub.add_parser("read_page"); s.add_argument("--url", required=True); s.add_argument("--limit", type=int, default=40); s.add_argument("--maxlen", type=int, default=160); s.add_argument("--keep-tab", action="store_true")
    s = sub.add_parser("close_tab"); s.add_argument("--url", required=True, help="the page this tool opened; the front tab is closed only if it shows that host")
    s = sub.add_parser("wa_learn"); s.add_argument("--to", required=True)
    a = ap.parse_args()
    t_all = time.time()
    p = None
    try:
        if a.verb == "open_app":
            check_allowed(APPS.get(a.app.lower()), a.app)
        check_allowed(VERB_APP.get(a.verb), a.verb)   # before touching the phone
        p = Phone(a.phone)
        p.mark("connect")
        if p.locked():
            raise Fail("phone is locked")
        res = globals()["v_" + a.verb](p, a)
        res.setdefault("ok", True)
    except Exception as e:
        res = {"verb": a.verb, "ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        if p is None:
            out(res)
            sys.exit(1)
    finally:
        if p:
            p.restore()
            p.save_state()
    res = finish(p, res, a.home and res.get("ok", False) or (a.home and True))
    res["wall_ms"] = int((time.time() - t_all) * 1000)
    out(res)
    sys.exit(0 if res.get("ok") else 1)


if __name__ == "__main__":
    main()
