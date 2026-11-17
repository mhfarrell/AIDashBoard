"""News & model intelligence - all from free, keyless sources (Pi-friendly, polled every few hours).

* Release notes for the tools you use (GitHub release Atom feeds)
* Vendor news (OpenAI, Google AI RSS)
* Community: Hacker News stories about your models (Algolia API) + stealth-model chatter
* OpenRouter public model catalogue -> new models, stealth models, retiring models,
  price changes, and "bang for buck" (quality per $, quality = Design Arena Elo from the catalogue)
"""
import email.utils
import hashlib
import html
import json
import re
import threading
import urllib.parse
import xml.etree.ElementTree as ET

from . import net
from .util import iso, now_ts, num

DAY = 86400

FEEDS = [
    # id, name, kind, url, group, platform it relates to, max items kept
    ("claude-code", "Claude Code releases", "atom", "https://github.com/anthropics/claude-code/releases.atom", "release", "claude", 3),
    ("codex-cli", "Codex CLI releases", "atom", "https://github.com/openai/codex/releases.atom", "release", "codex", 3),
    ("gemini-cli", "Gemini CLI releases", "atom", "https://github.com/google-gemini/gemini-cli/releases.atom", "release", "gemini", 2),
    ("opencode", "OpenCode releases", "atom", "https://github.com/sst/opencode/releases.atom", "release", "opencode", 3),
    ("kimi-code", "Kimi Code releases", "atom", "https://github.com/MoonshotAI/kimi-code/releases.atom", "release", "kimi_code", 2),
    ("openai-news", "OpenAI news", "rss", "https://openai.com/news/rss.xml", "vendor", "codex", 8),
    ("google-ai", "Google AI blog", "rss", "https://blog.google/technology/ai/rss/", "vendor", "gemini", 8),
]
HN_URL = "https://hn.algolia.com/api/v1/search"
CATALOG_URL = "https://openrouter.ai/api/v1/models"

DEFAULT_WATCH = {
    # OpenRouter author prefixes for the "models you use" highlights
    "families": ["anthropic", "openai", "google", "moonshotai", "deepseek", "qwen", "z-ai"],
    # Hacker News queries (one request each, every few hours)
    "hn_queries": ["Claude", "Anthropic", "OpenAI Codex", "GPT", "Gemini", "Kimi", "DeepSeek", "OpenRouter"],
    "hn_min_points": 60,
}
# openrouter/* ids that are routers/tools, not stealth models
OPENROUTER_TOOLS = {"auto", "auto-code", "auto-beta", "fusion", "pareto-code", "free", "bodybuilder"}
STEALTH_WORDS = ("stealth", "cloaked", "anonymous model", "undisclosed", "codename", "mystery model")
RELEVANT_WORDS = ("model", "gpt", "codex", "claude", "gemini", "kimi", "deepseek", "qwen", "reasoning", "pricing",
                  "api ", "context window", "benchmark")
# Release feeds: keep real versions only (drop alpha/beta/rc/nightly builds and sub-package tags).
RELEASE_TITLE = re.compile(r"^(?:release\s+|@[\w.-]+/[\w.-]+@)?(?:rust-)?v?\d+\.\d+(?:\.\d+)?$", re.I)
PRICE_TIERS = [("Free", 0.0), ("Under $0.50 / M", 0.5), ("Under $2 / M", 2.0), ("Under $6 / M", 6.0), ("Premium", 1e9)]


# ---------------------------------------------------------------------------- parsing helpers
def _strip_html(s, limit=240):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(re.sub(r"\s+", " ", s)).strip()
    return s if len(s) <= limit else s[:limit - 1].rsplit(" ", 1)[0] + "…"


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _child(el, name):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _text(el, name):
    c = _child(el, name)
    return (c.text or "").strip() if c is not None and c.text else ""


def _rfc822(s):
    try:
        return email.utils.parsedate_to_datetime(s).timestamp()
    except (TypeError, ValueError, IndexError):
        return None


