"""Monthly tab: subscriptions + credit spend across every platform, with a per-platform breakdown.

Headline TOTAL = subscriptions + credit spend (money actually consumed this month).
Top-ups (credits bought) and remaining balances are shown in their own columns but NOT added
to the total - adding "bought" and "spent" would count the same money twice. The UI can switch
to a cash-flow view (subscriptions + top-ups) using `total_cashflow`.
"""
import time

from .providers import PLATFORMS, PLATFORM_NAMES
from .util import month_key, month_start_ts, now_ts

LIFETIME_WINDOWS = ("credits",)

# Manual entry types (e.g. from invoices) -> default label.
MANUAL_KINDS = {
    "subscription": "Subscription charge",  # replaces the recurring subscription for that month
    "spend": "Credit / usage spend",
    "topup": "Credits bought",
    "credit": "Credits bought & used",      # counts in both Bought and Spend
}

# Published monthly list prices for plans the dashboard can detect from the provider's API.
# Used only when a subscription's amount is left at 0 ("auto"); any amount you type wins.
# GBP = UK prices incl. VAT. Edit here if prices change.
LIST_PRICES = {
    "claude": {"pro": {"USD": 20, "GBP": 18}, "max 5x": {"USD": 100, "GBP": 90}, "max 20x": {"USD": 200, "GBP": 180}},
    "codex": {"plus": {"USD": 20, "GBP": 20}, "pro": {"USD": 200, "GBP": 200}},
}


def list_price_from_map(platform, plan, price_map):
    """Public price map {"Go": 10.0, "Go Plus": 40.0} -> (amount, "USD"), matched by plan name."""
    if not price_map:
        return None
    plan = (plan or "").strip().lower()
    aliases = {"opencode": {"go": "go", "go plus": "go plus", "opencode go": "go", "opencode go plus": "go plus"}}
    plan = (aliases.get(platform) or {}).get(plan, plan)
    for name, value in price_map.items():
        if name.strip().lower() == plan and float(value) > 0:
            return float(value), "USD"
    return None


def list_price(platform, plan, display_currency, price_map=None):
    """(amount, currency) for a detected plan, or None when its price isn't published anywhere."""
    if price_map:
        public = list_price_from_map(platform, plan, price_map)
        if public:
            return public
    prices = (LIST_PRICES.get(platform) or {}).get((plan or "").strip().lower())
    if not prices:
        return None
    cur = "GBP" if display_currency == "GBP" else "USD"
    return float(prices[cur]), cur


LOW_PCT = 80.0      # a quota window at/over this is "nearly spent" (amber)
LOW_CREDIT = 5.0    # a prepaid balance under this (display currency) is "nearly spent" (amber)
_RANK = {"green": 0, "amber": 1, "red": 2}


def _live_pct(w, now):
    """Used % of a window; one whose reset time has already passed counts as reset (0%)."""
    ra = w.get("resets_at")
    if ra and ra <= now:
        return 0.0
    return w.get("used_pct") or 0.0


def _length(w):
    # Monthly outranks weekly outranks 5h; caps with no period (spend caps) outrank everything.
    return w.get("window_s") or float("inf")


def _longest_at(windows, threshold, now):
    """The longest window at/over `threshold` (monthly, then weekly, then 5h)."""
    hits = [w for w in windows if _live_pct(w, now) >= threshold]
    return max(hits, key=_length) if hits else None


def _blocker(windows, now):
    """The maxed-out window that clears LAST: if the monthly is spent, the 5h reset is irrelevant."""
    hits = [w for w in windows if _live_pct(w, now) >= 100.0]
    return max(hits, key=lambda w: (w.get("resets_at") or float("inf"), _length(w))) if hits else None


def _st(level, label, detail, w=None, short=None):
    # short: a few words shown next to the platform name in the hero's "Available now" line
    return {"level": level, "label": label, "detail": detail, "short": short,
            "resets_at": w.get("resets_at") if w else None,
            "reset_label": w.get("label") if w else None}


def _pool_name(name):
    """'Gemini Models' -> 'Gemini', 'Claude and GPT models' -> 'Claude and GPT'."""
    name = (name or "").strip()
    return name[:-7].strip() if name.lower().endswith(" models") else name


