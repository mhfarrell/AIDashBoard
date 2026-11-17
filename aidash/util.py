"""Small helpers shared across the app. Python 3.7 compatible (no walrus, no 3.9+ typing)."""
import base64
import calendar
import datetime as _dt
import json
import re
import time

UTC = _dt.timezone.utc


def now_ts():
    return time.time()


def iso(ts):
    """Unix seconds -> ISO-8601 UTC string (or None)."""
    if ts is None:
        return None
    return _dt.datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?$"
)


def parse_time(value):
    """Parse ISO strings / unix seconds / unix millis into unix seconds. Returns None when unknown."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v <= 0:
            return None
        return v / 1000.0 if v > 1e11 else v
    if isinstance(value, str):
        s = value.strip()
        if re.match(r"^\d+(\.\d+)?$", s):
            return parse_time(float(s))
        m = _ISO_RE.match(s)
        if not m:
            return None
        y, mo, d, h, mi, sec, frac, tz = m.groups()
        dt = _dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(sec or 0))
        ts = calendar.timegm(dt.timetuple()) + (float(frac) if frac else 0.0)
        if tz and tz != "Z":
            sign = 1 if tz[0] == "+" else -1
            digits = tz[1:].replace(":", "")
            ts -= sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
        return ts
    return None


def month_key(ts=None):
    """'YYYY-MM' for the given unix time (local time of the Pi)."""
    t = time.localtime(ts if ts is not None else time.time())
    return "%04d-%02d" % (t.tm_year, t.tm_mon)


def month_start_ts(ts=None):
    t = time.localtime(ts if ts is not None else time.time())
    return time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, -1))


def prev_months(n, ts=None):
    """List of the last n month keys, oldest first, ending with the current month."""
    t = time.localtime(ts if ts is not None else time.time())
    y, m = t.tm_year, t.tm_mon
    out = []
    for _ in range(n):
        out.append("%04d-%02d" % (y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(out))


def num(value, default=None):
    """Coerce numbers / numeric strings; returns default for anything else."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        if value != value or value in (float("inf"), float("-inf")):
            return default
        return float(value)
    if isinstance(value, str):
        try:
            v = float(value.strip())
        except ValueError:
            return default
        return v if v == v else default
    return default


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def rec(value):
    return value if isinstance(value, dict) else {}


def jwt_claims(token):
    """Decode a JWT payload without verifying (used only to read our own id_token claims)."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part.encode()).decode("utf-8", "replace"))
    except Exception:
        return {}


def mask_secret(value):
    if not value:
        return None
    v = str(value)
    if len(v) <= 8:
        return "****"
    return v[:4] + "…" + v[-4:]
