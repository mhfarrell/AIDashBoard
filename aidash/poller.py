"""Single background poller. Pages never trigger upstream calls - they read this cached state."""
import random
import threading
import traceback

from . import net, oauth
from .providers import FETCHERS, PLATFORMS
from .util import month_key, now_ts, num

MAX_BACKOFF = 30 * 60
MIN_MANUAL_GAP = {"claude": 120}


class Ctx(object):
    """What fetchers get: config/secrets store, OAuth manager, a scratch cache."""

    def __init__(self, store, oauth_mgr):
        self.store = store
        self.oauth = oauth_mgr
        self.cache = {}

    def ua(self, name):
        return (self.store.get("user_agents") or {}).get(name, "")


class Poller(object):
    def __init__(self, store, oauth_mgr, history, fx, news=None):
        self.news = news
        self.store = store
        self.oauth = oauth_mgr
        self.history = history
        self.fx = fx
        self.ctx = Ctx(store, oauth_mgr)
        self.state = {}
        self._next = {}
        self._fails = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = False
        # Stagger first polls so a Pi 1 isn't doing 8 TLS handshakes at once.
        for i, (pid, _, _) in enumerate(PLATFORMS):
            self._next[pid] = now_ts() + 2 + i * 4
            self.state[pid] = {"status": "pending", "data": None, "error": None, "updated_at": None, "checked_at": None}
        self._next["fx"] = now_ts() + 1
        self.fx_error = None
        if news is not None:
            self._refresh_public_prices()
            # News is low priority: start after the providers, then every few hours.
            self._next["prices"] = now_ts() + self._interval("prices")
            upd = news.updated or {}
            self._next["news"] = max(now_ts() + 60, (upd.get("feeds") or 0) + self._interval("news"))
            self._next["catalog"] = max(now_ts() + 90, (upd.get("catalog") or 0) + self._interval("catalog"))

    # ------------------------------------------------------------------ public
    def start(self):
        t = threading.Thread(target=self._run, name="poller", daemon=True)
        t.start()

    def stop(self):
        self._stop = True
        self._wake.set()

    def refresh_now(self, platform):
        gap = MIN_MANUAL_GAP.get(platform, 30)
        st = self.state.get(platform) or {}
        last = st.get("checked_at") or 0
        self._next[platform] = max(now_ts(), last + gap)
        self._wake.set()
        return self._next[platform]

    def schedule_soon(self, key, delay=5):
        if key in self._next:
            self._next[key] = now_ts() + delay
            self._wake.set()

    def snapshot(self):
        with self._lock:
            out = {}
            for pid, st in self.state.items():
                s = dict(st)
                s["next_poll"] = self._next.get(pid)
                out[pid] = s
            return out

    # ------------------------------------------------------------------ loop
    def _run(self):
        while not self._stop:
            now = now_ts()
            due = [k for k, t in self._next.items() if t <= now]
            for key in due:
                if self._stop:
                    break
                try:
                    if key == "fx":
                        self._poll_fx()
                    elif key == "news":
                        self._schedule(key, self._interval("news"))
                        self.news.refresh_feeds()
                    elif key == "catalog":
                        self._schedule(key, self._interval("catalog"))
                        self.news.refresh_catalog()
                        self._refresh_public_prices()
                    elif key == "prices":
                        self._schedule(key, self._interval("prices"))
                        self._refresh_public_prices()
                    else:
                        self._poll(key)
                except Exception:  # never let one provider kill the loop
                    traceback.print_exc()
                    self._schedule(key, 300)
            try:
                self.history.prune(int(self.store.get("history_retention_days") or 120))
            except Exception:
                traceback.print_exc()
            self._wake.wait(5)
            self._wake.clear()

    def _interval(self, key):
        iv = num((self.store.get("intervals") or {}).get(key), 300)
        if key == "claude":
            iv = max(iv, 300)  # Claude's usage endpoint 429s easily
        return max(60, iv)

    def _schedule(self, key, delay):
        self._next[key] = now_ts() + delay + random.uniform(0, min(20, delay * 0.1))

    def _configured(self, pid):
        if pid in ("claude", "codex", "antigravity"):
            st = self.oauth.status(pid)
            return st.get("connected"), ("Not connected - log in from Settings." if not st.get("connected") else None)
        secret = {"openrouter": "openrouter_api_key", "opencode": "opencode_api_key", "moonshot": "moonshot_api_key",
                  "kimi_code": "kimi_code_api_key", "gemini": "gemini_api_key"}[pid]
        ok = bool(self.store.secret(secret))
        return ok, (None if ok else "No API key - add one in Settings.")

    def _set(self, pid, **kw):
        with self._lock:
            self.state[pid].update(kw)

    def _poll(self, pid):
        enabled = (self.store.get("enabled") or {}).get(pid, True)
        if not enabled:
            self._set(pid, status="disabled", error=None, checked_at=now_ts())
            self._schedule(pid, 60)
            return
        ok, why = self._configured(pid)
        if not ok:
            self._set(pid, status="unconfigured", error=why, checked_at=now_ts())
            self._schedule(pid, 30)
            return
        started = now_ts()
        try:
            data = FETCHERS[pid](self.ctx)
        except oauth.NeedsLogin as e:
            self._fail(pid, str(e), relogin=True)
            return
        except oauth.AuthError as e:
            self._fail(pid, str(e))
            return
        except net.UpstreamError as e:
            retry_after = None
            if e.status == 429:
                retry_after = num((e.headers or {}).get("Retry-After") or (e.headers or {}).get("retry-after"))
            if e.status == 401 and pid in ("claude", "codex", "antigravity"):
                # Access token rejected early - force one refresh, then retry once.
                try:
                    self.oauth.force_refresh(pid)
                    data = FETCHERS[pid](self.ctx)
                except Exception as e2:
                    self._fail(pid, "Auth rejected: %s" % (e2.short() if hasattr(e2, "short") else e2))
                    return
            else:
                msg = e.short()
                if e.status == 429:
                    msg = "Rate limited by provider (429) - showing last known values."
                self._fail(pid, msg, retry_after=retry_after)
                return
        self._fails[pid] = 0
        self._set(pid, status="ok", data=data, error=None, updated_at=now_ts(), checked_at=now_ts(),
                  fetch_ms=int((now_ts() - started) * 1000))
        self._record(pid, data)
        self._schedule(pid, self._interval(pid))

    def _fail(self, pid, message, retry_after=None, relogin=False):
        n = self._fails.get(pid, 0) + 1
        self._fails[pid] = n
        has_data = bool(self.state[pid].get("data"))
        self._set(pid, status=("relogin" if relogin else ("stale" if has_data else "error")),
                  error=message, checked_at=now_ts())
        delay = min(MAX_BACKOFF, self._interval(pid) * (2 ** min(n - 1, 4)))
        if retry_after:
            delay = max(delay, retry_after)
        if relogin:
            delay = 120
        self._schedule(pid, delay)

    def _record(self, pid, data):
        self.history.maybe_snapshot(pid, data.get("windows") or [])
        self._remember_plan(pid, data.get("plan"))
        m = data.get("metrics") or {}
        if pid == "openrouter":
            self.history.track_cumulative(pid, "credits_used", m.get("credits_used"), "spend", "USD")
            self.history.track_cumulative(pid, "credits_total", m.get("credits_total"), "topup", "USD")
            self.history.track_period_total(pid, "key_usage_month", m.get("key_usage_month"), "key_month", "USD")
        elif pid == "claude":
            self.history.track_period_total(pid, "extra_usage", m.get("extra_usage_amount"), "extra_usage",
                                            m.get("extra_usage_currency") or "USD")
        elif pid == "moonshot":
            self.history.track_cumulative(pid, "balance", m.get("balance"), "topup",
                                          m.get("balance_currency") or "USD", kind_down="spend")

    def _refresh_public_prices(self):
        try:
            from . import prices
            prices.track(self.store, self.history)
        except Exception:
            traceback.print_exc()

    def _remember_plan(self, pid, plan):
        """Persist the detected subscription plan (used for auto list prices). Writes only on change."""
        if pid not in ("claude", "codex", "opencode") or not plan:
            return
        plans = self.store.get("detected_plans") or {}
        cur = plans.get(pid) or {}
        if cur.get("plan") == plan:
            return
        plans[pid] = {"plan": plan, "since": cur.get("since") or month_key()}
        self.store.update_config({"detected_plans": plans})

    def _poll_fx(self):
        try:
            self.fx.refresh()
            self.fx_error = None
            self._schedule("fx", self._interval("fx"))
        except net.UpstreamError as e:
            self.fx_error = e.short()
            self._schedule("fx", 900)