def parse_feed(raw, kind):
    """Atom or RSS bytes/str -> [{title, url, ts, summary}] (newest first)."""
    from .util import parse_time
    root = ET.fromstring(raw.encode("utf-8") if isinstance(raw, str) else raw)
    out = []
    if kind == "atom" or _local(root.tag) == "feed":
        for e in root:
            if _local(e.tag) != "entry":
                continue
            link = ""
            for c in e:
                if _local(c.tag) == "link" and c.get("rel", "alternate") == "alternate":
                    link = c.get("href", "")
            ts = parse_time(_text(e, "updated") or _text(e, "published"))
            body = _text(e, "content") or _text(e, "summary")
            out.append({"title": _text(e, "title"), "url": link, "ts": ts, "summary": _strip_html(body)})
    else:
        channel = _child(root, "channel")
        for it in (channel if channel is not None else root):
            if _local(it.tag) != "item":
                continue
            out.append({"title": _text(it, "title"), "url": _text(it, "link"), "ts": _rfc822(_text(it, "pubDate")),
                        "summary": _strip_html(_text(it, "description"))})
    out = [o for o in out if o["title"] and o["url"]]
    out.sort(key=lambda o: -(o["ts"] or 0))
    return out


def _blended(pricing):
    """$ per 1M tokens, blended 3:1 input:output (the common industry convention)."""
    p, c = num(pricing.get("prompt")), num(pricing.get("completion"))
    if p is None or c is None or p < 0 or c < 0:
        return None
    return round((3 * p + c) / 4 * 1e6, 4)


def _quality(benchmarks):
    """Mean Design Arena Elo across 'models' arena categories (None when not rated)."""
    rows = [r for r in ((benchmarks or {}).get("design_arena") or []) if r.get("arena") == "models"]
    elos = [num(r.get("elo")) for r in rows if num(r.get("elo")) is not None]
    return round(sum(elos) / len(elos)) if len(elos) >= 2 else None


def compact_catalog(models):
    out = {}
    for m in models:
        mid = m.get("id") or ""
        if not mid or mid.endswith(":batch"):
            continue
        pr = m.get("pricing") or {}
        out[mid] = {
            "name": m.get("name") or mid,
            "created": num(m.get("created")),
            "in": num(pr.get("prompt")), "out": num(pr.get("completion")),
            "blended": _blended(pr),
            "ctx": num(m.get("context_length")),
            "q": _quality(m.get("benchmarks")),
            "exp": m.get("expiration_date"),
            "desc": _strip_html(m.get("description"), 200),
        }
    return out


def _family(mid):
    return mid.split("/", 1)[0]


def _base_id(mid):
    return mid.split(":", 1)[0]


