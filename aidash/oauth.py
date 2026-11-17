"""OAuth logins owned by the Pi (Claude, ChatGPT/Codex, Antigravity).

Each login here creates the Pi's OWN token family. We never import tokens from Claude Code,
Codex CLI or other tools: Claude and OpenAI rotate refresh tokens on every refresh, so sharing a
family between two refreshers would log one of them out. `prompt=login` is sent on the
Claude/OpenAI authorize URLs so the new login does not take over an existing browser session's
token family (forcing a fresh sign-in keeps each login's tokens independent).

Flows (headless-friendly):
  claude       PKCE; Anthropic shows the code on its own callback page -> paste "code#state".
  codex        Device code (enter a code at auth.openai.com/codex/device) or PKCE paste-back
               of the http://localhost:1455/... URL the browser fails to load.
  antigravity  Google installed-app PKCE; paste back the http://localhost:51121/... URL.
"""
import base64
import hashlib
import os
import threading
import time
import urllib.parse

from . import net
from .util import jwt_claims, now_ts, rec

CLAUDE = {
    "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
    "authorize_url": "https://claude.ai/oauth/authorize",
    "token_url": "https://api.anthropic.com/v1/oauth/token",
    "redirect_uri": "https://platform.claude.com/oauth/code/callback",
    "scopes": "org:create_api_key user:profile user:inference user:sessions:claude_code user:mcp_servers",
}
CODEX = {
    "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
    "authorize_url": "https://auth.openai.com/oauth/authorize",
    "token_url": "https://auth.openai.com/oauth/token",
    "redirect_uri": "http://localhost:1455/auth/callback",
    "scope": "openid profile email offline_access",
    "device_usercode_url": "https://auth.openai.com/api/accounts/deviceauth/usercode",
    "device_token_url": "https://auth.openai.com/api/accounts/deviceauth/token",
    "device_verify_url": "https://auth.openai.com/codex/device",
    "device_redirect_uri": "https://auth.openai.com/deviceauth/callback",
}
GOOGLE = {
    "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
    "token_url": "https://oauth2.googleapis.com/token",
    "redirect_uri": "http://localhost:51121/oauth-callback",
    "scopes": " ".join([
        "https://www.googleapis.com/auth/cloud-platform",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "https://www.googleapis.com/auth/cclog",
        "https://www.googleapis.com/auth/experimentsandconfigs",
    ]),
    "userinfo_url": "https://www.googleapis.com/oauth2/v1/userinfo",
}

PENDING_TTL = 20 * 60


class AuthError(Exception):
    pass


