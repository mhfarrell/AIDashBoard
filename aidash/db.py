"""SQLite history: usage snapshots (sparklines), monthly spend/top-up counters, FX rates.

SD-card friendly: snapshots are written at most every SNAPSHOT_EVERY seconds per platform,
WAL mode, and old rows are pruned daily.
"""
import json
import re
import sqlite3
import threading
import time

from .util import month_key, now_ts, num

SNAPSHOT_EVERY = 15 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  platform TEXT NOT NULL, ts REAL NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_snap ON snapshots(platform, ts);
CREATE TABLE IF NOT EXISTS counters (
  platform TEXT NOT NULL, month TEXT NOT NULL, kind TEXT NOT NULL,
  currency TEXT NOT NULL, value REAL NOT NULL DEFAULT 0, first_seen REAL,
  PRIMARY KEY (platform, month, kind)
);
CREATE TABLE IF NOT EXISTS last_values (
  platform TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL, ts REAL NOT NULL,
  PRIMARY KEY (platform, metric)
);
CREATE TABLE IF NOT EXISTS kv (
  k TEXT PRIMARY KEY, v TEXT NOT NULL, ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fx (
  day TEXT PRIMARY KEY, base TEXT NOT NULL, rates TEXT NOT NULL, source TEXT, ts REAL NOT NULL
);
"""


class History(object):
    def __init__(self, path):
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._last_snap = {}
        self._last_prune = 0

    def _exec(self, sql, args=()):
        with self._lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def _query(self, sql, args=()):
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    # ------------------------------------------------------------- snapshots
    def maybe_snapshot(self, platform, windows, force=False):
        now = now_ts()
        if not force and now - self._last_snap.get(platform, 0) < SNAPSHOT_EVERY:
            return
        compact = dict((w["key"], w["used_pct"]) for w in windows if w.get("used_pct") is not None)
        if not compact:
            return
        self._last_snap[platform] = now
        self._exec("INSERT INTO snapshots(platform, ts, data) VALUES (?,?,?)",
                   (platform, now, json.dumps(compact, separators=(",", ":"))))

    def sparks(self, since_s=48 * 3600):
        """{platform: {window_key: [[ts, pct], ...]}} for the last `since_s` seconds."""
        out = {}
        rows = self._query("SELECT platform, ts, data FROM snapshots WHERE ts >= ? ORDER BY ts",
                           (now_ts() - since_s,))
        for platform, ts, data in rows:
            try:
                d = json.loads(data)
            except ValueError:
                continue
            p = out.setdefault(platform, {})
            for k, v in d.items():
                p.setdefault(k, []).append([int(ts), v])
        return out

    # ------------------------------------------------------------- counters
    def track_cumulative(self, platform, metric, value, kind_up, currency, kind_down=None):
        """Turn a cumulative/balance metric into monthly counters.

        value increases -> add the delta to `kind_up` (e.g. 'spend' for usage totals, 'topup' for balances)
        value decreases -> add |delta| to `kind_down` when given (e.g. 'spend' for balances)
        The first observation only sets the baseline.
        """
        if value is None:
            return
        now = now_ts()
        month = month_key(now)
        rows = self._query("SELECT value FROM last_values WHERE platform=? AND metric=?", (platform, metric))
        if rows and abs(float(value) - float(rows[0][0])) < 1e-9:
            self._ensure_counter(platform, month, kind_up, currency)  # month rollover; no-op otherwise
            return  # unchanged: skip the write (SD card)
        self._exec("INSERT OR REPLACE INTO last_values(platform, metric, value, ts) VALUES (?,?,?,?)",
                   (platform, metric, float(value), now))
        self._ensure_counter(platform, month, kind_up, currency)
        if kind_down:
            self._ensure_counter(platform, month, kind_down, currency)
        if not rows:
            return
        delta = float(value) - float(rows[0][0])
        if delta > 0:
            self._add(platform, month, kind_up, currency, delta)
        elif kind_down:
            self._add(platform, month, kind_down, currency, -delta)

    def track_period_total(self, platform, metric, value, kind, currency):
        """For provider figures that count up within a billing period and then reset
        (Claude extra usage, OpenRouter key 'usage_monthly').

        first sighting -> the whole value belongs to this month
        increase       -> add the delta
        decrease       -> the period reset: the new value is fresh spend
        """
        if value is None:
            return
        now = now_ts()
        month = month_key(now)
        rows = self._query("SELECT value FROM last_values WHERE platform=? AND metric=?", (platform, metric))
        prev = float(rows[0][0]) if rows else None
        v = float(value)
        if prev is not None and abs(v - prev) < 1e-9:
            return  # unchanged: skip the write (SD card)
        self._exec("INSERT OR REPLACE INTO last_values(platform, metric, value, ts) VALUES (?,?,?,?)",
                   (platform, metric, v, now))
        self._ensure_counter(platform, month, kind, currency)
        if prev is None or v < prev - 1e-9:
            add = v
        else:
            add = v - prev
        if add > 1e-9:
            self._add(platform, month, kind, currency, add)

    def _ensure_counter(self, platform, month, kind, currency):
        self._exec("INSERT OR IGNORE INTO counters(platform, month, kind, currency, value, first_seen) VALUES (?,?,?,?,0,?)",
                   (platform, month, kind, currency, now_ts()))

    def _add(self, platform, month, kind, currency, amount):
        self._ensure_counter(platform, month, kind, currency)
        self._exec("UPDATE counters SET value = value + ? WHERE platform=? AND month=? AND kind=?",
                   (amount, platform, month, kind))

    def all_counters(self):
        return [{"platform": p, "month": m, "kind": k, "currency": c, "value": v, "first_seen": f}
                for p, m, k, c, v, f in self._query(
                    "SELECT platform, month, kind, currency, value, first_seen FROM counters")]

    def merge_counters(self, rows):
        """Merge spend/top-up counters from another install of this dashboard (push_to_pi).

        Counters are running totals for a month, so the larger value wins: an install that has
        been watching longer has seen everything the newer one has, plus what came before it.
        """
        merged = 0
        for r in rows:
            try:
                p, m, k, c = str(r["platform"])[:32], str(r["month"])[:7], str(r["kind"])[:32], str(r["currency"])[:3].upper()
                v = float(r["value"])
            except (KeyError, TypeError, ValueError):
                continue
            if v < 0 or not re.match(r"^\d{4}-\d{2}$", m):
                continue
            cur = self._query("SELECT value, first_seen FROM counters WHERE platform=? AND month=? AND kind=?", (p, m, k))
            fs = num(r.get("first_seen"))
            if not cur:
                self._exec("INSERT INTO counters(platform, month, kind, currency, value, first_seen) VALUES (?,?,?,?,?,?)",
                           (p, m, k, c, v, fs or now_ts()))
                merged += 1
            elif v > cur[0][0] + 1e-9:
                first = min(x for x in (cur[0][1], fs) if x) if (cur[0][1] or fs) else now_ts()
                self._exec("UPDATE counters SET value=?, first_seen=? WHERE platform=? AND month=? AND kind=?", (v, first, p, m, k))
                merged += 1
        self._months_cache = (0, [])
        return merged

    def counters(self, month):
        """{platform: {kind: {"value","currency","first_seen"}}}"""
        out = {}
        for platform, kind, currency, value, first_seen in self._query(
                "SELECT platform, kind, currency, value, first_seen FROM counters WHERE month=?", (month,)):
            out.setdefault(platform, {})[kind] = {"value": value, "currency": currency, "first_seen": first_seen}
        return out

    _months_cache = (0, [])

    def months_with_data(self):
        """Months with any recorded data. Cheap: counters are tiny, and snapshots are reduced to
        their MIN/MAX timestamp (indexed) - every month in between is filled in. Cached 10 min."""
        at, cached = self._months_cache
        if now_ts() - at < 600:
            return cached
        out = set(m for (m,) in self._query("SELECT DISTINCT month FROM counters") if m)
        lo, hi = self._query("SELECT MIN(ts), MAX(ts) FROM snapshots")[0]
        if lo and hi:
            y, mth = [int(x) for x in time.strftime("%Y-%m", time.localtime(lo)).split("-")]
            end = time.strftime("%Y-%m", time.localtime(hi))
            while True:
                key = "%04d-%02d" % (y, mth)
                out.add(key)
                if key >= end:
                    break
                mth += 1
                if mth == 13:
                    y, mth = y + 1, 1
        self._months_cache = (now_ts(), sorted(out))
        return self._months_cache[1]
    def kv_get(self, key):
        rows = self._query("SELECT v FROM kv WHERE k=?", (key,))
        if not rows:
            return None
        try:
            return json.loads(rows[0][0])
        except ValueError:
            return None

    def kv_set(self, key, value):
        self._exec("INSERT OR REPLACE INTO kv(k, v, ts) VALUES (?,?,?)",
                   (key, json.dumps(value, separators=(",", ":")), now_ts()))

    # ------------------------------------------------------------- FX
    def save_fx(self, day, base, rates, source):
        self._exec("INSERT OR REPLACE INTO fx(day, base, rates, source, ts) VALUES (?,?,?,?,?)",
                   (day, base, json.dumps(rates), source, now_ts()))

    def latest_fx(self):
        rows = self._query("SELECT day, base, rates, source, ts FROM fx ORDER BY day DESC LIMIT 1")
        if not rows:
            return None
        day, base, rates, source, ts = rows[0]
        return {"day": day, "base": base, "rates": json.loads(rates), "source": source, "ts": ts}

    # ------------------------------------------------------------- housekeeping
    def prune(self, retention_days):
        now = now_ts()
        if now - self._last_prune < 86400:
            return
        self._last_prune = now
        self._exec("DELETE FROM snapshots WHERE ts < ?", (now - retention_days * 86400,))
        self._exec("DELETE FROM fx WHERE ts < ?", (now - 60 * 86400,))


def today():
    return time.strftime("%Y-%m-%d", time.gmtime())
