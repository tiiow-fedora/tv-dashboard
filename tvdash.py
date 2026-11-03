"""TV dashboard service.

- Renders Grafana dashboards to JPEG with headless Chromium (Playwright), authenticated
  with a Viewer service-account token, so the old Tizen browser never runs Grafana JS.
- :8900 (LAN)        TV page: fullscreen black page that swaps in the latest frame.
- :8901 (localhost)  Control panel: pick dashboards, rotation, TV remote.

Run detached: start.cmd / the "TVDashboard" scheduled task (pythonw, no console).
"""
import io
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler

from PIL import Image

import tvctl

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
GRAFANA = "http://192.168.1.70:30037"
PC_IP = "192.168.1.181"
TV_PORT = 8900
PANEL_PORT = 8901
WIDTH, HEIGHT = 1920, 1080

DEFAULTS = {
    "selected": ["truenas-summary"],  # dashboards in rotation, in order
    "rotate_s": 60,                   # seconds per dashboard when >1 selected
    "refresh_s": 30,                  # re-render the visible dashboard this often
    "time_from": "now-3h",
    "theme": "dark",
    "fit": "whole",                   # "whole" = scale entire dashboard to screen, "top" = 1:1
}

os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
log = logging.getLogger("tvdash")
log.setLevel(logging.INFO)
_h = RotatingFileHandler(os.path.join(HERE, "logs", "tvdash.log"), maxBytes=1_000_000, backupCount=2)
_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_h)