class NeedsLogin(AuthError):
    """Refresh token rejected (invalid_grant / reused): the user must log in again."""


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _pkce():
    verifier = _b64url(os.urandom(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _extract_code(pasted):
    """Accept a bare code, 'code#state', or a full redirect URL with ?code=...&state=..."""
    text = (pasted or "").strip()
    if not text:
        raise AuthError("Paste the code or the full redirect URL.")
    if text.startswith("http://") or text.startswith("https://"):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(text).query)
        if "error" in q:
            raise AuthError("Provider returned error: %s" % q["error"][0])
        code = (q.get("code") or [""])[0]
        state = (q.get("state") or [""])[0]
        if not code:
            raise AuthError("No ?code= found in that URL.")
        return code, state
    if "#" in text:
        code, state = text.split("#", 1)
        return code.strip(), state.strip()
    return text, ""


class OAuthManager(object):
    def __init__(self, store):
        self.store = store
        self._pending = {}  # provider -> dict(state, verifier, created, ...)
        self._locks = {"claude": threading.Lock(), "codex": threading.Lock(), "antigravity": threading.Lock()}
        self._device = {}  # codex device-flow status for the UI

    def _hdr(self, provider):
        """Client identity for OAuth endpoints (auth.openai.com is behind Cloudflare)."""
        ua = rec(self.store.get("user_agents"))
        if provider == "claude":
            return {"User-Agent": ua.get("claude_oauth") or ua.get("claude", "")}
        if provider == "codex":
            return {"User-Agent": ua.get("codex", ""), "originator": "codex_cli_rs"}
        return {"User-Agent": ua.get("antigravity", "")}

    # ------------------------------------------------------------------ tokens
    def tokens(self, provider):
        return rec(rec(self.store.secret("oauth", {})).get(provider))

    def _save_tokens(self, provider, data):
        allt = rec(self.store.secret("oauth", {}))
        allt[provider] = data
        self.store.set_secret("oauth", allt)

    def logout(self, provider):
        allt = rec(self.store.secret("oauth", {}))
        allt.pop(provider, None)
        self.store.set_secret("oauth", allt)

    def status(self, provider):
        t = self.tokens(provider)
        if not t.get("refresh_token") and not t.get("access_token"):
            return {"connected": False}
        return {
            "connected": True,
            "account": t.get("account"),
            "needs_login": bool(t.get("needs_login")),
            "expires_at": t.get("expires_at"),
        }

    def access_token(self, provider):
        """Valid access token, refreshing (serialized per provider) when within 2 min of expiry."""
        lock = self._locks[provider]
        with lock:
            t = self.tokens(provider)
            if t.get("needs_login"):
                raise NeedsLogin("Login expired - reconnect in Settings.")
            if not t.get("access_token") and not t.get("refresh_token"):
                raise AuthError("Not connected - log in from Settings.")
            exp = t.get("expires_at") or 0
            if t.get("access_token") and (not t.get("refresh_token") or exp - 120 > now_ts()):
                return t["access_token"], t
            return self._refresh_locked(provider, t)

    def force_refresh(self, provider):
        with self._locks[provider]:
            return self._refresh_locked(provider, self.tokens(provider))

    def _refresh_locked(self, provider, t):
        rt = t.get("refresh_token")
        if not rt:
            raise NeedsLogin("No refresh token - log in again.")
        try:
            hdr = self._hdr(provider)
            if provider == "claude":
                hdr["anthropic-beta"] = "oauth-2025-04-20"
                resp = net.post_form(CLAUDE["token_url"], {
                    "grant_type": "refresh_token", "refresh_token": rt, "client_id": CLAUDE["client_id"],
                }, headers=hdr)
            elif provider == "codex":
                # No `scope` on refresh: re-scoping can invalidate sibling token families.
                resp = net.post_form(CODEX["token_url"], {
                    "grant_type": "refresh_token", "refresh_token": rt, "client_id": CODEX["client_id"],
                }, headers=hdr)
            else:
                cid, secret = self._google_client()
                resp = net.post_form(GOOGLE["token_url"], {
                    "grant_type": "refresh_token", "refresh_token": rt,
                    "client_id": cid, "client_secret": secret,
                }, headers=hdr)
        except net.UpstreamError as e:
            body = (e.body or "").lower()
            if e.status in (400, 401) and ("invalid_grant" in body or "reused" in body or "invalid_request" in body):
                t["needs_login"] = True
                self._save_tokens(provider, t)
                raise NeedsLogin("Refresh token rejected (%s) - reconnect in Settings." % e.short())
            raise AuthError("Token refresh failed: %s" % e.short())
        merged = self._merge_token_response(provider, t, resp)
        self._save_tokens(provider, merged)  # persist the rotated refresh token immediately
        return merged["access_token"], merged

    def _merge_token_response(self, provider, old, resp):
        out = dict(old)
        out.pop("needs_login", None)
        out["access_token"] = resp.get("access_token") or old.get("access_token")
        if resp.get("refresh_token"):
            out["refresh_token"] = resp["refresh_token"]
        expires_in = resp.get("expires_in")
        out["expires_at"] = now_ts() + float(expires_in) if expires_in else now_ts() + 3000
        if resp.get("id_token"):
            out["id_token"] = resp["id_token"]
            if provider == "codex":
                out.update(self._codex_identity(resp["id_token"]))
        out["updated_at"] = now_ts()
        return out

    # ------------------------------------------------------------------ claude
    def start_claude(self):
        verifier, challenge = _pkce()
        state = _b64url(os.urandom(24))
        self._pending["claude"] = {"state": state, "verifier": verifier, "created": now_ts()}
        params = {
            "code": "true",
            "client_id": CLAUDE["client_id"],
            "response_type": "code",
            "redirect_uri": CLAUDE["redirect_uri"],
            "scope": CLAUDE["scopes"],
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "prompt": "login",
        }
        return {
            "auth_url": CLAUDE["authorize_url"] + "?" + urllib.parse.urlencode(params),
            "instructions": "Log in, then copy the code Anthropic shows you (it looks like abc...#xyz...) and paste it here.",
        }

    def finish_claude(self, pasted):
        p = self._take_pending("claude")
        code, state = _extract_code(pasted)
        try:
            resp = net.post_json(CLAUDE["token_url"], {
                "code": code,
                "state": state or p["state"],
                "grant_type": "authorization_code",
                "client_id": CLAUDE["client_id"],
                "redirect_uri": CLAUDE["redirect_uri"],
                "code_verifier": p["verifier"],
            }, headers=self._hdr("claude"))
        except net.UpstreamError as e:
            raise AuthError("Claude token exchange failed: %s" % e.short())
        t = self._merge_token_response("claude", {}, resp)
        acct = rec(resp.get("account"))
        t["account"] = acct.get("email_address") or acct.get("email") or "Claude account"
        self._save_tokens("claude", t)
        return self.status("claude")

    # ------------------------------------------------------------------ codex
    @staticmethod
    def _codex_identity(id_token):
        claims = jwt_claims(id_token)
        auth = rec(claims.get("https://api.openai.com/auth"))
        out = {}
        if claims.get("email"):
            out["account"] = claims["email"]
        if auth.get("chatgpt_account_id"):
            out["account_id"] = auth["chatgpt_account_id"]
        if auth.get("chatgpt_plan_type"):
            out["plan"] = auth["chatgpt_plan_type"]
        return out

    def start_codex_pkce(self):
        verifier, challenge = _pkce()
        state = _b64url(os.urandom(24))
        self._pending["codex"] = {"state": state, "verifier": verifier, "created": now_ts()}
        params = [
            ("response_type", "code"),
            ("client_id", CODEX["client_id"]),
            ("redirect_uri", CODEX["redirect_uri"]),
            ("scope", CODEX["scope"]),
            ("code_challenge", challenge),
            ("code_challenge_method", "S256"),
            ("id_token_add_organizations", "true"),
            ("codex_cli_simplified_flow", "true"),
            ("originator", "codex_cli_rs"),
            ("prompt", "login"),
            ("state", state),
        ]
        return {
            "auth_url": CODEX["authorize_url"] + "?" + urllib.parse.urlencode(params),
            "instructions": "Log in. Your browser will then fail to open a localhost:1455 page - that's expected. Copy the whole URL from the address bar and paste it here.",
        }

    def finish_codex_pkce(self, pasted):
        p = self._take_pending("codex")
        code, state = _extract_code(pasted)
        if state and state != p["state"]:
            raise AuthError("State mismatch - start the login again.")
        self._codex_exchange(code, p["verifier"], CODEX["redirect_uri"])
        return self.status("codex")

    def _codex_exchange(self, code, verifier, redirect_uri):
        try:
            resp = net.post_form(CODEX["token_url"], {
                "grant_type": "authorization_code",
                "client_id": CODEX["client_id"],
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            }, headers=self._hdr("codex"))
        except net.UpstreamError as e:
            raise AuthError("ChatGPT token exchange failed: %s" % e.short())
        t = self._merge_token_response("codex", {}, resp)
        t.setdefault("account", "ChatGPT account")
        self._save_tokens("codex", t)

    def start_codex_device(self):
        try:
            resp = net.post_json(CODEX["device_usercode_url"], {"client_id": CODEX["client_id"]},
                                 headers=self._hdr("codex"))
        except net.UpstreamError as e:
            if e.status == 404:
                raise AuthError(
                    "Device-code login is disabled for this ChatGPT account. Enable it in ChatGPT "
                    "Settings > Security, or use the 'paste URL' login instead.")
            raise AuthError("Could not start device login: %s" % e.short())
        user_code = resp.get("user_code") or resp.get("usercode")
        dev_id = resp.get("device_auth_id")
        if not user_code or not dev_id:
            raise AuthError("Unexpected device-login response from OpenAI.")
        interval = max(3, int(float(resp.get("interval") or 5)))
        self._device = {"state": "pending", "user_code": user_code, "verify_url": CODEX["device_verify_url"],
                        "started": now_ts(), "error": None}
        th = threading.Thread(target=self._poll_codex_device, args=(dev_id, user_code, interval), daemon=True)
        th.start()
        return dict(self._device)

    def codex_device_status(self):
        return dict(self._device) if self._device else {"state": "idle"}

    def _poll_codex_device(self, dev_id, user_code, interval):
        deadline = time.time() + 15 * 60
        while time.time() < deadline:
            time.sleep(interval)
            try:
                resp = net.post_json(CODEX["device_token_url"], {"device_auth_id": dev_id, "user_code": user_code},
                                     headers=self._hdr("codex"))
            except net.UpstreamError as e:
                if e.status in (403, 404) or e.status is None:
                    continue  # still pending / transient
                self._device.update(state="error", error=e.short())
                return
            code, verifier = resp.get("authorization_code"), resp.get("code_verifier")
            if not code or not verifier:
                self._device.update(state="error", error="OpenAI returned no authorization code.")
                return
            try:
                self._codex_exchange(code, verifier, CODEX["device_redirect_uri"])
                self._device.update(state="done", error=None)
            except AuthError as e:
                self._device.update(state="error", error=str(e))
            return
        self._device.update(state="error", error="Timed out waiting for authorization.")

    # ------------------------------------------------------------------ antigravity (Google)
    def _google_client(self):
        cid = (self.store.get("antigravity_client_id") or "").strip()
        secret = (self.store.get("antigravity_client_secret") or "").strip()
        if not cid or not secret:
            raise AuthError("Set the Antigravity OAuth client ID/secret in Settings first.")
        return cid, secret

    def start_antigravity(self):
        cid, _ = self._google_client()
        verifier, challenge = _pkce()
        state = _b64url(os.urandom(24))
        self._pending["antigravity"] = {"state": state, "verifier": verifier, "created": now_ts()}
        params = {
            "client_id": cid,
            "redirect_uri": GOOGLE["redirect_uri"],
            "response_type": "code",
            "scope": GOOGLE["scopes"],
            "access_type": "offline",
            "prompt": "consent",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        return {
            "auth_url": GOOGLE["authorize_url"] + "?" + urllib.parse.urlencode(params),
            "instructions": "Sign in with the Google account you use for Antigravity. The browser will then fail to open a localhost:51121 page - copy that whole URL and paste it here.",
        }

    def finish_antigravity(self, pasted):
        p = self._take_pending("antigravity")
        code, state = _extract_code(pasted)
        if state and state != p["state"]:
            raise AuthError("State mismatch - start the login again.")
        cid, secret = self._google_client()
        try:
            resp = net.post_form(GOOGLE["token_url"], {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": GOOGLE["redirect_uri"],
                "client_id": cid,
                "client_secret": secret,
                "code_verifier": p["verifier"],
            }, headers=self._hdr("antigravity"))
        except net.UpstreamError as e:
            raise AuthError("Google token exchange failed: %s" % e.short())
        t = self._merge_token_response("antigravity", {}, resp)
        try:
            info = net.get_json(GOOGLE["userinfo_url"], headers={"Authorization": "Bearer " + t["access_token"]})
            t["account"] = info.get("email") or "Google account"
        except net.UpstreamError:
            t["account"] = "Google account"
        self._save_tokens("antigravity", t)
        return self.status("antigravity")

    def update_extra(self, provider, **fields):
        """Persist provider metadata (e.g. Antigravity project id) alongside the tokens."""
        with self._locks[provider]:
            t = self.tokens(provider)
            if not t:
                return
            t.update(fields)
            self._save_tokens(provider, t)

    # ------------------------------------------------------------------ helpers
    def _take_pending(self, provider):
        p = self._pending.pop(provider, None)
        if not p or now_ts() - p["created"] > PENDING_TTL:
            raise AuthError("No login in progress (or it expired) - click Connect again.")
        return p
