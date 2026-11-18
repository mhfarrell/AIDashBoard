"""Direct upstream fetchers - one function per platform.

Every fetcher returns a normalized dict:
  {
    "plan": str | None,
    "account": str | None,
    "windows":  [{"key","label","used_pct","resets_at"(unix s|None),"window_s"(int|None)}],
    "balances": [{"key","label","amount","currency"}],
    "metrics":  {...provider specific numbers used by the cost engine...},
    "notes":    [str],
  }
and raises net.UpstreamError / oauth.AuthError on failure (the poller turns those into tile errors).

Endpoints and response shapes are the ones the official CLIs/apps use for their own usage displays.
"""
import random
import string

from . import net
from .util import clamp, num, parse_time, rec, now_ts

PLATFORMS = [
    # id, display name, auth kind
    ("claude", "Claude", "oauth"),
    ("codex", "ChatGPT / Codex", "oauth"),
    ("antigravity", "Antigravity", "oauth"),
    ("gemini", "Gemini", "key"),
    ("opencode", "OpenCode Go", "key"),
    ("openrouter", "OpenRouter", "key"),
    ("moonshot", "Kimi (Moonshot API)", "key"),
    ("kimi_code", "Kimi Code", "key"),
]
# Rows that only take manual entries (no API to poll).
MANUAL_PLATFORMS = [("anthropic_api", "Anthropic API (console)"), ("other", "Other")]
PLATFORM_NAMES = dict((p[0], p[1]) for p in PLATFORMS)
PLATFORM_NAMES.update(dict(MANUAL_PLATFORMS))

FIVE_H = 5 * 3600
WEEK = 7 * 86400


def _window(key, label, used_pct, resets_at=None, window_s=None):
    u = num(used_pct)
    return {
        "key": key,
        "label": label,
        "used_pct": None if u is None else round(clamp(u, 0.0, 100.0), 2),
        "resets_at": parse_time(resets_at),
        "window_s": window_s,
    }


def _result(**kw):
    out = {"plan": None, "account": None, "windows": [], "balances": [], "metrics": {}, "notes": []}
    out.update(kw)
    return out


# ============================================================================ Claude
CLAUDE_USAGE = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_PROFILE = "https://api.anthropic.com/api/oauth/profile"

_LEGACY_MODEL_NAMES = {"omelette": "designer"}


def _claude_plan_label(*cands):
    for c in cands:
        if isinstance(c, str) and c.strip() and c.strip().lower() not in ("claude code", "unknown"):
            s = c.strip()
            pretty = {
                "default_claude_max_20x": "Max 20x", "default_claude_max_5x": "Max 5x",
                "claude_max": "Max", "claude_pro": "Pro", "max": "Max", "pro": "Pro",
            }
            return pretty.get(s.lower(), s.replace("_", " "))
    return None


