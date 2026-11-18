#!/usr/bin/env python3
"""Copy your settings + API keys from this PC's dashboard to the Pi, in one go.

Copied:  currency, subscriptions, invoices/manual entries, poll settings, Moonshot region,
         Antigravity OAuth client, and API keys (OpenRouter, OpenCode, Moonshot, Kimi Code, Gemini).
NOT copied: Claude / ChatGPT / Antigravity LOGINS. Those providers rotate tokens, so two copies of a
         login would sign each other out - connect them on the Pi's Settings page instead.

    python tools/push_to_pi.py                                  # -> http://AIdashboard.local
    python tools/push_to_pi.py --to http://192.168.1.50 --from ./data [--admin-password PW]
"""
import argparse
import json
import os
import sqlite3
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_KEYS = ("display_currency", "subscriptions", "manual_entries", "intervals", "enabled",
               "moonshot_base_url", "antigravity_client_id", "antigravity_client_secret",
               "news_watch", "tracked_models")
SECRET_KEYS = ("openrouter_api_key", "opencode_api_key", "moonshot_api_key", "kimi_code_api_key", "gemini_api_key")


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", default=os.path.join(ROOT, "data"), help="local data dir")
    ap.add_argument("--to", default="http://AIdashboard.local", help="the Pi's dashboard URL")
    ap.add_argument("--admin-password", default="", help="the Pi's admin password, if set")
    a = ap.parse_args()
    cfg = load(os.path.join(a.src, "config.json"))
    sec = load(os.path.join(a.src, "secrets.json"))
    if not cfg:
        raise SystemExit("No config.json in %s" % a.src)
    body = dict((k, cfg[k]) for k in CONFIG_KEYS if k in cfg and cfg[k] not in (None, ""))
    body["secrets"] = dict((k, sec[k]) for k in SECRET_KEYS if sec.get(k))
    req = urllib.request.Request(a.to.rstrip("/") + "/api/settings", method="POST", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Admin-Password": a.admin_password})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
    except urllib.error.HTTPError as e:
        raise SystemExit("Pi refused (%s): %s" % (e.code, e.read()[:200].decode("utf-8", "replace")))
    except urllib.error.URLError as e:
        raise SystemExit("Can't reach %s (%s). Is the Pi on, and does %s resolve? Try its IP." % (a.to, e.reason, a.to))
    # Spend/top-up counters this install has tracked (e.g. OpenRouter, Moonshot balance drops), so the
    # Pi's monthly tab includes spend from before it was running. The Pi keeps the larger of each total.
    merged = None
    hist = os.path.join(a.src, "history.sqlite")
    if os.path.isfile(hist):
        con = sqlite3.connect("file:%s?mode=ro" % hist.replace("\\", "/"), uri=True)
        rows = [{"platform": p, "month": m, "kind": k, "currency": c, "value": v, "first_seen": f}
                for p, m, k, c, v, f in con.execute(
                    "SELECT platform, month, kind, currency, value, first_seen FROM counters WHERE value > 0")]
        con.close()
        if rows:
            req = urllib.request.Request(a.to.rstrip("/") + "/api/history/counters", method="POST",
                                         data=json.dumps({"counters": rows}).encode(),
                                         headers={"Content-Type": "application/json", "X-Admin-Password": a.admin_password})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    merged = (json.loads(r.read() or b"{}").get("merged"), len(rows))
            except urllib.error.HTTPError as e:
                print("  (spend history not copied: Pi returned %s - update the Pi's dashboard first)" % e.code)
    print("Pushed to %s:" % a.to)
    print("  settings: %s" % ", ".join(k for k in CONFIG_KEYS if k in body))
    print("  API keys: %s" % (", ".join(sorted(body["secrets"])) or "none"))
    if merged:
        print("  spend history: %d of %d counters updated on the Pi" % merged)
    print("Now open %s/#/settings and connect Claude, ChatGPT and Antigravity on the Pi." % a.to.rstrip("/"))


if __name__ == "__main__":
    main()
