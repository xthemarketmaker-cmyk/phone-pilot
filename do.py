#!/usr/bin/env python
"""do.py - generic fast iPhone primitives for tasks nobody scripted. One Shell call -> one JSON line.
  do.py [--phone 17|14] [--home] [--allow-risky] CMD ARGS...
  do.py [--phone 17] chain "open settings; tap_label General; tap_label About; read --contains iOS"
Quoting: ';' inside quotes ("type 'a; b'") or written as \\; does not split a chain. `--` ends flags. type takes its
  text literally (only --clear / --enter are flags, so "-5 deg" and "well - ok" type as written). type --clear alone
  just empties the field. A chain stops at the first failed step but still goes Home if it ends with home.
Commands: open APP|BUNDLE, open_url URL, tap_label TEXT [--nth N] [--type T] [--timeout S], tap X Y,
  type TEXT [--clear] [--enter], read [--contains S] [--limit N] [--no-rects] [--xml] [--all], wait_for TEXT [--timeout S],
  back, scroll up|down [--px N], home, wait MS, tap_rel FX FY, tap_nth TYPE N, dismiss, hide_keyboard, shot PATH, close_tab (the Safari tab open_url opened; open_url reuses it)
Speed: every lookup is ONE light XML dump (source with the costly attributes such as `visible` excluded:
  ~60-250 ms instead of 0.3-6 s) filtered by on-screen rect, then a coordinate tap at the element centre.
  snapshotMaxDepth is raised only inside this process and restored to 1 in finally (Phone Panel needs 1)."""
import sys, os, re, json, time, html, shlex, argparse, hashlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # emoji in labels crashed print() on a cp1252 pipe
except Exception:
    pass
import act
from act import Fail

EXCL = "visible,accessible,enabled,index,placeholderValue,selected,hittable,traits,focused"
TAG = re.compile(r"<XCUIElementType(\w+)\s([^>]*?)/?>")
ATTR = re.compile(r'(\w+)="([^"]*)"')
APPS = dict(act.APP_NAMES, settings="com.apple.Preferences", appstore="com.apple.AppStore", **{"app store": "com.apple.AppStore"},
            youtube="com.google.ios.youtube", safari="com.apple.mobilesafari", clock="com.apple.mobiletimer", calculator="com.apple.calculator",
            compass="com.apple.compass", mail="com.apple.mobilemail", camera="com.apple.camera", files="com.apple.DocumentsApp",
            calendar="com.apple.mobilecal", maps="com.apple.Maps", weather="com.apple.weather", music="com.apple.Music",
            reminders="com.apple.reminders", translate="com.apple.Translate", measure="com.apple.measure", tips="com.apple.tips",
            facebook="com.facebook.Facebook", tiktok="com.zhiliaoapp.musically", chatgpt="com.openai.chat", grok="ai.x.GrokApp",
            brave="com.brave.ios.browser", kick="com.kick.mobile", chrome="com.google.chrome.ios", spotify="com.spotify.client", telegram="ph.telegra.Telegraph")
RISKY = re.compile(r"(?i)^\s*(send|post|reply|repost|retweet|quote|like|unlike|follow|unfollow|subscribe|unsubscribe|delete|remove|erase|reset|"
                   r"buy|purchase|pay|install|get|update|download|redownload|sign out|log out|sign in|log in|turn off|turn on|allow|block|report|"
                   r"call|facetime|share|add|save|done|confirm|ok|accept|join|book(?!\s*store)|order|checkout|place order|gift|tip|donate|sub|"
                   r"notification|bell|claim)\b|^\s*(follow|unfollow|like|subscrib|send|notification|gift)\w*")
CHAT_APPS = {"net.whatsapp.WhatsApp", "com.hammerandchisel.discord", "com.burbn.instagram", "com.atebits.Tweetie2", "com.apple.mobilemail",
             "com.facebook.Messenger", "ph.telegra.Telegraph", "org.whispersystems.signal"}
APP_DEPTH = {"com.atebits.Tweetie2": 24}   # X: feed cells sit at depth ~23; deeper = their subtrees (seconds)
HEAVY = {"com.atebits.Tweetie2"}            # no settle loops: one dump can take 0.4-15 s while the feed animates
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "do_state.json")     # busy flags per phone+app
LAYOUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "layout_cache.json")  # label/id -> centre, per phone+app (local only)
BUSY_MS = 3000          # a light dump slower than this => the app is "busy" (video + fast chat, animating feed)
BUSY_TTL = 900          # stay in busy mode for 15 min after the last slow dump
BUSY_SETTINGS = {"snapshotMaxChildren": 40, "customSnapshotTimeout": 3, "waitForIdleTimeout": 0, "animationCoolOffTimeout": 0, "shouldUseCompactResponses": True}
NORMAL_SETTINGS = {"snapshotMaxChildren": 2147483647, "customSnapshotTimeout": 15}
RELAUNCH_BACK = {"com.kick.mobile"}   # in busy mode, "back" = terminate + relaunch (in-stream back is unreliable / slow)
PRIO = {"StaticText": 0, "Key": 1, "Cell": 2, "Button": 3, "Link": 4, "Icon": 5, "SearchField": 6, "TextField": 6, "TextView": 7, "Image": 8, "Other": 9}


