# Agent Playbook

Guidance for LLM agents driving these tools — **paste-able into a system prompt**.

1. **Always pass the user's coordinates** (`latitude`/`longitude`) — nearly every tool is location-scoped. Get saved addresses from `user_info` if the user says "home" or "work".
2. **Respect chain dependencies**: `context_blob` from `request_ride` must be passed verbatim to `create_ride` and expires quickly; `pricingId` from `get_ride_cancellation_attributes` is required by `cancel_ride`; `catalog_option_id`s from `food_get_item_options` are required to add customizable food items.
3. **Never checkout blind**: call `cart_get_basket_price` first and pass its total as `total_price` to checkout. Confirm order contents, address, and price with the user before any mutating tool.
4. **Payment** defaults are auto-resolved (working card → wallet → cash); only pass `payment_type` if the user asks.
5. **Personalize** with `get_activities(past)` — prefer the user's known favourites over cold search.
6. **Budget for multi-step journeys**: a full food order is ~6+ sequential tool calls; keep your agent's tool-loop limits generous.
7. **Errors** come back as an `error` string field in the tool's structured output, or as MCP tool errors (e.g. a missing `Authorization` header is rejected). Retry idempotent reads; never blind-retry mutating tools.
8. **Long streams**: keepalive comments (`: keepalive`) arrive every 15 s on SSE streams — ignore them.

> **The golden rule**
>
> Read-only tools are free to call while reasoning. **Mutating tools spend the user's money or change their real-world state** — always confirm first, and never retry them blindly after a timeout.
