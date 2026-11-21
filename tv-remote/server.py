#!/usr/bin/env python3
"""
Local web remote for Sony BRAVIA TVs (Android TV / Google TV models, ~2013+).

Run this on any computer on the same Wi-Fi as the TV, then open the printed
URL on your phone. No dependencies beyond the Python 3 standard library.

It talks to the TV's built-in network control API:
  - REST (JSON-RPC)  http://<tv>/sony/<service>     power, system info
  - IRCC (SOAP)      http://<tv>/sony/IRCC          remote-control key presses
Auth is either PIN pairing (TV shows a 4-digit code, default on most TVs)
or a Pre-Shared Key you set in the TV's settings.
"""

import base64
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
CONFIG_PATH = os.environ.get("TV_REMOTE_CONFIG", os.path.join(HERE, "config.json"))
PORT = int(os.environ.get("TV_REMOTE_PORT", "8765"))
TV_PORT = int(os.environ.get("TV_PORT", "80"))  # only changed for testing
NICKNAME = "Web Remote"

# Fallback IRCC codes (standard Sony codes). The TV's own list, fetched via
# getRemoteControllerInfo, takes precedence when available.
FALLBACK_IRCC = {
    "Power": "AAAAAQAAAAEAAAAVAw==",
    "PowerOff": "AAAAAQAAAAEAAAAvAw==",
    "VolumeUp": "AAAAAQAAAAEAAAASAw==",
    "VolumeDown": "AAAAAQAAAAEAAAATAw==",
    "Mute": "AAAAAQAAAAEAAAAUAw==",
    "ChannelUp": "AAAAAQAAAAEAAAAQAw==",
    "ChannelDown": "AAAAAQAAAAEAAAARAw==",
    "Up": "AAAAAQAAAAEAAAB0Aw==",
    "Down": "AAAAAQAAAAEAAAB1Aw==",
    "Left": "AAAAAQAAAAEAAAA0Aw==",
    "Right": "AAAAAQAAAAEAAAAzAw==",
    "Confirm": "AAAAAQAAAAEAAABlAw==",
    "Home": "AAAAAQAAAAEAAABgAw==",
    "Return": "AAAAAgAAAJcAAAAjAw==",
    "Input": "AAAAAQAAAAEAAAAlAw==",
    "Play": "AAAAAgAAAJcAAAAaAw==",
    "Pause": "AAAAAgAAAJcAAAAZAw==",
    "Stop": "AAAAAgAAAJcAAAAYAw==",
    "Rewind": "AAAAAgAAAJcAAAAbAw==",
    "Forward": "AAAAAgAAAJcAAAAcAw==",
    "Num0": "AAAAAQAAAAEAAAAJAw==",
    "Num1": "AAAAAQAAAAEAAAAAAw==",
    "Num2": "AAAAAQAAAAEAAAABAw==",
    "Num3": "AAAAAQAAAAEAAAACAw==",
    "Num4": "AAAAAQAAAAEAAAADAw==",
    "Num5": "AAAAAQAAAAEAAAAEAw==",
    "Num6": "AAAAAQAAAAEAAAAFAw==",
    "Num7": "AAAAAQAAAAEAAAAGAw==",
    "Num8": "AAAAAQAAAAEAAAAHAw==",
    "Num9": "AAAAAQAAAAEAAAAIAw==",
}

IRCC_ENVELOPE = (
    '<?xml version="1.0"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    "<s:Body><u:X_SendIRCC xmlns:u=\"urn:schemas-sony-com:service:IRCC:1\">"
    "<IRCCCode>{code}</IRCCCode></u:X_SendIRCC></s:Body></s:Envelope>"
)


class TVError(Exception):
    def __init__(self, message, status=502, auth=False):
        super().__init__(message)
        self.status = status
        self.auth = auth


# --------------------------------------------------------------------------
# Config persistence
# --------------------------------------------------------------------------

_lock = threading.Lock()


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


CONFIG = load_config()
_ircc_cache = {}


# --------------------------------------------------------------------------
# Talking to the TV
# --------------------------------------------------------------------------

def _base(host):
    return f"http://{host}" if TV_PORT == 80 else f"http://{host}:{TV_PORT}"