FOLD = str.maketrans({"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"', "\u00a0": " ", "\u2013": "-", "\u2014": "-"})
INTERACTIVE = ("Button", "Cell", "Image", "Switch", "Link", "Icon", "SearchField", "TextField", "SegmentedControl", "Slider")
SAFE_DISMISS = ("Not Now", "Not now", "Close", "Cancel", "Dismiss", "Maybe Later", "Later", "Remind Me Later")   # "No Thanks" can be a real choice (e.g. "Restore All Downloads?"): ask


class Do:
    def __init__(self, phone, allow_risky=False):
        self.p = act.Phone(phone)
        self.allow = allow_risky
        ws = self.p.call("GET", "/window/size")
        self.W, self.H = ws["width"], ws["height"]
        self.last_dump_ms = 0
        self.app = self.p.active()
        self.phone = str(phone) if phone else self.p.alias
        self.state = act.jload(STATE)
        self.layout = act.jload(LAYOUT)
        self.busy_set = False
        self.p.call("POST", "/appium/settings", {"settings": {"waitForIdleTimeout": 0, "animationCoolOffTimeout": 0}})

    def key(self, app=None):
        return "%s|%s" % (self.phone, app or self.app)

    def busy(self):
        return self.state.get(self.key(), {}).get("until", 0) > time.time()

    def mark_busy(self, ms):
        self.state[self.key()] = {"until": time.time() + BUSY_TTL, "last_ms": ms}
        if not self.busy_set:
            self.p.call("POST", "/appium/settings", {"settings": BUSY_SETTINGS})
            self.busy_set = True

    def close(self):
        if self.busy_set:
            try:
                self.p.call("POST", "/appium/settings", {"settings": NORMAL_SETTINGS})
            except Exception:
                pass
        for path, d in ((STATE, self.state), (LAYOUT, self.layout)):
            try:
                act.jsave(path, d)
            except Exception:
                pass

    def remember(self, els):
        """cache centres of short on-screen labels/ids for coordinate taps in busy mode."""
        lay = self.layout.setdefault(self.key(), {})
        seen_now = {}   # this dump wins over older entries; within it the best tappable type wins
        spots = {}
        for e in els:   # a label shown in two different places (tab + page title) is ambiguous: never cache it from a dump
            if e["on"]:
                for k in (self.text(e), e.get("name") or ""):
                    if k:
                        c = (round((e["r"][0] + e["r"][2] / 2) / 40), round((e["r"][1] + e["r"][3] / 2) / 40))
                        spots.setdefault(k, set()).add(c)
        pinned = self.layout.setdefault(self.key() + "|tapped", {})
        for e in els:
            if not e["on"]:
                continue
            x, y, w, h = e["r"]
            if w * h > self.W * self.H * 0.3:
                continue
            if e["t"] in ("NavigationBar", "Toolbar", "TabBar", "ScrollView", "Table", "CollectionView", "Window", "Application", "Keyboard"):
                continue
            for k in (self.text(e), e.get("name") or ""):
                if len(spots.get(k, ())) > 1 or k in pinned:
                    continue
                old = lay.get(k)
                if k and len(k) <= 60 and (old is None or PRIO.get(e["t"], 9) <= PRIO.get(old[2], 9) or seen_now.get(k) is None):
                    if seen_now.get(k) is not None and PRIO.get(e["t"], 9) > PRIO.get(seen_now[k], 9):
                        continue
                    lay[k] = [round(x + w / 2), round(y + h / 2), e["t"]]
                    seen_now[k] = e["t"]

    # ---- one light dump
    def dump(self, depth=None, full=False):
        t = time.time()
        if self.busy() and not self.busy_set:
            self.p.call("POST", "/appium/settings", {"settings": BUSY_SETTINGS})
            self.busy_set = True
        self.p.depth(depth or APP_DEPTH.get(self.app, 50))
        xml = self.p.call("GET", "/source?format=xml" + ("" if full else "&excluded_attributes=" + EXCL), timeout=60)
        els = []
        for m in TAG.finditer(xml):
            a = {k: html.unescape(v) for k, v in ATTR.findall(m.group(2))}
            try:
                x, y, w, h = (float(a.get(k, 0)) for k in ("x", "y", "width", "height"))
            except ValueError:
                continue
            a["t"], a["r"] = m.group(1), (x, y, w, h)
            a["on"] = w > 0 and h > 0 and x + w > 0 and y + h > 0 and x < self.W and y < self.H
            els.append(a)
        self.last_dump_ms = int((time.time() - t) * 1000)
        if self.last_dump_ms > BUSY_MS:
            self.mark_busy(self.last_dump_ms)
        self.remember(els)
        return els

    @staticmethod
    def text(e):
        return act.clean(e.get("label") or e.get("value") or "")

    @staticmethod
    def full(e):
        l, v = act.clean(e.get("label") or ""), act.clean(e.get("value") or "")
        return "%s: %s" % (l, v) if l and v and v != l else (l or v)

    def sig(self, els):
        # scroll bars fade out for ~1.5 s after any scroll and change their value: ignore them or nothing ever "settles"
        return hashlib.md5("|".join("%s%s%d,%d" % (e["t"], self.text(e), e["r"][0], e["r"][1]) for e in els
                                    if e["on"] and "scroll bar" not in self.text(e).lower()).encode()).hexdigest()

    def settle(self, before, maxwait=2.0):
        """after an action: poll light dumps until the screen differs from `before`, then until two dumps match."""
        if self.app in HEAVY or self.busy():
            return None
        end, prev = time.time() + maxwait, None
        changed = False
        self.settle_log = []
        while time.time() < end:
            els = self.dump()
            s = self.sig(els)
            self.settle_log.append(s[:4] + ("*" if s == before else ""))
            if self.last_dump_ms > 1200:      # huge tree (X feed): one dump is all we can afford
                return els
            if s != before:
                changed = True
            if changed and s == prev:
                return els
            prev = s
        return None

    def find_label(self, text, nth=1, typ=None, els=None):
        els = els if els is not None else self.dump()
        cand = [e for e in els if e["on"] and (self.text(e) or e.get("name")) and e["t"] not in ("Application", "Window", "Switch", "Slider") and (not typ or e["t"] == typ)]
        F = lambda x: x.translate(FOLD).lower()   # curly quotes / NBSP / dashes as typed vs as shown ("What\u2019s new")
        tl = F(text)
        N = lambda e: e.get("name") or ""
        for lvl, fn in ((1, lambda e: self.text(e) == text), (2, lambda e: N(e) == text), (3, lambda e: text in self.text(e)),
                        (4, lambda e: tl in F(self.text(e))), (5, lambda e: tl in F(N(e)))):
            hits = [e for e in cand if fn(e)]
            if hits:
                hits.sort(key=lambda e: (round(e["r"][1] / 8), PRIO.get(e["t"], 9)))   # top-first, then preferred type
                uniq = []
                for e in hits:   # one per on-screen spot (a Cell and its StaticText are the same target)
                    cx, cy = e["r"][0] + e["r"][2] / 2, e["r"][1] + e["r"][3] / 2
                    dup = next((u for u in uniq if u["r"][0] <= cx <= u["r"][0] + u["r"][2] and u["r"][1] <= cy <= u["r"][1] + u["r"][3]
                                or e["r"][0] <= u["r"][0] + u["r"][2] / 2 <= e["r"][0] + e["r"][2] and e["r"][1] <= u["r"][1] + u["r"][3] / 2 <= e["r"][1] + e["r"][3]), None)
                    if dup is None:
                        uniq.append(e)
                    elif PRIO.get(e["t"], 9) < PRIO.get(dup["t"], 9):
                        uniq[uniq.index(dup)] = e
                if len(uniq) >= nth:
                    return uniq[nth - 1], lvl, len(uniq)
        return None, 0, 0

    # ---- commands
    def c_open(self, a):
        name = " ".join(a.rest)
        bid = APPS.get(name.lower()) or (name if "." in name else None)
        if not bid:
            raise Fail("unknown app '%s' (use a bundle id)" % name)
        act.check_allowed(bid, name)   # owner's list in config.json "blocked_apps"; checked before anything is pressed
        if a.fresh:   # start the app at its first screen (it reopens wherever it was left otherwise)
            self.p.call("POST", "/wda/apps/terminate", {"bundleId": bid})
        if self.p.active() != bid:
            self.p.launch(bid)
        ok = self.p.until(lambda: self.p.active() == bid, 6) is True
        self.app = bid
        self.settle("", 1.5)
        return {"ok": ok, "app": bid}

    def c_open_url(self, a):
        url = a.rest[0]
        if not re.match(r"^https?://", url):
            raise Fail("only http(s) URLs")
        # one tab per phone: if the front tab is the one do.py opened last time, load the URL there; else a new tab.
        tabkey = self.phone + "|safari_tab"
        mine = self.state.get(tabkey, {}).get("host")
        how = act.safari_goto(self.p, url, reuse_host=mine)
        self.state[tabkey] = {"host": act.host_of(url), "ts": time.time()}
        ok = True
        self.app = "com.apple.mobilesafari"
        host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
        CHROME = re.compile(r"^(Back|Forward|Page Menu|Close|Tabs|Share|Reload|Address.*|.*scroll bar.*|Apple|Done|Bookmarks)$")
        def content():
            els = self.dump()
            return [e for e in els if e["on"] and e["t"] in ("StaticText", "Link", "Image", "Button") and e["r"][1] > 60 and not CHROME.match(self.text(e) or "x")]
        got = self.p.until(lambda: len(content()) >= 3, 12, 0.15)
        self.settle("", 1.5)
        return {"ok": ok and bool(got), "content": bool(got), "tab": how}

    def c_close_tab(self, a):
        """close the Safari tab do.py opened (only if it is in front and still shows that host)."""
        tabkey = self.phone + "|safari_tab"
        mine = self.state.get(tabkey, {}).get("host")
        r = act.safari_close_tab(self.p, mine)
        if r["closed"]:
            self.state.pop(tabkey, None)
        return dict(r, ok=r["closed"])

    def drag(self, x1, y1, x2, y2, ms=300, hold=0):
        """a scroll with NO fling: move, then hold still before lifting. A fling keeps the list gliding and iOS spends
        the next tap on stopping it (seen on an iPhone 14 Pro Max: Settings > General was 'tapped' but never opened)."""
        self.p.call("POST", "/actions", {"actions": [{"type": "pointer", "id": "f1", "parameters": {"pointerType": "touch"}, "actions": [
            {"type": "pointerMove", "duration": 0, "x": x1, "y": y1}, {"type": "pointerDown", "button": 0},
            {"type": "pointerMove", "duration": ms, "x": x2, "y": y2}] + ([{"type": "pause", "duration": hold}] if hold else []) + [{"type": "pointerUp", "button": 0}]}]})

    def c_tap_label(self, a):
        text = " ".join(a.rest)
        if self.busy() and not a.fresh_lookup and not RISKY.match(text) and a.nth == 1:
            hit = self.layout.get(self.key() + "|tapped", {}).get(text) or self.layout.get(self.key(), {}).get(text)
            if hit:   # busy app: skip the (10 s+) dump, tap the cached centre
                self.p.tap(hit[0], hit[1])
                return {"ok": True, "tapped": text[:60], "type": hit[2], "match": "cached-layout", "busy": True}
        end = time.time() + a.timeout
        self._t_tl, self.scroll_ms = time.time(), []
        scrolls, prev, stable, since = 0, None, 0, 0
        while True:
            els = self.dump()
            e, lvl, n = self.find_label(text, a.nth, a.type, els)
            sg = self.sig(els)
            stable = stable + 1 if sg == prev else 0
            prev = sg
            if e and (scrolls == 0 or stable >= 2):   # after a scroll, only trust a position once three dumps agree (momentum)
                if scrolls:
                    # iOS spends the first tap on a list that is still (invisibly) decelerating on stopping it: let it rest
                    time.sleep(0.35)
                    els = self.dump()
                    e, lvl, n = self.find_label(text, a.nth, a.type, els)
                    if not e:
                        continue
                break
            # with --scroll: once the screen has held still for 2 dumps the label is simply below the fold, so scroll now
            # instead of waiting out the whole timeout (that cost ~4 s per scroll)
            since += 1
            # label not there 2 dumps after a scroll: scroll again right away (no need to wait for the glide to end)
            if not e and scrolls < a.scroll and (time.time() > end or stable >= 2 or (scrolls and since >= 2)):
                t_d = time.time()
                self.drag(self.W / 2, self.H * 0.72, self.W / 2, self.H * 0.38)
                self.scroll_ms = getattr(self, "scroll_ms", []) + [int((t_d - self._t_tl) * 1000), int((time.time() - t_d) * 1000)]
                self._t_tl = time.time()
                scrolls += 1
                prev, stable, since = None, 0, 0
                end = time.time() + 1.5
                continue
            if time.time() > end:
                break
        if not e:
            al = self.alert(els)
            raise Fail("label '%s' not on screen%s" % (text, al and "; " + al or ""))
        al = self.alert(els, e)
        if al:
            raise Fail(al)
        lab = self.text(e) or e.get("name") or ""
        if (RISKY.match(lab) or RISKY.match(e.get("name") or "")) and not self.allow:
            raise Fail("'%s' looks like an action that sends/changes/buys; refused (needs --allow-risky on the owner's word)" % lab[:40])
        x, y, w, h = e["r"]
        self.p.tap(x + w / 2, y + h / 2)
        self.layout.setdefault(self.key() + "|tapped", {})[text] = [round(x + w / 2), round(y + h / 2), e["t"]]   # what worked
        retap = False
        if e["t"] != "Key" and self.app not in HEAVY and not self.busy():   # keys never navigate: no settle needed
            after = self.settle(self.sig(els))
            same = None
            if scrolls and after is not None:   # after a scroll iOS often spends the first tap on stopping the list
                same = next((f for f in after if f["on"] and self.text(f) == self.text(e) and abs(f["r"][1] - e["r"][1]) < 12 and abs(f["r"][0] - e["r"][0]) < 12), None)
            self.tapcheck = {"settled": after is not None, "same": same is not None, "y": e["r"][1],
                             "y_after": [f["r"][1] for f in (after or []) if f["on"] and self.text(f) == self.text(e)][:2]}
            if (after is None and e["t"] in ("StaticText", "Cell")) or same is not None:
                # nothing changed: the tap was swallowed (list still gliding, keyboard closing). Re-find and tap once more.
                els2 = self.dump()
                e2, _, _ = self.find_label(text, a.nth, a.type, els2)
                if e2:
                    x, y, w, h = e2["r"]
                    self.p.tap(x + w / 2, y + h / 2)
                    retap = self.settle(self.sig(els2)) is not None
        return {"ok": True, **({"retapped": retap} if retap else {}), "scrolled": scrolls, "tapped": lab[:60], "type": e["t"], "match": ["", "exact", "id", "contains", "icase", "id-icase"][lvl], "of": n}

    def c_tap(self, a):
        x, y = float(a.rest[0]), float(a.rest[1])
        before = self.sig(self.dump())
        self.p.tap(x, y)
        self.settle(before)
        return {"ok": True}

    def alert(self, els, target=None):
        """an iOS alert takes every tap outside it (the tap 'works' but nothing happens). Returns a plain description if
        one is up and target is not inside it, else None."""
        for al in els:
            if al["on"] and al["t"] == "Alert":
                ax, ay, aw, ah = al["r"]
                if target is not None:
                    x, y, w, h = target["r"]
                    if ax <= x + w / 2 <= ax + aw and ay <= y + h / 2 <= ay + ah:
                        return None
                btns = [self.text(b) for b in els if b["on"] and b["t"] == "Button" and ax <= b["r"][0] <= ax + aw and ay <= b["r"][1] <= ay + ah and self.text(b)]
                return "a popup is in the way: '%s' (buttons: %s). dismiss taps only Not Now/Cancel/Close/Dismiss/Later; any other choice is the owner's" % (
                    self.text(al)[:60], ", ".join(btns[:4]))
        return None

    def c_tap_rel(self, a):
        """tap at a fraction of the screen (0-1), for controls with no label: tap_rel 0.5 0.9"""
        fx, fy = float(a.rest[0]), float(a.rest[1])
        if not (0 <= fx <= 1 and 0 <= fy <= 1):
            raise Fail("tap_rel takes fractions 0-1")
        before = self.sig(self.dump())
        self.p.tap(fx * self.W, fy * self.H)
        self.settle(before)
        return {"ok": True, "x": int(fx * self.W), "y": int(fy * self.H)}

    def c_tap_nth(self, a):
        """tap the Nth (1-based) on-screen element of a type, top to bottom: tap_nth Cell 2 (unlabeled lists / grids)"""
        t, n = a.rest[0], int(a.rest[1]) if len(a.rest) > 1 else 1
        end = time.time() + max(a.timeout, 6)
        while True:   # screens that are still loading (spinner) get time to fill in
            allels = self.dump()
            els = sorted([e for e in allels if e["on"] and e["t"] == t and e["r"][3] > 8 and e["r"][1] > 50],
                         key=lambda e: (round(e["r"][1] / 10), e["r"][0]))
            if len(els) >= n or time.time() > end:
                break
            time.sleep(0.3)
        if len(els) < n:
            spin = any(e["on"] and e["t"] == "ActivityIndicator" for e in allels)
            raise Fail("only %d %s on screen%s" % (len(els), t, " (still loading)" if spin else ""))
        e = els[n - 1]
        al = self.alert(self.dump(), e)
        if al:
            raise Fail(al)
        lab = self.text(e)
        if lab and RISKY.match(lab) and not self.allow:
            raise Fail("refused risky label '%s' (needs the user's OK: --allow-risky)" % lab[:40])
        before = self.sig(els)
        x, y, w, h = e["r"]
        self.p.tap(x + w / 2, y + h / 2)
        self.settle(before)
        return {"ok": True, "tapped": lab[:60] or "<no label>", "type": t, "n": n}

    def c_dismiss(self, a):
        """close a popup, sheet or banner without accepting anything: taps only Not Now / Close / Cancel / Dismiss / Later;
        else swipes a sheet down by its top edge. Never taps Allow, OK, Continue, Accept."""
        els = self.dump()
        before = self.sig(els)
        for want in SAFE_DISMISS:
            for e in els:
                if e["on"] and e["t"] in ("Button", "Link", "StaticText") and self.text(e) == want:
                    x, y, w, h = e["r"]
                    self.p.tap(x + w / 2, y + h / 2)
                    self.settle(before)
                    return {"ok": self.sig(self.dump()) != before, "via": want}
        sheets = [e for e in els if e["on"] and e["t"] in ("Sheet", "Alert", "Other") and (self.text(e).lower() in ("sheet grabber", "grabber") or e["t"] == "Sheet")]
        if sheets:
            x, y, w, h = sheets[0]["r"]
            self.drag(self.W / 2, max(y, 60) + 10, self.W / 2, self.H * 0.95, ms=250)
            self.settle(before)
            return {"ok": self.sig(self.dump()) != before, "via": "swipe sheet down"}
        return {"ok": False, "via": None, "why": "no safe dismiss button or sheet found (an Allow/OK choice is the user's)"}

    def c_hide_keyboard(self, a):
        """hide the on-screen keyboard without pressing Return (Return can send or submit)."""
        kb = lambda: any(e["on"] and e["t"] == "Keyboard" for e in self.dump())
        if not kb():
            return {"ok": True, "was_shown": False}
        try:
            self.p.call("POST", "/wda/keyboard/dismiss", {"keyNames": []})
        except Fail:
            pass
        via = "keyboard/dismiss"
        if kb():   # many apps ignore keyboard/dismiss: tap a plain heading (ends editing in most scroll views)
            els = self.dump()
            head = [e for e in els if e["on"] and e["t"] == "StaticText" and 70 < e["r"][1] < 220 and e["r"][3] >= 28 and not RISKY.match(self.text(e))]
            if head:
                x, y, w, h = head[0]["r"]
                self.p.tap(x + 10, y + h / 2)
                time.sleep(0.4)
                via = "tap heading"
        if kb():   # interactive dismiss: drag the content down a little
            self.drag(self.W / 2, self.H * 0.35, self.W / 2, self.H * 0.6, ms=200)
            time.sleep(0.3)
            via = "drag down"
        if kb():   # last: the editor's own top-bar Done (ends editing; it never sends)
            done = [e for e in self.dump() if e["on"] and e["t"] == "Button" and self.text(e) == "Done" and e["r"][1] < 130]
            if done:
                x, y, w, h = done[0]["r"]
                self.p.tap(x + w / 2, y + h / 2)
                time.sleep(0.4)
                via = "top-bar Done"
        return {"ok": not kb(), "was_shown": True, "via": via}

    def c_shot(self, a):
        """save a screenshot to a file ON THIS PC (it never leaves the PC by itself): shot C:\\phonebot\\logs\\s.png.
        For a vision fallback only with the user's OK and never on private screens."""
        import base64
        path = a.rest[0]
        png = base64.b64decode(self.p.call("GET", "/screenshot"))
        open(path, "wb").write(png)
        return {"ok": True, "path": path, "bytes": len(png)}

    def c_type(self, a):
        text = " ".join(a.rest)
        if text in ('""', "''"):   # PowerShell quoting leftovers (type '""' --clear): the owner meant "no text"
            text = ""
        if not text and not a.clear:
            raise Fail("type needs text (or --clear to empty the field)")
        app = self.p.active()
        if a.enter and app in CHAT_APPS and not self.allow:
            raise Fail("--enter refused in a messaging app (could send)")
        eid = None
        try:
            r = self.p.call("GET", "/element/active")
            eid = r.get("ELEMENT") or (list(r.values())[0] if r else None)
        except Fail:
            pass
        if not eid:
            f = [e for e in self.dump() if e["on"] and e["t"] in ("SearchField", "TextField", "TextView")]
            if not f:
                raise Fail("no text field on screen")
            x, y, w, h = f[0]["r"]
            self.p.tap(x + w / 2, y + h / 2)
            time.sleep(0.3)
            try:
                r = self.p.call("GET", "/element/active")
                eid = r.get("ELEMENT") or list(r.values())[0]
            except Exception:
                eid = None
        if eid and self.p.attr(eid, "type") == "XCUIElementTypeSecureTextField":
            raise Fail("refusing to type into a password field")
        ph = None
        if a.clear and eid:
            v = self.p.attr(eid, "value") or ""
            try:
                ph = self.p.attr(eid, "placeholderValue") or None
            except Exception:
                ph = None
            if v and v != ph:
                self.p.keys("\b" * (len(v) + 2))
        if text:
            self.p.keys(text)
        v = (self.p.attr(eid, "value") or "") if eid else None
        if a.enter:
            self.p.keys("\n")
            self.settle("", 2.0)
        if not text:   # clear only: verified when the field is empty (or shows just its placeholder)
            if eid and v not in ("", ph):   # the value attribute can lag the last backspaces by a moment
                got = self.p.until(lambda: (lambda z: [z] if z in ("", ph) else None)(self.p.attr(eid, "value") or ""), 1.5, 0.1)
                v = got[0] if got else (self.p.attr(eid, "value") or "")
            empty = v is not None and v in ("", ph)
            return {"ok": v is None or empty, "verified": empty, "cleared": True, **({} if empty or v is None else {"left": v[:40]})}
        return {"ok": v is None or text in v, "verified": v is not None and text in v}

    def c_read(self, a):
        els = self.dump(full=a.xml)
        out, seen = [], set()
        for e in els:
            if not e["on"] or (e["t"] in ("Application", "Window", "Key", "Keyboard") and a.type != e["t"]):
                continue
            if a.type and e["t"] != a.type:
                continue
            l = self.full(e)
            if not l and a.all and e["t"] in INTERACTIVE and e["r"][2] * e["r"][3] < self.W * self.H * 0.5:
                l = "<no label %s @%d,%d>" % (e["t"], e["r"][0] + e["r"][2] / 2, e["r"][1] + e["r"][3] / 2)   # tap it with: tap X Y
            if not l or l in seen or (a.contains and a.contains.lower() not in l.lower()) or (a.match and not re.search(a.match, l)):
                continue
            seen.add(l)
            out.append(l[:a.maxlen] if a.no_rects else [l[:a.maxlen], e["t"], [int(v) for v in e["r"]]])
            if len(out) >= a.limit:
                break
        return {"ok": True, "app": self.p.active(), "dump_ms": self.last_dump_ms, "n": len(out), "labels": out}

    def c_wait_for(self, a):
        text = " ".join(a.rest).lower()
        hit = self.p.until(lambda: next((self.full(e) for e in self.dump() if e["on"] and text in self.full(e).lower()), None), a.timeout, 0.05)
        return {"ok": bool(hit), "found": (hit or "")[:60]}

    def c_back(self, a):
        if self.app in RELAUNCH_BACK and self.busy():
            self.p.call("POST", "/wda/apps/terminate", {"bundleId": self.app})
            self.p.launch(self.app)
            self.p.until(lambda: self.p.active() == self.app, 6)
            return {"ok": True, "via": "relaunch (busy app)"}
        els = self.dump()
        before = self.sig(els)
        # back button = a small Button in the top bar, at the left edge (iOS 26/27: a 44x44 round button labelled with the
        # previous page's title, often with no NavigationBar element in the light dump). Never a row's disclosure chevron.
        top = [e for e in els if e["on"] and e["t"] == "Button" and e["r"][1] < 120 and e["r"][1] + e["r"][3] < 130
               and e["r"][0] < self.W * 0.3 and e["r"][2] <= 160 and not RISKY.search(self.text(e) or "")]
        btn = sorted(top, key=lambda e: (e["r"][1] // 20, e["r"][0]))[0] if top else None
        if not btn:
            btn = next((e for e in els if e["on"] and e["t"] == "Button" and self.text(e) in ("Back", "Close", "Cancel") and e["r"][1] < 160), None)
        tries = []
        if btn:
            x, y, w, h = btn["r"]
            self.p.tap(x + w / 2, y + h / 2)
            tries.append("button '%s'" % self.text(btn)[:30])
            if self.settle(before, 1.5) is not None:
                return {"ok": True, "via": tries[-1]}
        self.p.swipe(2, self.H / 2, self.W * 0.75, self.H / 2, 300)
        tries.append("edge swipe")
        ok = self.settle(before, 2.0) is not None
        return {"ok": ok, "via": " then ".join(tries), **({} if ok else {"error": "screen did not change"})}

    def c_scroll(self, a):
        d = (a.rest[0] if a.rest else "down").lower()
        px = a.px or self.H * 0.45
        cx, cy = self.W / 2, self.H / 2
        if d == "down":
            self.p.swipe(cx, cy + px / 2, cx, cy - px / 2, 300)
        else:
            self.p.swipe(cx, cy - px / 2, cx, cy + px / 2, 300)
        time.sleep(0.35)   # let momentum stop before the next read
        return {"ok": True, "dir": d}

    # ---- Kick (com.kick.mobile)
    def c_kick_go(self, a):
        """kick_go NAME: fresh Kick, Search tab, type NAME, open "Navigate to NAME's channel", verify the streamer name."""
        name = " ".join(a.rest)
        K = "com.kick.mobile"
        t = {}
        t0 = time.time()
        self.p.call("POST", "/wda/apps/terminate", {"bundleId": K})
        self.p.launch(K)
        self.app = K
        self.p.until(lambda: self.p.active() == K, 6)
        READY = ("Featured Creators", "Streams For You", "Categories For You")
        sb = self.p.until(lambda: (lambda els: next((e for e in els if e["on"] and e.get("name") == "Bottom-Navbar-Button-Search"), None)
                                   if any((e.get("name") or "").startswith("homePageFeaturedStreamCard-0") for e in els) else None)(self.dump()), 10, 0.1)
        if not sb:
            raise Fail("Kick search tab not found")
        t["launch"] = int((time.time() - t0) * 1000)
        x, y, w, h = sb["r"]
        f = None
        for i in range(6):   # right after launch the tab bar is drawn before it accepts taps: re-tap until the field shows
            self.p.tap(x + w / 2, y + h / 2)
            f = self.p.until(lambda: next((e for e in self.dump() if e["on"] and e["t"] in ("TextField", "SearchField")), None), 0.6, 0.05)
            if f:
                break
        t["search_taps"] = i + 1
        if not f:
            raise Fail("Kick search field not found")
        x, y, w, h = f["r"]
        self.p.tap(x + w / 2, y + h / 2)
        self.p.keys(name)
        t["search"] = int((time.time() - t0) * 1000)
        want = "navigate to %s's channel" % name.lower()
        prev = [None]
        def stable_hit():   # the result list re-renders while search runs: only tap once it is the same twice in a row
            els = self.dump()
            h = next((e for e in els if e["on"] and (self.text(e).lower() == want or (e.get("name") or "").lower() == want)), None)
            sig = h and (tuple(int(v) for v in h["r"]), self.sig(els))
            ok = h if (h and sig == prev[0]) else None
            prev[0] = sig
            return ok
        hit = self.p.until(stable_hit, 8, 0.05)
        if not hit:
            near = [self.text(e)[:40] for e in self.dump() if e["on"] and self.text(e).lower().startswith("navigate to")][:3]
            raise Fail("no exact channel result for %s (saw: %s)" % (name, "; ".join(near) or "none"))
        x, y, w, h = hit["r"]
        for i in range(3):   # the first tap on a result can just dismiss the keyboard: re-tap while the results are still showing
            self.p.tap(x + w / 2, y + h / 2)
            gone = self.p.until(lambda: not any(self.text(e).lower() == want for e in self.dump() if e["on"]), 1.5, 0.05)
            if gone:
                break
        t["result_taps"] = i + 1
        t["results"] = int((time.time() - t0) * 1000)
        # verify: the channel/stream screen shows the streamer name as its own label (not the search result row)
        ok = self.p.until(lambda: (lambda els: not any(self.text(e).lower().startswith("navigate to") for e in els if e["on"])
                                   and any(self.text(e).lower() == name.lower() for e in els if e["on"]))(self.dump()), 10, 0.1)
        t["verified"] = int((time.time() - t0) * 1000)
        diag = None if ok else [self.text(e)[:30] for e in self.dump() if e["on"] and self.text(e)][:10]
        return {"ok": bool(ok), "diag": diag, "channel": name, "name_on_screen": bool(ok), "t_ms": t, "busy": self.busy(), "last_dump_ms": self.last_dump_ms}

    def c_kick_say(self, a):
        """kick_say TEXT [--dry-run]: type into Kick chat, verify, tap the 'send' button. RISKY: posts publicly."""
        text = " ".join(a.rest)
        if self.app != "com.kick.mobile":
            raise Fail("Kick is not the front app")
        if not self.allow and not a.dry_run:
            raise Fail("kick_say posts in a public chat: refused without --allow-risky (owner's own words only)")
        els = self.dump(40)
        f = next((e for e in els if e["on"] and e["t"] == "TextView" and self.text(e).startswith("Send a message")), None) or \
            next((e for e in els if e["on"] and e["t"] == "TextView"), None)
        if not f:
            raise Fail("chat box not on screen (log in, or chat hidden)")
        x, y, w, h = f["r"]
        kb = None
        for i in range(3):
            self.p.tap(x + w / 2, y + h / 2)
            kb = self.p.until(lambda: any(e["t"] == "Keyboard" and e["on"] for e in self.dump(40)), 2.0, 0.1)
            if kb:
                break
        if not kb:
            raise Fail("chat box did not take focus (no keyboard) - logged out or chat restricted?")
        tv = self.p.find("type == 'XCUIElementTypeTextView'")
        eid = tv[0] if tv else None
        if not eid:
            raise Fail("chat box element not found")
        old = self.p.attr(eid, "value") or ""
        if old and not old.startswith("Send a message"):
            self.p.keys("\b" * (len(old) + 2))      # empty first: only our text can go out
        self.p.keys(text)
        v = self.p.attr(eid, "value") or ""
        res = {"typed_ok": v == text, "sent": False}
        if a.dry_run or v != text:
            self.p.keys("\b" * (len(v) + 2))
            v2 = self.p.attr(eid, "value") or ""
            res.update(ok=(v == text) and (v2 == "" or v2.startswith("Send a message")) if a.dry_run else False, cleared=v2 == "" or v2.startswith("Send a message"))
            if not a.dry_run:
                res["error"] = "typed text mismatch; cleared, nothing sent"
            return res
        sb = next((e for e in self.dump(40) if e["on"] and (e.get("name") == "send" or self.text(e).lower() == "send") and e["t"] == "Button"), None)
        if not sb:
            self.p.keys("\b" * (len(v) + 2))
            raise Fail("send button not found; field cleared, nothing sent")
        x, y, w, h = sb["r"]
        self.p.tap(x + w / 2, y + h / 2)
        v3 = self.p.until(lambda: (lambda z: z if z == "" or z.startswith("Send a message") else None)(self.p.attr(eid, "value") or ""), 4)
        res.update(ok=v3 is not None, sent=v3 is not None, cleared=v3 is not None)
        return res

    def c_home(self, a):
        self.p.home()
        self.app = "com.apple.springboard"
        return {"ok": bool(self.p.until(lambda: self.p.active() == "com.apple.springboard", 6, 0.1))}

    def c_wait(self, a):
        time.sleep(float(a.rest[0]) / 1000)
        return {"ok": True}


class StepParser(argparse.ArgumentParser):
    def error(self, message):   # never print usage + SystemExit(2): that skipped the JSON line and the go-Home
        raise Fail("bad step: " + message)


def split_chain(s):
    """split a chain on ';' but not inside quotes, and not when written as \\;. A quote only opens at the start of a
    word, so an apostrophe inside a label (What's new) is plain text. An unclosed quote: split on every bare ';'."""
    steps, cur, q, i = [], [], None, 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s) and s[i + 1] == ";":
            cur.append(";"); i += 2; continue
        if q:
            if ch == q:
                q = None
        elif ch in "'\"" and (not cur or cur[-1].isspace()):
            q = ch
        elif ch == ";":
            steps.append("".join(cur)); cur = []; i += 1; continue
        cur.append(ch); i += 1
    if q:   # unbalanced quote: old behaviour (split on every ';' not escaped)
        return [x.replace("\x00", ";") for x in s.replace("\\;", "\x00").split(";")]
    steps.append("".join(cur))
    return steps


def split_step(s):
    """one step's words. Backslashes stay literal (Windows paths: shot C:\\phonebot\\logs\\s.png); an apostrophe in a
    label ("What's new") falls back to plain whitespace splitting."""
    try:
        lx = shlex.shlex(s, posix=True)
        lx.whitespace_split, lx.escape, lx.commenters = True, "", ""
        return list(lx)
    except ValueError:
        return s.split()


def parse_step(sp, s):
    """argparse for every command, except type: its text is literal. Only --clear / --enter (anywhere) are flags,
    `--` ends flags, everything else (-5 deg, well - ok, --x) is text."""
    if s and s[0] == "type":
        a = sp.parse_args(["type"])
        words, flags = [], True
        for w in s[1:]:
            if flags and w == "--":
                flags = False
            elif flags and w in ("--clear", "--enter"):
                setattr(a, w[2:], True)
            else:
                words.append(w)
        a.rest = words
        return a
    return sp.parse_args(s)


def parser():
    sp = StepParser(prog="step", add_help=False)
    sp.add_argument("cmd")
    sp.add_argument("rest", nargs="*")
    sp.add_argument("--nth", type=int, default=1)
    sp.add_argument("--type")
    sp.add_argument("--timeout", type=float, default=4.0)
    sp.add_argument("--clear", action="store_true")
    sp.add_argument("--enter", action="store_true")
    sp.add_argument("--contains")
    sp.add_argument("--limit", type=int, default=60)
    sp.add_argument("--maxlen", type=int, default=80)
    sp.add_argument("--no-rects", dest="no_rects", action="store_true")
    sp.add_argument("--xml", action="store_true")
    sp.add_argument("--all", action="store_true", help="read: also list unlabeled buttons/cells/images with their centre")
    sp.add_argument("--px", type=float)
    sp.add_argument("--fresh", action="store_true")
    sp.add_argument("--match")
    sp.add_argument("--dry-run", dest="dry_run", action="store_true")
    sp.add_argument("--fresh-lookup", dest="fresh_lookup", action="store_true")
    sp.add_argument("--scroll", type=int, default=0)
    return sp


def main():
    t_all = time.time()
    argv = sys.argv[1:]
    g = {"phone": None, "home": False, "allow": False}
    while argv and argv[0].startswith("--"):
        k = argv.pop(0)
        if k == "--phone": g["phone"] = argv.pop(0)
        elif k == "--home": g["home"] = True
        elif k == "--allow-risky": g["allow"] = True
    if not argv:
        print(__doc__); sys.exit(2)
    if argv[0] == "chain":
        steps = [split_step(s) for s in split_chain(" ".join(argv[1:])) if s.strip()]
        steps = [s for s in steps if s]
    else:
        steps = [argv]
    sp = parser()
    res = {"ok": True, "steps": []}
    d = None
    ends_home = len(steps) > 1 and steps[-1][:1] == ["home"]   # "stops at the first failed step but still goes Home"
    started = home_done = False
    try:
        for s in steps:   # owner's blocked apps: refuse the whole chain before touching the phone
            if s and s[0] == "open":
                name = " ".join(x for x in s[1:] if not x.startswith("--"))
                act.check_allowed(APPS.get(name.lower()) or (name if "." in name else None), name)
        d = Do(g["phone"], g["allow"])
        if d.p.locked():
            raise Fail("phone is locked")
        started = True   # from here on a failure still honours a final `home`
        for i, s in enumerate(steps):
            t = time.time()
            try:
                a = parse_step(sp, s)
                fn = getattr(d, "c_" + a.cmd, None)
                if not fn:
                    raise Fail("unknown command '%s'" % a.cmd)
                r = fn(a)
            except Exception as e:
                r = {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:160])}
            r = dict({"cmd": " ".join(s)[:60]}, **r, ms=int((time.time() - t) * 1000))
            res["steps"].append(r)
            if s[:1] == ["home"] and r.get("ok"):
                home_done = True
            if not r.get("ok"):
                res["ok"] = False
                break
    except Exception as e:
        res["ok"] = False
        res["error"] = "%s: %s" % (type(e).__name__, str(e)[:160])
    finally:
        if d:
            try:
                if g["home"] or (started and ends_home and not home_done and not res["ok"]):
                    try:
                        d.p.home()
                        if not g["home"]:   # the chain's own final home, done after a failed step
                            res["went_home"] = bool(d.p.until(lambda: d.p.active() == "com.apple.springboard", 6, 0.1))
                    except Exception as e:
                        res["went_home"] = False
                        res["home_error"] = "%s: %s" % (type(e).__name__, str(e)[:100])
            finally:
                d.close()
                d.p.restore()
                d.p.save_state()
                res["phone_ms"] = int((time.time() - d.p.t0) * 1000)
    res["wall_ms"] = int((time.time() - t_all) * 1000)
    print(json.dumps(res, ensure_ascii=False, separators=(",", ":")))
    sys.exit(0 if res["ok"] else 1)


if __name__ == "__main__":
    main()
