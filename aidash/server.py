"""HTTP server: static dashboard + JSON API. LAN-only by default."""
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import re
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__, costs, oauth
from .config import SECRET_KEYS
from .providers import PLATFORMS
from .util import mask_secret, month_key, now_ts, num, prev_months

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
MAX_BODY = 256 * 1024


def hash_password(pw, salt=None):
    salt = salt or uuid.uuid4().hex
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode("ascii"), 60000)
    return "pbkdf2$%s$%s" % (salt, dk.hex())


def check_password(pw, stored):
    try:
        _, salt, _h = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(pw or "", salt), stored)


def is_lan(ip):
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return addr.is_private or addr.is_loopback or addr.is_link_local


class App(object):
    """Glue between HTTP handlers and the poller/config."""

    def __init__(self, store, history, fx, oauth_mgr, poller, news=None):
        self.store = store
        self.history = history
        self.fx = fx
        self.oauth = oauth_mgr
        self.poller = poller
        self.news = news

    # ------------------------------------------------------------------ state
    def state(self, month=None):
        now = now_ts()
        cur_month = month_key()
        month = month if month and MONTH_RE.match(month) else cur_month
        snap = self.poller.snapshot()
        sparks = self.history.sparks()
        cfg = {
            "display_currency": self.store.get("display_currency"),
            "subscriptions": self.store.get("subscriptions"),
            "manual_entries": self.store.get("manual_entries"),
            "detected_plans": self.store.get("detected_plans"),
        }
        tab = costs.compute(month, cfg, snap, self.history.counters(month), self.fx)
        trend = costs.history(prev_months(6), cfg, self.history.counters, self.fx, snap)
        platforms, active = [], []
        for pid, name, auth in PLATFORMS:
            st = snap.get(pid) or {}
            data = st.get("data") or {}
            windows = []
            for w in data.get("windows") or []:
                w = dict(w)
                ws, ra, used = w.get("window_s"), w.get("resets_at"), w.get("used_pct")
                if ws and ra and used is not None and ra > now:
                    elapsed = ws - (ra - now)
                    w["started_at"] = ra - ws
                    if elapsed > ws * 0.08:
                        w["projected_pct"] = round(used * ws / elapsed, 1)
                    if ws <= 6 * 3600:
                        active.append({"platform": pid, "name": name, "label": w["label"], "used_pct": used,
                                       "started_at": ra - ws, "resets_at": ra, "window_s": ws})
                windows.append(w)
            platforms.append({
                "id": pid, "name": name, "auth": auth,
                "status": st.get("status"), "error": st.get("error"),
                "updated_at": st.get("updated_at"), "checked_at": st.get("checked_at"),
                "next_poll": st.get("next_poll"),
                "plan": data.get("plan"), "account": data.get("account"),
                "windows": windows, "balances": data.get("balances") or [],
                "notes": data.get("notes") or [], "metrics": data.get("metrics") or {},
                "sparks": sparks.get(pid) or {},
            })
        active.sort(key=lambda a: a["resets_at"])
        return {
            "version": __version__,
            "now": now,
            "currency": tab["currency"],
            "currencies": self.fx.currencies(),
            "fx": dict(self.fx.info(), error=self.poller.fx_error),
            "fx_rates": dict((c, self.fx.convert(1, "USD", c)) for c in ("USD", "GBP", "EUR", "CNY")),
            "month": month,
            "current_month": cur_month,
            "months": list(reversed(prev_months(12))),
            "costs": tab,
            "trend": trend,
            "platforms": platforms,
            "active_windows": active,
        }

    # ------------------------------------------------------------------ settings
    def settings(self):
        cfg = dict(self.store.config)
        cfg.pop("admin_password_sha256", None)
        cfg.pop("upstream_overrides", None)
        secret_val = cfg.pop("antigravity_client_secret", "")
        return {
            "config": cfg,
            "antigravity_client_secret_set": bool(secret_val),
            "secrets": dict((k, mask_secret(self.store.secret(k))) for k in SECRET_KEYS),
            "oauth": dict((p, self.oauth.status(p)) for p in ("claude", "codex", "antigravity")),
            "codex_device": self.oauth.codex_device_status(),
            "admin_required": bool(self.store.get("admin_password_sha256")),
            "platforms": [{"id": p[0], "name": p[1], "auth": p[2]} for p in PLATFORMS],
            "currencies": self.fx.currencies(),
        }

    def apply_settings(self, body):
        cur = month_key()
        changes = {}
        if "display_currency" in body:
            c = str(body["display_currency"]).upper()
            if c not in self.fx.currencies():
                raise ValueError("Unknown currency %s" % c)
            changes["display_currency"] = c
        if "subscriptions" in body:
            old = dict((s.get("id"), s) for s in self.store.get("subscriptions") or [])
            subs = []
            for s in body["subscriptions"] or []:
                if not isinstance(s, dict):
                    continue
                sid = str(s.get("id") or uuid.uuid4().hex[:8])
                prev = old.get(sid, {})
                active = bool(s.get("active", True))
                amount = num(s.get("amount"), 0.0)
                start = s.get("start_month") or prev.get("start_month") or cur
                end = s.get("end_month") if "end_month" in s else prev.get("end_month")
                if active:
                    end = None  # running subscription: counts every month from start_month on
                elif not end:
                    end = cur  # just cancelled: still counts this month, not future months
                subs.append({
                    "id": sid, "platform": str(s.get("platform") or "other")[:32],
                    "name": str(s.get("name") or "Subscription")[:80], "amount": max(0.0, amount),
                    # 0 = use the detected plan's list price (where known); any amount you type wins.
                    "auto_price": amount <= 0,
                    "currency": str(s.get("currency") or "USD").upper()[:3],
                    "billing_day": int(num(s.get("billing_day"), 1) or 1),
                    "active": active, "start_month": start if MONTH_RE.match(str(start)) else cur,
                    "end_month": end if end and MONTH_RE.match(str(end)) else None,
                })
            changes["subscriptions"] = subs
        if "manual_entries" in body:
            entries = []
            for e in body["manual_entries"] or []:
                if not isinstance(e, dict):
                    continue
                m = str(e.get("month") or cur)
                entries.append({
                    "id": str(e.get("id") or uuid.uuid4().hex[:8]),
                    "month": m if MONTH_RE.match(m) else cur,
                    "platform": str(e.get("platform") or "other")[:32],
                    "kind": e.get("kind") if e.get("kind") in costs.MANUAL_KINDS else "spend",
                    "amount": max(0.0, num(e.get("amount"), 0.0)),
                    "currency": str(e.get("currency") or "USD").upper()[:3],
                    "note": str(e.get("note") or "")[:120],
                })
            changes["manual_entries"] = entries
        if isinstance(body.get("intervals"), dict):
            iv = self.store.get("intervals") or {}
            for k, v in body["intervals"].items():
                if k in iv and num(v) is not None:
                    iv[k] = int(max(60, num(v)))
            changes["intervals"] = iv
        if isinstance(body.get("enabled"), dict):
            en = self.store.get("enabled") or {}
            for k, v in body["enabled"].items():
                if k in en:
                    en[k] = bool(v)
            changes["enabled"] = en
        for k in ("moonshot_base_url", "antigravity_client_id", "antigravity_client_secret"):
            if k in body and isinstance(body[k], str):
                v = body[k].strip()
                if k == "moonshot_base_url" and v and not re.match(r"^https://api\.moonshot\.(ai|cn)/v1/?$", v):
                    raise ValueError("Moonshot base URL must be https://api.moonshot.ai/v1 or https://api.moonshot.cn/v1")
                if k == "antigravity_client_secret" and not v:
                    continue  # blank = keep existing
                changes[k] = v
        if isinstance(body.get("news_watch"), dict):
            nw = body["news_watch"]
            clean = {}
            for k in ("families", "hn_queries"):
                if isinstance(nw.get(k), list):
                    clean[k] = [str(x).strip()[:40] for x in nw[k] if str(x).strip()][:20]
            if num(nw.get("hn_min_points")) is not None:
                clean["hn_min_points"] = int(max(1, num(nw["hn_min_points"])))
            changes["news_watch"] = clean
        if changes:
            self.store.update_config(changes)
        if "news_watch" in changes and self.news is not None:
            self.poller.schedule_soon("news")
            self.poller.schedule_soon("catalog")
        for k, v in (body.get("secrets") or {}).items():
            if k in SECRET_KEYS and isinstance(v, str):
                self.store.set_secret(k, v.strip())
                self.poller.refresh_now(k.replace("_api_key", ""))
        if "admin_password" in body:
            pw = body.get("admin_password") or ""
            self.store.set_config_value("admin_password_sha256", hash_password(pw) if pw else "")
        return self.settings()


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "aidash/" + __version__
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # keep journald quiet: only log errors
            pass

        # ------------------------------------------------------------- plumbing
        def _guard(self):
            if app.store.get("lan_only", True) and not is_lan(self.client_address[0]):
                self._send(403, {"error": "LAN access only"})
                return False
            return True

        def _send(self, code, payload, ctype="application/json", extra=None):
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text/", "application/json", "application/javascript")) else ""))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store" if ctype == "application/json" else "max-age=300")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _body(self):
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                raise ValueError("Content-Type must be application/json")
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("Request too large")
            raw = self.rfile.read(n) if n else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object")
            return data

        def _admin_ok(self):
            stored = app.store.get("admin_password_sha256")
            if not stored:
                return True
            return check_password(self.headers.get("X-Admin-Password") or "", stored)

        # ------------------------------------------------------------- GET
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._guard():
                return
            url = urlparse(self.path)
            path = url.path
            try:
                if path == "/api/state":
                    month = (parse_qs(url.query).get("month") or [None])[0]
                    return self._send(200, app.state(month))
                if path == "/api/settings":
                    return self._send(200, app.settings())
                if path == "/api/news":
                    if app.news is None:
                        return self._send(404, {"error": "news disabled"})
                    return self._send(200, app.news.state())
                if path == "/api/oauth/codex/device":
                    return self._send(200, app.oauth.codex_device_status())
                if path == "/healthz":
                    return self._send(200, {"ok": True})
                return self._static(path)
            except Exception as e:  # pragma: no cover - defensive
                return self._send(500, {"error": "internal error: %s" % type(e).__name__})

        def _static(self, path):
            if path in ("/", "/index.html"):
                path = "/index.html"
            rel = os.path.normpath(path.lstrip("/"))
            if rel.startswith("..") or os.path.isabs(rel):
                return self._send(404, {"error": "not found"})
            full = os.path.join(STATIC_DIR, rel)
            if not os.path.isfile(full):
                return self._send(404, {"error": "not found"})
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as f:
                data = f.read()
            return self._send(200, data, ctype)

        # ------------------------------------------------------------- POST
        def do_POST(self):
            if not self._guard():
                return
            path = urlparse(self.path).path
            try:
                body = self._body()
            except (ValueError, UnicodeDecodeError) as e:
                return self._send(400, {"error": str(e)})
            try:
                if path == "/api/currency":
                    return self._send(200, app.apply_settings({"display_currency": body.get("currency")}))
                m = re.match(r"^/api/refresh/([a-z_]+)$", path)
                if m:
                    if m.group(1) not in [p[0] for p in PLATFORMS]:
                        return self._send(404, {"error": "unknown platform"})
                    return self._send(200, {"next_poll": app.poller.refresh_now(m.group(1))})
                # everything below can change credentials/settings
                if not self._admin_ok():
                    return self._send(401, {"error": "admin password required"})
                if path == "/api/settings":
                    return self._send(200, app.apply_settings(body))
                if path == "/api/admin/check":
                    return self._send(200, {"ok": True})
                m = re.match(r"^/api/oauth/(claude|codex|antigravity)/(start|finish|logout)$", path)
                if m:
                    return self._oauth(m.group(1), m.group(2), body)
                if path == "/api/oauth/codex/device":
                    return self._send(200, app.oauth.start_codex_device())
                return self._send(404, {"error": "not found"})
            except oauth.AuthError as e:
                return self._send(400, {"error": str(e)})
            except ValueError as e:
                return self._send(400, {"error": str(e)})

        def _oauth(self, provider, action, body):
            o = app.oauth
            if action == "logout":
                o.logout(provider)
                app.poller.refresh_now(provider)
                return self._send(200, o.status(provider))
            if action == "start":
                fn = {"claude": o.start_claude, "codex": o.start_codex_pkce, "antigravity": o.start_antigravity}[provider]
                return self._send(200, fn())
            fn = {"claude": o.finish_claude, "codex": o.finish_codex_pkce, "antigravity": o.finish_antigravity}[provider]
            result = fn(body.get("code") or "")
            app.poller.refresh_now(provider)
            return self._send(200, result)

    return Handler


def serve(app, host, port):
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    return httpd