def _period(w):
    """'5h' / 'weekly' / 'monthly' for a window (prefixed with its model pool), else its label."""
    ws = w.get("window_s")
    if ws and ws <= 6 * 3600:
        p = "%dh" % round(ws / 3600.0)
    elif ws and 6 * 86400 <= ws <= 8 * 86400:
        p = "weekly"
    elif ws and ws >= 28 * 86400:
        p = "monthly"
    else:
        return w.get("label") or "limit"
    return ("%s %s" % (_pool_name(w["pool"]), p)) if w.get("pool") else p


def limit_status(windows, now):
    """Traffic light + the one reset that matters, from a platform's quota windows.

    Windows tagged with a `pool` (a model family: Antigravity's Gemini vs Claude & GPT, Claude's
    Fable weekly, Codex Spark) are separate allowances that ALSO count against the untagged
    (all-models) windows. The platform is red only when every pool is blocked.
    """
    quota = [w for w in windows or [] if w.get("key") not in LIFETIME_WINDOWS and w.get("used_pct") is not None]
    if not quota:
        return None
    generic = [w for w in quota if not w.get("pool")]
    names = []
    for w in quota:
        if w.get("pool") and w["pool"] not in names:
            names.append(w["pool"])
    pools = ([("", generic)] if generic else []) + [(n, generic + [w for w in quota if w.get("pool") == n]) for n in names]
    blocked = [(n, _blocker(ws, now)) for n, ws in pools]
    blocked = [(n, w) for n, w in blocked if w]

    def soonest(ws):
        known = [w for w in ws if w.get("resets_at")]
        return min(known, key=lambda w: w["resets_at"]) if known else (ws[0] if ws else None)

    if blocked and len(blocked) == len(pools):
        w = soonest([w for _, w in blocked])  # back as soon as ANY pool comes back
        return _st("red", "Inactive", "%s at 100%%" % w.get("label"), w)
    if blocked:
        w = soonest([w for _, w in blocked])
        gone = [n for n, _ in blocked]
        left = [n for n, _ in pools if n not in gone]
        if all(left):  # only named model groups still work: say which
            short = " & ".join(_pool_name(n) for n in left) + " only"
        else:
            short = " & ".join(_pool_name(n) for n in gone) + " used up"
        return _st("amber", "Limited", ", ".join(n or "All models" for n in gone) + " used up - " +
                   ", ".join(n or "other models" for n in left) + " still available", w, short)
    w = _longest_at(quota, LOW_PCT, now)
    if w:
        pct = round(_live_pct(w, now))
        return _st("amber", "Low", "%s at %d%%" % (w.get("label"), pct), w, "%d%% %s" % (pct, _period(w)))
    upcoming = [w for w in quota if _live_pct(w, now) > 0 and (w.get("resets_at") or 0) > now]
    w = min(upcoming, key=lambda w: w["resets_at"]) if upcoming else None
    return _st("green", "Active", "All limits below %d%%" % LOW_PCT, w)


def credit_status(balance):
    """Prepaid balance (display currency) -> traffic light. No reset: credit only comes back by topping up."""
    if balance is None:
        return None
    if balance <= 0.01:
        return _st("red", "Inactive", "No credit left - top up")
    if balance < LOW_CREDIT:
        return _st("amber", "Low", "Credit nearly spent", short="credit low")
    return _st("green", "Active", "Credit available")


def platform_status(tile, windows, balance, now):
    """One status per platform for the breakdown table, or None when there's nothing to say."""
    st = (tile or {}).get("status")
    if not tile or st in (None, "unconfigured", "disabled", "pending"):
        return None
    if st != "ok":
        label = {"stale": "Stale", "relogin": "Log in again", "error": "Error"}.get(st, st.title())
        return _st("grey", label, (tile.get("error") or "Showing no live status") if st != "stale" else "Last refresh failed")
    lim = limit_status(windows, now)
    cred = credit_status(balance)
    periodic = any(w.get("window_s") for w in windows or [] if w.get("key") not in LIFETIME_WINDOWS)
    if lim and periodic:
        # Subscription quotas decide - but quotas spent with prepaid credit left (Claude extra
        # usage) still works, just on paid credit until the reset.
        if lim["level"] == "red" and cred and cred["level"] != "red":
            return dict(lim, level="amber", label="On credit", short="on credit",
                        detail="%s - running on prepaid credit until the reset" % lim["detail"])
        return lim  # an extra-usage balance never makes a working subscription look "inactive"
    if lim and cred:  # e.g. OpenRouter: a key spend cap and a credit balance - the worse one wins
        return lim if _RANK[lim["level"]] >= _RANK[cred["level"]] else cred
    # Connected but nothing to measure (e.g. a Gemini API key: Google exposes no usage, limit or
    # balance for keys). The poll itself proves the key works, so it's usable.
    return lim or cred or _st("green", "Active", "Connected and working - this platform reports no usage "
                                                  "or limits, so there's nothing to run out of as far as the dashboard can see")


