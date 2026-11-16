# Careem Genie — project bible for Claude Code

Read this fully before writing code. Hackathon build, live 3-minute TECHNICAL demo at 18:00 (no slides). Code freeze 16:00.
Two people work in parallel: **P1 = Brain** (Careem MCP, agent, orders, fallbacks) and **P2 = Surfaces** (Instagram, confirm page, dashboard, simulator). See TASKS.md. Only edit files your person owns; shared contracts change only with both agreeing.

## What the demo shows (in this order)
1. **Share a reel → DM.** A user shares a food reel to the Instagram account **Careem Genie (demo)**. Genie replies in DM: the exact dish on Careem (or closest match), 2–3 similar options with prices to compare, one add-on suggestion, and a "Pay in Careem" link.
2. **Collab post → comment → reply + DM.** Genie is a *collaborator* on creator/restaurant posts (Instagram Collab posts, so they are Genie's own media). Someone comments "yummy" / "price?" / "where is this?" / "بكم" / "b kam". Genie replies publicly ("Sent you a DM 📩 — check message requests if you don't follow us") and sends one private reply DM with the dish card + link.
3. **Dashboard.** Live and impressive: signal feed, agent reasoning streaming step by step, KPIs, funnel, leaderboards by reel / creator / restaurant / platform, missed demand, system health.

## Hard safety rules (never break)
1. The LLM agent gets **read-only** Careem tools only. Cart/checkout run in plain code, only after a human taps Confirm.
2. `create_ride`, `cancel_ride`, `save_new_address` are **never registered or called**.
3. **Never retry a mutating call.** Write `checkout_attempted=1` to DB before calling; on timeout show "Check your Careem app". Reads may retry ≤2 times.
4. **Dedupe**: every comment ID and DM message ID is processed once. Meta allows ONE private reply per comment (within 7 days).
5. Secrets only in `.env` (git-ignored). Never print tokens. Never commit `.env`, `fixtures/*token*`.
6. Every fallback is **visible** on the dashboard (badge: LIVE / CACHED / SCRIPTED). We never pretend.

## Stack
Python 3.11 · FastAPI + uvicorn · `mcp` Python SDK (Streamable HTTP client) · `boto3` Bedrock Runtime **Converse** API with tool use · `httpx` · SQLite (`genie.db`) · Jinja2 + Tailwind CDN + Chart.js + SSE for the dashboard · cloudflared tunnel for public confirm links. Runs on a laptop.

## Environment (.env, see .env.example)
```
# Instagram API with Instagram Login (graph.instagram.com)
IG_TOKEN=            IG_USER_ID=          IG_API=https://graph.instagram.com/v23.0   # use the version the Meta dashboard shows
IG_WATCH_MEDIA_IDS=  # comma-separated collab post IDs to watch for comments (auto-filled by scripts/list_media.py)
# Careem MCP (hackathon production client, public PKCE client — no secret)
CAREEM_MCP_URL=https://mcp.careemapis.com/mcp
CAREEM_SANDBOX_MCP_URL=http://127.0.0.1:8787/mcp      # local sandbox (no rides), for cart/checkout tests
CAREEM_CLIENT_ID=<your Careem client id>
CAREEM_REDIRECT_URI=http://localhost:8080/callback
CAREEM_REFRESH_TOKEN=   # written by scripts/login_careem.py
DEMO_LAT=  DEMO_LNG=     # venue coordinates — sent on every Careem call
# Bedrock (organizer setup, region us-east-1)
AWS_REGION=us-east-1
AGENT_MODEL_ID=global.anthropic.claude-sonnet-5-5
FAST_MODEL_ID=global.anthropic.claude-haiku-5-5
# Modes
PUBLIC_BASE_URL=     # cloudflared URL for confirm links
ORDER_MODE=basket    # basket | sandbox_checkout | live_checkout
AGENT_MODE=live      # live | cached | scripted   (dashboard can switch at runtime)
POLL_SECONDS=4
```

## Repo layout and owners
```
app/
  contracts.py      [P1, shared]  dataclasses + JSON schemas below. FROZEN after 12:00 unless both agree.
  db.py             [P1, shared]  SQLite schema + helpers. FROZEN after 12:00.
  events.py         [P2, shared]  emit(signal_id, stage, detail, level) → DB + SSE broadcast
  careem_auth.py    [P1]  PKCE login helpers + token refresh
  careem_mcp.py     [P1]  MCP session, READ_ONLY allowlist, call(), list_tools(), timeouts
  agent.py          [P1]  resolve(signal) -> AgentResult  (Bedrock tool loop + fallback ladder)
  fallbacks.py      [P1]  rule-based resolver, cached catalog, scripted results, canned messages
  orders.py         [P1]  confirm → basket/price/checkout per ORDER_MODE, idempotent
  instagram.py      [P2]  Graph API calls (comments, replies, conversations, messages, media)
  pollers.py        [P2]  comment poller + DM poller → handle_signal()
  pipeline.py       [P2]  handle_signal(): dedupe → intent → agent.resolve → reply → DM → events
  messages.py       [P2]  DM/comment copy templates (uses AgentResult)
  web.py            [P2]  FastAPI routes: /c/{token}, /api/*, /dashboard, /api/stream (SSE), /simulate
  templates/        [P2]  confirm.html, dashboard.html
scripts/
  login_careem.py   [P1]  one-time PKCE login on laptop → refresh token into .env
  list_tools.py     [P1]  print Careem tools, ⚠️ on mutating ones
  snapshot.py       [P1]  capture REAL production results for the demo dishes → fixtures/catalog.json
  list_media.py     [P2]  list Genie's media (incl. collab posts) → IG_WATCH_MEDIA_IDS
  test_instagram.py [P2]  smoke test: me, media, comments, conversations
  seed_dashboard.py [P2]  fake history so the dashboard looks alive during development (clearly flagged demo=1)
fixtures/catalog.json, fixtures/scripted.json   (P1)
main.py   starts web + pollers
```

## Contracts (app/contracts.py)
```python
Signal:       id, platform("instagram"), kind("dm_share"|"comment"), source_id(comment_id|message_id),
              user_ref(IGSID or comment author), text, media_id|None, media_url|None, caption|None,
              creator|None, restaurant|None, received_at
AgentResult:  status("found"|"closest"|"none"|"not_buying"), mode("live"|"cached"|"scripted"|"rules"),
              wanted(str), service("food"|"groceries"|"shops"), merchant{name,id}, item{name,id,price_aed,eta_min,image_url|None},
              alternatives[{merchant,item,price_aed,eta_min}] (≤3), addon{name,price_aed}|None,
              confidence(0..1), missed_demand(str|None), steps[str], latency_ms
Match:        token, signal_id, result(AgentResult), created_at
Order:        match_token, status("basket"|"checked_out"|"failed"|"handed_to_app"), aed, service, created_at
```

## DB (app/db.py)
signals(id PK, platform, kind, source_id UNIQUE, user_ref, text, media_id, caption, creator, restaurant, received_at)
events(id PK, signal_id, stage, detail, level, ts)      -- stages: received, intent, agent_step, tool_call, result, reply_public, dm_sent, confirm_open, order, fallback, error
matches(token PK, signal_id, result_json, confidence, mode, created_at)
orders(token PK, status, aed, service, checkout_attempted, created_at)
missed(id PK, signal_id, wanted, ts)
health(component PK, ok, detail, ts)                     -- instagram, careem_mcp, bedrock, tunnel

## Careem MCP facts
- Login: Authorization Code + PKCE (S256) at Careem Identity; discover via `https://mcp.careemapis.com/.well-known/oauth-protected-resource` → identity `/.well-known/openid-configuration`. Scopes `openid profile offline_access`. Redirect must be exactly `http://localhost:8080/callback`. Bearer token on every MCP request.
- Pass latitude/longitude on almost every call. A food order takes ~6+ sequential calls → agent loop limit 12.
- Customisable items need `food_get_item_options` first. Price with `cart_get_basket_price` and pass its total as `total_price` to checkout. Payment auto-resolves (card → wallet → cash); don't pass payment_type.
- Production = real account, real money. Sandbox = made-up data (no rides). Use production for reads, sandbox for checkout tests.
- READ_ONLY allowlist (verify names with list_tools): food_merchant_search, food_dish_search, food_get_restaurant_menu, food_get_item_options, food_personalized_search, search_groceries_items, get_top_user_items, get_all_deals, global_search, user_info, get_activities, reverse_geocode.
- Code-only (after Confirm): cart_add_item(s), cart_get_basket_price, cart_checkout, create_grocery_cart_with_quantities, cart_grocery_checkout.

## Instagram facts (Instagram API with Instagram Login, Standard Access, testers only)
- Comments: `GET /{media_id}/comments?fields=id,text,timestamp,username`. Public reply: `POST /{comment_id}/replies?message=…`.
- Private reply to a comment: `POST /{IG_USER_ID}/messages` body `{"recipient":{"comment_id":…},"message":{"text":…}}` — once per comment, ≤7 days.
- DMs: `GET /me/conversations?platform=instagram` then messages (fields id,created_time,from,message,attachments/shares — VERIFY what a shared reel looks like; P2 task 1). Reply: `POST /{IG_USER_ID}/messages` `{"recipient":{"id":IGSID},"message":{"text":…}}`. Only within 24 h of the user's last message.
- Webhooks need a Live app + Advanced Access for comments → we **poll** every POLL_SECONDS instead.
- Only accounts with an app role (Instagram Testers) work. Testers should follow Genie so DMs don't land in Requests.

## Agent (app/agent.py) — resolve(signal) -> AgentResult
1. Input = comment/DM text + caption (+ creator/restaurant names). If a shared reel has no caption but has a media/thumbnail URL: download the thumbnail, send it as an image to Claude to identify the dish (vision).
2. Bedrock Converse, model AGENT_MODEL_ID, tools = READ_ONLY specs, max 12 turns, overall timeout 20 s (botocore read_timeout 15, max_attempts 1).
3. Find the dish → also find 2–3 similar dishes at other merchants (food_dish_search) to compare prices → one add-on from the menu or deals.
4. Return ONLY JSON matching AgentResult. Every value must come from a tool result. Emit an `agent_step` / `tool_call` event for every turn (for the dashboard).
System prompt: see fallbacks/prompt in agent.py; append Careem's agent playbook text.

## Fallback ladder (deterministic, always visible)
| Failure | Detect | Fallback | Badge |
|---|---|---|---|
| Bedrock error / timeout / blocked | exception or >20 s | `fallbacks.rule_resolve()`: keyword extraction from caption/comment (dish & restaurant dictionary built from fixtures) → direct read-only MCP calls in fixed order (food_dish_search → food_merchant_search) → top result + top 3 alternatives | RULES |
| Careem MCP down / 401 / timeout | exception, health check | `fallbacks.cached_resolve()`: look up fixtures/catalog.json (REAL production snapshot taken by scripts/snapshot.py) | CACHED (snapshot time shown) |
| Both down, or AGENT_MODE=scripted | — | fixtures/scripted.json keyed by media_id/keyword | SCRIPTED |
| Instagram API down | poll errors | `/simulate` form on dashboard injects a Signal; reply rendered in the dashboard's phone mock | SIMULATED |
| Checkout fails / times out | exception | never retry; status "handed_to_app"; page says "Finish in your Careem app" | — |
| Nothing found | status none | DM: closest 3 alternatives or "not on Careem yet"; log to missed | — |
| Not a buying comment | intent=false | no reply (no spam) | — |

Canned copy lives in `app/messages.py`, e.g.:
- comment reply: "Sent you a DM 📩 — check your message requests if you don't follow us!"
- found: "🧞 Found it on Careem! {item} from {merchant} · AED {price} · ~{eta} min\nSimilar near you: {alt1} AED {p1} · {alt2} AED {p2}\nAdd {addon} for AED {ap}?\nPay in Careem 👉 {link}"
- closest: "Couldn't find that exact dish on Careem, but these are close: …"
- careem_down: "Careem is taking a breather 😅 — here's what I found earlier today: …"
- none: "Not on Careem yet — I've told the team you want it! 🙌"

## Confirm page /c/{token}
Shows item, merchant, price, ETA, alternatives, add-on toggle, delivery location label, one big **Confirm on Careem** button (disabled after one tap). On POST: orders.place(token) per ORDER_MODE:
- `basket`: cart_add_item → cart_get_basket_price → status "basket" → "Your basket is ready — complete payment in the Careem app" (default; no money moves).
- `sandbox_checkout`: same against sandbox + cart_checkout.
- `live_checkout`: production checkout, real money — only if the team decides to.

## Dashboard (/dashboard) — make it genuinely impressive
Dark Careem-green theme. Top: health pills (Instagram, Careem MCP, Bedrock, tunnel, last poll Ns ago) + mode badge + mode switcher. KPI row (signals, DMs sent, confirm opens, orders, AED, avg latency). Left: live signal feed (cards: platform icon, kind, text, reel thumbnail/caption, status). Center: **agent reasoning timeline** streaming via SSE (each tool call with args summary + ms). Right: phone mock showing the exact DM Genie sent. Bottom: funnel chart, leaderboard tables (by reel, creator, restaurant, platform), missed-demand list, latency per stage. Must look good on a projector at 1920×1080.

## Conventions
- Small functions, type hints, `logging` (never print secrets). One `.env`. All times UTC in DB, Dubai time in UI.
- Every external call: timeout, try/except, emit event, update health.
- Commit after each checkpoint; branch per person (`p1-brain`, `p2-surfaces`), merge to `main` at sync points.