def analyse_catalog(cat, prev, families, stealth_memory, now=None):
    now = now or now_ts()
    fams = set(families or [])
    watched = lambda mid: _family(mid) in fams  # noqa: E731

    new = [dict(id=k, **v) for k, v in cat.items() if v["created"] and v["created"] >= now - 14 * DAY]
    new.sort(key=lambda m: (-int(watched(m["id"])), -(m["created"] or 0)))
    for m in new:
        m["watched"] = watched(m["id"])

    stealth = []
    for k, v in cat.items():
        tail = k.split("/", 1)[-1].split(":")[0]
        is_or = k.startswith("openrouter/") and tail not in OPENROUTER_TOOLS
        if is_or or any(w in (v["desc"] or "").lower() for w in STEALTH_WORDS):
            rec = stealth_memory.setdefault(k, {"first_seen": now, "name": v["name"]})
            rec["last_seen"] = now
            stealth.append(dict(id=k, first_seen=rec["first_seen"], **v))
    gone = [dict(id=k, **r) for k, r in stealth_memory.items() if k not in cat]

    retiring = []
    for k, v in cat.items():
        if not v["exp"]:
            continue
        from .util import parse_time
        ts = parse_time(v["exp"] + "T00:00:00Z" if len(v["exp"]) == 10 else v["exp"])
        if ts and now - DAY < ts < now + 75 * DAY:
            retiring.append(dict(id=k, exp_ts=ts, watched=watched(k), **v))
    retiring.sort(key=lambda m: (-int(m["watched"]), m["exp_ts"]))

    changes = []
    for k, v in cat.items():
        o = (prev or {}).get(k)
        if not o or o.get("blended") in (None, 0) or v["blended"] is None:
            continue
        if abs(v["blended"] - o["blended"]) / o["blended"] >= 0.02:
            changes.append({"id": k, "name": v["name"], "old": o["blended"], "new": v["blended"],
                            "pct": round(100.0 * (v["blended"] - o["blended"]) / o["blended"], 1), "watched": watched(k)})
    changes.sort(key=lambda c: (-int(c["watched"]), -abs(c["pct"])))

    # Bang for buck: best quality in each price band (dedupe :free/:variants by base id).
    rated = [dict(id=k, **v) for k, v in cat.items() if v["q"] and v["blended"] is not None]
    tiers = []
    lo = -1.0
    for label, hi in PRICE_TIERS:
        if label == "Free":
            band = [m for m in rated if m["blended"] == 0]
        else:
            band = [m for m in rated if lo < m["blended"] <= hi and m["blended"] > 0]
        band.sort(key=lambda m: (-m["q"], m["blended"]))
        seen, top = set(), []
        for m in band:
            if _base_id(m["id"]) in seen:
                continue
            seen.add(_base_id(m["id"]))
            m["watched"] = watched(m["id"])
            m["q_per_dollar"] = round(m["q"] / m["blended"]) if m["blended"] else None
            top.append(m)
            if len(top) == 4:
                break
        tiers.append({"label": label, "models": top})
        if label != "Free":
            lo = hi
    # Pareto frontier: nothing else is both better AND cheaper.
    frontier, best_q = [], -1
    for m in sorted([m for m in rated if m["blended"] > 0], key=lambda m: (m["blended"], -m["q"])):
        if m["q"] > best_q:
            best_q = m["q"]
            if _base_id(m["id"]) not in [_base_id(f["id"]) for f in frontier]:
                frontier.append({"id": m["id"], "name": m["name"], "q": m["q"], "blended": m["blended"], "watched": watched(m["id"])})
    return {"new": new[:20], "stealth": stealth, "stealth_gone": gone[-10:], "retiring": retiring[:15],
            "price_changes": changes[:15], "value_tiers": tiers, "frontier": frontier[-12:],
            "catalog_size": len(cat), "rated": len(rated)}


