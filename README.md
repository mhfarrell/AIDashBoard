# AI Usage Dashboard (Raspberry Pi 1 edition)

A LAN-only dashboard that pulls **live usage, quota windows, credit balances and spend** straight from
each provider and adds your **subscriptions + credit spend into one monthly tab**, with a per-platform
breakdown — plus a **News & models** tab to keep on top of new, stealth and best-value models. No
cloud service, no proxy: the Pi talks to each provider itself and only serves your local network.

| Platform | How it's read | What you get |
|---|---|---|
| Claude (Pro/Max) | Pi's own OAuth login → `api.anthropic.com/api/oauth/usage` | 5-hour session, weekly, per-model weekly, extra (pay-as-you-go) usage $ |
| ChatGPT / Codex | Pi's own OAuth login → `chatgpt.com/backend-api/wham/usage` | 5-hour + weekly windows, plan, Codex credits |
| Antigravity | Pi's own Google login → Cloud Code `retrieveUserQuotaSummary` | Per model-family 5-hour / weekly windows, tier |
| Gemini | API key → key health; Google AI Pro as a subscription | Gemini API spend has **no API** → add it as a manual entry |
| OpenCode Go | API key → `opencode.ai/zen/go/v1/usage` | 5-hour ($12), weekly ($30), monthly ($60) windows |
| OpenRouter | API key → `/api/v1/key` + `/api/v1/credits` | Credit balance, day/week/month spend, top-ups |
| Kimi (Moonshot API) | API key → `/v1/users/me/balance` | Balance; spend & top-ups derived from balance changes |
| Kimi Code | API key → `api.kimi.com/coding/v1/usages` | 5-hour / 7-day plan windows |

## How the monthly tab is counted

* **Total = subscriptions + credit spend.** Credit spend = OpenRouter credit used, Moonshot balance used,
  Claude extra usage, and any manual "spend" entries (e.g. Gemini API billing).
* **Credits bought** (top-ups, detected automatically when a balance goes *up*, or entered manually) and
  **credit left** are shown in their own columns but **not added** — otherwise the same money would be
  counted twice. Switch *Credits: as bought* in the top bar for a cash-out view (subscriptions + top-ups).
* Moonshot/OpenRouter spend tracked from balance changes starts when the Pi first sees the account; the
  first month is labelled "since <date>". OpenRouter falls back to the key's own calendar-month spend
  when that's larger.
* Subscriptions count every month from their *start month*; unticking *active* ends them after the
  current month, so past months stay correct. Leave the amount empty to use the list price of the plan
  detected from your login (Claude Pro/Max, ChatGPT Plus/Pro).
