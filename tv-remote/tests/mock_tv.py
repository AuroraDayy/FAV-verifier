"""Fake Sony BRAVIA REST/IRCC endpoint, for testing server.py without a real TV."""
import base64, json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PSK, PIN, COOKIE = "1234", "5678", "auth=goodcookie"
state = {"power": "active", "keys": [], "text": None, "pin_shown": False}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _authed(self):
        return self.headers.get("X-Auth-PSK") == PSK or COOKIE in (self.headers.get("Cookie") or "")
    def _reply(self, code, obj=None, headers=None, raw=None):
        body = raw if raw is not None else json.dumps(obj or {}).encode()
        self.send_response(code)
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if self.path == "/_state": return self._reply(200, state)
        self._reply(404)
    def do_POST(self):
        data = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/sony/IRCC":
            if not self._authed(): return self._reply(403, raw=b"")
            code = data.decode().split("<IRCCCode>")[1].split("<")[0]
            state["keys"].append(code)
            if code == "AAAAAQAAAAEAAAAvAw==": state["power"] = "standby"
            return self._reply(200, raw=b"<ok/>")
        req = json.loads(data); m = req["method"]
        if self.path == "/sony/accessControl" and m == "actRegister":
            auth = self.headers.get("Authorization")
            if not auth:
                state["pin_shown"] = True; return self._reply(401, raw=b"")
            if base64.b64decode(auth.split()[1]).decode() == f":{PIN}":
                return self._reply(200, {"result": [], "id": 1},
                                   {"Set-Cookie": f"{COOKIE}; Path=/sony/; max-age=1209600"})
            return self._reply(401, raw=b"")
        public = {"getInterfaceInformation", "getPowerStatus"}
        if m not in public and not self._authed():
            return self._reply(200, {"error": [403, "Forbidden"], "id": 1})
        r = {
            "getInterfaceInformation": [{"productCategory": "tv", "modelName": "KD-49X8050H",
                                         "productName": "BRAVIA", "serverName": "", "interfaceVersion": "5.0.1"}],
            "getPowerStatus": [{"status": state["power"]}],
            "getSystemInformation": [{"model": "KD-49X8050H", "name": "BRAVIA", "macAddr": "aa:bb:cc:dd:ee:ff"}],
            "getRemoteControllerInfo": [{"bundled": True, "type": "RM-J1100"},
                                        [{"name": "Home", "value": "HOMECODE=="}]],
        }.get(m)
        if m == "setPowerStatus":
            state["power"] = "active" if req["params"][0]["status"] else "standby"; r = []
        if m == "setTextForm": state["text"] = req["params"][0]; r = []
        if r is None: return self._reply(200, {"error": [12, "No Such Method"], "id": 1})
        self._reply(200, {"result": r, "id": 1})

ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