with open(os.path.join(HERE, "grafana_token.txt")) as f:
    GRAFANA_TOKEN = f.read().strip()


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.cfg = dict(DEFAULTS)
        try:
            with open(CONFIG_PATH) as f:
                self.cfg.update(json.load(f))
        except (OSError, ValueError):
            pass
        self.frame = b""          # latest JPEG shown on the TV
        self.version = 0          # bumps on every new frame
        self.reload_gen = int(time.time())  # bump to make the TV page do a full reload
        self.diag = ""            # last viewport report from the TV page
        self.last_auto_fs = 0.0
        self.current_uid = None
        self.last_render = 0.0
        self.last_error = ""
        self.idx = 0
        self.wake = threading.Event()  # poke the renderer to act now
        self._dash_cache = (0.0, [])

    def save(self):
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.cfg, f, indent=2)
        os.replace(tmp, CONFIG_PATH)

    def dashboards(self):
        ts, cached = self._dash_cache
        if time.time() - ts < 60 and cached:
            return cached
        req = urllib.request.Request(f"{GRAFANA}/api/search?type=dash-db",
                                     headers={"Authorization": f"Bearer {GRAFANA_TOKEN}"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                items = json.load(r)
            out = [{"uid": d["uid"], "title": d["title"], "folder": d.get("folderTitle", "")}
                   for d in items]
            out.sort(key=lambda d: (d["folder"], d["title"].lower()))
            self._dash_cache = (time.time(), out)
            return out
        except Exception as e:
            log.warning("dashboard list failed: %s", e)
            return cached


S = State()


# ---------------------------------------------------------------- renderer

def dash_url(uid, cfg):
    return (f"{GRAFANA}/d/{uid}?kiosk&theme={cfg['theme']}&from={cfg['time_from']}&to=now"
            "&_dash.hideTimePicker&_dash.hideVariables&_dash.hideLinks")


def renderer():
    from playwright.sync_api import sync_playwright
    while True:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                ctx = browser.new_context(
                    viewport={"width": WIDTH, "height": HEIGHT},
                    extra_http_headers={"Authorization": f"Bearer {GRAFANA_TOKEN}"})
                page = ctx.new_page()
                renders = 0
                last_rotate = time.time()
                while renders < 500:  # recycle the browser periodically to cap memory
                    with S.lock:
                        cfg = dict(S.cfg)
                    sel = cfg["selected"] or DEFAULTS["selected"]
                    now = time.time()
                    if len(sel) > 1 and now - last_rotate >= cfg["rotate_s"]:
                        S.idx += 1
                        last_rotate = now
                    S.idx %= len(sel)
                    uid = sel[S.idx]
                    due = (uid != S.current_uid or now - S.last_render >= cfg["refresh_s"]
                           or S.wake.is_set())
                    if due:
                        S.wake.clear()
                        if uid != S.current_uid:
                            last_rotate = now
                        _render(page, uid, cfg)
                        renders += 1
                    S.wake.wait(1.0)
                browser.close()
        except Exception as e:
            S.last_error = f"renderer crashed: {e}"
            log.exception("renderer crashed; restarting in 10s")
            time.sleep(10)


HEIGHTS = {}  # uid -> (content height px at 1920 wide, renders since measured)


def _settle(page):
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass  # streaming panels can keep the network busy; render anyway
    page.wait_for_timeout(1200)  # let panel animations settle


def _render(page, uid, cfg):
    """Fit mode lays the dashboard out on a 16:9 virtual screen tall enough for all of it
    (Grafana row heights are fixed px, so a wider grid doesn't change content height),
    then scales the shot down to the TV's 1920x1080."""
    t0 = time.time()
    try:
        fit = cfg.get("fit", "whole") == "whole"
        h, n = HEIGHTS.get(uid, (None, 0))
        if not fit or h is None or n >= 20:  # (re)measure at native size now and then
            page.set_viewport_size({"width": WIDTH, "height": HEIGHT})
            page.goto(dash_url(uid, cfg), wait_until="load", timeout=30000)
            _settle(page)
            h, n = page.evaluate("document.documentElement.scrollHeight"), 0
        vh = max(HEIGHT, h) if fit else HEIGHT
        vw = round(vh * WIDTH / HEIGHT)
        if page.viewport_size != {"width": vw, "height": vh} or n > 0:
            page.set_viewport_size({"width": vw, "height": vh})
            page.goto(dash_url(uid, cfg), wait_until="load", timeout=30000)
            _settle(page)
        HEIGHTS[uid] = (h, n + 1)
        if vh == HEIGHT:
            jpg = page.screenshot(type="jpeg", quality=88)
        else:
            img = Image.open(io.BytesIO(page.screenshot(type="png")))
            img = img.convert("RGB").resize((WIDTH, HEIGHT), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=90)
            jpg = buf.getvalue()
        with S.lock:
            S.frame = jpg
            S.version += 1
            S.current_uid = uid
            S.last_render = time.time()
            S.last_error = ""
        log.info("rendered %s in %.1fs (%d KB)", uid, time.time() - t0, len(jpg) // 1024)
    except Exception as e:
        S.last_error = f"{uid}: {e}"
        S.last_render = time.time()  # back off until next refresh
        log.warning("render %s failed: %s", uid, e)


# ---------------------------------------------------------------- TV page

TV_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<meta http-equiv="refresh" content="3600">
<title>Dashboard</title>
<style>
html,body{margin:0;padding:0;width:100%;height:100%;overflow:hidden;background:#000;cursor:none}
#f{position:fixed;left:0;top:0;width:100%;height:100%;border:0;display:block}
</style></head>
<body><img id="f" src="/frame.jpg" alt="">
<script>
var cur = "", gen = null;
// The TV browser zooms pages (125%) and its layout viewport can be larger than what is
// visible, so size the image to the *visible* viewport in px rather than trusting 100%.
function fit() {
  // Letterbox to 16:9 (exactly full-screen once fullscreen; no stretching before then).
  var W = window.innerWidth, H = window.innerHeight, f = document.getElementById("f");
  var w = Math.min(W, H * 16 / 9), h = w * 9 / 16;
  f.style.width = w + "px"; f.style.height = h + "px";
  f.style.left = (W - w) / 2 + "px"; f.style.top = (H - h) / 2 + "px";
  report();
}
function report() {
  var d = document.documentElement, x = new XMLHttpRequest();
  x.open("GET", "/diag?iw=" + window.innerWidth + "&ih=" + window.innerHeight +
    "&cw=" + d.clientWidth + "&ch=" + d.clientHeight + "&sw=" + screen.width +
    "&sh=" + screen.height + "&dpr=" + (window.devicePixelRatio || 1) +
    "&fs=" + (document.fullscreenElement || document.webkitFullscreenElement ? 1 : 0) +
    "&ua=" + encodeURIComponent(navigator.userAgent), true);
  x.send();
}
// Fullscreen needs a user gesture: the panel sends KEY_ENTER to the TV, which clicks/keys here.
function goFull() {
  var e = document.documentElement;
  var r = e.requestFullscreen || e.webkitRequestFullscreen || e.webkitRequestFullScreen;
  if (r && !(document.fullscreenElement || document.webkitFullscreenElement)) r.call(e);
}
document.addEventListener("click", goFull, false);
document.addEventListener("keydown", goFull, false);
document.addEventListener("webkitfullscreenchange", function () { setTimeout(fit, 300); }, false);
document.addEventListener("fullscreenchange", function () { setTimeout(fit, 300); }, false);
window.addEventListener("resize", fit, false);
fit();
function poll() {
  var x = new XMLHttpRequest();
  x.open("GET", "/version?_=" + new Date().getTime(), true);
  x.onreadystatechange = function () {
    if (x.readyState != 4 || x.status != 200) return;
    var p = x.responseText.split("|");
    if (gen === null) gen = p[1];
    else if (p[1] != gen) { location.reload(); return; }
    if (p[0] != cur) {
      var v = p[0], im = new Image();
      im.onload = function () { document.getElementById("f").src = im.src; cur = v; };
      im.src = "/frame.jpg?v=" + v;
    }
  };
  x.send();
}
setInterval(poll, 3000);
poll();
setTimeout(function () { window.scrollTo(0, 1); }, 500);
</script></body></html>"""


class TVHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype, cache=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/tv"):
            self._send(200, TV_HTML.encode(), "text/html; charset=utf-8")
        elif path == "/frame.jpg":
            with S.lock:
                frame = S.frame
            if frame:
                self._send(200, frame, "image/jpeg")
            else:
                self._send(503, b"no frame yet", "text/plain")
        elif path == "/diag":
            q = self.path.partition("?")[2][:600]
            if q != S.diag:
                S.diag = q
                log.info("tv diag: %s", urllib.parse.unquote(q))
            # Our page is on screen but not fullscreen (fresh load/reload): fix it. Only
            # reachable while the TV is showing this page, so the OK press is safe.
            if "&fs=0&" in q and time.time() - S.last_auto_fs > 60:
                S.last_auto_fs = time.time()
                fullscreen_soon(3)
            self._send(204, b"", "text/plain")
        elif path == "/version":
            self._send(200, f"{S.version}|{S.reload_gen}".encode(), "text/plain")
        else:
            self._send(404, b"not found", "text/plain")


# ---------------------------------------------------------------- control panel

def tv_url():
    return f"http://{PC_IP}:{TV_PORT}/"


def fullscreen_soon(delay):
    """Once the TV page has loaded, press OK (the page turns any key/click into
    requestFullscreen, which needs a user gesture) then park the pointer in a corner."""
    def run():
        time.sleep(delay)
        try:
            tvctl.send_key("KEY_ENTER")
            time.sleep(2)
            tvctl.park_pointer()
        except Exception as e:
            log.warning("fullscreen sequence failed: %s", e)
    threading.Thread(target=run, daemon=True).start()


class PanelHandler(TVHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            with open(os.path.join(HERE, "panel.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif path == "/frame.jpg":
            super().do_GET()
        elif path == "/api/state":
            self._json(self._state())
        elif path == "/api/tv/status":
            self._json({"tv": tvctl.status()})
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        # JSON-only: a cross-site form post can't set this without a CORS preflight,
        # which this server never answers.
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._send(415, b"json only", "text/plain")
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, b"bad json", "text/plain")
        path = self.path.split("?")[0]
        try:
            if path == "/api/config":
                self._set_config(body)
            elif path == "/api/next":
                S.idx += 1
                S.wake.set()
            elif path == "/api/refresh":
                S.wake.set()
            elif path == "/api/tv/reload":
                S.reload_gen += 1
            elif path == "/api/tv/fullscreen":
                fullscreen_soon(0)
            elif path == "/api/tv/key":
                key = str(body.get("key", ""))
                if not key.startswith("KEY_") or len(key) > 32:
                    return self._send(400, b"bad key", "text/plain")
                tvctl.send_key(key)
            elif path == "/api/tv/open":
                tvctl.open_browser(tv_url())
                S.last_auto_fs = 0  # let the page's first diag trigger fullscreen
            elif path == "/api/tv/wake":
                tvctl.wake()
            else:
                return self._send(404, b"not found", "text/plain")
        except Exception as e:
            log.warning("%s failed: %s", path, e)
            return self._json({"ok": False, "error": str(e)}, 502)
        self._json({"ok": True, **self._state()})

    def _set_config(self, body):
        known = {d["uid"] for d in S.dashboards()}
        with S.lock:
            if "selected" in body:
                sel = [u for u in body["selected"] if isinstance(u, str) and u in known]
                if sel:
                    S.cfg["selected"] = sel
                    S.idx = 0
            for k, lo, hi in (("rotate_s", 10, 86400), ("refresh_s", 10, 3600)):
                if k in body:
                    S.cfg[k] = max(lo, min(hi, int(body[k])))
            if body.get("time_from") in ("now-15m", "now-1h", "now-3h", "now-6h",
                                         "now-12h", "now-24h", "now-7d"):
                S.cfg["time_from"] = body["time_from"]
            if body.get("theme") in ("dark", "light"):
                S.cfg["theme"] = body["theme"]
            if body.get("fit") in ("whole", "top"):
                S.cfg["fit"] = body["fit"]
            S.save()
        S.wake.set()

    def _state(self):
        dashboards = S.dashboards()
        with S.lock:
            return {"config": dict(S.cfg), "current": S.current_uid, "version": S.version,
                    "last_render": S.last_render, "error": S.last_error,
                    "dashboards": dashboards, "tv_url": tv_url(),
                    "tv_diag": urllib.parse.unquote(S.diag)}

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")


def serve(addr, handler):
    srv = ThreadingHTTPServer(addr, handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("listening on %s:%d", *addr)


if __name__ == "__main__":
    log.info("starting tvdash")
    serve(("0.0.0.0", TV_PORT), TVHandler)
    serve(("127.0.0.1", PANEL_PORT), PanelHandler)
    renderer()
