# Careem Genie — task split (build until 16:00, then rehearse)

P1 = **Brain** (Himasri): Careem MCP, agent, fallbacks, orders.
P2 = **Surfaces** (teammate): Instagram, pipeline, confirm page, dashboard, simulator.
Both run Claude Code on Opus with CLAUDE.md in the repo root. P2 works against `fake_resolve()` until P1's agent is ready, so nobody waits.

## Sync points (both stop, merge to main, test together)
| Time | Sync | Pass when |
|---|---|---|
| **12:00** | Repo skeleton + contracts.py + db.py merged | Both branches import contracts; `python main.py` starts |
| **13:30** | Halfway | P1: agent CLI returns a real dish from production. P2: comment → public reply + DM works with fake_resolve |
| **14:30** | **Flow B end-to-end** (comment on collab post → reply + DM with real dish → confirm → basket) | 3 of 3 tries |
| **15:15** | **Flow A end-to-end** (shared reel in DM → DM reply with dish, alternatives, add-on, link) | 3 of 3 tries |
| **16:00** | **FREEZE**: dashboard live, fallbacks tested by unplugging each service | All fallback rows pass |
| 16:00–17:00 | Snapshot refresh, 5 full rehearsals, record backup video, then only bug fixes | 5 of 5, under 90 s for all three parts |

## P1 — Brain
| # | Task | Done when | Time |
|---|---|---|---|
| P1-0 | Repo skeleton, requirements.txt, .env.example, .gitignore, contracts.py, db.py | merged by 12:00 | 11:30–12:00 |
| P1-1 | `scripts/login_careem.py` PKCE login (localhost:8080 callback) → refresh token in .env; `careem_auth.refresh()` | token refreshes; `list_tools.py` prints tools with ⚠️ | 12:00–12:30 |
| P1-2 | `careem_mcp.py`: session, READ_ONLY allowlist, `call(name,args)` with timeout 10 s, 2 retries for reads, health updates; sandbox URL switch | read tools callable from a REPL | 12:30–12:50 |
| P1-3 | `agent.py`: Bedrock Converse tool loop (Sonnet), MCP tool specs → Bedrock toolSpec, system prompt + playbook, JSON AgentResult, events per step, 20 s budget | CLI: caption + "price?" → real dish, 3 alternatives, add-on, < 20 s | 12:50–13:30 |
| P1-4 | Vision path: thumbnail URL → Claude image input → dish name → same search | a reel thumbnail with no caption still resolves | 13:30–14:00 |
| P1-5 | `fallbacks.py`: rule_resolve (keywords → fixed MCP calls), cached_resolve (fixtures/catalog.json), scripted (fixtures/scripted.json), mode switch | each mode returns a valid AgentResult with correct `mode` | 14:00–14:30 |
| P1-6 | `scripts/snapshot.py`: for each demo dish/restaurant, run real read-only calls on production and save results + timestamp | fixtures/catalog.json has all demo posts | 14:30–14:45 |
| P1-7 | `orders.py`: ORDER_MODE basket/sandbox_checkout/live_checkout; idempotent; checkout_attempted before call; never retry | double-tap places nothing twice | 14:45–15:15 |
| P1-8 | Intent check (FAST_MODEL_ID with keyword pre-filter: yummy, price, where, how, بكم, b kam, wen, delivery…) + Bedrock-down fallback to keywords only | 10 test comments classified right | 15:15–15:40 |
| P1-9 | Kill tests: wrong MCP URL, bad AWS creds, timeout → correct fallback badge | all ladder rows pass | 15:40–16:00 |

## P2 — Surfaces
| # | Task | Done when | Time |
|---|---|---|---|
| P2-0 | Meta app check: Genie account (Creator, public, name "Careem Genie (demo)"), testers accepted and following Genie, token in .env | `scripts/test_instagram.py` prints me + media | 11:30–12:00 |
| P2-1 | **Verify DMs first**: tester shares a reel to Genie → `GET /me/conversations` → inspect the message JSON. Write down exactly where the reel URL/caption/thumbnail appears | JSON sample saved in `fixtures/dm_share_sample.json` (no tokens) | 12:00–12:30 |
| P2-2 | Collab posts: publish 2 demo posts as **Collab** between a creator/restaurant test account and Genie (caption names dish + restaurant); `list_media.py` fills IG_WATCH_MEDIA_IDS | Genie's token sees the collab posts and their comments | 12:30–12:45 |
| P2-3 | `instagram.py` + `pollers.py`: comment poller and DM poller (every 4 s, dedupe by id, health updates) | new comment / DM appears as Signal in DB within 5 s | 12:45–13:30 |
| P2-4 | `pipeline.py` + `messages.py`: handle_signal → intent → resolve (fake first) → public reply + private reply (comments) or DM reply (shares) → events; one reply per id | flow B works with fake_resolve | 13:30–14:15 |
| P2-5 | `web.py` confirm page `/c/{token}` + cloudflared tunnel + PUBLIC_BASE_URL | link opens on a phone on mobile data; Confirm calls orders.place | 14:15–14:45 |
| P2-6 | `events.py` SSE stream + `/simulate` form (inject a Signal when Instagram is down) | simulated signal flows through the full pipeline | 14:45–15:00 |
| P2-7 | **Dashboard** (see CLAUDE.md): health pills, mode switch, KPIs, live feed, reasoning timeline (SSE), phone mock of sent DM, funnel, leaderboards, missed demand, latency. Start with `seed_dashboard.py` data from 13:30 in parallel if time allows | looks great at 1920×1080; updates live during a test comment | 15:00–16:00 |

## Demo assets (both, before 14:30)
- [ ] Genie account: Creator, public, profile pic, bio "Send me any reel 🧞 I'll find it on Careem. Hackathon demo."
- [ ] 2 collab posts (creator + restaurant) with dishes that exist on production near the venue
- [ ] 3 reels to share for Flow A: 1 exact match, 1 closest match, 1 not on Careem (shows missed demand)
- [ ] 2–3 tester phones, logged in, following Genie, charged
- [ ] DEMO_LAT/LNG = venue
- [ ] Decide ORDER_MODE (default `basket`)

## Demo script (90 s, no slides)
1. (30 s) Scroll a reel on phone → share to Careem Genie → dashboard timeline streams the agent → DM arrives: dish, compare prices, add-on, Pay in Careem → tap → basket ready.
2. (25 s) Open the collab post → tester comments "where is this?" → public reply appears → DM arrives.
3. (25 s) Dashboard: funnel, leaderboard by reel/creator/restaurant, missed demand, health pills.
If anything fails live: switch mode badge (CACHED/SCRIPTED) or use /simulate, and say it plainly. Backup video on second screen.

## Claude Code starter prompts
**P1 first prompt:** "Read CLAUDE.md and TASKS.md. I'm P1. Do P1-0 now: create the repo skeleton, requirements.txt, .env.example, .gitignore, app/contracts.py and app/db.py exactly per CLAUDE.md. Show me the files, then stop."
**P2 first prompt:** "Read CLAUDE.md and TASKS.md. I'm P2. Do P2-0 and P2-1: write scripts/test_instagram.py and a script that lists conversations and prints the latest message JSON with all fields so we can see how a shared reel arrives. Don't touch app/contracts.py or app/db.py."
