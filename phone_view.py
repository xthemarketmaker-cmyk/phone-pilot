r"""phone_view.py: live window of the phone on THIS PC. Click = tap, drag = swipe, wheel = scroll, keys = type.
  pythonw phone_view.py [phone-name] [--top]      (name from config.json; Esc hands the turn back)
Home button / Home key / middle-click = Home; Apps button / Tab / right-click = app switcher (hold a drag that starts at the bottom edge = switcher too); Android also has Back.
Shares pp.py's lock (owner PhoneLive-user): your first click takes your turn, 8 s idle releases it, and it never
fights the bot (badge: Bot driving / Your turn / Idle). Needs Pillow. Errors go to C:\phonebot\logs\viewer.log."""
import ctypes, io, os, queue, socket, sys, threading, time, tkinter as tk
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pp
os.makedirs(pp.LOGS, exist_ok=True)
sys.stdout = sys.stderr = open(os.path.join(pp.LOGS, "viewer.log"), "a", buffering=1)  # pythonw has no stderr
from PIL import Image, ImageTk

ME, IDLE = "PhoneLive-user", 8
args = [x for x in sys.argv[1:] if not x.startswith("--")]
ph = pp.pick(args[0] if args else None)
frame = {"jpg": None, "n": 0, "t": 0.0}
dim = {"pt": None}
jobs = queue.Queue()
sc = {"n": 0.0, "x": 0, "y": 0, "q": False}
put = lambda a: jobs.put((time.time(), a))
last = {"t": 0.0, "mine": False}


def mjpeg():
    while True:
        try:
            s = socket.create_connection(("127.0.0.1", ph["mjpeg"]), timeout=5)
            s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            buf = b""
            while True:
                d = s.recv(262144)
                if not d:
                    break
                buf += d
                while True:
                    i = buf.find(b"Content-Length:")
                    j = buf.find(b"\r\n", i) if i >= 0 else -1
                    if i < 0 or j < 0:
                        break
                    ln = int(buf[i + 15:j]); k = buf.find(b"\r\n\r\n", j)
                    if k < 0 or len(buf) < k + 4 + ln:
                        break
                    frame.update(jpg=buf[k + 4:k + 4 + ln], t=time.time(), n=frame["n"] + 1); buf = buf[k + 4 + ln:]
        except OSError:
            time.sleep(1)


def screencap():
    while True:
        try:
            p = pp.subprocess.run([pp.ADB, "-s", ph["serial"], "exec-out", "screencap", "-p"], capture_output=True, timeout=10, creationflags=0x08000000).stdout
            if p.startswith(b"\x89PNG"):
                frame.update(jpg=p, t=time.time(), n=frame["n"] + 1)
        except Exception:
            time.sleep(1)


def worker():
    while True:
        t0, a = jobs.get()
        try:
            if dim["pt"] is None:
                dim["pt"] = pp.size(ph)
            if a == "scroll":  # wheel notches gathered meanwhile become ONE stroke (<= 8 notches)
                time.sleep(.1)
                n = max(-8, min(8, sc["n"])); sc.update(n=0, q=False)
                if abs(n) < .3:
                    continue
                a = {"type": "scroll", "x": sc["x"], "y": sc["y"], "dy": n * 110}
            elif not a or (a["type"] in ("swipe", "scroll") and time.time() - t0 > 2):
                continue  # drop stale strokes instead of replaying a backlog
            st, who = pp.lock_status(ph, ME)
            if st == "other":
                continue
            pp.lock_write(ph, ME); last.update(t=time.time(), mine=True)
            print(a) if os.environ.get("PV_DRY") else pp.act(ph, a)
        except Exception as e:
            print(time.strftime("%H:%M:%S"), repr(e))


threading.Thread(target=mjpeg if ph["kind"] == "ios" else screencap, daemon=True).start()
threading.Thread(target=worker, daemon=True).start()
put(None)

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp, not blurry-scaled
except Exception:
    pass
root = tk.Tk(); root.title("Phone Pilot - " + ph["name"]); root.configure(bg="#111"); root.resizable(False, False)
H = min(820, root.winfo_screenheight() - 140)
img = tk.Label(root, bg="#111", fg="#aaa", text="Waiting for the phone...", font=("Segoe UI", 12), width=30, height=20); img.pack()
bar = tk.Frame(root, bg="#222"); bar.pack(fill="x")
btn = lambda txt, a: tk.Button(bar, text=txt, command=lambda: put(a), bg="#333", fg="#fff", activebackground="#555", activeforeground="#fff", relief="flat", bd=0, padx=8, font=("Segoe UI", 9, "bold"), takefocus=0).pack(side="left", padx=(4, 0), pady=3)
btn("Home", {"type": "home"})
btn("Apps", {"type": "apps"})
if ph["kind"] == "android":
    btn("Back", {"type": "key", "key": "BACK"})