* **Invoices beat estimates.** Settings → *Invoices & manual entries*: entries for a platform + month
  replace the automatic figure for that column (subscription / spend / bought), and the replaced figure
  stays visible as "not counted". Types: *subscription charge*, *spend*, *bought*, and *bought & used*
  (prepaid credit you've spent — counts in both Bought and Spend, e.g. Claude extra-usage credit).
  Claude's API doesn't report past extra-usage spend once credits run out, so invoices are the source of truth.
* Currency: pick any currency in the top bar. Rates are fetched daily from the ECB (frankfurter.app,
  fallback open.er-api.com) and cached.

## News & models tab

All from free, keyless sources, refreshed every 3 h (news) / 6 h (model catalogue) to stay light on a Pi 1:

* **Stealth models** live on OpenRouter (unnamed models under test), remembered after they disappear,
  plus Hacker News "stealth model" chatter.
* **New models** (last 14 days) — ★ marks families you use (Settings → News: model families).
* **Best bang for buck** — quality vs price chart with the Pareto frontier, and the best model in each
  price band (Free, < $0.50, < $2, < $6, Premium). Quality = average Design Arena Elo from OpenRouter's
  catalogue (a proxy, not a full benchmark); price = blended $/1M tokens (3 input : 1 output).
* **Retiring soon** (OpenRouter expiration dates) and **price changes** (day-to-day catalogue diff).
* **News feed** — release notes for Claude Code, Codex CLI, Gemini CLI, OpenCode and Kimi Code
  (pre-releases hidden), OpenAI and Google AI news, and Hacker News stories (≥ 60 points, term in the
  title) about your models. Filters: For you / Releases / Vendors / Community / Stealth / All.

**Sessions:** no provider exposes per-conversation sessions through an API. The "Active usage windows"
panel shows each running 5-hour window instead (when it started, time elapsed vs quota used).

## Install on the Pi

Needs Raspberry Pi OS (any release with Python ≥ 3.7 — every Pi 1-compatible release has it). No pip packages.

```sh
# from your PC (adjust host)
scp -r AIDashBoard pi@raspberrypi.local:~/
ssh pi@raspberrypi.local
cd AIDashBoard && sudo sh deploy/install.sh
```

The installer renames the Pi to **AIdashboard**, makes sure avahi (mDNS) is running, and serves on
port 80 — so the dashboard is at **http://AIdashboard.local** from any device on your network
(use `AIDASH_HOSTNAME=name` to pick another name, `AIDASH_KEEP_HOSTNAME=1` to keep the current one).

Then, from the PC where you've been running it, copy your settings, invoices and API keys across in one go
(logins are deliberately not copied — connect those on the Pi):

```sh
python tools/push_to_pi.py --to http://AIdashboard.local
```

Then open **http://AIdashboard.local → Settings** (or set everything up there directly if you're starting fresh):

1. **API keys** — paste OpenRouter, OpenCode Go, Moonshot, Kimi Code and (optionally) Gemini keys.
2. **Subscription logins** — *Connect* Claude, ChatGPT and Antigravity. Each creates a **separate**
   login for the Pi (forced fresh sign-in with `prompt=login`), so it shouldn't sign out Claude Code,
   Codex CLI or other tools using the same account — check they're still signed in after your first connect.
   * Claude: log in, copy the code Anthropic shows, paste it back.
   * ChatGPT: *Use device code* (enter the code at auth.openai.com/codex/device — enable device-code
     login in ChatGPT → Settings → Security if it says disabled), or *Connect* and paste the
     `localhost:1455/...` URL your browser fails to open.
   * Antigravity: Google's desktop-app sign-in needs the OAuth client ID + secret that ship inside the
     Antigravity app (public values, not your credentials) — enter them once under *Antigravity OAuth
     client* (`push_to_pi.py` copies them if your PC install has them), then *Connect* and paste the
     `localhost:51121/...` URL.
3. **Subscriptions** — enter what you actually pay (amount + currency + start month).
4. Optional: set an **admin password** so only you can change settings/logins.

## Security

* Only private / loopback / link-local client addresses are served (`lan_only`), everything else gets 403.
* API keys and OAuth tokens live in `/var/lib/aidash/secrets.json` (mode 600, service user only) and are
  never sent to the browser (settings shows `abcd…wxyz`).
* Settings changes require the admin password once one is set; POSTs must be JSON (blocks simple CSRF).
* Don't port-forward it to the internet. For remote viewing use a VPN/Tailscale to your LAN.

## Pi 1 notes

* Python stdlib only; one background thread polls each provider every 3–15 min (Claude ≥ 5 min because
  its usage endpoint rate-limits; 429s honour `Retry-After` and keep showing the last value as *Stale*).
  Pages never trigger provider calls.
* The Pi 1 has no clock battery: the service waits for NTP time sync (TLS fails with a wrong clock).
* Usage snapshots are written at most every 15 min per provider (SD-card friendly), pruned after 120 days.

## Developing

```sh
python3 -m unittest discover -s tests -v     # offline tests against a fake upstream
python3 tools/demo.py --port 8790            # preview with fake data, no accounts needed
python3 run.py --data-dir ./data             # run for real on any machine
```

Tested on Python 3.7 and 3.11 (incl. an emulated ARMv6 Pi 1 userspace) and 3.13.

## Known limits

* Verified against the live services (Nov 2026): Claude, ChatGPT/Codex, Antigravity, Gemini, OpenCode Go,
  OpenRouter and Moonshot. **Kimi Code** is implemented and tested offline only. These are the endpoints
  the official apps use for their own usage screens, not documented public APIs, so they can change —
  every tile shows the exact upstream error if one does.
* Client identity strings (Claude Code 2.1.280, Codex 0.165.0, Antigravity CLI 1.4.4) are in
  `config.json → user_agents`; bump them if a provider starts rejecting old clients.
* Moving to the Pi: copy `data/config.json` (subscriptions, invoices, settings) if you like — **never**
  `secrets.json`; log in again on the Pi.
* Gemini API key spend can't be read via any API — use a manual entry.
* **Never run two copies against the same `secrets.json`** (e.g. a test copy on your PC and the Pi with
  copied data): both would refresh the same rotating Claude/ChatGPT tokens and log each other out. Give
  each install its own logins.
