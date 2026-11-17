"""Config + secrets persistence.

config.json  - non-secret settings (currency, subscriptions, manual entries, intervals). Editable.
secrets.json - API keys and OAuth tokens. Written 0600, never sent to the browser.

Writes are atomic (temp file + rename) and rare, to spare the Pi's SD card.
"""
import copy
import json
import os
import threading

DEFAULT_CONFIG = {
    "listen_host": "0.0.0.0",
    "listen_port": 8787,
    # Only clients on private / loopback / link-local addresses may connect.
    "lan_only": True,
    # Optional: when set (sha256 hex via /api/settings), settings changes require it.
    "admin_password_sha256": "",
    "display_currency": "GBP",
    # Subscriptions: fixed monthly charges. amount is in `currency`.
    "subscriptions": [
        {"id": "claude", "platform": "claude", "name": "Claude Pro/Max", "amount": 0, "currency": "GBP", "billing_day": 1, "active": True},
        {"id": "chatgpt", "platform": "codex", "name": "ChatGPT Plus", "amount": 0, "currency": "GBP", "billing_day": 1, "active": True},
        {"id": "opencode-go", "platform": "opencode", "name": "OpenCode Go", "amount": 0, "currency": "USD", "billing_day": 1, "active": True},
        {"id": "kimi-code", "platform": "kimi_code", "name": "Kimi Code plan", "amount": 0, "currency": "USD", "billing_day": 1, "active": True},
        {"id": "google-ai", "platform": "gemini", "name": "Google AI Pro (Gemini + Antigravity)", "amount": 0, "currency": "GBP", "billing_day": 1, "active": True},
    ],
    # Manual ledger entries: {"id","month":"YYYY-MM","platform","kind":"spend"|"topup","amount","currency","note"}
    "manual_entries": [],
    # Poll intervals (seconds) per provider. Claude's usage endpoint rate-limits: keep >= 300.
    "intervals": {
        "claude": 300,
        "codex": 180,
        "antigravity": 240,
        "opencode": 180,
        "openrouter": 180,
        "moonshot": 300,
        "kimi_code": 240,
        "gemini": 900,
        "fx": 21600,
        "news": 10800,
        "catalog": 21600,
    },
    "enabled": {
        "claude": True,
        "codex": True,
        "antigravity": True,
        "opencode": True,
        "openrouter": True,
        "moonshot": True,
        "kimi_code": True,
        "gemini": True,
    },
    "moonshot_base_url": "https://api.moonshot.ai/v1",
    # Public desktop-app OAuth client for Antigravity (Google). Not a personal secret,
    # but Google's installed-app flow needs both values. Fill in via Settings if blank.
    "antigravity_client_id": "",
    "antigravity_client_secret": "",
    # Client identity strings sent upstream (current CLI versions as of Nov 2026: Claude Code 2.1.280,
    # Codex 0.165.0, Antigravity CLI 1.4.4 + its build changelist). Update here if a provider starts
    # rejecting old versions.
    "user_agents": {
        "claude": "claude-code/2.1.280",
        "claude_oauth": "claude-cli/2.1.280 (external, cli)",
        "codex": "codex-cli/0.165.0 (Linux; arm)",
        "codex_version": "0.165.0",
        "antigravity": "antigravity/cli/1.4.4 (aidev_client; os_type=linux; arch=arm; cl=1003595037; auth_method=consumer)",
        "opencode": "opencode/1.18.31",
        "kimi_code": "kimi-code-cli/1.0.0",
        "default": "aidash/1.0 (LAN usage dashboard)",
    },
    # Test hook: {"https://api.anthropic.com": "http://127.0.0.1:9999/anthropic"}
    "upstream_overrides": {},
    "history_retention_days": 120,
}

SECRET_KEYS = (
    "openrouter_api_key",
    "opencode_api_key",
    "moonshot_api_key",
    "kimi_code_api_key",
    "gemini_api_key",
)


def _deep_merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _atomic_write(path, data, private=False):
    tmp = path + ".tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, 0o600 if private else 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    if private:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


class Store(object):
    """Thread-safe holder for config.json + secrets.json."""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.config_path = os.path.join(data_dir, "config.json")
        self.secrets_path = os.path.join(data_dir, "secrets.json")
        self._lock = threading.RLock()
        self.config = _deep_merge(DEFAULT_CONFIG, self._read(self.config_path))
        self.secrets = self._read(self.secrets_path)
        if not os.path.exists(self.config_path):
            self.save_config()

    @staticmethod
    def _read(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    # ---- config -------------------------------------------------------------
    def get(self, key, default=None):
        with self._lock:
            return copy.deepcopy(self.config.get(key, default))

    def update_config(self, changes):
        with self._lock:
            self.config = _deep_merge(self.config, changes)
            self.save_config()

    def set_config_value(self, key, value):
        with self._lock:
            self.config[key] = value
            self.save_config()

    def save_config(self):
        with self._lock:
            _atomic_write(self.config_path, self.config)

    # ---- secrets ------------------------------------------------------------
    def secret(self, key, default=None):
        with self._lock:
            return copy.deepcopy(self.secrets.get(key, default))

    def set_secret(self, key, value):
        with self._lock:
            if value in (None, ""):
                self.secrets.pop(key, None)
            else:
                self.secrets[key] = value
            _atomic_write(self.secrets_path, self.secrets, private=True)