def _fmt_date(ts):
    return time.strftime("%d %b", time.localtime(ts))


def compute(month, cfg, state, counters, fx):
    """Return the monthly tab for `month` ('YYYY-MM') in the configured display currency."""
    cur = (cfg.get("display_currency") or "GBP").upper()
    is_current = month == month_key()
    mstart = month_start_ts()

    def conv(amount, src):
        v = fx.convert(amount, src, cur)
        return round(v, 2) if v is not None else None

    rows = []
    price_map = ((cfg.get("price_data") or {}).get("opencode") or {}).get("plans")
    known = [p[0] for p in PLATFORMS]
    platforms = list(known)
    for e in cfg.get("manual_entries") or []:
        if e.get("platform") and e["platform"] not in platforms:
            platforms.append(e["platform"])

    for p in platforms:
        # Automatic figures and your manual entries are collected per column; for each column,
        # manual entries for this platform+month REPLACE the automatic figure (invoices beat
        # estimates), so nothing is ever counted twice.
        auto = {"subscription": [], "spend": [], "topup": []}  # recurring subs count as "automatic"
        detected = (cfg.get("detected_plans") or {}).get(p) or {}
        for s in cfg.get("subscriptions") or []:
            if s.get("platform") != p or not subscription_applies(s, month):
                continue
            amt = float(s.get("amount") or 0)
            s_cur = (s.get("currency") or cur).upper()
            label = s.get("name") or "Subscription"
            if amt <= 0 and s.get("auto_price", True) and detected.get("plan") \
                    and month >= (detected.get("since") or "0000-00"):
                lp = list_price(p, detected["plan"], cur, price_map)
                if lp:
                    amt, s_cur = lp
                    brand = PLATFORM_NAMES.get(p, p).split(" /")[0]
                    name = detected["plan"] if detected["plan"].lower().startswith(brand.lower()) else "%s %s" % (brand, detected["plan"])
                    label = "%s (list price, auto-detected - set your own in Settings)" % name
            if amt <= 0:
                continue
            c = conv(amt, s_cur)
            if c is None:
                continue
            auto["subscription"].append({"kind": "subscription", "label": label,
                                         "amount": c, "original": "%.2f %s" % (amt, s_cur)})

        tile = (state.get(p) or {}) if is_current else {}
        data = tile.get("data") or {}
        metrics = data.get("metrics") or {}
        pc = counters.get(p) or {}

        # ---- automatic credit spend --------------------------------------------------
        if p == "openrouter":
            tracked = pc.get("spend")
            # Both figures are persisted per month by the poller, so past months stay complete.
            key_month = (pc.get("key_month") or {}).get("value")
            t_val = tracked["value"] if tracked else 0.0
            if key_month is not None and key_month > t_val:
                c = conv(key_month, "USD")
                auto["spend"].append({"kind": "spend", "label": "Credit spend (this API key, calendar month UTC)",
                                      "amount": c, "original": "$%.2f" % key_month})
            elif tracked and t_val > 0:
                c = conv(t_val, tracked["currency"])
                auto["spend"].append({"kind": "spend", "label": _since("Credit spend (account-wide)", tracked, mstart),
                                      "amount": c, "original": "$%.2f" % t_val})
        elif p == "moonshot":
            tracked = pc.get("spend")
            if tracked and tracked["value"] > 0:
                c = conv(tracked["value"], tracked["currency"])
                auto["spend"].append({"kind": "spend", "label": _since("Balance used", tracked, mstart),
                                      "amount": c, "original": "%.2f %s" % (tracked["value"], tracked["currency"])})
        elif p == "claude":
            # Per-month share of extra usage, accumulated by the poller (handles cycle resets).
            ctr = pc.get("extra_usage") or {}
            v = ctr.get("value") or 0.0
            if v > 0:
                ccy = ctr.get("currency") or "USD"
                c = conv(v, ccy)
                lim = metrics.get("extra_usage_limit")
                auto["spend"].append({"kind": "spend",
                                      "label": "Extra usage (pay-as-you-go, tracked from API%s)" % (
                                          ", cap %.0f %s" % (lim, ccy) if lim else ""),
                                      "amount": c, "original": "%.2f %s" % (v, ccy)})

        tracked_top = pc.get("topup")
        if tracked_top and tracked_top["value"] > 0:
            c = conv(tracked_top["value"], tracked_top["currency"])
            auto["topup"].append({"kind": "topup", "label": "Credits bought (detected)", "amount": c,
                                  "original": "%.2f %s" % (tracked_top["value"], tracked_top["currency"])})

        # ---- manual entries (invoices) -------------------------------------------------
        manual = {"subscription": [], "spend": [], "topup": []}
        for e in cfg.get("manual_entries") or []:
            if e.get("platform") != p or e.get("month") != month:
                continue
            amt = float(e.get("amount") or 0)
            c = conv(amt, e.get("currency"))
            if c is None:
                continue
            kind = e.get("kind") if e.get("kind") in MANUAL_KINDS else "spend"
            base = {"label": e.get("note") or MANUAL_KINDS[kind], "amount": c, "manual_id": e.get("id"),
                    "original": "%.2f %s" % (amt, (e.get("currency") or cur).upper())}
            # "credit" = prepaid credit bought and used up: it is both a purchase and spend.
            for col in (("topup", "spend") if kind == "credit" else (kind,)):
                manual[col].append(dict(base, kind=col))

        # ---- per column: manual entries win over automatic figures ----------------------
        items, sums = [], {}
        for col in ("subscription", "spend", "topup"):
            use = manual[col] if manual[col] else auto[col]
            if manual[col]:
                for it in auto[col]:
                    items.append(dict(it, kind="info", amount=None,
                                      label="%s = %s - not counted, your entries for this month replace it" % (
                                          it["label"], it.get("original"))))
            items.extend(use)
            sums[col] = sum(it["amount"] or 0 for it in use)
        subs, spend, topups = sums["subscription"], sums["spend"], sums["topup"]

        # ---- balances + usage ---------------------------------------------------------
        balance = None
        for b in data.get("balances") or []:
            if b.get("key") in ("credits", "available") and b.get("currency"):
                balance = conv(b.get("amount"), b["currency"])
        status = platform_status(tile, data.get("windows") or [], balance, now_ts())

        total = subs + spend
        if total == 0 and topups == 0 and balance is None and not data and p not in known:
            continue
        rows.append({
            "platform": p,
            "name": PLATFORM_NAMES.get(p, p.title()),
            "subscription": round(subs, 2),
            "credit_spend": round(spend, 2),
            "total": round(total, 2),
            "topups": round(topups, 2),
            "balance_left": balance,
            "status": status,
            "items": items,
        })

    totals = {
        "subscriptions": round(sum(r["subscription"] for r in rows), 2),
        "credit_spend": round(sum(r["credit_spend"] for r in rows), 2),
        "topups": round(sum(r["topups"] for r in rows), 2),
    }
    totals["total"] = round(totals["subscriptions"] + totals["credit_spend"], 2)
    totals["total_cashflow"] = round(totals["subscriptions"] + totals["topups"], 2)
    for r in rows:
        r["share"] = round(100.0 * r["total"] / totals["total"], 1) if totals["total"] else 0.0
    rows.sort(key=lambda r: (-r["total"], r["name"]))
    return {"month": month, "currency": cur, "rows": rows, "totals": totals}


def subscription_applies(sub, month):
    """A subscription counts in every month from start_month to end_month (inclusive)."""
    if not sub.get("active", True) and not sub.get("end_month"):
        return False
    start = sub.get("start_month") or "0000-00"
    end = sub.get("end_month")
    return start <= month and (not end or month <= end)


def history(months, cfg, counters_for, fx, live_state=None):
    """Totals for several months (oldest first) - used for the trend chart.

    The current month uses the live provider state (e.g. Claude extra usage, OpenRouter key spend)
    so its bar always matches the headline total."""
    out = []
    current = month_key()
    for m in months:
        tab = compute(m, cfg, (live_state or {}) if m == current else {}, counters_for(m), fx)
        t = tab["totals"]
        out.append({"month": m, "subscriptions": t["subscriptions"], "credit_spend": t["credit_spend"],
                    "topups": t["topups"], "total": t["total"]})
    return out


def _since(label, counter, month_start):
    fs = counter.get("first_seen")
    if fs and fs - month_start > 86400:
        return "%s since %s (tracking started)" % (label, _fmt_date(fs))
    return label
