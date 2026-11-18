"""A fake of every upstream the dashboard talks to, for offline tests.

Responses mirror the real upstream shapes. Mutable `STATE` lets tests
simulate top-ups, spend and rate limiting.
"""
import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

NOW = time.time()

STATE = {
    "openrouter_credits": {"total_credits": 20.0, "total_usage": 5.0},
    "moonshot_balance": 10.0,
    "claude_429": False,
    "token_calls": [],
    "seen_headers": {},
}


def _m(mid, prompt, completion, elos, created_days_ago=100, exp=None, desc="A model."):
    return {"id": mid, "name": mid.split("/")[1], "created": int(NOW - created_days_ago * 86400),
            "pricing": {"prompt": str(prompt / 1e6), "completion": str(completion / 1e6)}, "context_length": 200000,
            "description": desc, "expiration_date": exp,
            "benchmarks": {"design_arena": [{"arena": "models", "category": c, "elo": e} for c, e in zip(("website", "gamedev"), elos)]} if elos else None}


CATALOG = [
    _m("anthropic/claude-fable-5.2", 10, 34, (1400, 1400), created_days_ago=8),
    _m("anthropic/claude-haiku-5.5", 0.1, 0.5, (1260, 1266)),
    _m("deepseek/deepseek-v4.1-flash", 0.08, 0.36, (1270, 1278)),
    _m("tencent/hy4-flash", 0.035, 0.14, (1220, 1230), created_days_ago=1),
    _m("openai/gpt-5", 1.25, 10, (1300, 1310), exp=time.strftime("%Y-%m-%d", time.gmtime(NOW + 20 * 86400))),
    _m("nvidia/nemotron:free", 0, 0, (1120, 1124)),
    _m("openrouter/auto", 0, 0, None),
    _m("openrouter/polaris-alpha", 0, 0, None, created_days_ago=2, desc="This is a cloaked model provided to gather feedback."),
    _m("openai/gpt-5:batch", 0.6, 5, (1300, 1310)),
]


