"""Monthly tab: subscriptions + credit spend across every platform, with a per-platform breakdown.

Headline TOTAL = subscriptions + credit spend (money actually consumed this month).
Top-ups (credits bought) and remaining balances are shown in their own columns but NOT added
to the total - adding "bought" and "spent" would count the same money twice. The UI can switch
to a cash-flow view (subscriptions + top-ups) using `total_cashflow`.
"""
import time

from .providers import PLATFORMS, PLATFORM_NAMES
from .util import month_key, month_start_ts

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
        worst = None
        for w in data.get("windows") or []:
            if w.get("used_pct") is None or w.get("key") in LIFETIME_WINDOWS:
                continue
            if worst is None or w["used_pct"] > worst["used_pct"]:
                worst = w

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
            "usage_pct": worst["used_pct"] if worst else None,
            "usage_label": worst["label"] if worst else None,
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
