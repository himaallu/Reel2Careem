# Careem Genie 🧞 — from a food reel to a Careem order

Careem Genie turns food content on Instagram into orders. Share a food reel with the Genie account, or comment
"price?" / "where is this?" / "بكم" on a collab post, and Genie finds that dish on Careem Food near you, compares
it with similar dishes nearby, suggests an add-on and DMs you a link that puts it straight into your Careem basket.

Built in one day at the **Careem Builders hackathon** (November 2026) on top of the
[Careem MCP server](https://builders.careem.com/docs/getting-started) and Claude on Amazon Bedrock.

> **Not an official Careem product.** "Careem" and the Careem logo are trademarks of Careem. This is a hackathon
> prototype; the production Careem MCP places real orders with real money (see [Safety](#safety)).

<p align="center"><img src="app/static/genie_medallion.webp" width="160" alt="Careem Genie mascot"></p>

## What it does

| Flow | What happens |
|---|---|
| **Share a reel → DM** | A user shares a food reel with Genie. Genie reads the reel caption, finds the exact dish (or the closest match) on Careem, adds 2–3 alternatives to compare and one add-on, and replies with a "Pay in Careem" link. |
| **Comment on a collab post** | Genie is a collaborator on creator/restaurant posts. Buying comments ("price?", "where?", "b kam", "بكم", 😋) get a public reply and a private DM with the dish card; spam and chit-chat get nothing. |
| **Confirm page** | The link opens a page with the dish, the alternatives (tap to switch), an add-on toggle and one **Confirm on Careem** button. By default it only fills the user's Careem basket — they pay in the Careem app. |
| **Live dashboard** | Signals, the agent's reasoning streaming step by step, conversion funnel, activity, creators, restaurant areas, most-wanted and missed dishes, and which answers came from the AI vs. a fallback. |

## How it works

```
Instagram ── comments (polled) ──┐
          └─ DMs/reels (webhook) ─┤
                                  ▼
                       pipeline.handle_signal
                                  │  dedupe (each comment / message once)
                                  ▼
                        intent.classify ── keywords (EN / AR / Franco-Arabic) → Claude Haiku if unclear
                                  │
                                  ▼
                         agent.resolve ── Claude Sonnet (Bedrock Converse) + READ-ONLY Careem MCP tools
                                  │       grounding check: every dish/price/ETA must come from a tool result
                                  │
                   fails? ───────►│ fallback ladder: RULES → CACHED → SCRIPTED (always labelled)
                                  ▼
             public reply + DM with link ──► /c/{token} confirm page ──► orders.place (basket by default)
                                  │
                                  ▼
                        SQLite events ──► /dashboard (SSE)
```

* **Agent** (`app/agent.py`) — a Bedrock Converse tool loop over 8 read-only food tools, 25 s budget, ≤ 12 turns,
  parallel tool calls, compacted tool output, and a `submit_result` tool for the final JSON. A grounding check
  drops any alternative or add-on that does not appear in a tool result and fails the run if the main dish is
  invented, so the fallback ladder takes over instead of showing a made-up answer. The same reel shared again
  reuses its earlier LIVE answer for consistency.
* **Fallbacks** (`app/fallbacks.py`) — **RULES** (keyword + restaurant extraction from the caption, fixed Careem
  calls, no LLM), **CACHED** (a real production snapshot in `fixtures/catalog.json`, made by
  `scripts/snapshot.py`), **SCRIPTED** (`fixtures/scripted.json`). Every answer carries its badge; nothing pretends
  to be live.
* **Careem MCP client** (`app/careem_mcp.py`) — one persistent Streamable-HTTP session, allowlists, timeouts,
  read retries, location auto-filled, health reporting. Login is OAuth 2.0 Authorization Code + PKCE
  (`app/careem_auth.py`, `scripts/login_careem.py`).
* **Orders** (`app/orders.py`) — runs only after a human taps Confirm, once per link, never retried.

## Safety

These rules are enforced in code, not just in the prompt:

1. The LLM only ever sees **read-only** Careem tools. Cart and checkout run in plain code after a human taps Confirm.
2. `create_ride`, `cancel_ride` and `save_new_address` are never registered or called; any tool not on an allowlist is refused.
3. **Mutating calls are never retried.** `checkout_attempted` is written before the first cart call; a second tap
   (or five simultaneous taps) makes no Careem call; a timeout tells the user to check their Careem app.
4. `ORDER_MODE=basket` (default) never checks out. `live_checkout` spends real money and also requires `ALLOW_REAL_MONEY=1`.
5. Every comment / message is processed once (Meta allows one private reply per comment).
6. Secrets live only in `.env` (git-ignored) and are never logged.

## Quick start — no accounts needed

Everything runs against a **local Careem MCP sandbox** (made-up Dubai test data, no login, nothing is ever ordered).

```bash
git clone <this repo> && cd Reel2Careem
python3 -m venv .venv && source .venv/bin/activate      # Python 3.10+
pip install -r requirements.txt
cp .env.example .env                                     # CAREEM_TARGET=sandbox by default
python -m sandbox.fetch_spec                             # downloads Careem's public sandbox package
python -m sandbox.ctl start                              # sandbox on http://127.0.0.1:8787/mcp
python -m tests.run_all                                  # every offline test suite
```

Try the pieces on their own:

```bash
python -m app.fallbacks rules --caption "Pepperoni pizza at Pizza Piazza"    # RULES resolver, no LLM
python -m app.intent "b kam?" "yummy 😋" "follow me for giveaways"            # intent (keywords; Bedrock for unclear ones)
python scripts/seed_dashboard.py && python main.py                           # fake history, then open localhost:8000/dashboard
python scripts/seed_dashboard.py --clear                                     # remove the fake rows
```

The dashboard's **Simulate** button injects a comment or reel without Instagram.

## Running it for real

You need: an **Instagram** professional account + Meta app (Instagram API with Instagram Login, testers),
a **Careem MCP client id** ([request one](https://builders.careem.com/docs/authentication)),
**AWS credentials** with Bedrock access to Claude Sonnet and Haiku, and **cloudflared** for a public URL.

1. Fill in `.env` (see the table below).
2. `python scripts/login_careem.py` — Careem login in the browser → `CAREEM_REFRESH_TOKEN` in `.env`.
   Then `python scripts/list_tools.py --target production` should list the tools and say `✅ allowlists OK`.
3. Set `CAREEM_TARGET=production`, `DEMO_LAT` / `DEMO_LNG` (the delivery location).
4. `python -m app.agent --target production --caption "Crispy chicken shawarma at Operation Falafel" --text "price?"`
   should end with `✅ found … mode=live`.
5. `cloudflared tunnel --url http://localhost:8000` → put the URL in `PUBLIC_BASE_URL`.
6. `python main.py`, then point the Meta webhook (Instagram → `messages`) at `<PUBLIC_BASE_URL>/webhook`.
7. Optional: list your demo posts in `fixtures/demo_posts.json` and run `python scripts/snapshot.py` for the CACHED fallback.

After any `.env` change, restart `python main.py`; check with `curl -s localhost:8000/api/health`.

### Configuration (`.env`)

| Variable | What it is |
|---|---|
| `IG_TOKEN`, `IG_USER_ID`, `IG_API` | Instagram API token, Genie's account id, Graph API base URL |
| `IG_WATCH_MEDIA_IDS` | collab post ids to poll for comments, or `auto` for all of Genie's posts |
| `IG_WEBHOOK_VERIFY_TOKEN`, `IG_APP_SECRET` | webhook verification token; app secret to check webhook signatures |
| `CAREEM_TARGET` | `sandbox` (local, default) or `production` |
| `CAREEM_CLIENT_ID`, `CAREEM_REDIRECT_URI`, `CAREEM_SCOPES` | Careem Identity PKCE client |
| `CAREEM_REFRESH_TOKEN` | written by `scripts/login_careem.py` |
| `DEMO_LAT`, `DEMO_LNG` | delivery coordinates sent on every Careem call |
| `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` | Bedrock credentials (keys in `.env` override exported ones) |
| `AGENT_MODEL_ID`, `FAST_MODEL_ID` | Bedrock model ids for the agent and the intent classifier |
| `AGENT_MODE` | `live` / `cached` / `scripted` (switchable from the dashboard) |
| `ORDER_MODE`, `ALLOW_REAL_MONEY` | `basket` (default) / `sandbox_checkout` / `live_checkout` (+ `ALLOW_REAL_MONEY=1`) |
| `PUBLIC_BASE_URL` | public URL for confirm links (cloudflared) |
| `REUSE_SAME_REEL_HOURS` | how long a reel's LIVE answer is reused (default 6; 0 disables) |

## Project layout

```
app/
  agent.py          Bedrock tool loop, grounding check, resolve() with the fallback ladder
  intent.py         buying-intent classifier (keywords EN/AR/Franco-Arabic + Haiku)
  fallbacks.py      RULES / CACHED / SCRIPTED resolvers
  careem_mcp.py     Careem MCP client (allowlists, retries, timeouts, health)
  careem_auth.py    PKCE login helpers + token refresh
  orders.py         confirm → options → basket → price (→ checkout), idempotent
  instagram.py      Graph API calls + webhook parsing
  pollers.py        comment poller, tunnel health
  pipeline.py       handle_signal: dedupe → intent → resolve → reply → DM → events
  messages.py       DM / reply copy
  metrics.py        business metrics for the dashboard (/api/metrics)
  web.py            FastAPI: webhook, confirm page, dashboard, SSE, simulate
  templates/, static/, prompts/careem_playbook.md
sandbox/            Python build of Careem's MCP sandbox (spec downloaded by fetch_spec.py)
scripts/            login, list tools, snapshot, Instagram helpers, dashboard seeding
tests/              offline suites (fake Bedrock client + local sandbox); run_all.py runs them all
fixtures/           snapshot catalog, scripted answers, demo posts, anonymised DM samples
```

`CLAUDE.md` and `TASKS.md` are the build notes the two-person team used with Claude Code during the hackathon.

## Credits

* [Careem MCP](https://builders.careem.com) — tools, agent playbook (`app/prompts/careem_playbook.md`, from
  builders.careem.com) and the sandbox package, which is **downloaded, not redistributed** here.
* Claude Sonnet and Haiku on Amazon Bedrock.
* Genie mascot artwork was generated for this project and shows the Careem logo; replace it if you reuse the code
  outside a Careem context.

## License

[MIT](LICENSE) for the code in this repository. Careem's sandbox package, agent playbook, name and logo remain
Careem's and are not covered by this license.
