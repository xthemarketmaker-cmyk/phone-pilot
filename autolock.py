"""autolock.py PHONE get|set VALUE  - Settings > Display & Brightness > Auto-Lock. Touches only that row.
PHONE = config.json name or alias. VALUE is the row text, e.g. "Never" or "5 minutes". Read the old value first, restore it after.
Takes the shared phone lock (C:\\phonebot\\locks, owner PhoneBot + this process id) for the whole run, the same lock
do.py / act.py / pp.py and the live window use: if anyone else is driving the phone it prints busy and exits 4 with
nothing pressed. Phone locked: exit 3, nothing pressed. Every screen is verified by its title; a step that does not
land is retried once from a fresh Settings. The Display & Brightness row is found by its identifier
(com.apple.settings.displayAndBrightness), so a Recent/Suggestions copy of the same label is never the one tapped.
Exit: 0 ok, 1 failed, 2 usage, 3 phone locked, 4 busy."""
import sys, time, json
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import act

SETTINGS = "com.apple.Preferences"
DB = "Display & Brightness"
DB_ID = "com.apple.settings.displayAndBrightness"


def main():
    if len(sys.argv) < 3 or sys.argv[2] not in ("get", "set") or (sys.argv[2] == "set" and len(sys.argv) < 4):
        print(__doc__)
        sys.exit(2)
    ph, mode = sys.argv[1], sys.argv[2]
    target = sys.argv[3] if len(sys.argv) > 3 else None
    out = {"phone": ph, "mode": mode}
    t0 = time.time()
    try:
        p = act.Phone(ph)   # checks + takes the lock; refreshed on every WDA call; released at exit (atexit)
    except Exception as e:
        busy = str(e).startswith("busy")
        out.update(ok=False, busy=busy, pressed=False, error=str(e)[:200], ms=int((time.time() - t0) * 1000))
        print(json.dumps(out))
        sys.exit(4 if busy else 1)
    out["lock"] = os.path.basename(act.pp.lockfile(p.ph))
    code = 1

    def navbar(n, t=5):
        return p.until(lambda: p.find("type == 'XCUIElementTypeNavigationBar' AND name == '%s'" % n), t)

    def rect(eid):
        return p.call("GET", "/element/%s/rect" % eid)

    def visible_rows(pred):
        """matching elements that sit inside the scrolling area (not under the nav bar or the bottom search bar), top first"""
        rows = []
        for eid in p.find(pred + " AND visible == 1"):
            r = rect(eid)
            if 120 < r["y"] and r["y"] + r["height"] / 2 < 850:
                rows.append((r["y"], eid, r))
        return sorted(rows, key=lambda x: x[0])

    def drag_up():
        """scroll the list up ~250 pt with NO fling (slow move, hold, lift): a gliding list spends the next tap on
        stopping itself, which is how "Display & Brightness not opened" happened with a fast swipe"""
        p.call("POST", "/actions", {"actions": [{"type": "pointer", "id": "f1", "parameters": {"pointerType": "touch"}, "actions": [
            {"type": "pointerMove", "duration": 0, "x": 215, "y": 700}, {"type": "pointerDown", "button": 0},
            {"type": "pointerMove", "duration": 450, "x": 215, "y": 450}, {"type": "pause", "duration": 200},
            {"type": "pointerUp", "button": 0}]}]})
        time.sleep(0.5)

    def tap_row(preds, title=None, scrolls=6):
        """scroll until a row matching the first predicate that has a hit is on screen, tap it, verify `title` shows.
        Several matches: the LAST (bottom-most) one - a Recent/Suggestions copy sits above the real row. A tap that
        did not open the page while the row is still there is tapped once more."""
        for i in range(scrolls + 1):
            for pred in preds:
                rows = visible_rows(pred)
                if rows:
                    for k in (1, 2):
                        y, eid, r = rows[-1]
                        out.setdefault("taps", []).append({"row": pred.split("'")[1][:40], "matches": len(rows), "y": int(r["y"])})
                        p.tap(r["x"] + r["width"] / 2, r["y"] + r["height"] / 2)
                        if title is None or navbar(title, 3):
                            return True
                        rows = visible_rows(pred)
                        if not rows:
                            return False
                    return False
            drag_up()
        return False

    def open_settings():
        p.call("POST", "/wda/apps/terminate", {"bundleId": SETTINGS})
        p.launch(SETTINGS)
        ok = bool(navbar("Settings", 6))
        time.sleep(0.4)
        return ok

    def goto_display():
        last = None
        for attempt in (1, 2):   # verify each screen; a step that does not land is retried once from a fresh Settings
            if not open_settings():
                last = "Settings did not open"
                continue
            if tap_row(["name == '%s'" % DB_ID, "type == 'XCUIElementTypeStaticText' AND label == '%s'" % DB], DB):
                out["display_tries"] = attempt
                time.sleep(0.4)
                return
            last = "Display & Brightness not opened"
            try:   # what page did we land on (nav bar titles only)
                out["landed_on"] = [act.clean(p.attr(n, "name") or "")[:40] for n in p.find("type == 'XCUIElementTypeNavigationBar'")][:3]
            except Exception:
                pass
        raise act.Fail(last + " (tried twice)")

    def autolock_row():
        for i in range(5):
            c = p.find("type == 'XCUIElementTypeCell' AND label BEGINSWITH 'Auto-Lock, ' AND visible == 1")
            if c:
                return c
            drag_up()
        return []

    def cur():
        c = p.find("type == 'XCUIElementTypeCell' AND label BEGINSWITH 'Auto-Lock, '")
        return act.clean(p.attr(c[0], "label")).split(", ", 1)[1] if c else None

    try:
        if p.locked():
            out.update(ok=False, pressed=False, error="phone is locked")
            code = 3
            raise act.Fail("phone is locked")
        goto_display()
        autolock_row()
        out["before"] = cur()
        if out["before"] is None:   # D&B page drawn but the row not read: once more from the start
            goto_display(); autolock_row(); out["before"] = cur()
        if mode == "set" and out["before"] != target:
            opened = False
            for attempt in (1, 2):
                if attempt == 2:
                    goto_display(); autolock_row()
                if tap_row(["type == 'XCUIElementTypeCell' AND label BEGINSWITH 'Auto-Lock, '"], "Auto-Lock", 2):
                    opened = True
                    out["autolock_tries"] = attempt
                    break
            if not opened:
                raise act.Fail("Auto-Lock page not opened (tried twice)")
            time.sleep(0.4)
            if not tap_row(["type == 'XCUIElementTypeCell' AND label BEGINSWITH '%s'" % target,
                            "type == 'XCUIElementTypeStaticText' AND label == '%s'" % target], None, 2):
                raise act.Fail("option '%s' not found" % target)
            time.sleep(0.5)
            sel = p.find("type == 'XCUIElementTypeCell' AND label BEGINSWITH '%s' AND selected == 1" % target) or \
                p.find("type == 'XCUIElementTypeButton' AND label BEGINSWITH '%s' AND selected == 1" % target)
            out["option_checked"] = bool(sel)
            b = p.find("type == 'XCUIElementTypeButton' AND (label == 'Display & Brightness' OR label == 'Back') AND visible == 1")
            if b:
                p.call("POST", "/element/%s/click" % b[0])
            if not navbar(DB, 4):
                goto_display(); autolock_row()
            time.sleep(0.4)
            out["after"] = cur()
        elif mode == "set":
            out["after"] = out["before"]
        out["ok"] = (out.get("after") == target) if mode == "set" else out["before"] is not None
        code = 0 if out["ok"] else 1
    except Exception as e:
        out["ok"] = False
        out.setdefault("error", str(e)[:200])
    finally:
        try:
            if code != 3:
                p.home()
        finally:
            p.restore(); p.save_state()
            p.lock_free()
    out["ms"] = int((time.time() - t0) * 1000)
    print(json.dumps(out))
    sys.exit(code)


if __name__ == "__main__":
    main()