def parse_claude_usage(data):
    data = rec(data)
    windows = []
    seen = set()
    # Newer shape: limits[] with kind/percent/resetsAt/scope.
    for i, lim in enumerate(data.get("limits") or []):
        lim = rec(lim)
        kind = lim.get("kind")
        if kind not in ("session", "weekly_all", "weekly_scoped"):
            continue
        pct = num(lim.get("percent"), num(lim.get("utilization"), num(lim.get("percentage"))))
        reset = lim.get("resetsAt") or lim.get("resets_at") or lim.get("resetAt") or lim.get("reset_at")
        scope = rec(lim.get("scope"))
        model = scope.get("model")
        mname = model if isinstance(model, str) else (rec(model).get("display_name") or rec(model).get("displayName") or rec(model).get("id"))
        if kind == "session":
            key, label, ws = "session", "5-hour session", FIVE_H
        elif kind == "weekly_all":
            key, label, ws = "weekly", "Weekly (all models)", WEEK
        else:
            key, label, ws = "weekly_%s" % (mname or i), "Weekly %s" % (mname or "scoped"), WEEK
        windows.append(_window(key, label, pct, reset, ws))
        seen.add(key)
    # Older shape: five_hour / seven_day / seven_day_<model> with utilization + resets_at.
    if "session" not in seen and isinstance(data.get("five_hour"), dict):
        w = data["five_hour"]
        windows.append(_window("session", "5-hour session", w.get("utilization"), w.get("resets_at"), FIVE_H))
    if "weekly" not in seen and isinstance(data.get("seven_day"), dict):
        w = data["seven_day"]
        windows.append(_window("weekly", "Weekly (all models)", w.get("utilization"), w.get("resets_at"), WEEK))
    for k, v in data.items():
        if k.startswith("seven_day_") and isinstance(v, dict) and v.get("utilization") is not None:
            name = k[len("seven_day_"):]
            name = _LEGACY_MODEL_NAMES.get(name, name).replace("_", " ").title()
            key = "weekly_" + name.lower()
            if key not in seen:
                windows.append(_window(key, "Weekly %s" % name, v.get("utilization"), v.get("resets_at"), WEEK))
    metrics, notes, balances = {}, [], []
    extra = rec(data.get("extra_usage"))
    spend = rec(data.get("spend"))
    used_m = rec(spend.get("used"))
    # Amounts are in MINOR units of the account's billing currency (e.g. pence for GBP).
    cur = (extra.get("currency") or used_m.get("currency") or "USD").upper()
    exp = int(num(extra.get("decimal_places"), num(used_m.get("exponent"), 2)))

    def major(v):
        if isinstance(v, dict):  # newer {"amount_minor", "currency", "exponent"} shape
            amt = num(v.get("amount_minor"))
            return None if amt is None else amt / (10 ** int(num(v.get("exponent"), exp)))
        amt = num(v)
        return None if amt is None else amt / (10 ** exp)

    if extra or spend:
        used = major(spend["used"]) if used_m else major(extra.get("used_credits"))
        limit = major(spend.get("limit")) if spend.get("limit") is not None else major(extra.get("monthly_limit"))
        enabled = bool(extra.get("is_enabled") or spend.get("enabled"))
        ever = enabled or extra.get("credits_ever_enabled") or (used or 0) > 0
        if ever:
            metrics["extra_usage_amount"] = round(used or 0.0, 2)
            metrics["extra_usage_currency"] = cur
            if limit:
                metrics["extra_usage_limit"] = round(limit, 2)
            bal = major(spend.get("balance")) if spend.get("balance") is not None else None
            if bal is not None:
                balances.append({"key": "credits", "label": "Extra-usage credit", "amount": round(bal, 2), "currency": cur})
            reason = extra.get("disabled_reason") or spend.get("disabled_reason")
            if enabled:
                notes.append("Extra (pay-as-you-go) usage is on.")
            elif reason == "out_of_credits":
                notes.append("Extra usage paused - out of credits. Past extra-usage spend isn't reported by the API; add it under Manual entries.")
            elif reason:
                notes.append("Extra usage off (%s)." % str(reason).replace("_", " "))
    metrics["_balances"] = balances
    plan = _claude_plan_label(data.get("tier"), data.get("plan"), data.get("subscription_type"))
    return windows, metrics, notes, plan


def fetch_claude(ctx):
    token, tinfo = ctx.oauth.access_token("claude")
    ua = ctx.ua("claude")
    hdrs = {
        "Authorization": "Bearer " + token,
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
    }
    data = net.get_json(CLAUDE_USAGE, headers=hdrs)
    windows, metrics, notes, plan = parse_claude_usage(data)
    balances = metrics.pop("_balances", [])
    # Plan label from the profile endpoint (cached for 6h - it rarely changes).
    cache = ctx.cache.setdefault("claude_profile", {})
    if now_ts() - cache.get("at", 0) > 6 * 3600:
        try:
            prof = net.get_json(CLAUDE_PROFILE, headers=hdrs)
            acct, org = rec(prof.get("account")), rec(prof.get("organization"))
            label = None
            if acct.get("has_claude_max"):
                label = _claude_plan_label(org.get("rate_limit_tier")) or "Max"
            elif acct.get("has_claude_pro"):
                label = "Pro"
            cache.update(at=now_ts(), plan=label, email=acct.get("email") or acct.get("email_address"))
        except net.UpstreamError:
            cache["at"] = now_ts() - 5 * 3600  # retry in ~1h
    return _result(
        plan=plan or cache.get("plan"),
        account=cache.get("email") or tinfo.get("account"),
        windows=windows, metrics=metrics, notes=notes, balances=balances,
    )