# ---------------------------------------------------------------------------- service
class News(object):
    def __init__(self, store, history):
        self.store = store
        self.history = history
        self._lock = threading.Lock()
        self.items = []
        self.models = {}
        self.errors = {}
        self.updated = {"feeds": None, "catalog": None}
        saved = history.kv_get("news_state") or {}
        self.items = saved.get("items") or []
        self.models = saved.get("models") or {}
        self.updated = saved.get("updated") or self.updated
        self.stealth_memory = saved.get("stealth_memory") or {}

    def watch(self):
        w = dict(DEFAULT_WATCH)
        w.update(self.store.get("news_watch") or {})
        return w

    def _persist(self):
        self.history.kv_set("news_state", {"items": self.items, "models": self.models, "updated": self.updated,
                                           "stealth_memory": self.stealth_memory})

    def refresh_feeds(self):
        w = self.watch()
        words = [q.lower() for q in w["hn_queries"]] + list(RELEVANT_WORDS)
        items, errors = [], {}
        for fid, name, kind, url, group, platform, limit in FEEDS:
            try:
                _, body, _ = net.request("GET", url, headers={"Accept": "application/atom+xml, application/rss+xml, */*"}, timeout=30)
                text = body if isinstance(body, str) else json.dumps(body)
                parsed = parse_feed(text, kind)
                if group == "release":
                    stable = [it for it in parsed if RELEASE_TITLE.match(it["title"].strip())]
                    if not stable and parsed:  # only pre-releases in the feed: show the newest, labelled
                        stable = [dict(parsed[0], title=parsed[0]["title"] + " (pre-release)")]
                    parsed = stable
                for it in parsed[:limit]:
                    blob = (it["title"] + " " + it["summary"]).lower()
                    items.append(dict(it, source=name, source_id=fid, group=group, platform=platform,
                                      relevant=group == "release" or any(x in blob for x in words)))
            except (net.UpstreamError, ET.ParseError) as e:
                errors[fid] = e.short() if hasattr(e, "short") else "Feed could not be parsed"
        since = int(now_ts() - 14 * DAY)
        queries = [(q, "community", int(w.get("hn_min_points") or 60)) for q in w["hn_queries"]]
        queries.append(("stealth model", "stealth", 10))
        for q, group, pts in queries:
            try:
                url = HN_URL + "?" + urllib.parse.urlencode({
                    "query": q, "tags": "story", "hitsPerPage": 6,
                    "numericFilters": "created_at_i>%d,points>=%d" % (since, pts)})
                must = re.compile(r"\b%s" % re.escape(q.split()[-1]), re.I)  # term must be in the TITLE
                for h in net.get_json(url, timeout=20).get("hits") or []:
                    if not must.search(h.get("title") or ""):
                        continue
                    link = h.get("url") or "https://news.ycombinator.com/item?id=%s" % h.get("objectID")
                    items.append({"title": h.get("title") or "", "url": link, "ts": num(h.get("created_at_i")),
                                  "summary": "%d points · %d comments on Hacker News" % (h.get("points") or 0, h.get("num_comments") or 0),
                                  "discuss": "https://news.ycombinator.com/item?id=%s" % h.get("objectID"),
                                  "source": "Hacker News", "source_id": "hn", "group": group, "query": q,
                                  "relevant": True, "points": h.get("points") or 0})
            except net.UpstreamError as e:
                errors["hn:" + q] = e.short()
        # de-duplicate by URL, keep newest first; a story found by the stealth search stays "stealth"
        # even if a model-family search found it too.
        seen, uniq = {}, []
        for it in sorted(items, key=lambda i: -(i.get("ts") or 0)):
            key = hashlib.sha1((it["url"] or it["title"]).encode("utf-8")).hexdigest()
            if key in seen:
                if it["group"] == "stealth":
                    seen[key]["group"] = "stealth"
                continue
            seen[key] = it
            uniq.append(it)
        with self._lock:
            if uniq or not self.items:
                self.items = uniq[:120]
            self.errors.update(errors)
            for k in [k for k in self.errors if not k.startswith("catalog") and k not in errors]:
                self.errors.pop(k, None)
            self.updated["feeds"] = now_ts()
            self._persist()

    def refresh_catalog(self):
        try:
            data = net.get_json(CATALOG_URL, timeout=60)
        except net.UpstreamError as e:
            self.errors["catalog"] = e.short()
            return
        cat = compact_catalog(data.get("data") or [])
        prev = self.history.kv_get("catalog_prev") or {}
        analysis = analyse_catalog(cat, prev, self.watch()["families"], self.stealth_memory)
        # Keep the previous snapshot for price-change detection; roll it forward daily.
        prev_ts = self.history.kv_get("catalog_prev_ts") or 0
        if not prev or now_ts() - prev_ts > DAY:
            self.history.kv_set("catalog_prev", cat)
            self.history.kv_set("catalog_prev_ts", now_ts())
        if analysis["price_changes"]:
            log = self.history.kv_get("price_change_log") or []
            known = set((c["id"], c["new"]) for c in log)
            for c in analysis["price_changes"]:
                if (c["id"], c["new"]) not in known:
                    log.append(dict(c, ts=now_ts()))
            self.history.kv_set("price_change_log", [c for c in log if now_ts() - c["ts"] < 30 * DAY][-40:])
        with self._lock:
            self.models = analysis
            self.errors.pop("catalog", None)
            self.updated["catalog"] = now_ts()
            self._persist()

    def state(self):
        with self._lock:
            m = dict(self.models)
            m["price_changes"] = sorted(self.history.kv_get("price_change_log") or [],
                                        key=lambda c: (-int(c.get("watched", False)), -c["ts"]))[:15]
            return {"items": list(self.items), "models": m, "errors": dict(self.errors),
                    "updated": dict(self.updated), "watch": self.watch(),
                    "updated_iso": dict((k, iso(v)) for k, v in self.updated.items())}