def _auth_headers(cfg):
    h = {}
    if cfg.get("psk"):
        h["X-Auth-PSK"] = cfg["psk"]
    if cfg.get("cookie"):
        h["Cookie"] = cfg["cookie"]
    return h


def _http(url, body, headers, timeout=4):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        raise TVError(f"Can't reach the TV at {url.split('/')[2]} ({getattr(e, 'reason', e)}). "
                      "Is it on the same Wi-Fi and powered (standby is fine)?")


def rest_raw(host, service, method, params=None, version="1.0", headers=None, timeout=4):
    body = json.dumps({"method": method, "params": params or [], "id": 1, "version": version}).encode()
    h = {"Content-Type": "application/json; charset=UTF-8"}
    h.update(headers or {})
    return _http(f"{_base(host)}/sony/{service}", body, h, timeout)


def _refresh_cookie(cfg):
    """Re-run actRegister with the stored PIN to get a fresh auth cookie."""
    if not (cfg.get("pin") and cfg.get("clientid")):
        return False
    try:
        cookie = _act_register(cfg["host"], cfg["clientid"], cfg["pin"])
    except TVError:
        return False
    if cookie:
        cfg["cookie"] = cookie
        save_config(cfg)
        return True
    return False


def rest(service, method, params=None, version="1.0", _retry=True):
    cfg = CONFIG
    if not cfg.get("host"):
        raise TVError("No TV selected yet.", 400)
    status, _, data = rest_raw(cfg["host"], service, method, params, version, _auth_headers(cfg))
    if status in (401, 403):
        if _retry and _refresh_cookie(cfg):
            return rest(service, method, params, version, _retry=False)
        raise TVError("The TV rejected the request (not authorized). Pair again.", 401, auth=True)
    try:
        payload = json.loads(data or b"{}")
    except json.JSONDecodeError:
        raise TVError(f"Unexpected reply from TV (HTTP {status}).")
    if "error" in payload:
        code, msg = (payload["error"] + [None, None])[:2]
        if code in (401, 403, 40005) and _retry and _refresh_cookie(cfg):
            return rest(service, method, params, version, _retry=False)
        raise TVError(f"TV error {code}: {msg}", 401 if code in (401, 403) else 502,
                      auth=code in (401, 403))
    return payload.get("result", [])


def ircc_codes():
    host = CONFIG.get("host")
    if host in _ircc_cache:
        return _ircc_cache[host]
    codes = dict(FALLBACK_IRCC)
    try:
        result = rest("system", "getRemoteControllerInfo")
        if len(result) > 1:
            for item in result[1]:
                codes[item["name"]] = item["value"]
            _ircc_cache[host] = codes
    except TVError:
        pass
    return codes


def send_ircc(name, _retry=True):
    cfg = CONFIG
    if not cfg.get("host"):
        raise TVError("No TV selected yet.", 400)
    code = ircc_codes().get(name)
    if not code:
        raise TVError(f"Unknown key '{name}'.", 400)
    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPACTION": '"urn:schemas-sony-com:service:IRCC:1#X_SendIRCC"',
    }
    headers.update(_auth_headers(cfg))
    status, _, data = _http(f"{_base(cfg['host'])}/sony/IRCC",
                            IRCC_ENVELOPE.format(code=code).encode(), headers)
    if status in (401, 403):
        if _retry and _refresh_cookie(cfg):
            return send_ircc(name, _retry=False)
        raise TVError("The TV rejected the key press (not authorized). Pair again.", 401, auth=True)
    if status >= 400:
        raise TVError(f"TV returned HTTP {status} for key '{name}'.")


def _act_register(host, clientid, pin=None):
    """PIN pairing. Without a PIN: TV shows a code and returns 401.
    With the PIN: TV returns 200 and an auth cookie."""
    params = [{"clientid": clientid, "nickname": NICKNAME, "level": "private"},
              [{"value": "yes", "function": "WOL"}]]
    headers = {}
    if pin is not None:
        headers["Authorization"] = "Basic " + base64.b64encode(f":{pin}".encode()).decode()
    status, resp_headers, data = rest_raw(host, "accessControl", "actRegister", params,
                                          "1.0", headers, timeout=8)
    if status == 401:
        if pin is not None:
            raise TVError("Wrong PIN (or it expired). Start pairing again.", 401, auth=True)
        return None  # PIN is now showing on the TV
    if status != 200:
        raise TVError(f"Pairing failed (HTTP {status}). On the TV, check that "
                      "Settings > Network > Remote device settings > Control remotely is ON.")
    try:
        payload = json.loads(data or b"{}")
        if "error" in payload:
            raise TVError(f"Pairing refused by TV: {payload['error']}", 401, auth=True)
    except json.JSONDecodeError:
        pass
    for header in resp_headers.get_all("Set-Cookie") or []:
        m = re.search(r"(auth=[^;]+)", header)
        if m:
            return m.group(1)
    return ""  # 200 without cookie: TV is in "no auth" mode


