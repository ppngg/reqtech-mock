#!/usr/bin/env python3
"""
mock_sap_snowflake.py — local stand-in for SAP ECC (inbound PR IDoc + async confirmation)
and Snowflake (status write-back), so the ReqTech iFlows can be built and tested with the
sample JSONs BEFORE any real ECC/Snowflake connection exists.

Standard library only — no pip install. Python 3.9+.

    python mock_sap_snowflake.py            # http://localhost:8099

Env (all optional):
    PORT=8099
    CONFIRM_CALLBACK=https://<tenant>/http/reqtech/v1/pr-confirmation   # where to POST the confirmation
    CONFIRM_DELAY=2                                                     # seconds before async confirmation

See ../README.md for the endpoint table and outcome rules.
"""
import json, os, re, threading, urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8099"))   # Render/Railway/Fly/Cloud Run set PORT
CONFIRM_CALLBACK = os.environ.get("CONFIRM_CALLBACK", "").strip()
CONFIRM_DELAY = float(os.environ.get("CONFIRM_DELAY", "2"))
MOCK_TOKEN = os.environ.get("MOCK_TOKEN", "").strip()   # if set, POSTs must send X-Mock-Token

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "out")
CONF_DIR = os.path.join(OUT, "confirmations")
os.makedirs(CONF_DIR, exist_ok=True)

_lock = threading.Lock()
_seen = {}          # correlationKey -> prNumber (idempotency)
_counter = {"n": 0} # PR number sequence


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def corr_key(body):
    k = body.get("correlationKey")
    if k:
        return str(k)
    rid = body.get("requestId", "REQ-UNKNOWN")
    rev = body.get("confirmedRevision", "0")
    return f"{rid}#{rev}"


def next_pr():
    _counter["n"] += 1
    return "00100000" + str(_counter["n"]).zfill(2)  # e.g. 0010000001


def decide(body, key):
    """Return (sapStatus, prNumber, messages) — mirrors confirmation_idoc_samples.json."""
    path = str(body.get("path", "")).upper()
    items = body.get("items") or []

    # forced / keyed failures so the unhappy paths are testable
    if body.get("_forceError") is True or "FAIL" in key.upper():
        return "SAP_PUSH_FAILED", None, [
            {"type": "E", "id": "ME", "number": "083",
             "message": "Enter GL account / order for the account assignment category"}]
    if path == "WORK_ORDER" and "LOCK" in key.upper():
        return "WO_ERROR", None, [
            {"type": "E", "id": "IWO_BAPI", "number": "206",
             "message": "Order is being processed by another user"}]
    # cost-center allocation without a cost center -> business error
    for it in items:
        acc = (it or {}).get("account") or {}
        if str(acc.get("knttp", "")).upper() == "K" and not acc.get("costCenter"):
            return "SAP_PUSH_FAILED", None, [
                {"type": "E", "id": "ME", "number": "083",
                 "message": "Cost center required for account assignment category K"}]

    # idempotent success
    with _lock:
        pr = _seen.get(key)
        if pr is None:
            pr = next_pr()
            _seen[key] = pr
    return "PR_CREATED", pr, [
        {"type": "S", "id": "06", "number": "017",
         "message": f"Purchase requisition {pr} created"}]


def build_confirmation(body, key):
    status, pr, msgs = decide(body, key)
    return {
        "correlationKey": key,
        "requestId": body.get("requestId"),
        "confirmedRevision": body.get("confirmedRevision"),
        "idocNumber": "00000000" + str(abs(hash(key)) % 10_000_000).zfill(8),
        "sapStatus": status,
        "prNumber": pr,
        "items": [{"itemNo": (it or {}).get("itemNo"), "prItem": (it or {}).get("itemNo")}
                  for it in (body.get("items") or [])] if status == "PR_CREATED" else [],
        "sapMessages": msgs,
        "writeBackStatus": {"PR_CREATED": "PR Created",
                            "SAP_PUSH_FAILED": "SAP Push Failed",
                            "WO_ERROR": "WO Error"}[status],
        "_mockEmittedAt": now(),
    }


def deliver_confirmation(conf):
    """Write the confirmation to disk and, if configured, POST it to the callback."""
    key = conf["correlationKey"]
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
    with open(os.path.join(CONF_DIR, f"{safe}.json"), "w", encoding="utf-8") as f:
        json.dump(conf, f, indent=2)
    print(f"[mock][confirm] {key} -> {conf['sapStatus']} pr={conf['prNumber']}")
    if CONFIRM_CALLBACK:
        try:
            req = urllib.request.Request(
                CONFIRM_CALLBACK, data=json.dumps(conf).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                print(f"[mock][confirm] POSTed to callback ({r.status})")
        except Exception as e:  # noqa: BLE001 - mock, surface and continue
            print(f"[mock][confirm] callback failed: {e}")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {"_raw": raw.decode("utf-8", "replace")}

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._send(200, {"status": "ok", "time": now()})
        if self.path.startswith("/state"):
            with _lock:
                return self._send(200, {"seen": dict(_seen), "count": _counter["n"]})
        return self._send(404, {"error": "not found", "path": self.path})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if MOCK_TOKEN and self.headers.get("X-Mock-Token", "") != MOCK_TOKEN:
            return self._send(401, {"error": "missing or bad X-Mock-Token"})
        body = self._body()

        if path == "/ecc/pr-idoc":
            key = corr_key(body)
            print(f"[mock][ecc] received PR IDoc  key={key}  path={body.get('path')}")
            conf = build_confirmation(body, key)
            if "mode=sync" in self.path:                 # sync proxy/SOAP alternative
                return self._send(200, conf)
            # async: ack now, confirm later
            self._send(202, {"status": "SUBMITTED_TO_SAP", "correlationKey": key,
                             "idocNumber": conf["idocNumber"]})
            threading.Timer(CONFIRM_DELAY, deliver_confirmation, args=(conf,)).start()
            return

        if path == "/snowflake/result":
            line = f"{now()} {json.dumps(body)}\n"
            with open(os.path.join(OUT, "writebacks.log"), "a", encoding="utf-8") as f:
                f.write(line)
            print(f"[mock][snowflake] write-back  key={body.get('correlationKey')} "
                  f"status={body.get('status')} pr={body.get('prNumber')}")
            return self._send(200, {"status": "recorded"})

        return self._send(404, {"error": "not found", "path": path})

    def log_message(self, *a):  # quiet default access log; we print our own
        pass


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[mock] listening on http://localhost:{PORT}")
    print(f"[mock] confirmation callback: {CONFIRM_CALLBACK or '(none — writing out/confirmations/*.json)'}")
    print(f"[mock] confirmation delay: {CONFIRM_DELAY}s")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[mock] stopped")


if __name__ == "__main__":
    main()