class CIDict(dict):
    """Case-insensitive header lookup, like a real HTTP server."""

    def __init__(self, items):
        dict.__init__(self, ((k.lower(), v) for k, v in items))

    def get(self, key, default=None):
        return dict.get(self, key.lower(), default)


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def _jwt(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return "%s.%s.sig" % (enc({"alg": "none"}), enc(claims))


def routes(path, method, body, headers):
    STATE["seen_headers"][path] = dict(headers)
    # ---------------- Anthropic
    if path == "/anthropic/api/oauth/usage":
        if STATE["claude_429"]:
            return 429, {"error": {"type": "rate_limit_error"}}, {"Retry-After": "600"}
        if headers.get("Authorization") != "Bearer claude-access-2":
            return 401, {"error": "bad token %s" % headers.get("Authorization")}, {}
        return 200, {
            "five_hour": {"utilization": 42.0, "resets_at": iso(NOW + 2 * 3600)},
            "seven_day": {"utilization": 61.5, "resets_at": iso(NOW + 3 * 86400)},
            "seven_day_opus": {"utilization": 12.0, "resets_at": iso(NOW + 3 * 86400)},
            "extra_usage": {"is_enabled": True, "monthly_limit": 5000, "used_credits": 1234, "utilization": 24.7},
        }, {}
    if path == "/anthropic/api/oauth/profile":
        return 200, {"account": {"email": "me@example.com", "has_claude_max": True},
                     "organization": {"rate_limit_tier": "default_claude_max_5x"}}, {}
    if path == "/anthropic/v1/oauth/token":
        if "json" in headers.get("content-type", ""):
            j = json.loads(body.decode() or "{}")  # authorization_code exchange is JSON
            STATE["token_calls"].append(("claude", j.get("grant_type"), j.get("code")))
            ok = (j.get("grant_type") == "authorization_code" and j.get("code") == "CLAUDE-CODE"
                  and j.get("state") == "STATE-1" and len(j.get("code_verifier") or "") >= 43
                  and j.get("redirect_uri") == "https://platform.claude.com/oauth/code/callback")
            if not ok:
                return 400, {"error": "invalid_grant", "got": j}, {}
            return 200, {"access_token": "claude-access-login", "refresh_token": "claude-refresh-login", "expires_in": 28800,
                         "account": {"email_address": "pi@example.com"}}, {}
        form = parse_qs(body.decode())
        STATE["token_calls"].append(("claude", form.get("grant_type", [""])[0], form.get("refresh_token", [""])[0]))
        if form.get("refresh_token", [""])[0] != "claude-refresh-1":
            return 400, {"error": "invalid_grant"}, {}
        return 200, {"access_token": "claude-access-2", "refresh_token": "claude-refresh-2", "expires_in": 28800}, {}
    # ---------------- OpenAI auth (PKCE + device flow)
    if path == "/openai/oauth/token":
        form = dict((k, v[0]) for k, v in parse_qs(body.decode()).items())
        STATE["token_calls"].append(("codex", form.get("grant_type"), form.get("code")))
        good = {("CODEX-CODE", "http://localhost:1455/auth/callback"),
                ("DEV-AUTH-CODE", "https://auth.openai.com/deviceauth/callback")}
        if form.get("grant_type") != "authorization_code" or (form.get("code"), form.get("redirect_uri")) not in good \
                or not form.get("code_verifier") or "codex-cli/" not in headers.get("user-agent", ""):
            return 400, {"error": "invalid_grant", "got": form}, {}
        return 200, {"access_token": "codex-access-login", "refresh_token": "codex-refresh-login", "expires_in": 864000,
                     "id_token": _jwt({"email": "pi@example.com", "https://api.openai.com/auth": {
                         "chatgpt_account_id": "acct-123", "chatgpt_plan_type": "plus"}})}, {}
    if path == "/openai/api/accounts/deviceauth/usercode":
        STATE["device_polls"] = 0
        return 200, {"device_auth_id": "dev-1", "user_code": "ABCD-1234", "interval": "1"}, {}
    if path == "/openai/api/accounts/deviceauth/token":
        STATE["device_polls"] = STATE.get("device_polls", 0) + 1
        if STATE["device_polls"] < 2:
            return 403, {"error": "authorization_pending"}, {}
        return 200, {"authorization_code": "DEV-AUTH-CODE", "code_verifier": "server-generated-verifier"}, {}
    # ---------------- Google OAuth
    if path == "/google-oauth/token":
        form = dict((k, v[0]) for k, v in parse_qs(body.decode()).items())
        STATE["token_calls"].append(("google", form.get("grant_type"), form.get("code")))
        if form.get("grant_type") == "authorization_code":
            if form.get("code") != "GOOGLE-CODE" or not form.get("client_secret") or not form.get("code_verifier") \
                    or form.get("redirect_uri") != "http://localhost:51121/oauth-callback":
                return 400, {"error": "invalid_grant", "got": form}, {}
            return 200, {"access_token": "ya29.login", "refresh_token": "1//google-refresh", "expires_in": 3599}, {}
        return 200, {"access_token": "ya29.refreshed", "expires_in": 3599}, {}  # Google doesn't rotate
    if path == "/googleapis/oauth2/v1/userinfo":
        return 200, {"email": "pi@gmail.com"}, {}
    # ---------------- ChatGPT / Codex
    if path == "/chatgpt/backend-api/wham/usage":
        if headers.get("chatgpt-account-id") != "acct-123":
            return 403, {"detail": "missing account"}, {}
        return 200, {
            "plan_type": "plus",
            "rate_limit": {
                "allowed": True, "limit_reached": False,
                "primary_window": {"used_percent": 18, "limit_window_seconds": 18000, "reset_after_seconds": 9000},
                "secondary_window": {"used_percent": 51, "limit_window_seconds": 604800, "reset_at": int(NOW + 4 * 86400)},
            },
            "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
        }, {}
    # ---------------- Google / Antigravity
    if path in ("/cloudcode/v1internal:loadCodeAssist",):
        return 200, {"cloudaicompanionProject": "proj-abc", "currentTier": {"id": "standard-tier", "name": "Google AI Pro"}}, {}
    if path == "/daily-cloudcode/v1internal:retrieveUserQuotaSummary":
        return 200, {"groups": [
            {"displayName": "Gemini Models", "buckets": [
                {"bucketId": "gemini-5h", "window": "5h", "remaining": {"remainingFraction": 0.8, "resetTime": iso(NOW + 3600)}},
                {"bucketId": "gemini-weekly", "window": "weekly", "remaining": {"remainingFraction": 0.55, "resetTime": iso(NOW + 5 * 86400)}}]},
            {"displayName": "Claude & GPT Models", "buckets": [
                {"bucketId": "claude_gpt_weekly", "displayName": "weekly", "remaining": {"remainingFraction": 0.0, "resetTime": iso(NOW + 2 * 86400)}}]},
        ]}, {}
    # ---------------- OpenCode
    if path == "/opencode/zen/go/v1/usage":
        if not headers.get("x-opencode-session", "").startswith("ses_"):
            return 400, {"error": "missing session"}, {}
        return 200, {"usage": {
            "rolling": {"status": "ok", "percent": 25, "resetsAt": iso(NOW + 4000)},
            "weekly": {"status": "ok", "percent": 40, "resetsAt": iso(NOW + 3 * 86400)},
            "monthly": {"status": "ok", "percent": 59, "resetsAt": iso(NOW + 20 * 86400)},
        }}, {}
    # ---------------- OpenRouter
    if path == "/openrouter/api/v1/key":
        return 200, {"data": {"limit": None, "limit_remaining": None, "usage": 15.2, "usage_daily": 0.4,
                              "usage_weekly": 1.9, "usage_monthly": 3.1, "is_free_tier": False}}, {}
    if path == "/openrouter/api/v1/credits":
        return 200, {"data": dict(STATE["openrouter_credits"])}, {}
    # ---------------- Moonshot
    if path == "/moonshot/v1/users/me/balance":
        b = STATE["moonshot_balance"]
        return 200, {"code": 0, "data": {"available_balance": b, "voucher_balance": 0, "cash_balance": b}}, {}
    # ---------------- Kimi Code
    if path == "/kimi/coding/v1/usages":
        if not headers.get("x-api-key"):
            return 401, {"error": "no key"}, {}
        return 200, {
            "usage": {"limit": 100, "used": 30, "resetTime": iso(NOW + 6 * 86400)},
            "limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                        "detail": {"limit": 50, "remaining": 40, "resetTime": iso(NOW + 7200)}}],
        }, {}
    if path == "/kimi/coding/v1/me":
        return 200, {"user_level_name": "Allegretto"}, {}
    # ---------------- Gemini
    if path.startswith("/gemini/v1beta/models"):
        if not headers.get("x-goog-api-key"):
            return 403, {"error": "no key"}, {}
        return 200, {"models": [{"name": "models/gemini-x"}, {"name": "models/gemini-y"}]}, {}
    # ---------------- News sources
    if path.startswith("/github/") and path.endswith("/releases.atom"):
        repo = path[len("/github/"):-len("/releases.atom")]
        titles = {"openai/codex": ["0.171.0-alpha.12", "0.171.0-alpha.11"],
                  "sst/opencode": ["v2.3.0", "process-daemon v0.6.0", "v2.2.7"]}.get(repo, ["v1.2.3", "v1.2.2"])
        entries = "".join(
            '<entry><title>%s</title><link rel="alternate" href="https://github.com/%s/releases/tag/%s"/>'
            '<updated>%s</updated><content type="html">&lt;h2&gt;What changed&lt;/h2&gt;&lt;li&gt;Added a thing&lt;/li&gt;</content></entry>'
            % (t, repo, t.replace(" ", "-"), iso(NOW - i * 86400)) for i, t in enumerate(titles))
        return 200, '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">%s</feed>' % entries, {"Content-Type": "application/atom+xml"}
    if path in ("/openai-web/news/rss.xml", "/bloggoogle/technology/ai/rss/"):
        items = ("<item><title>Introducing a new reasoning model</title><link>https://example.com/a</link>"
                 "<pubDate>%s</pubDate><description>&lt;p&gt;Better at code.&lt;/p&gt;</description></item>"
                 "<item><title>Our new office</title><link>https://example.com/b</link><pubDate>%s</pubDate>"
                 "<description>Hello</description></item>") % (time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(NOW - 3600)),
                                                                time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(NOW - 7200)))
        return 200, '<?xml version="1.0"?><rss version="2.0"><channel>%s</channel></rss>' % items, {"Content-Type": "application/rss+xml"}
    if path == "/hn/api/v1/search":
        return 200, {"hits": [
            {"title": "Claude Fable 5.2 released", "url": "https://example.com/c", "objectID": "1", "points": 300, "num_comments": 90, "created_at_i": int(NOW - 5000)},
            {"title": "Malware found in npm package", "url": "https://example.com/m", "objectID": "2", "points": 200, "num_comments": 10, "created_at_i": int(NOW - 6000)},
            {"title": "A new stealth model appears on OpenRouter", "url": "https://example.com/s", "objectID": "3", "points": 50, "num_comments": 5, "created_at_i": int(NOW - 7000)},
        ]}, {}
    if path == "/openrouter/api/v1/models":
        return 200, {"data": STATE.get("catalog") or CATALOG}, {}
    # ---------------- FX
    if path.startswith("/fx/latest"):
        return 200, {"base": "USD", "rates": {"GBP": 0.8, "EUR": 0.9, "CNY": 7.0}}, {}
    return 404, {"error": "no route %s" % path}, {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _go(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        hdrs = CIDict(self.headers.items())
        code, payload, extra = routes(self.path.split("?")[0], self.command, body, hdrs)
        raw = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
        self.send_response(code)
        if "Content-Type" not in extra:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    do_GET = _go
    do_POST = _go


def start():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    overrides = {
        "https://api.anthropic.com": base + "/anthropic",
        "https://chatgpt.com": base + "/chatgpt",
        "https://auth.openai.com": base + "/openai",
        "https://cloudcode-pa.googleapis.com": base + "/cloudcode",
        "https://daily-cloudcode-pa.googleapis.com": base + "/daily-cloudcode",
        "https://oauth2.googleapis.com": base + "/google-oauth",
        "https://www.googleapis.com": base + "/googleapis",
        "https://opencode.ai": base + "/opencode",
        "https://openrouter.ai": base + "/openrouter",
        "https://api.moonshot.ai": base + "/moonshot",
        "https://api.kimi.com": base + "/kimi",
        "https://api.kimi.ai": base + "/kimi",
        "https://generativelanguage.googleapis.com": base + "/gemini",
        "https://github.com": base + "/github",
        "https://openai.com": base + "/openai-web",
        "https://blog.google": base + "/bloggoogle",
        "https://hn.algolia.com": base + "/hn",
        "https://api.frankfurter.app": base + "/fx",
        "https://open.er-api.com": base + "/fx",
    }
    return srv, overrides