def wake_on_lan(mac):
    mac_bytes = bytes.fromhex(re.sub(r"[^0-9a-fA-F]", "", mac))
    if len(mac_bytes) != 6:
        return
    packet = b"\xff" * 6 + mac_bytes * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for port in (9, 7):
            try:
                s.sendto(packet, ("255.255.255.255", port))
            except OSError:
                pass


def power_status():
    result = rest("system", "getPowerStatus")
    return result[0].get("status", "unknown") if result else "unknown"


def power(action):
    if action == "toggle":
        try:
            action = "off" if power_status() == "active" else "on"
        except TVError:
            action = "on"  # unreachable usually means fully off
    if action == "off":
        try:
            rest("system", "setPowerStatus", [{"status": False}])
        except TVError as e:
            if e.auth:
                raise
            send_ircc("PowerOff")  # some models only honour the key press
        return "standby"
    if action == "on":
        if CONFIG.get("mac"):
            wake_on_lan(CONFIG["mac"])
        last = None
        for _ in range(6):
            try:
                rest("system", "setPowerStatus", [{"status": True}])
                return "active"
            except TVError as e:
                last = e
                if e.auth:
                    raise
                time.sleep(1)
        raise TVError(f"{last} Turning ON over the network needs "
                      "Settings > Network > Remote start = ON on the TV.")
    raise TVError(f"Unknown power action '{action}'.", 400)


def fetch_device_info():
    """Best-effort: model name and MAC (for Wake-on-LAN)."""
    try:
        info = rest("system", "getSystemInformation")
        if info:
            i = info[0]
            CONFIG["model"] = i.get("model") or CONFIG.get("model")
            CONFIG["name"] = i.get("name") or CONFIG.get("name")
            if i.get("macAddr"):
                CONFIG["mac"] = i["macAddr"]
            save_config(CONFIG)
    except TVError:
        pass


# --------------------------------------------------------------------------
# Discovery: SSDP first, then a quick /24 scan as a fallback
# --------------------------------------------------------------------------

def local_ip():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))  # no packet is actually sent
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def _probe(host):
    """Return TV info if `host` answers the Sony REST API, else None."""
    try:
        status, _, data = rest_raw(host, "system", "getInterfaceInformation", timeout=1.2)
        if status != 200:
            return None
        res = json.loads(data).get("result", [{}])[0]
        if not isinstance(res, dict):
            return None
        return {"host": host, "model": res.get("modelName", ""),
                "name": res.get("productName", "Sony TV")}
    except Exception:
        return None


def ssdp_discover(timeout=2.5):
    hosts = set()
    targets = ["urn:schemas-sony-com:service:ScalarWebAPI:1",
               "urn:schemas-sony-com:service:IRCC:1"]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as s:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.settimeout(0.3)
        for st in targets:
            msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
                   f'MAN: "ssdp:discover"\r\nMX: 2\r\nST: {st}\r\n\r\n').encode()
            try:
                s.sendto(msg, ("239.255.255.250", 1900))
            except OSError:
                return hosts
        end = time.time() + timeout
        while time.time() < end:
            try:
                _, addr = s.recvfrom(4096)
                hosts.add(addr[0])
            except socket.timeout:
                continue
            except OSError:
                break
    return hosts


def discover():
    found = {}
    for h in ssdp_discover():
        info = _probe(h)
        if info:
            found[h] = info
    if not found:
        prefix = local_ip().rsplit(".", 1)[0]
        with ThreadPoolExecutor(max_workers=64) as pool:
            for info in pool.map(_probe, [f"{prefix}.{i}" for i in range(1, 255)]):
                if info:
                    found[info["host"]] = info
    return list(found.values())


# --------------------------------------------------------------------------
# HTTP server for the phone UI
# --------------------------------------------------------------------------