badge = tk.Label(bar, bg="#1d6fe0", fg="white", font=("Segoe UI", 10, "bold"), width=14); badge.pack(side="left", padx=4, pady=3)
info = tk.Label(bar, bg="#222", fg="#999", font=("Segoe UI", 9)); info.pack(side="left")
top = tk.BooleanVar(value="--top" in sys.argv)
tk.Checkbutton(bar, text="Pin", variable=top, command=lambda: root.attributes("-topmost", top.get()), bg="#222", fg="#ccc",
               selectcolor="#222", activebackground="#222", activeforeground="#fff").pack(side="right")
root.attributes("-topmost", True); root.after(1500, lambda: root.attributes("-topmost", top.get()))  # surface once, then respect Pin
shown = {"n": -1, "img": None, "w": 0}; stamps = []; press = {}


def pt(e):
    p = dim["pt"]
    if not p or not shown["w"]:
        return None
    return round(max(0, min(e.x, shown["w"] - 1)) * p[0] / shown["w"], 1), round(max(0, min(e.y, H - 1)) * p[1] / H, 1)


def down(e):
    press.update(x=e.x, y=e.y, t=time.time(), m=time.time())


def up(e):
    a, b = pt(type("E", (), press)()), pt(e)
    if not a or not b or "t" not in press:
        return
    d = time.time() - press["t"]
    if press["y"] > H - 12 and dim["pt"]:
        a = (a[0], dim["pt"][1] - 2)  # a drag that starts on the bottom edge starts exactly on the home bar
    # a quick drag flicks; a slow or paused drag (hold stops the fling) scrolls precisely
    put({"type": "tap", "x": b[0], "y": b[1]} if abs(e.x - press["x"]) + abs(e.y - press["y"]) < 10 else
        {"type": "swipe", "x1": a[0], "y1": a[1], "x2": b[0], "y2": b[1], "duration_ms": int(d * 1000), "hold_ms": (500 if press["y"] > H - 12 else 200) if time.time() - press["m"] > .12 else 0})


def wheel(e):
    p = pt(e)
    if p:  # every notch just adds to one pending stroke; the worker sends it as soon as it is free
        sc.update(n=sc["n"] + e.delta / 120, x=p[0], y=p[1])
        if not sc["q"]:
            sc["q"] = True; put("scroll")


def key(e):
    if e.keysym == "Escape":
        pp.lock_release(ph, ME); last["mine"] = False
    elif e.keysym == "Home":
        put({"type": "home"})
    elif e.keysym == "Tab":
        put({"type": "apps"})
    elif e.keysym in ("BackSpace", "Return") or (e.char and e.char.isprintable()):
        put({"type": "type", "text": "\b" if e.keysym == "BackSpace" else "\n" if e.keysym == "Return" else e.char})


img.bind("<ButtonPress-1>", down); img.bind("<ButtonRelease-1>", up); img.bind("<MouseWheel>", wheel); root.bind("<Key>", key)
img.bind("<B1-Motion>", lambda e: press.update(m=time.time()))
img.bind("<Button-2>", lambda e: put({"type": "home"}))  # middle-click = Home; right-click = app switcher (Android: Back)
img.bind("<Button-3>", lambda e: put({"type": "key", "key": "BACK"} if ph["kind"] == "android" else {"type": "apps"}))


def tick():
    now = time.time()
    if frame["n"] != shown["n"] and frame["jpg"]:
        try:
            im = Image.open(io.BytesIO(frame["jpg"])).convert("RGB"); w = int(im.width * H / im.height)
            shown.update(img=ImageTk.PhotoImage(im.resize((w, H))), w=w, n=frame["n"]); img.configure(image=shown["img"], text="", width=w, height=H)
            stamps.append(now)
        except Exception:
            pass
    stamps[:] = [t for t in stamps if now - t < 2]
    if last["mine"] and now - last["t"] > IDLE:
        pp.lock_release(ph, ME); last["mine"] = False
    st, who = pp.lock_status(ph, ME)
    badge.configure(text={"other": "Bot driving", "mine": "Your turn"}.get(st, "Idle: click to drive"), bg={"other": "#1d6fe0", "mine": "#1a9c4a"}.get(st, "#555"))
    info.configure(text=("%d fps" % (len(stamps) / 2)) if now - frame["t"] < 3 else "no video: phone unlocked and connected?")
    root.after(40, tick)


def bye():
    pp.lock_release(ph, ME); root.destroy()


root.protocol("WM_DELETE_WINDOW", bye); tick(); root.mainloop()