# ============================================================================ ChatGPT / Codex
CODEX_USAGE = "https://chatgpt.com/backend-api/wham/usage"


def _codex_window(raw, fallback_key):
    raw = rec(raw)
    pct = num(raw.get("used_percent"), num(raw.get("usedPercent")))
    if pct is None:
        return None
    ws = num(raw.get("limit_window_seconds"), num(raw.get("window_seconds")))
    reset = parse_time(raw.get("reset_at") or raw.get("resetAt"))
    if reset is None:
        after = num(raw.get("reset_after_seconds"), num(raw.get("resetAfterSeconds")))
        if after is not None:
            reset = now_ts() + after
    ws = int(ws) if ws else None
    if ws and ws <= 6 * 3600:
        key, label = "session", "%d-hour window" % round(ws / 3600.0)
    elif ws and ws >= 6 * 86400:
        key, label = "weekly", "Weekly"
    else:
        key, label = fallback_key, ("5-hour window" if fallback_key == "session" else "Weekly")
    return _window(key, label, pct, reset, ws)


def parse_codex_usage(data):
    data = rec(data)
    rl = rec(data.get("rate_limit") or data.get("rateLimit"))
    windows = []
    for raw, fb in ((rl.get("primary_window") or rl.get("primaryWindow"), "session"),
                    (rl.get("secondary_window") or rl.get("secondaryWindow"), "weekly")):
        w = _codex_window(raw, fb)
        if w:
            windows.append(w)
    for extra in data.get("additional_rate_limits") or []:
        extra = rec(extra)
        name = extra.get("limit_name") or extra.get("metered_feature") or extra.get("name") or "extra"
        erl = rec(extra.get("rate_limit"))
        for raw, fb in ((erl.get("primary_window"), "session"), (erl.get("secondary_window"), "weekly")):
            w = _codex_window(raw, fb)
            if w:
                w["key"] = "%s_%s" % (name, w["key"])
                w["label"] = "%s · %s" % (name, w["label"])
                windows.append(w)
    balances, notes = [], []
    credits = rec(data.get("credits"))
    if credits:
        bal = num(credits.get("balance"))
        if credits.get("unlimited"):
            notes.append("Unlimited Codex credits.")
        elif bal is not None and (credits.get("has_credits") or bal > 0):
            balances.append({"key": "codex_credits", "label": "Codex credits (units)", "amount": bal, "currency": None})
    plan = data.get("plan_type") or data.get("planType")
    if rl.get("limit_reached") or rl.get("allowed") is False:
        notes.append("Rate limit currently reached.")
    return windows, balances, notes, plan


def fetch_codex(ctx):
    token, tinfo = ctx.oauth.access_token("codex")
    hdrs = {
        "Authorization": "Bearer " + token,
        "User-Agent": ctx.ua("codex"),
        "originator": "codex_cli_rs",
        "Version": ctx.ua("codex_version"),
    }
    if tinfo.get("account_id"):
        hdrs["chatgpt-account-id"] = tinfo["account_id"]
    try:
        data = net.get_json(CODEX_USAGE, headers=hdrs)
    except net.UpstreamError as e:
        if e.status == 403 and "<html" in (e.body or "").lower():
            raise net.UpstreamError(
                "chatgpt.com blocked the request (Cloudflare challenge) - HTTP 403", 403, "")
        raise
    windows, balances, notes, plan = parse_codex_usage(data)
    plan = plan or tinfo.get("plan")
    return _result(plan=(plan or "").title() or None, account=tinfo.get("account"),
                   windows=windows, balances=balances, notes=notes)


