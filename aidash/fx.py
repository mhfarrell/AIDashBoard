"""Daily exchange rates (USD base). ECB rates via frankfurter.app, open.er-api.com as fallback."""
from . import net
from .db import today

SOURCES = (
    ("frankfurter.app (ECB)", "https://api.frankfurter.app/latest?from=USD", "rates"),
    ("open.er-api.com", "https://open.er-api.com/v6/latest/USD", "rates"),
)

# Used only until the first successful fetch (e.g. Pi offline at boot). Approximate.
FALLBACK = {"USD": 1.0, "GBP": 0.79, "EUR": 0.92, "CNY": 7.2, "JPY": 150.0, "CAD": 1.37, "AUD": 1.52}


class FX(object):
    def __init__(self, history):
        self.history = history
        latest = history.latest_fx()
        self.rates = dict(FALLBACK)
        self.day, self.source = None, "built-in approximate rates"
        if latest:
            self.rates.update(latest["rates"])
            self.day, self.source = latest["day"], latest["source"]

    def refresh(self):
        last_err = None
        for name, url, field in SOURCES:
            try:
                data = net.get_json(url, timeout=15)
                rates = data.get(field) or {}
                if not isinstance(rates, dict) or "GBP" not in rates:
                    raise net.UpstreamError("bad FX payload from %s" % name)
                clean = dict((k, float(v)) for k, v in rates.items() if isinstance(v, (int, float)) and v > 0)
                clean["USD"] = 1.0
                self.rates = clean
                self.day, self.source = today(), name
                self.history.save_fx(self.day, "USD", clean, name)
                return True
            except net.UpstreamError as e:
                last_err = e
        raise last_err or net.UpstreamError("FX unavailable")

    def convert(self, amount, src, dst):
        if amount is None:
            return None
        src = (src or "USD").upper()
        dst = (dst or "USD").upper()
        if src == dst:
            return float(amount)
        r_src, r_dst = self.rates.get(src), self.rates.get(dst)
        if not r_src or not r_dst:
            return None
        return float(amount) / r_src * r_dst

    def currencies(self):
        return sorted(self.rates.keys())

    def info(self):
        return {"day": self.day, "source": self.source}