def public_state():
    return {
        "host": CONFIG.get("host"),
        "name": CONFIG.get("name"),
        "model": CONFIG.get("model"),
        "auth": "psk" if CONFIG.get("psk") else ("pin" if "cookie" in CONFIG else None),
        "canWake": bool(CONFIG.get("mac")),
    }


def handle_api(path, body):
    global CONFIG
    if path == "/api/state":
        return public_state()

    if path == "/api/discover":
        return {"tvs": discover()}

    if path == "/api/select":
        host = (body.get("host") or "").strip()
        if not host:
            raise TVError("Enter the TV's IP address.", 400)
        info = _probe(host)
        if not info:
            raise TVError(f"No Sony TV answered at {host}.", 404)
        with _lock:
            CONFIG = {"host": host, "model": info["model"], "name": info["name"],
                      "clientid": CONFIG.get("clientid") or f"WebRemote:{uuid.uuid4()}"}
            save_config(CONFIG)
            _ircc_cache.pop(host, None)
        return public_state()

    if path == "/api/pair/start":
        cookie = _act_register(CONFIG["host"], CONFIG["clientid"])
        if cookie is None:
            return {"pinShown": True}
        CONFIG.update(cookie=cookie, pin="")
        save_config(CONFIG)
        fetch_device_info()
        return {"pinShown": False, "state": public_state()}

    if path == "/api/pair/finish":
        pin = re.sub(r"\D", "", str(body.get("pin", "")))
        if not pin:
            raise TVError("Enter the code shown on the TV.", 400)
        cookie = _act_register(CONFIG["host"], CONFIG["clientid"], pin)
        CONFIG.update(cookie=cookie or "", pin=pin)
        CONFIG.pop("psk", None)
        save_config(CONFIG)
        fetch_device_info()
        return public_state()

    if path == "/api/psk":
        psk = str(body.get("psk", "")).strip()
        if not psk:
            raise TVError("Enter the Pre-Shared Key.", 400)
        CONFIG["psk"] = psk
        for k in ("cookie", "pin"):
            CONFIG.pop(k, None)
        save_config(CONFIG)
        try:
            rest("system", "getSystemInformation", _retry=False)
        except TVError as e:
            CONFIG.pop("psk", None)
            save_config(CONFIG)
            raise TVError(f"Key not accepted: {e}", 401, auth=True)
        fetch_device_info()
        return public_state()

    if path == "/api/status":
        return {"power": power_status()}

    if path == "/api/power":
        return {"power": power(body.get("action", "toggle"))}

    if path == "/api/key":
        send_ircc(str(body.get("name", "")))
        return {"ok": True}

    if path == "/api/text":
        rest("appControl", "setTextForm", [str(body.get("text", ""))])
        return {"ok": True}

    if path == "/api/forget":
        CONFIG = {}
        save_config(CONFIG)
        return public_state()

    raise TVError("Not found", 404)


class Handler(BaseHTTPRequestHandler):
    server_version = "TVRemote/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("TV_REMOTE_DEBUG"):
            super().log_message(fmt, *args)

    def _send(self, status, content, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/api/"):
            return self._api(path, {})
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.realpath(os.path.join(STATIC, name))
        if not full.startswith(os.path.realpath(STATIC) + os.sep) or not os.path.isfile(full):
            return self._send(404, b"Not found", "text/plain")
        ctype = {".html": "text/html; charset=utf-8", ".json": "application/manifest+json",
                 ".svg": "image/svg+xml", ".js": "text/javascript", ".png": "image/png",
                 ".css": "text/css"}.get(os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError:
            body = {}
        self._api(self.path.split("?")[0], body)

    def _api(self, path, body):
        try:
            self._json(200, handle_api(path, body))
        except TVError as e:
            self._json(e.status, {"error": str(e), "auth": e.auth})
        except Exception as e:  # keep the server alive no matter what
            self._json(500, {"error": f"Internal error: {e}"})


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    ip = local_ip()
    print("\n  Sony TV web remote is running.\n")
    print(f"  On your phone (same Wi-Fi), open:  http://{ip}:{PORT}")
    print(f"  On this computer:                  http://localhost:{PORT}\n")
    print("  Tip: in Safari use Share > Add to Home Screen to make it an app icon.")
    print("  Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