# ============================================================================ Antigravity
AG_BOOTSTRAP = "https://cloudcode-pa.googleapis.com"
AG_RUNTIME = ("https://daily-cloudcode-pa.googleapis.com", "https://cloudcode-pa.googleapis.com")


def _ag_post(path, body, hdrs):
    last = None
    for base in AG_RUNTIME:
        try:
            return net.post_json(base + path, body, headers=hdrs)
        except net.UpstreamError as e:
            last = e
            if e.status in (401, 403):
                raise
    raise last


def _ag_window_name(bucket):
    explicit = str(bucket.get("window") or "").strip().lower()
    if explicit in ("5h", "session"):
        return "session"
    if explicit in ("weekly", "7d"):
        return "weekly"
    text = ("%s %s" % (bucket.get("bucketId") or "", bucket.get("displayName") or "")).lower()
    if "5h" in text or "five" in text or "session" in text:
        return "session"
    if "weekly" in text or "7d" in text or "seven" in text:
        return "weekly"
    return None


def parse_ag_summary(data):
    root = rec(data)
    groups = root.get("groups")
    if not isinstance(groups, list):
        groups = rec(root.get("quotaSummary")).get("groups") or []
    windows = []
    for g in groups:
        g = rec(g)
        gname = str(g.get("displayName") or g.get("groupId") or g.get("id") or "Models").strip()
        for b in g.get("buckets") or []:
            b = rec(b)
            if b.get("disabled") is True:
                continue
            wn = _ag_window_name(b)
            if not wn:
                continue
            remaining = rec(b.get("remaining"))
            frac = num(remaining.get("remainingFraction"), num(b.get("remainingFraction")))
            if frac is None or frac < 0:
                continue
            reset = remaining.get("resetTime") or b.get("resetTime")
            if not reset and frac >= 1:
                continue  # unlimited bucket
            used = (1 - clamp(frac, 0.0, 1.0)) * 100
            label = "%s · %s" % (gname, "5-hour" if wn == "session" else "Weekly")
            key = "%s_%s" % (gname.lower().replace(" ", "_"), wn)
            windows.append(_window(key, label, used, reset, FIVE_H if wn == "session" else WEEK))
    return windows


def parse_ag_models(data):
    """Fallback: per-model quotaInfo from fetchAvailableModels (deduped by identical buckets)."""
    windows, seen = [], set()
    for mid, info in rec(rec(data).get("models")).items():
        info = rec(info)
        qi = rec(info.get("quotaInfo"))
        frac = num(qi.get("remainingFraction"))
        reset = qi.get("resetTime")
        if frac is None or not reset or info.get("isInternal"):
            continue
        sig = (round(frac, 4), reset)
        name = info.get("displayName") or mid
        if sig in seen:
            continue
        seen.add(sig)
        windows.append(_window("model_" + mid, name, (1 - clamp(frac, 0.0, 1.0)) * 100, reset, None))
    return windows[:8]


