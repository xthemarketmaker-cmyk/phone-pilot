"""autolock.py PHONE get|set VALUE  - Settings > Display & Brightness > Auto-Lock. Touches only that row.
PHONE = config.json name or alias. VALUE is the row text, e.g. "Never" or "5 minutes". Read the old value first, restore it after."""
import sys, time, json
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import act
ph, mode = sys.argv[1], sys.argv[2]; target = sys.argv[3] if len(sys.argv) > 3 else None
p = act.Phone(ph); out = {"phone": ph, "mode": mode}; t0 = time.time()
def tap_text(label, scrolls=5, nav=None):
    for i in range(scrolls + 2):
        c = p.find("type == 'XCUIElementTypeStaticText' AND label == '%s' AND visible == 1" % label)
        if c:
            r = p.call("GET", "/element/%s/rect" % c[0])
            if 120 < r["y"] < 860:
                p.tap(r["x"] + r["width"] / 2, r["y"] + r["height"] / 2)
                if nav is None or navbar(nav, 2.5): return True
                continue
        p.swipe(215, 700, 215, 450, 250); time.sleep(0.9)
    return False
def navbar(n, t=5):
    return p.until(lambda: p.find("type == 'XCUIElementTypeNavigationBar' AND name == '%s'" % n), t)
def cur():
    c = p.find("type == 'XCUIElementTypeCell' AND label BEGINSWITH 'Auto-Lock, '")
    return act.clean(p.attr(c[0], "label")).split(", ", 1)[1] if c else None
try:
    p.call("POST", "/wda/apps/terminate", {"bundleId": "com.apple.Preferences"})
    p.launch("com.apple.Preferences"); navbar("Settings", 6); time.sleep(0.5)
    if not tap_text("Display & Brightness", nav="Display & Brightness"): raise act.Fail("Display & Brightness not opened")
    time.sleep(0.4)
    for i in range(4):
        if p.find("type == 'XCUIElementTypeCell' AND label BEGINSWITH 'Auto-Lock, ' AND visible == 1"): break
        p.swipe(215, 700, 215, 450, 250); time.sleep(0.4)
    out["before"] = cur()
    if mode == "set" and out["before"] != target:
        if not tap_text("Auto-Lock", nav="Auto-Lock"): raise act.Fail("Auto-Lock page not opened")
        time.sleep(0.4)
        if not tap_text(target, 2): raise act.Fail("option '%s' not found" % target)
        time.sleep(0.5)
        sel = p.find("type == 'XCUIElementTypeCell' AND label BEGINSWITH '%s' AND selected == 1" % target) or p.find("type == 'XCUIElementTypeButton' AND label BEGINSWITH '%s' AND selected == 1" % target)
        out["option_checked"] = bool(sel)
        b = p.find("type == 'XCUIElementTypeButton' AND (label == 'Display & Brightness' OR label == 'Back') AND visible == 1")
        if b: p.call("POST", "/element/%s/click" % b[0])
        navbar("Display & Brightness", 4); time.sleep(0.4)
        out["after"] = cur()
    elif mode == "set":
        out["after"] = out["before"]
    out["ok"] = (out.get("after") == target) if mode == "set" else out["before"] is not None
except Exception as e:
    out["ok"] = False; out["error"] = str(e)[:200]
finally:
    try:
        p.home()
    finally:
        p.restore(); p.save_state()
out["ms"] = int((time.time() - t0) * 1000)
print(json.dumps(out))
