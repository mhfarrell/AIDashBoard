"""Offline test-suite. Run:  python3 -m unittest discover -s tests -v"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fake_upstream  # noqa: E402
from aidash import costs, net, providers  # noqa: E402
from aidash.config import Store  # noqa: E402
from aidash.db import History  # noqa: E402
from aidash.fx import FX  # noqa: E402
from aidash.oauth import OAuthManager, _extract_code  # noqa: E402
from aidash.poller import Poller  # noqa: E402
from aidash.server import App, is_lan, serve  # noqa: E402
from aidash.util import month_key, parse_time  # noqa: E402


class ParserTests(unittest.TestCase):
    def test_parse_time(self):
        # reference values from datetime(..., tzinfo=utc).timestamp()
        self.assertAlmostEqual(parse_time("2026-11-22T14:00:00.075Z"), 1795356000.075, places=3)
        self.assertEqual(parse_time("2026-11-18T16:29:59+00:00"), 1795019399.0)
        self.assertEqual(parse_time("2026-11-18T17:29:59+01:00"), 1795019399.0)
        self.assertEqual(parse_time(1795356000000), 1795356000.0)
        self.assertIsNone(parse_time("not a date"))

    def test_claude_legacy_and_current_shapes(self):
        w, m, notes, plan = providers.parse_claude_usage({
            "five_hour": {"utilization": 10, "resets_at": "2026-11-17T20:00:00Z"},
            "seven_day": {"utilization": 80.5, "resets_at": "2026-11-22T14:00:00Z"},
            "extra_usage": {"is_enabled": True, "used_credits": 250, "monthly_limit": 1000},
        })
        keys = dict((x["key"], x["used_pct"]) for x in w)
        self.assertEqual(keys, {"session": 10.0, "weekly": 80.5})
        self.assertEqual((m["extra_usage_amount"], m["extra_usage_currency"]), (2.5, "USD"))
        w2, _, _, _ = providers.parse_claude_usage({"limits": [
            {"kind": "session", "percent": 33, "resetsAt": "2026-11-17T20:00:00Z"},
            {"kind": "weekly_scoped", "utilization": 5, "scope": {"model": {"display_name": "Opus"}}},
        ]})
        self.assertEqual([x["label"] for x in w2], ["5-hour session", "Weekly Opus"])

    def test_claude_extra_usage_real_shape_paused_gbp(self):
        # Shape captured from a live account (2026-11-17): extra usage paused, out of credits, GBP.
        data = {
            "five_hour": {"utilization": 55.0, "resets_at": "2026-11-17T19:49:59.943805+00:00", "used_dollars": None},
            "extra_usage": {"is_enabled": False, "monthly_limit": None, "used_credits": 0.0, "currency": "GBP",
                            "decimal_places": 2, "disabled_reason": "out_of_credits", "credits_ever_enabled": True},
            "spend": {"used": {"amount_minor": 1850, "currency": "GBP", "exponent": 2}, "limit": None,
                      "enabled": False, "disabled_reason": "out_of_credits", "balance": {"amount_minor": 0, "currency": "GBP", "exponent": 2}},
        }
        w, m, notes, _ = providers.parse_claude_usage(data)
        self.assertEqual((m["extra_usage_amount"], m["extra_usage_currency"]), (18.5, "GBP"))  # pence -> pounds
        self.assertEqual(m["_balances"][0]["amount"], 0.0)
        self.assertTrue(any("out of credits" in n for n in notes))
        _, m2, _, _ = providers.parse_claude_usage({"extra_usage": {"is_enabled": False, "credits_ever_enabled": False}})
        self.assertNotIn("extra_usage_amount", m2)  # never used extra usage -> nothing shown

    def test_codex(self):
        w, b, notes, plan = providers.parse_codex_usage({
            "plan_type": "plus",
            "rate_limit": {"primary_window": {"used_percent": 7, "limit_window_seconds": 18000, "reset_after_seconds": 100},
                           "secondary_window": {"used_percent": 49, "limit_window_seconds": 604800, "reset_at": 1795800000},
                           "limit_reached": True},
        })
        self.assertEqual([(x["key"], x["used_pct"]) for x in w], [("session", 7.0), ("weekly", 49.0)])
        self.assertEqual(w[1]["resets_at"], 1795800000.0)
        self.assertTrue(any("reached" in n for n in notes))
        self.assertEqual(plan, "plus")

    def test_opencode_rate_limited_counts_as_full(self):
        w = providers.parse_opencode({"usage": {"rolling": {"status": "rate-limited", "percent": 80, "resetsAt": "2026-11-17T20:00:00Z"}}})
        self.assertEqual(w[0]["used_pct"], 100.0)

    def test_kimi_and_antigravity(self):
        w = providers.parse_kimi_code({"usages": {"limit_5h": {"used_ratio": 0.25, "reset_time": "2026-11-17T20:00:00Z"}}})
        self.assertEqual((w[0]["label"], w[0]["used_pct"]), ("5-hour", 25.0))
        ag = providers.parse_ag_summary({"quotaSummary": {"groups": [{"displayName": "Gemini Models", "buckets": [
            {"window": "weekly", "remaining": {"remainingFraction": 0.25, "resetTime": "2026-11-20T00:00:00Z"}},
            {"window": "5h", "remaining": {"remainingFraction": 1.0}}]}]}})
        self.assertEqual(len(ag), 1)  # unlimited (no reset, full) bucket is skipped
        self.assertEqual(ag[0]["used_pct"], 75.0)

    def test_extract_code(self):
        self.assertEqual(_extract_code("abc#xyz"), ("abc", "xyz"))
        self.assertEqual(_extract_code("http://localhost:1455/auth/callback?code=C1&state=S1"), ("C1", "S1"))

    def test_is_lan(self):
        for ip in ("127.0.0.1", "192.168.1.20", "10.0.0.5", "172.16.3.4", "::1", "fe80::1", "::ffff:192.168.0.9"):
            self.assertTrue(is_lan(ip), ip)
        for ip in ("8.8.8.8", "2001:4860:4860::8888", "::ffff:1.1.1.1"):
            self.assertFalse(is_lan(ip), ip)


class FakeFX(object):
    rates = {"USD": 1.0, "GBP": 0.8, "CNY": 7.0}

    def convert(self, a, s, d):
        return None if a is None else float(a) / self.rates[(s or "USD").upper()] * self.rates[(d or "USD").upper()]


class CostTests(unittest.TestCase):
    def test_total_is_subs_plus_spend_not_topups(self):
        m = month_key()
        cfg = {"display_currency": "GBP",
               "subscriptions": [{"platform": "claude", "name": "Max", "amount": 90, "currency": "GBP", "active": True, "start_month": m},
                                 {"platform": "codex", "name": "Plus", "amount": 20, "currency": "USD", "active": True, "start_month": m}],
               "manual_entries": [{"platform": "gemini", "month": m, "kind": "spend", "amount": 5, "currency": "USD", "note": "API"},
                                  {"platform": "openrouter", "month": m, "kind": "topup", "amount": 10, "currency": "USD"}]}
        state = {"openrouter": {"data": {"metrics": {"key_usage_month": 2.0},
                                         "balances": [{"key": "credits", "amount": 12.5, "currency": "USD"}], "windows": []}}}
        counters = {"openrouter": {"spend": {"value": 3.0, "currency": "USD", "first_seen": 0},
                                   "key_month": {"value": 2.0, "currency": "USD", "first_seen": 0},
                                   "topup": {"value": 10.0, "currency": "USD", "first_seen": 0}}}
        tab = costs.compute(m, cfg, state, counters, FakeFX())
        t = tab["totals"]
        # 90 GBP + 20 USD(16 GBP) subs ; spend: openrouter 3 USD (tracked > key 2) + gemini 5 USD = 8 USD = 6.4 GBP
        self.assertAlmostEqual(t["subscriptions"], 106.0)
        self.assertAlmostEqual(t["credit_spend"], 6.4)
        self.assertAlmostEqual(t["total"], 112.4)
        # top-ups: the manual $10 entry replaces the detected $10 (same month+column) = 8 GBP, not in total
        self.assertAlmostEqual(t["topups"], 8.0)
        self.assertAlmostEqual(t["total_cashflow"], 114.0)
        orow = [r for r in tab["rows"] if r["platform"] == "openrouter"][0]
        self.assertEqual(orow["balance_left"], 10.0)
        self.assertTrue(any(i["kind"] == "info" and "not counted" in i["label"] for i in orow["items"]))
        self.assertEqual(len(tab["rows"]), len(providers.PLATFORMS))  # every platform listed

    def test_invoices_replace_automatic_figures(self):
        m = month_key()
        cfg = {"display_currency": "GBP",
               "subscriptions": [{"platform": "claude", "name": "Claude", "amount": 0, "currency": "GBP", "active": True}],
               "detected_plans": {"claude": {"plan": "Max 5x", "since": m}},
               "manual_entries": [
                   {"platform": "claude", "month": m, "kind": "subscription", "amount": 81, "currency": "GBP", "note": "Max upgrade"},
                   {"platform": "claude", "month": m, "kind": "credit", "amount": 12.10, "currency": "GBP"},
                   {"platform": "claude", "month": m, "kind": "credit", "amount": 84.25, "currency": "GBP"}]}
        counters = {"claude": {"extra_usage": {"value": 10.0, "currency": "GBP", "first_seen": 0}}}
        row = [r for r in costs.compute(m, cfg, {}, counters, FakeFX())["rows"] if r["platform"] == "claude"][0]
        self.assertEqual(row["subscription"], 81.0)       # invoice replaces the £90 list price
        self.assertAlmostEqual(row["credit_spend"], 96.35)  # invoices replace the API-tracked £10
        self.assertAlmostEqual(row["topups"], 96.35)        # bought & used -> also in Bought
        self.assertAlmostEqual(row["total"], 177.35)
        replaced = [i for i in row["items"] if i["kind"] == "info"]
        self.assertEqual(len(replaced), 2)  # list price + API figure shown as "not counted"
        self.assertTrue(all(i["amount"] is None for i in replaced))

    def test_auto_list_price_from_detected_plan(self):
        m = month_key()
        cfg = {"display_currency": "GBP",
               "subscriptions": [{"platform": "claude", "name": "Claude", "amount": 0, "currency": "GBP", "active": True},
                                 {"platform": "codex", "name": "ChatGPT", "amount": 25, "currency": "GBP", "active": True,
                                  "auto_price": False}],
               "detected_plans": {"claude": {"plan": "Max 5x", "since": m}, "codex": {"plan": "Plus", "since": m}}}
        tab = costs.compute(m, cfg, {}, {}, FakeFX())
        rows = dict((r["platform"], r) for r in tab["rows"])
        self.assertEqual(rows["claude"]["subscription"], 90.0)  # list price used for amount 0
        self.assertIn("list price", rows["claude"]["items"][0]["label"])
        self.assertEqual(rows["codex"]["subscription"], 25.0)  # a typed amount always wins
        # auto list price is not backfilled into months before the plan was first detected
        old = dict((r["platform"], r) for r in costs.compute("2000-01", cfg, {}, {}, FakeFX())["rows"])
        self.assertEqual(old["claude"]["subscription"], 0.0)
        # unknown plan -> nothing invented
        cfg["detected_plans"]["claude"]["plan"] = "Enterprise"
        self.assertEqual(dict((r["platform"], r) for r in costs.compute(m, cfg, {}, {}, FakeFX())["rows"])["claude"]["subscription"], 0.0)

    def test_subscription_month_range(self):
        s = {"platform": "claude", "amount": 10, "currency": "USD", "active": False, "start_month": "2026-09", "end_month": "2026-10"}
        self.assertFalse(costs.subscription_applies(s, "2026-08"))
        self.assertTrue(costs.subscription_applies(s, "2026-09"))
        self.assertTrue(costs.subscription_applies(s, "2026-10"))
        self.assertFalse(costs.subscription_applies(s, "2026-11"))


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake, overrides = fake_upstream.start()
        cls.tmp = tempfile.mkdtemp(prefix="aidash-test-")
        store = Store(cls.tmp)
        store.update_config({"upstream_overrides": overrides, "display_currency": "GBP",
                             "antigravity_client_id": "x.apps.googleusercontent.com", "antigravity_client_secret": "s"})
        net.set_overrides(overrides)
        for k in ("openrouter_api_key", "opencode_api_key", "moonshot_api_key", "kimi_code_api_key", "gemini_api_key"):
            store.set_secret(k, "test-" + k)
        past = time.time() - 10
        store.set_secret("oauth", {
            # expired access token -> must refresh with claude-refresh-1 and persist the rotated token
            "claude": {"access_token": "claude-access-1", "refresh_token": "claude-refresh-1", "expires_at": past},
            "codex": {"access_token": "codex-access", "refresh_token": "codex-r", "expires_at": time.time() + 3600, "account_id": "acct-123"},
            "antigravity": {"access_token": "ya29.test", "refresh_token": "g-r", "expires_at": time.time() + 3600},
        })
        cls.store = store
        cls.history = History(os.path.join(cls.tmp, "history.sqlite"))
        cls.fx = FX(cls.history)
        cls.oauth = OAuthManager(store)
        cls.poller = Poller(store, cls.oauth, cls.history, cls.fx)
        cls.app = App(store, cls.history, cls.fx, cls.oauth, cls.poller)
        cls.httpd = serve(cls.app, "127.0.0.1", 0)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        cls.poller._poll_fx()
        for pid, _, _ in providers.PLATFORMS:
            cls.poller._poll(pid)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.fake.shutdown()
        cls.history.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.status, r.read(), dict(r.headers.items())

    def post(self, path, body, headers=None):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_01_every_platform_live(self):
        snap = self.poller.snapshot()
        bad = dict((k, (v["status"], v["error"])) for k, v in snap.items() if v["status"] != "ok")
        self.assertEqual(bad, {})

    def test_02_claude_refresh_rotated_and_persisted(self):
        t = self.oauth.tokens("claude")
        self.assertEqual(t["access_token"], "claude-access-2")
        self.assertEqual(t["refresh_token"], "claude-refresh-2")
        with open(os.path.join(self.tmp, "secrets.json")) as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["oauth"]["claude"]["refresh_token"], "claude-refresh-2")
        # exactly one refresh, presenting the original refresh token
        claude_calls = [c for c in fake_upstream.STATE["token_calls"] if c[0] == "claude"]
        self.assertEqual(claude_calls, [("claude", "refresh_token", "claude-refresh-1")])
        if os.name != "nt":
            self.assertEqual(os.stat(os.path.join(self.tmp, "secrets.json")).st_mode & 0o777, 0o600)

    def test_03_state_api(self):
        status, raw, _ = self.get("/api/state")
        st = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(st["currency"], "GBP")
        names = [p["id"] for p in st["platforms"]]
        self.assertEqual(len(names), 8)
        claude = [p for p in st["platforms"] if p["id"] == "claude"][0]
        self.assertEqual(claude["plan"], "Max 5x")
        self.assertEqual([w["used_pct"] for w in claude["windows"]][:2], [42.0, 61.5])
        self.assertIn("projected_pct", claude["windows"][1])
        ag = [p for p in st["platforms"] if p["id"] == "antigravity"][0]
        self.assertEqual(ag["plan"], "Google AI Pro")
        self.assertIn(100.0, [w["used_pct"] for w in ag["windows"]])
        # Active 5h windows: claude, codex, opencode, kimi, antigravity gemini 5h
        self.assertEqual(len(st["active_windows"]), 5)
        # Claude extra usage $12.34 at 0.8 GBP/USD = 9.87 GBP of credit spend; openrouter key month $3.10 = 2.48
        t = st["costs"]["totals"]
        self.assertAlmostEqual(t["credit_spend"], round(12.34 * 0.8, 2) + round(3.1 * 0.8, 2), places=2)
        self.assertEqual(len(st["trend"]), 6)
        # the current month's trend bar must equal the headline total
        self.assertAlmostEqual(st["trend"][-1]["total"], t["total"], places=2)
        # secrets never leak into state or settings
        status, raw2, _ = self.get("/api/settings")
        for blob in (raw, raw2):
            for secret in (b"claude-refresh-2", b"claude-access-2", b"test-openrouter_api_key", b"ya29.test"):
                self.assertNotIn(secret, blob)

    def test_04_topup_and_spend_tracking(self):
        fake_upstream.STATE["openrouter_credits"] = {"total_credits": 30.0, "total_usage": 6.5}  # +10 bought, +1.5 used
        fake_upstream.STATE["moonshot_balance"] = 8.0  # 2.0 used
        self.poller._poll("openrouter")
        self.poller._poll("moonshot")
        c = self.history.counters(month_key())
        self.assertAlmostEqual(c["openrouter"]["topup"]["value"], 10.0)
        self.assertAlmostEqual(c["openrouter"]["spend"]["value"], 1.5)
        self.assertAlmostEqual(c["moonshot"]["spend"]["value"], 2.0)
        fake_upstream.STATE["moonshot_balance"] = 18.0  # top-up of 10
        self.poller._poll("moonshot")
        c = self.history.counters(month_key())
        self.assertAlmostEqual(c["moonshot"]["topup"]["value"], 10.0)

    def test_05_claude_429_keeps_last_data(self):
        fake_upstream.STATE["claude_429"] = True
        try:
            self.poller._poll("claude")
            st = self.poller.snapshot()["claude"]
            self.assertEqual(st["status"], "stale")
            self.assertIn("429", st["error"])
            self.assertTrue(st["data"]["windows"])
            self.assertGreaterEqual(st["next_poll"] - time.time(), 590)  # honours Retry-After: 600
        finally:
            fake_upstream.STATE["claude_429"] = False

    def test_06_settings_and_admin_password(self):
        status, s = self.post("/api/settings", {"subscriptions": [
            {"id": "claude", "platform": "claude", "name": "Claude Max 5x", "amount": 90, "currency": "GBP", "active": True}]})
        self.assertEqual(status, 200)
        sub = s["config"]["subscriptions"][0]
        self.assertEqual(sub["start_month"], month_key())
        _, raw, _ = self.get("/api/state")
        self.assertGreaterEqual(json.loads(raw)["costs"]["totals"]["subscriptions"], 90.0)
        status, _ = self.post("/api/settings", {"admin_password": "hunter2"})
        self.assertEqual(status, 200)
        status, _ = self.post("/api/settings", {"display_currency": "USD"})
        self.assertEqual(status, 401)
        status, _ = self.post("/api/settings", {"admin_password": ""}, {"X-Admin-Password": "hunter2"})
        self.assertEqual(status, 200)
        status, _ = self.post("/api/settings", {"display_currency": "EUR"})
        self.assertEqual(status, 200)
        self.post("/api/settings", {"display_currency": "GBP"})

    def test_07_static_and_bad_requests(self):
        status, body, hdrs = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"AI Usage", body)
        self.assertEqual(hdrs.get("X-Frame-Options"), "DENY")
        try:
            self.get("/../config.py")
            self.fail("path traversal should 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)
        req = urllib.request.Request(self.base + "/api/settings", data=b"x=1", method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_08_oauth_start_urls(self):
        status, r = self.post("/api/oauth/claude/start", {})
        self.assertEqual(status, 200)
        self.assertIn("prompt=login", r["auth_url"])
        self.assertIn("code_challenge_method=S256", r["auth_url"])
        status, r = self.post("/api/oauth/codex/start", {})
        self.assertIn("prompt=login", r["auth_url"])
        self.assertIn("localhost%3A1455", r["auth_url"])
        status, r = self.post("/api/oauth/antigravity/start", {})
        self.assertIn("access_type=offline", r["auth_url"])
        status, r = self.post("/api/oauth/claude/finish", {"code": ""})
        self.assertEqual(status, 400)


class NewsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from aidash.news import News
        cls.fake, overrides = fake_upstream.start()
        net.set_overrides(overrides)
        cls.tmp = tempfile.mkdtemp(prefix="aidash-news-")
        cls.store = Store(cls.tmp)
        cls.history = History(os.path.join(cls.tmp, "h.sqlite"))
        cls.news = News(cls.store, cls.history)
        cls.news.refresh_feeds()
        cls.news.refresh_catalog()
        cls.state = cls.news.state()

    @classmethod
    def tearDownClass(cls):
        cls.fake.shutdown()
        cls.history.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_feeds_parsed_and_filtered(self):
        st = self.state
        self.assertEqual(st["errors"], {})
        titles = [i["title"] for i in st["items"]]
        self.assertIn("v2.3.0", titles)                                  # stable OpenCode release kept
        self.assertNotIn("process-daemon v0.6.0", titles)                # sub-package tag dropped
        self.assertIn("0.171.0-alpha.12 (pre-release)", titles)          # only pre-releases -> newest, labelled
        self.assertNotIn("0.171.0-alpha.11", titles)
        self.assertIn("Claude Fable 5.2 released", titles)
        self.assertNotIn("Malware found in npm package", titles)         # HN: query term must be in the title
        rss = [i for i in st["items"] if i["title"] == "Introducing a new reasoning model"][0]
        self.assertTrue(rss["relevant"])
        self.assertEqual(rss["summary"], "Better at code.")              # HTML stripped
        self.assertFalse([i for i in st["items"] if i["title"] == "Our new office"][0]["relevant"])
        self.assertTrue(any(i["group"] == "stealth" for i in st["items"]))

    def test_catalog_analysis(self):
        m = self.state["models"]
        self.assertEqual([s["id"] for s in m["stealth"]], ["openrouter/polaris-alpha"])  # router tools excluded
        new_ids = [x["id"] for x in m["new"]]
        self.assertEqual(new_ids[0], "anthropic/claude-fable-5.2")  # watched families first
        self.assertIn("tencent/hy4-flash", new_ids)
        self.assertEqual([r["id"] for r in m["retiring"]], ["openai/gpt-5"])
        tiers = dict((t["label"], [x["id"] for x in t["models"]]) for t in m["value_tiers"])
        self.assertEqual(tiers["Free"], ["nvidia/nemotron:free"])
        self.assertEqual(tiers["Under $0.50 / M"][0], "deepseek/deepseek-v4.1-flash")  # best quality in band
        self.assertEqual(tiers["Premium"], ["anthropic/claude-fable-5.2"])
        frontier = [f["id"] for f in m["frontier"]]
        self.assertEqual(frontier, ["tencent/hy4-flash", "deepseek/deepseek-v4.1-flash", "openai/gpt-5", "anthropic/claude-fable-5.2"])
        self.assertNotIn("openai/gpt-5:batch", [x["id"] for t in m["value_tiers"] for x in t["models"]])

    def test_price_change_and_stealth_memory(self):
        cat = [dict(x) for x in fake_upstream.CATALOG if x["id"] != "openrouter/polaris-alpha"]
        for x in cat:
            if x["id"] == "deepseek/deepseek-v4.1-flash":
                x["pricing"] = {"prompt": str(0.04 / 1e6), "completion": str(0.18 / 1e6)}  # halved
        fake_upstream.STATE["catalog"] = cat
        try:
            self.news.refresh_catalog()
            m = self.news.state()["models"]
            ch = [c for c in m["price_changes"] if c["id"] == "deepseek/deepseek-v4.1-flash"][0]
            self.assertEqual(ch["pct"], -50.0)
            self.assertEqual(m["stealth"], [])
            self.assertEqual([g["id"] for g in m["stealth_gone"]], ["openrouter/polaris-alpha"])  # remembered
        finally:
            fake_upstream.STATE.pop("catalog", None)

    def test_state_survives_restart(self):
        from aidash.news import News
        again = News(self.store, self.history)
        self.assertEqual(len(again.state()["items"]), len(self.news.state()["items"]))


class LoginFlows(unittest.TestCase):
    """Full start -> paste -> token exchange for each provider, against the fake auth servers."""

    @classmethod
    def setUpClass(cls):
        cls.fake, overrides = fake_upstream.start()
        net.set_overrides(overrides)
        cls.tmp = tempfile.mkdtemp(prefix="aidash-login-")
        cls.store = Store(cls.tmp)
        cls.store.update_config({"antigravity_client_id": "x.apps.googleusercontent.com", "antigravity_client_secret": "s"})
        cls.oauth = OAuthManager(cls.store)

    @classmethod
    def tearDownClass(cls):
        cls.fake.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _state_of(self, url):
        from urllib.parse import parse_qs, urlparse
        return parse_qs(urlparse(url).query)["state"][0]

    def test_claude_paste_code(self):
        self.oauth.start_claude()
        self.oauth._pending["claude"]["state"] = "STATE-1"  # the fake checks the state echoes back
        st = self.oauth.finish_claude("CLAUDE-CODE#STATE-1")
        self.assertTrue(st["connected"])
        self.assertEqual(st["account"], "pi@example.com")
        self.assertEqual(self.oauth.tokens("claude")["refresh_token"], "claude-refresh-login")

    def test_codex_paste_redirect_url(self):
        r = self.oauth.start_codex_pkce()
        state = self._state_of(r["auth_url"])
        st = self.oauth.finish_codex_pkce("http://localhost:1455/auth/callback?code=CODEX-CODE&state=" + state)
        self.assertTrue(st["connected"])
        t = self.oauth.tokens("codex")
        self.assertEqual((t["account_id"], t["plan"], t["account"]), ("acct-123", "plus", "pi@example.com"))

    def test_codex_wrong_state_rejected(self):
        self.oauth.start_codex_pkce()
        with self.assertRaises(Exception):
            self.oauth.finish_codex_pkce("http://localhost:1455/auth/callback?code=CODEX-CODE&state=forged")

    def test_codex_device_flow(self):
        self.oauth.logout("codex")
        d = self.oauth.start_codex_device()
        self.assertEqual(d["user_code"], "ABCD-1234")
        deadline = time.time() + 20
        while time.time() < deadline and self.oauth.codex_device_status()["state"] == "pending":
            time.sleep(0.5)
        self.assertEqual(self.oauth.codex_device_status()["state"], "done", self.oauth.codex_device_status())
        self.assertEqual(self.oauth.tokens("codex")["account_id"], "acct-123")

    def test_antigravity_paste_url_and_refresh_keeps_refresh_token(self):
        r = self.oauth.start_antigravity()
        self.assertIn("code_challenge=", r["auth_url"])
        state = self._state_of(r["auth_url"])
        st = self.oauth.finish_antigravity("http://localhost:51121/oauth-callback?state=%s&code=GOOGLE-CODE&scope=x" % state)
        self.assertEqual(st["account"], "pi@gmail.com")
        tok, t = self.oauth.force_refresh("antigravity")
        self.assertEqual(tok, "ya29.refreshed")
        self.assertEqual(t["refresh_token"], "1//google-refresh")  # Google omits it on refresh; keep ours

    def test_period_total_counter(self):
        h = History(os.path.join(self.tmp, "h.sqlite"))
        for v in (5.0, 7.0, 7.0, 2.0):  # 2.0 = billing cycle reset
            h.track_period_total("claude", "extra_usage_usd", v, "extra_usage", "USD")
        self.assertAlmostEqual(h.counters(month_key())["claude"]["extra_usage"]["value"], 9.0)
        h.conn.close()


if __name__ == "__main__":
    unittest.main()