def fetch_antigravity(ctx):
    token, tinfo = ctx.oauth.access_token("antigravity")
    hdrs = {"Authorization": "Bearer " + token, "User-Agent": ctx.ua("antigravity")}
    project, plan = tinfo.get("project_id"), tinfo.get("plan")
    if not project or now_ts() - (tinfo.get("project_checked_at") or 0) > 6 * 3600:
        lca = net.post_json(AG_BOOTSTRAP + "/v1internal:loadCodeAssist",
                            {"metadata": {"ideType": "ANTIGRAVITY"}}, headers=hdrs)
        proj = lca.get("cloudaicompanionProject")
        if isinstance(proj, dict):
            proj = proj.get("id")
        tier = rec(lca.get("paidTier")) or rec(lca.get("currentTier"))
        plan = tier.get("name") or tier.get("id") or plan
        project = proj or project
        ctx.oauth.update_extra("antigravity", project_id=project, plan=plan, project_checked_at=now_ts())
    if not project:
        raise net.UpstreamError("Antigravity: this Google account has no Code Assist project (open Antigravity once to onboard).")
    windows, notes = [], []
    try:
        windows = parse_ag_summary(_ag_post("/v1internal:retrieveUserQuotaSummary", {"project": project}, hdrs))
    except net.UpstreamError as e:
        if e.status in (401, 403):
            raise
        notes.append("Quota summary unavailable (%s); showing per-model quotas." % e.short())
    if not windows:
        windows = parse_ag_models(_ag_post("/v1internal:fetchAvailableModels", {"project": project}, hdrs))
    return _result(plan=plan, account=tinfo.get("account"), windows=windows, notes=notes)


# ============================================================================ OpenCode Go
OPENCODE_USAGE = "https://opencode.ai/zen/go/v1/usage"
OPENCODE_CAPS = {"rolling": ("session", "5-hour ($12)", FIVE_H), "weekly": ("weekly", "Weekly ($30)", WEEK),
                 "monthly": ("monthly", "Monthly ($60)", 31 * 86400)}


def opencode_session_id(seed_store):
    """Stable 'ses_' id in the canonical shape OpenCode expects on background calls."""
    sid = seed_store.get("opencode_session_id")
    if sid:
        return sid
    hexd = "0123456789abcdef"
    rnd = random.SystemRandom()
    time_part = "".join(rnd.choice(hexd) for _ in range(9)) + "f" + rnd.choice("cdef") + rnd.choice(hexd)
    tail = "".join(rnd.choice(string.ascii_letters + string.digits) for _ in range(14))
    sid = "ses_" + time_part + tail
    seed_store.set_config_value("opencode_session_id", sid)
    return sid


def parse_opencode(data):
    usage = rec(rec(data).get("usage"))
    windows = []
    for k, (key, label, ws) in OPENCODE_CAPS.items():
        w = rec(usage.get(k))
        pct = num(w.get("percent"))
        if pct is None:
            continue
        if w.get("status") == "rate-limited":
            pct = 100.0
        windows.append(_window(key, label, pct, w.get("resetsAt"), ws))
    return windows


def fetch_opencode(ctx):
    key = ctx.store.secret("opencode_api_key")
    if not key:
        raise net.UpstreamError("Add your OpenCode API key in Settings.")
    data = net.get_json(OPENCODE_USAGE, headers={
        "Authorization": "Bearer " + key.strip(),
        "User-Agent": ctx.ua("opencode"),
        "x-opencode-session": opencode_session_id(ctx.store),
        "x-opencode-client": "desktop",
        "x-opencode-project": "global",
    })
    windows = parse_opencode(data)
    if not windows:
        raise net.UpstreamError("OpenCode usage response had no windows", 200, str(data)[:200])
    # The usage endpoint reports no plan name or price (only windows), so base Go is assumed.
    # Go Plus users: type your amount in Settings (it always overrides the list price).
    plan = "OpenCode Go Plus" if "plus" in str(data.get("plan") or data.get("planName") or "").lower() else "OpenCode Go"
    return _result(plan=plan, windows=windows)


# ============================================================================ OpenRouter
OPENROUTER = "https://openrouter.ai/api/v1"


