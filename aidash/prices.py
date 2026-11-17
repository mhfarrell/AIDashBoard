"""Subscription prices read from providers' PUBLIC pricing pages (no keys, no login).

Currently OpenCode Go / Go Plus (opencode.ai/console/go). Where no public price page exists
(Claude, ChatGPT), the dashboard uses the LIST_PRICES table in costs.py or your Settings entries.
Results are kept in config so the prices usually come from memory, not the network.
"""
import html
import re

from . import net
from .util import now_ts

OPENCODE_URL = "https://opencode.ai/console/go"  # public pricing page (override via config price_sources)
TTL = 12 * 3600
_GO_TIERS = {"go": "Go", "go plus": "Go Plus"}
_PLAN_RE = re.compile(r"(Go(?:\s+Plus)?)\s*\$(\d+(?:\.\d+)?)\s*(?:USD\s*)?(?:per|/)\s*month", re.I)


def parse_opencode_pricing(page_text):
    """Return {"Go": 10.0, "Go Plus": 40.0} from the page's plan headings (USD/month).

    Only matches '<plan> $<price> per month' pairs; per-model allowance figures elsewhere on the
    page never match. Returns None when the expected plans aren't found (page changed).
    """
    found = {}
    for name, price in _PLAN_RE.findall(page_text):
        plan = _GO_TIERS.get(" ".join(name.lower().split()))
        if plan and plan not in found:
            found[plan] = float(price)
    if set(found) == set(_GO_TIERS.values()):
        return found
    return None


def _static_pipeline(text):
    """OpenCode's static HTML renders the plan headings as 'Go  $10  per month' between tags."""
    plain = re.sub(r"(?is)<(script|style|template)[^>]*>.*?</\1>", " ", text)
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", plain)))


def _embedded_pipeline(text):
    """Client-rendered plans in the page's embedded app data: '"name":"Go" ... price 10 ...'."""
    out = {}
    for name in _GO_TIERS.values():
        m = re.search(r"(?:\"(?:plan_name|planName|name|title|plan)\"|'(?:plan_name|planName|name|title|plan)')\s*:\s*[\"']%s[\"']"
                      % re.escape(name), text)
        if not m:
            continue
        window = text[max(0, m.start() - 600):m.end() + 1200]
        for pat in (r"(?:price|monthly_?price|amount)\D{0,12}?(\d+(?:\.\d+)?)", r"\$\s*(\d+)\\?\s*(?:USD)?\s*(?:per|/)\s*month"):
            pm = re.search(pat, window, re.I)
            if pm:
                out[_GO_TIERS[name.lower()]] = float(pm.group(1))
                break
    return out if set(out) == set(_GO_TIERS.values()) else None


def fetch_opencode_plans(url=OPENCODE_URL):
    """Try the page as rendered, then its embedded client-side data. Returns a plans dict, not money."""
    _, body, _ = net.request("GET", url, timeout=25)
    text = body if isinstance(body, str) else ""
    if not text:
        raise net.UpstreamError("OpenCode pricing page returned no HTML", 200)
    for pipeline in (_static_pipeline, _embedded_pipeline):
        plain = pipeline(text)
        plans = parse_opencode_pricing(plain)
        if plans:
            return plans
    raise net.UpstreamError("Couldn't read OpenCode plan prices from the pricing page (its layout may have changed)", 200)


def opencode_plans(store, history, force=False):
    """Cached public prices: config first, fetched at most every TTL seconds."""
    cached = (store.get("price_data") or {}).get("opencode") or {}
    if cached.get("plans") and not force and now_ts() - cached.get("ts", 0) < TTL:
        return cached["plans"], cached.get("ts")
    url = (store.get("price_sources") or {}).get("opencode") or OPENCODE_URL
    plans = fetch_opencode_plans(url)
    data = store.get("price_data") or {}
    ts = now_ts()
    data["opencode"] = {"plans": plans, "ts": ts, "source": url}
    store.set_config_value("price_data", data)
    history.kv_set("price_fetch_log", {"opencode": {"status": "ok", "ts": ts}})
    return plans, ts


def track(store, history):
    """Best-effort refresh of public prices; called on its own schedule by the poller."""
    try:
        return opencode_plans(store, history)
    except net.UpstreamError as e:
        history.kv_set("price_fetch_log", {"opencode": {"status": e.short(), "ts": now_ts()}})
        return None
