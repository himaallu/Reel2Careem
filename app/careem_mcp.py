"""[P1] Careem MCP client: persistent session, allowlists, timeouts, retries, health.

Sync API (safe to call from any thread; the MCP session lives on its own event loop thread):

    from app import careem_mcp
    mcp = careem_mcp.get()                       # target from CAREEM_TARGET (production|sandbox)
    mcp.call("food_dish_search", {"query": "shawarma"})        # READ_ONLY only; lat/lng auto-filled
    mcp.cart_call("cart_get_basket_price", {...})               # code-only tools (orders.py), never retried
    mcp.tool_specs()                             # READ_ONLY definitions the server actually has (for Bedrock)

Safety (CLAUDE.md hard rules 1–3):
  * call() only runs READ_ONLY tools. Reads retry ≤ 2 times on transport errors / timeouts.
  * cart_call() only runs CODE_ONLY tools, exactly once — mutating calls are never retried.
  * NEVER tools (rides, save_new_address) and anything not listed are refused before any I/O.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import AsyncExitStack
from typing import Any, Literal, Optional

from dotenv import load_dotenv

from app import careem_auth, db

load_dotenv(careem_auth.ENV_PATH)
log = logging.getLogger("genie.careem_mcp")

Target = Literal["production", "sandbox"]

# Tools the LLM agent may use (all readOnlyHint=true in the tool catalog).
READ_ONLY: frozenset[str] = frozenset({
    "food_merchant_search", "food_dish_search", "food_dynamic_search",
    "food_get_restaurant_menu", "food_get_item_options", "food_get_items_options",
    "food_search_restaurant_items",
    "search_groceries_items", "search_groceries_get_top_user_items", "search_groceries_get_all_deals",
    "global_search", "user_info", "get_activities", "reverse_geocode",
})
# Tools only plain code may call, after a human taps Confirm (orders.py).
CODE_ONLY_READS: frozenset[str] = frozenset({"cart_get_basket", "cart_get_basket_price"})
MUTATING: frozenset[str] = frozenset({
    "cart_add_item", "cart_add_items", "cart_checkout",
    "create_grocery_cart_with_quantities", "cart_grocery_checkout",
})
CODE_ONLY: frozenset[str] = CODE_ONLY_READS | MUTATING
# Never registered, never called — refused even if someone adds them to a list above.
NEVER: frozenset[str] = frozenset({"create_ride", "cancel_ride", "save_new_address"})

READ_TIMEOUT_S = float(os.getenv("CAREEM_CALL_TIMEOUT", "10"))
# Production search answers can exceed the MCP SDK's 1 MB per-event default (seen live: food_dish_search).
MAX_EVENT_BYTES = int(float(os.getenv("CAREEM_MAX_EVENT_MB", "16")) * 1024 * 1024)
# Smaller pages = smaller, faster answers. Applied only when the caller didn't set them.
SEARCH_DEFAULTS: dict[str, dict[str, int]] = {
    "food_dish_search": {"limit": 6, "items_limit": 3},
    "food_dynamic_search": {"limit": 6, "items_limit": 3},
    "food_merchant_search": {"limit": 6},
}
READ_RETRIES = 2
SANDBOX_HOME = (25.0866, 55.1474)  # sandbox persona's Home (Dubai Marina)


class CareemError(RuntimeError):
    """Transport/protocol failure talking to the Careem MCP (message never contains tokens)."""


class CareemTimeout(CareemError):
    """The call did not finish in time. For a mutating call the outcome is UNKNOWN — do not retry."""


class CareemToolError(CareemError):
    """The tool returned isError=true (schema validation, auth failure, hard failure)."""


class ToolNotAllowed(PermissionError):
    pass


def _target_from_env() -> Target:
    t = os.getenv("CAREEM_TARGET", "production").strip().lower()
    return "sandbox" if t == "sandbox" else "production"


class CareemMCP:
    def __init__(self, target: Target = "production", url: Optional[str] = None) -> None:
        self.target = target
        self.url = url or (
            os.getenv("CAREEM_SANDBOX_MCP_URL", "http://127.0.0.1:8787/mcp") if target == "sandbox"
            else os.getenv("CAREEM_MCP_URL", "https://mcp.careemapis.com/mcp")
        )
        self.needs_auth = target == "production"
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, name=f"careem-mcp-{target}", daemon=True).start()
        self._session: Any = None
        self._closer: Optional[asyncio.Event] = None
        self._owner: Optional[asyncio.Task] = None
        self._connect_lock: Optional[asyncio.Lock] = None
        self._tools: dict[str, dict[str, Any]] = {}
        self._saw_401 = False

    # ------------------------------------------------------------ public sync API
    def call(self, name: str, args: Optional[dict[str, Any]] = None, *,
             signal_id: Optional[str] = None, timeout: float = READ_TIMEOUT_S,
             retries: int = READ_RETRIES) -> dict[str, Any]:
        """Run a READ_ONLY tool. Returns the tool's output object (may contain an "error" field).

        `timeout` is per attempt; `retries` (≤ READ_RETRIES) lets time-boxed callers like the agent use fewer.
        """
        self._check_allowed(name, code=False)
        return self._run(name, args, signal_id=signal_id, timeout=timeout,
                         retries=max(0, min(retries, READ_RETRIES)))

    def cart_call(self, name: str, args: Optional[dict[str, Any]] = None, *,
                  signal_id: Optional[str] = None, timeout: float = READ_TIMEOUT_S) -> dict[str, Any]:
        """Run a CODE_ONLY tool (cart/checkout) exactly once. Only orders.py should use this."""
        self._check_allowed(name, code=True)
        # Retry only if we think it's a read AND the live server agrees (production marks
        # cart_get_basket / cart_get_basket_price as not read-only, so they are never retried there).
        ann = (self._tools.get(name) or {}).get("annotations") or {}
        retries = READ_RETRIES if name in CODE_ONLY_READS and ann.get("readOnlyHint") is True else 0
        return self._run(name, args, signal_id=signal_id, timeout=timeout, retries=retries)

    def list_tools(self, refresh: bool = False, timeout: float = READ_TIMEOUT_S) -> list[dict[str, Any]]:
        """Every tool the server advertises (raw definitions, including ones we never call)."""
        if refresh or not self._tools:
            self._submit(self._list_tools(timeout), timeout + 1)
        return list(self._tools.values())

    def tool_specs(self) -> list[dict[str, Any]]:
        """READ_ONLY tools present on the server and annotated read-only: name/description/inputSchema."""
        out = []
        for t in self.list_tools():
            if t["name"] in READ_ONLY and (t.get("annotations") or {}).get("readOnlyHint") is not False:
                out.append({"name": t["name"], "description": t.get("description", ""),
                            "inputSchema": t.get("inputSchema", {"type": "object"})})
        return out

    def ping(self) -> bool:
        """Health check: (re)connect and list tools."""
        try:
            self.list_tools(refresh=True)
            return True
        except Exception as e:  # noqa: BLE001
            self._health(False, f"{type(e).__name__}: {e}")
            return False

    def close(self) -> None:
        self._reset()

    def _reset(self) -> None:
        """Drop the session so the next call reconnects. Never raises."""
        try:
            self._submit(self._disconnect(), 5)
        except BaseException:  # noqa: BLE001
            self._session = None

    # ------------------------------------------------------------ guards / args
    @staticmethod
    def _check_allowed(name: str, *, code: bool) -> None:
        if name in NEVER:
            raise ToolNotAllowed(f"{name} is never allowed")
        allowed = (READ_ONLY | CODE_ONLY) if code else READ_ONLY
        if name not in allowed:
            raise ToolNotAllowed(f"{name} is not in the {'code' if code else 'read-only'} allowlist")

    def _with_location(self, name: str, args: Optional[dict[str, Any]]) -> dict[str, Any]:
        args = dict(args or {})
        props = ((self._tools.get(name) or {}).get("inputSchema") or {}).get("properties") or {}
        for k, v in SEARCH_DEFAULTS.get(name, {}).items():
            if k in props:
                args.setdefault(k, v)
        if ("latitude" in props or not self._tools) and "latitude" not in args:
            loc = self._demo_location()
            if loc:
                args.setdefault("latitude", loc[0])
                args.setdefault("longitude", loc[1])
        return args

    def _demo_location(self) -> Optional[tuple[float, float]]:
        lat, lng = os.getenv("DEMO_LAT", "").strip(), os.getenv("DEMO_LNG", "").strip()
        if lat and lng:
            return float(lat), float(lng)
        return SANDBOX_HOME if self.target == "sandbox" else None

    # ------------------------------------------------------------ execution
    def _run(self, name: str, args: Optional[dict[str, Any]], *, signal_id: Optional[str],
             timeout: float, retries: int) -> dict[str, Any]:
        attempt = 0
        while True:
            t0 = time.monotonic()
            try:
                # One time budget per attempt covers connect + tools/list + the call itself.
                if not self._tools:
                    self.list_tools(timeout=timeout)
                left = max(1.0, timeout - (time.monotonic() - t0))
                out = self._submit(self._call(name, self._with_location(name, args), left), left + 1)
                ms = int((time.monotonic() - t0) * 1000)
                self._health(True, f"{name} ok {ms}ms")
                self._event(signal_id, name, args, ms, out)
                return out
            except CareemToolError as e:
                ms = int((time.monotonic() - t0) * 1000)
                self._event(signal_id, name, args, ms, {"error": str(e)}, level="warn")
                raise
            except (CareemError, OSError) as e:
                ms = int((time.monotonic() - t0) * 1000)
                self._health(False, f"{name}: {type(e).__name__}: {e}")
                self._event(signal_id, name, args, ms, {"error": f"{type(e).__name__}: {e}"}, level="error")
                if attempt >= retries:
                    raise
                attempt += 1
                log.warning("%s failed (%s: %s); retry %d/%d", name, type(e).__name__, str(e)[:160],
                            attempt, retries)
                self._reset()

    def _submit(self, coro: Any, timeout: float) -> Any:
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout)
        except FutureTimeout:
            fut.cancel()
            raise CareemTimeout(f"no answer within {timeout:.0f}s") from None

    # ------------------------------------------------------------ async internals (MCP loop thread)
    async def _call(self, name: str, args: dict[str, Any], timeout: float) -> dict[str, Any]:
        session = await self._ensure_session(timeout)
        try:
            r = await asyncio.wait_for(session.call_tool(name, args), timeout)
        except asyncio.TimeoutError:
            raise CareemTimeout(f"{name}: no answer within {timeout:.0f}s") from None
        except Exception as e:  # noqa: BLE001
            await self._handle_failure()
            raise CareemError(f"{name}: {type(e).__name__}: {e}") from None
        text = "".join(getattr(c, "text", "") for c in (r.content or []))
        if r.is_error:
            if self._looks_like_auth(text):
                await self._handle_failure(auth=True)
            raise CareemToolError(f"{name}: {text[:300]}")
        if isinstance(r.structured_content, dict):
            return r.structured_content
        try:
            v = json.loads(text)
            return v if isinstance(v, dict) else {"result": v}
        except ValueError:
            return {"text": text}

    async def _list_tools(self, timeout: float = READ_TIMEOUT_S) -> None:
        session = await self._ensure_session(timeout)
        try:
            res = await asyncio.wait_for(session.list_tools(), timeout)
        except Exception as e:  # noqa: BLE001
            await self._handle_failure()
            raise CareemError(f"tools/list: {type(e).__name__}: {e}") from None
        self._tools = {t.name: t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in res.tools}
        self._health(True, f"{len(self._tools)} tools")

    async def _ensure_session(self, timeout: float = READ_TIMEOUT_S) -> Any:
        if self._connect_lock is None:
            self._connect_lock = asyncio.Lock()
        async with self._connect_lock:
            if self._session is not None:
                return self._session
            ready: asyncio.Future = asyncio.get_running_loop().create_future()
            self._closer = asyncio.Event()
            self._owner = asyncio.create_task(self._own_session(ready, self._closer))
            try:
                self._session = await asyncio.wait_for(ready, timeout)
            except asyncio.TimeoutError:
                self._closer.set()
                self._owner.cancel()
                raise CareemTimeout("connect: no answer") from None
            except careem_auth.CareemAuthError as e:
                raise CareemError(f"auth: {e}") from None
            except Exception as e:  # noqa: BLE001
                root = _root_cause(e)
                if isinstance(root, careem_auth.CareemAuthError):
                    raise CareemError(f"auth: {root}") from None
                raise CareemError(f"connect: {type(root).__name__}: {root}") from None
            return self._session

    async def _own_session(self, ready: asyncio.Future, closer: asyncio.Event) -> None:
        """Enter and exit the transport contexts in ONE task (anyio cancel scopes require it)."""
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async def add_auth(request: Any) -> None:
            if self.needs_auth:
                token = await asyncio.to_thread(careem_auth.get_access_token)
                request.headers["Authorization"] = f"Bearer {token}"

        async def watch_status(response: Any) -> None:
            if response.status_code == 401:
                self._saw_401 = True

        try:
            async with AsyncExitStack() as stack:
                http = await stack.enter_async_context(httpx2.AsyncClient(
                    timeout=httpx2.Timeout(READ_TIMEOUT_S, read=300),
                    event_hooks={"request": [add_auth], "response": [watch_status]},
                ))
                read, write, *_ = await stack.enter_async_context(streamable_http_client(self.url, http_client=http, max_sse_event_size=MAX_EVENT_BYTES))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                ready.set_result(session)
                await closer.wait()
        except Exception as e:  # noqa: BLE001
            if not ready.done():
                ready.set_exception(e)
            else:
                log.warning("MCP session ended: %s: %s", type(e).__name__, e)
        finally:
            if self._closer is closer:
                self._session = None

    async def _disconnect(self) -> None:
        if self._closer is not None:
            self._closer.set()
        self._session = None
        if self._owner is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._owner), 3)
            except (Exception, asyncio.CancelledError):  # noqa: BLE001 — owner may be cancelled/failed
                pass
        self._owner = None

    async def _handle_failure(self, auth: bool = False) -> None:
        if (auth or self._saw_401) and self.needs_auth:
            careem_auth.invalidate()
        self._saw_401 = False
        await self._disconnect()

    @staticmethod
    def _looks_like_auth(text: str) -> bool:
        t = text.lower()
        return any(s in t for s in ("authorization header", "subject claim", "x-user-id", "unauthorized"))

    # ------------------------------------------------------------ observability
    def _health(self, ok: bool, detail: str) -> None:
        try:
            db.set_health("careem_mcp", ok, f"[{self.target}] {detail}")
        except Exception:  # noqa: BLE001 — health must never break a call
            pass

    def _event(self, signal_id: Optional[str], name: str, args: dict[str, Any], ms: int,
               out: dict[str, Any], level: str = "info") -> None:
        if signal_id is None:
            return
        try:
            from app.events import emit
            summary = {k: v for k, v in args.items() if k not in ("latitude", "longitude")}
            emit(signal_id, "tool_call", {
                "tool": name, "args": summary, "ms": ms, "target": self.target,
                "error": out.get("error") if isinstance(out, dict) else None,
            }, level)
        except Exception:  # noqa: BLE001
            pass


def _root_cause(e: BaseException) -> BaseException:
    """Unwrap anyio/asyncio ExceptionGroups to the first real error."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return e


_clients: dict[str, CareemMCP] = {}
_clients_lock = threading.Lock()


def get(target: Optional[Target] = None) -> CareemMCP:
    """Shared client per target. Default target from CAREEM_TARGET (production|sandbox)."""
    t = target or _target_from_env()
    with _clients_lock:
        if t not in _clients:
            _clients[t] = CareemMCP(t)
        return _clients[t]