def fetch_openrouter(ctx):
    key = ctx.store.secret("openrouter_api_key")
    if not key:
        raise net.UpstreamError("Add your OpenRouter API key in Settings.")
    hdrs = {"Authorization": "Bearer " + key.strip()}
    metrics, balances, windows, notes = {}, [], [], []
    errors = []
    try:
        k = rec(net.get_json(OPENROUTER + "/key", headers=hdrs).get("data"))
        metrics.update(
            key_usage_day=num(k.get("usage_daily"), 0.0),
            key_usage_week=num(k.get("usage_weekly"), 0.0),
            key_usage_month=num(k.get("usage_monthly"), 0.0),
            key_usage_total=num(k.get("usage"), 0.0),
        )
        limit, rem = num(k.get("limit")), num(k.get("limit_remaining"))
        if limit:
            windows.append(_window("key_limit", "Key spend cap ($%.0f)" % limit,
                                   100.0 * (limit - (rem if rem is not None else limit)) / limit,
                                   k.get("limit_reset"), None))
        if k.get("is_free_tier"):
            notes.append("Free tier (no credits purchased).")
    except net.UpstreamError as e:
        errors.append(e)
    try:
        c = rec(net.get_json(OPENROUTER + "/credits", headers=hdrs).get("data"))
        tc, tu = num(c.get("total_credits")), num(c.get("total_usage"))
        if tc is not None and tu is not None:
            metrics.update(credits_total=tc, credits_used=tu)
            balances.append({"key": "credits", "label": "Credit balance", "amount": round(tc - tu, 4), "currency": "USD"})
            if tc > 0:
                windows.append(_window("credits", "Credits used (lifetime)", 100.0 * tu / tc, None, None))
    except net.UpstreamError as e:
        errors.append(e)
    if len(errors) == 2:
        raise errors[0]
    return _result(plan="Pay-as-you-go", windows=windows, balances=balances, metrics=metrics, notes=notes)


# ============================================================================ Moonshot (Kimi API)
def fetch_moonshot(ctx):
    key = ctx.store.secret("moonshot_api_key")
    if not key:
        raise net.UpstreamError("Add your Moonshot (platform.moonshot.ai) API key in Settings.")
    base = (ctx.store.get("moonshot_base_url") or "https://api.moonshot.ai/v1").rstrip("/")
    data = net.get_json(base + "/users/me/balance", headers={"Authorization": "Bearer " + key.strip()})
    if data.get("code") not in (0, None):
        raise net.UpstreamError("Moonshot error code %s" % data.get("code"), 200, str(data)[:200])
    d = rec(data.get("data"))
    cur = "CNY" if "moonshot.cn" in base else "USD"
    avail = num(d.get("available_balance"), 0.0)
    balances = [
        {"key": "available", "label": "Available", "amount": avail, "currency": cur},
        {"key": "cash", "label": "Cash", "amount": num(d.get("cash_balance"), 0.0), "currency": cur},
        {"key": "voucher", "label": "Voucher", "amount": num(d.get("voucher_balance"), 0.0), "currency": cur},
    ]
    notes = ["Balance exhausted - API calls will fail."] if avail <= 0 else []
    return _result(plan="Prepaid", balances=balances, metrics={"balance": avail, "balance_currency": cur}, notes=notes)


# ============================================================================ Kimi Code (subscription)
_KIMI_UNITS = {"TIME_UNIT_MINUTE": 60, "TIME_UNIT_HOUR": 3600, "TIME_UNIT_DAY": 86400, "TIME_UNIT_WEEK": 7 * 86400}


def _kimi_count(detail):
    d = rec(detail)
    limit = num(d.get("limit"), num(d.get("Limit")))
    if not limit or limit <= 0:
        return None
    used = num(d.get("used"), num(d.get("Used")))
    if used is None:
        rem = num(d.get("remaining"), num(d.get("Remaining")))
        used = limit - rem if rem is not None else 0.0
    return 100.0 * used / limit, d.get("resetTime") or d.get("reset_at") or d.get("resetAt")


