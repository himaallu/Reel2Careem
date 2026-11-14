"""Acceptance tests from sandbox/spec/BUILD.md §7, run against a running sandbox over HTTP.

    python -m sandbox.server &        # or: python -m sandbox.ctl start
    python -m sandbox.test_acceptance
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from typing import Any

from jsonschema import Draft202012Validator
from mcp import Client
from mcp.shared.exceptions import MCPError

from sandbox import server as sb

URL = os.getenv("SANDBOX_URL", "http://127.0.0.1:8787/mcp")
LOC = {"latitude": 25.0866, "longitude": 55.1474}
BANNED = ("mock", "sandbox", "sbx", "fake", "dummy", "example.com")

failures: list[str] = []


def check(n: int, cond: bool, what: str) -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {n:>2}. {what}")
    if not cond:
        failures.append(f"{n}. {what}")


def sc(r: Any) -> dict[str, Any]:
    return r.structured_content or json.loads(r.content[0].text)


def set_overrides(d: dict[str, str]) -> None:
    sb.OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    sb.OVERRIDES_PATH.write_text(json.dumps(d))


# ---- test 11 helpers (FORMAT.md "Keeping scenarios honest")
def sample(schema: dict[str, Any]) -> Any:
    if "enum" in schema:
        return schema["enum"][0]
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    if t == "string":
        return "sample"
    if t in ("integer", "number"):
        return 1
    if t == "boolean":
        return False
    if t == "array":
        return [sample(schema.get("items") or {"type": "string"})]
    if t == "object" or "properties" in schema:
        return {k: sample(v) for k, v in (schema.get("properties") or {}).items()}
    return "sample"


def overlay(base: Any, match: Any) -> Any:
    if isinstance(match, dict):
        out = copy.deepcopy(base) if isinstance(base, dict) else {}
        for k, v in match.items():
            out[k] = overlay(out.get(k), v)
        return out
    if isinstance(match, list):
        item = base[0] if isinstance(base, list) and base else None
        return [overlay(item, m) if isinstance(m, dict) else overlay(None, m) for m in match]
    if isinstance(match, str):
        if match == "*":
            return base if base is not None else "sample"
        if match[:1] in ("~", "="):
            return match[1:]
    return match


async def main() -> int:
    set_overrides({})
    defs = sb.load_all()
    async with Client(URL) as c:
        info = c.session.initialize_result if hasattr(c.session, "initialize_result") else None
        si = getattr(c, "server_info", None) or (info.server_info if info else None)
        check(1, si is not None and si.name == "careem-mcp-service" and si.version == "v1.0.0"
              and getattr(si, "title", None) is None
              and not getattr(info, "instructions", None), f"initialize serverInfo {si}")

        tools = (await c.list_tools()).tools
        got = {t.name: t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in tools}
        same = len(got) == len(defs) and all(got.get(n) == tf.definition for n, tf in defs.items())
        bad = [n for n, tf in defs.items() if got.get(n) != tf.definition]
        check(2, same, f"tools/list returns {len(got)}/{len(defs)} tools verbatim {bad[:3] if bad else ''}")
        res = (await c.list_resources()).resources
        check(2, any(str(r.uri) == "ui://careem-mcp-app" for r in res), "resources/list has ui://careem-mcp-app")

        r = await c.call_tool("food_get_restaurant_menu", {"restaurant_id": 30104, **LOC})
        d = sc(r)
        check(3, not r.is_error and d["restaurant"]["id"] == 30104
              and d["restaurant"]["name"]["value"] == "Morning Syrniki Café", "menu 30104 = Morning Syrniki Café")
        d = sc(await c.call_tool("food_get_restaurant_menu", {"restaurant_id": 30101, **LOC}))
        check(4, d["restaurant"]["name"]["value"] == "Shawarma Station", "menu 30101 = Shawarma Station")
        r = await c.call_tool("food_get_restaurant_menu", {"restaurant_id": 1, **LOC})
        check(5, not r.is_error and sc(r).get("error") == "failed to get restaurant menu: unexpected response status 404",
              "menu 1 = 404 business error")
        r = await c.call_tool("food_get_restaurant_menu", {"restaurant_id": "x", **LOC})
        r2 = await c.call_tool("food_get_restaurant_menu", {})
        check(6, r.is_error and r.content[0].text.startswith('validating "arguments"') and r2.is_error,
              "invalid / missing arguments -> isError")
        d = sc(await c.call_tool("food_merchant_search", {"query": "sushi", **LOC}))
        d2 = sc(await c.call_tool("food_merchant_search", {"query": "NOPE", **LOC}))
        r3 = await c.call_tool("food_merchant_search", {"query": "sushi", "longitude": 55.1474})
        check(7, d["restaurants"][0]["id"] == 30102 and d2["restaurants"] == [] and r3.is_error,
              "merchant search sushi / NOPE / missing latitude")
        d = sc(await c.call_tool("reverse_geocode", LOC))
        d2 = sc(await c.call_tool("reverse_geocode", {"latitude": 91, "longitude": 55}))
        check(8, d["location"]["locationUuid"] == "LOC-50001" and "error" in d2, "reverse_geocode Home / lat 91")

        set_overrides({"food_get_restaurant_menu": "upstream-unavailable"})
        d = sc(await c.call_tool("food_get_restaurant_menu", {"restaurant_id": 30104, **LOC}))
        set_overrides({})
        d2 = sc(await c.call_tool("food_get_restaurant_menu", {"restaurant_id": 30104, **LOC}))
        check(9, "error" in d and d2["restaurant"]["id"] == 30104, "overrides force + reset without restart")

        try:
            await c.call_tool("does_not_exist", {})
            ok = False
        except MCPError as e:
            ok = e.code == -32602
        check(10, ok, "unknown tool -> JSON-RPC -32602")

        observed = [json.dumps(si.model_dump(mode="json")) if si else "", json.dumps(got),
                    json.dumps([r.model_dump(by_alias=True, mode="json") for r in res])]
        observed.append((await c.read_resource("ui://careem-mcp-app")).contents[0].text)

    bad11: list[str] = []
    for name, tf in defs.items():
        out_schema = tf.definition.get("outputSchema")
        base = sample(tf.definition.get("inputSchema", {}))
        for s in tf.scenarios:
            args = overlay(base, s.match) if s.match is not None else base
            unknown = set((s.match or {}).keys()) - set((tf.definition["inputSchema"].get("properties") or {}))
            try:
                v = sb.render(s.template, args)
                observed.append(json.dumps(v, ensure_ascii=False))
                if unknown:
                    bad11.append(f"{name}/{s.name}: match keys not inputs {unknown}")
                elif out_schema and not s.is_error:
                    errs = list(Draft202012Validator(out_schema).iter_errors(v))
                    if errs:
                        bad11.append(f"{name}/{s.name}: {errs[0].message[:100]}")
            except Exception as e:  # noqa: BLE001
                bad11.append(f"{name}/{s.name}: {e}")
    check(11, not bad11, f"every scenario validates against outputSchema ({len(bad11)} bad) {bad11[:3]}")

    blob = "\n".join(observed).lower()
    hits = [w for w in BANNED if w in blob]
    check(12, not hits, f"no banned words in observable output {hits}")

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
