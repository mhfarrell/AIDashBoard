"""Tiny urllib wrapper: JSON in/out, timeouts, gzip, readable errors, test URL overrides."""
import gzip
import http.client
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

_SSL = ssl.create_default_context()
_overrides = {}
# Never send urllib's "Python-urllib/3.x" (Cloudflare-fronted hosts tend to block it).
_default_ua = "aidash/1.0 (LAN usage dashboard)"


def set_overrides(mapping):
    """Rewrite URL prefixes (used by the test suite to point at a fake upstream)."""
    global _overrides
    _overrides = dict(mapping or {})


def set_default_user_agent(ua):
    global _default_ua
    if ua:
        _default_ua = ua


def _rewrite(url):
    for prefix, target in _overrides.items():
        if url.startswith(prefix):
            return target.rstrip("/") + url[len(prefix):]
    return url


class UpstreamError(Exception):
    """Raised for non-2xx responses and transport failures. `status` is None for network errors."""

    def __init__(self, message, status=None, body="", headers=None):
        Exception.__init__(self, message)
        self.status = status
        self.body = body or ""
        self.headers = headers or {}

    def short(self):
        snippet = " ".join(self.body.split())[:160]
        if self.status:
            return "HTTP %s%s" % (self.status, (": " + snippet) if snippet else "")
        return str(self)


def request(method, url, headers=None, json_body=None, form=None, timeout=20):
    """Return (status, parsed_json_or_text, headers_dict). Raises UpstreamError on failure."""
    hdrs = {"Accept": "application/json", "Accept-Encoding": "gzip", "User-Agent": _default_ua}
    hdrs.update(headers or {})
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(_rewrite(url), data=data, headers=hdrs, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=_SSL)
    except urllib.error.HTTPError as e:
        raw = e.read() or b""
        if (e.headers.get("Content-Encoding") or "").lower() == "gzip":
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
        raise UpstreamError(
            "HTTP %d" % e.code, e.code, raw.decode("utf-8", "replace"), dict(e.headers.items())
        )
    except ssl.SSLError as e:
        # A Pi 1 has no RTC: before NTP sync the clock is 1970 and every cert "isn't valid yet".
        raise UpstreamError("TLS error (is the Pi clock synced?): %s" % e)
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, ssl.SSLError):
            raise UpstreamError("TLS error (is the Pi clock synced?): %s" % reason)
        raise UpstreamError("Network error: %s" % reason)
    except (socket.timeout, OSError, http.client.HTTPException) as e:
        raise UpstreamError("Network error: %s" % (e or type(e).__name__))
    try:
        with resp:
            raw = resp.read()
            if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                raw = gzip.decompress(raw)
            status = resp.status
            rh = dict(resp.headers.items())
    except (socket.timeout, OSError, http.client.HTTPException, EOFError) as e:
        # Timeout / truncation while reading the body - must not count as a successful poll.
        raise UpstreamError("Network error while reading response: %s" % (e or type(e).__name__))
    text = raw.decode("utf-8", "replace")
    try:
        return status, (json.loads(text) if text.strip() else {}), rh
    except ValueError:
        return status, text, rh


def get_json(url, headers=None, timeout=20):
    status, body, _ = request("GET", url, headers=headers, timeout=timeout)
    if not isinstance(body, (dict, list)):
        raise UpstreamError("Expected JSON from %s" % url.split("?")[0], status, str(body)[:300])
    return body


def post_json(url, body, headers=None, timeout=20):
    status, data, _ = request("POST", url, headers=headers, json_body=body, timeout=timeout)
    if not isinstance(data, (dict, list)):
        raise UpstreamError("Expected JSON from %s" % url.split("?")[0], status, str(data)[:300])
    return data


def post_form(url, form, headers=None, timeout=20):
    status, data, _ = request("POST", url, headers=headers, form=form, timeout=timeout)
    if not isinstance(data, (dict, list)):
        raise UpstreamError("Expected JSON from %s" % url.split("?")[0], status, str(data)[:300])
    return data