def parse_kimi_code(data):
    data = rec(data)
    windows, seen = [], set()
    for i, item in enumerate(data.get("limits") or []):
        item = rec(item)
        c = _kimi_count(item.get("detail"))
        if not c:
            continue
        w = rec(item.get("window"))
        dur, unit = num(w.get("duration")), w.get("timeUnit")
        ws = int(dur * _KIMI_UNITS.get(unit, 0)) if dur else None
        if ws and ws % 86400 == 0:
            label = "%d-day" % (ws // 86400)
        elif ws and ws % 3600 == 0:
            label = "%d-hour" % (ws // 3600)
        else:
            label = item.get("name") or "Limit %d" % (i + 1)
        key = "kimi_%s" % (ws or i)
        seen.add(ws)
        windows.append(_window(key, label, c[0], c[1], ws))
    usages = rec(data.get("usages"))
    for src, label, ws in (("limit_5h", "5-hour", FIVE_H), ("limit_7d", "7-day", WEEK), ("limit_month_total", "Monthly", 30 * 86400)):
        u = rec(usages.get(src))
        ratio = num(u.get("used_ratio"), num(u.get("usedRatio")))
        if ratio is None or ws in seen:
            continue
        seen.add(ws)
        windows.append(_window("kimi_%s" % src, label, ratio * 100.0, u.get("reset_time") or u.get("resetTime"), ws))
    if WEEK not in seen:
        c = _kimi_count(data.get("usage"))
        if c:
            windows.append(_window("kimi_7d", "7-day", c[0], c[1], WEEK))
    return windows


KIMI_HOSTS = ("https://api.kimi.com", "https://api.kimi.ai")  # kimi.com (China/global) and kimi.ai (international)


def fetch_kimi_code(ctx):
    key = ctx.store.secret("kimi_code_api_key")
    if not key:
        raise net.UpstreamError("Add your Kimi Code API key in Settings.")
    key = key.strip()
    cache = ctx.cache.setdefault("kimi_profile", {})
    # Keys are tied to the account's region: try the host that worked last, then the other one.
    hosts = sorted(KIMI_HOSTS, key=lambda h: h != cache.get("host"))
    data, last = None, None
    for host in hosts:
        try:
            data = net.get_json(host + "/coding/v1/usages", headers={"x-api-key": key, "Authorization": "Bearer " + key,
                                                                    "User-Agent": ctx.ua("kimi_code")})
            cache["host"] = host
            break
        except net.UpstreamError as e:
            last = e
            if e.status not in (401, 403, 404):
                raise
    if data is None:
        raise net.UpstreamError("Kimi Code rejected the key on both api.kimi.com and api.kimi.ai - it must be a Kimi Code "
                                "console key (kimi.com/code/console), not a platform.moonshot.ai key. Last: %s" % last.short(),
                                last.status, last.body)
    windows = parse_kimi_code(data)
    plan = None
    if now_ts() - cache.get("at", 0) > 6 * 3600:
        try:
            prof = net.get_json(cache["host"] + "/coding/v1/me", headers={"x-api-key": key, "Authorization": "Bearer " + key})
            cache.update(at=now_ts(), plan=(prof.get("user_level_name") or "").strip() or None)
        except net.UpstreamError:
            cache["at"] = now_ts() - 5 * 3600
    plan = cache.get("plan") or "Kimi Code"
    return _result(plan=plan, windows=windows)


# ============================================================================ Gemini API key
GEMINI_MODELS = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=50"


def fetch_gemini(ctx):
    key = ctx.store.secret("gemini_api_key")
    if not key:
        raise net.UpstreamError("Add your Gemini API key in Settings (optional).")
    data = net.get_json(GEMINI_MODELS, headers={"x-goog-api-key": key.strip()})
    count = len(data.get("models") or [])
    return _result(
        plan="API key",
        metrics={"models_available": count},
        notes=["Key valid - %d models available." % count,
               "Google has no usage/spend API for keys: add Gemini API spend as a manual entry under Costs."],
    )


FETCHERS = {
    "claude": fetch_claude,
    "codex": fetch_codex,
    "antigravity": fetch_antigravity,
    "opencode": fetch_opencode,
    "openrouter": fetch_openrouter,
    "moonshot": fetch_moonshot,
    "kimi_code": fetch_kimi_code,
    "gemini": fetch_gemini,
}
