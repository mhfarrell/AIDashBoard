#!/usr/bin/env python3
"""Preview the dashboard with fake provider data - no accounts or keys needed.

    python tools/demo.py [--port 8790]

Starts a fake upstream (tests/fake_upstream.py), a throwaway data dir with demo subscriptions,
and the real app pointed at the fake. Nothing leaves your machine.
"""
import argparse
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import fake_upstream  # noqa: E402
from aidash import net  # noqa: E402
from aidash.config import Store  # noqa: E402
from aidash.db import History  # noqa: E402
from aidash.fx import FX  # noqa: E402
from aidash.oauth import OAuthManager  # noqa: E402
from aidash.poller import Poller  # noqa: E402
from aidash.server import App, serve  # noqa: E402
from aidash.util import month_key, prev_months  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8790)
    a = ap.parse_args()
    fake, overrides = fake_upstream.start()
    tmp = tempfile.mkdtemp(prefix="aidash-demo-")
    store = Store(tmp)
    m = month_key()
    start = prev_months(4)[0]
    store.update_config({
        "upstream_overrides": overrides, "display_currency": "GBP",
        "antigravity_client_id": "demo.apps.googleusercontent.com", "antigravity_client_secret": "demo",
        "subscriptions": [
            {"id": "claude", "platform": "claude", "name": "Claude Max 5x", "amount": 90, "currency": "GBP", "active": True, "start_month": start},
            {"id": "chatgpt", "platform": "codex", "name": "ChatGPT Plus", "amount": 20, "currency": "GBP", "active": True, "start_month": start},
            {"id": "opencode-go", "platform": "opencode", "name": "OpenCode Go", "amount": 10, "currency": "USD", "active": True, "start_month": prev_months(2)[0]},
            {"id": "kimi-code", "platform": "kimi_code", "name": "Kimi Code", "amount": 19, "currency": "USD", "active": True, "start_month": m},
            {"id": "google-ai", "platform": "gemini", "name": "Google AI Pro", "amount": 18.99, "currency": "GBP", "active": True, "start_month": start},
        ],
        "manual_entries": [
            {"id": "g1", "month": m, "platform": "gemini", "kind": "spend", "amount": 4.20, "currency": "USD", "note": "Gemini API (Cloud billing)"},
            {"id": "o1", "month": prev_months(2)[0], "platform": "openrouter", "kind": "spend", "amount": 6.5, "currency": "USD", "note": "OpenRouter"},
        ],
    })
    net.set_overrides(overrides)
    for k in ("openrouter_api_key", "opencode_api_key", "moonshot_api_key", "kimi_code_api_key", "gemini_api_key"):
        store.set_secret(k, "demo")
    store.set_secret("oauth", {
        "claude": {"access_token": "claude-access-1", "refresh_token": "claude-refresh-1", "expires_at": 0, "account": "you@example.com"},
        "codex": {"access_token": "x", "refresh_token": "r", "expires_at": time.time() + 86400, "account_id": "acct-123", "account": "you@example.com"},
        "antigravity": {"access_token": "x", "refresh_token": "r", "expires_at": time.time() + 86400, "account": "you@gmail.com"},
    })
    history = History(os.path.join(tmp, "history.sqlite"))
    # fake 48h of snapshots so sparklines render
    now = time.time()
    import json as _j
    for i in range(96):
        ts = now - (96 - i) * 1800
        history.conn.execute("INSERT INTO snapshots(platform, ts, data) VALUES (?,?,?)",
                             ("claude", ts, _j.dumps({"session": (i * 7) % 60, "weekly": 30 + i * 0.33})))
        history.conn.execute("INSERT INTO snapshots(platform, ts, data) VALUES (?,?,?)",
                             ("codex", ts, _j.dumps({"weekly": 20 + i * 0.32})))
    history.conn.commit()
    fx = FX(history)
    oauth = OAuthManager(store)
    from aidash.news import News
    news = News(store, history)
    poller = Poller(store, oauth, history, fx, news)
    app = App(store, history, fx, oauth, poller, news)
    poller._poll_fx()
    news.refresh_feeds()
    news.refresh_catalog()
    for pid in ("claude", "codex", "antigravity", "gemini", "opencode", "openrouter", "moonshot", "kimi_code"):
        poller._poll(pid)
    # simulate a top-up + spend on OpenRouter and Moonshot since the first poll
    fake_upstream.STATE["openrouter_credits"] = {"total_credits": 30.0, "total_usage": 8.4}
    fake_upstream.STATE["moonshot_balance"] = 7.25
    poller._poll("openrouter")
    poller._poll("moonshot")
    poller.start()
    httpd = serve(app, "127.0.0.1", a.port)
    print("Demo dashboard: http://127.0.0.1:%d   (Ctrl+C to stop)" % a.port, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        poller.stop()
        history.conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
