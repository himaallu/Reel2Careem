"""Local Careem MCP sandbox (Python build of Careem's no-code package in sandbox/spec/).

Implements spec/BUILD.md: serves every tool definition verbatim and answers tools/call from the
tool file's scenarios. No network calls, no auth, no state. Logs one line per call to stderr.

    python -m sandbox.server                 # Streamable HTTP on http://127.0.0.1:8787/mcp
    python -m sandbox.server --stdio         # stdio transport
    python -m sandbox.server --check         # parse + validate every tool file, then exit
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import random
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import mcp_types as types
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

HERE = Path(__file__).resolve().parent
SPEC_DIR = Path(os.getenv("SANDBOX_SPEC_DIR", HERE / "spec"))
OVERRIDES_PATH = Path(os.getenv("SANDBOX_OVERRIDES", HERE / ".run" / "overrides.json"))
SERVER_NAME = os.getenv("SANDBOX_SERVER_NAME", "careem-mcp-service")
SERVER_VERSION = os.getenv("SANDBOX_SERVER_VERSION", "v1.0.0")
WIDGET_DOMAIN = os.getenv("SANDBOX_WIDGET_DOMAIN", "https://careem-mcp-app.pages.dev")
WIDGET_URI = "ui://careem-mcp-app"
DUBAI = ZoneInfo("Asia/Dubai")

log = logging.getLogger("careem-mcp")


class SpecError(Exception):
    pass


# ================================================================ parsing (BUILD.md §2)
@dataclass
class Scenario:
    name: str
    match: Optional[Any]
    template: str
    is_error: bool


@dataclass
class ToolFile:
    tool: str
    path: Path
    definition: dict[str, Any]
    scenarios: list[Scenario]


_BLOCK_RE = re.compile(r"^```json[ \t]+(match|response|error|tool)[ \t]*\n(.*?)^```[ \t]*$", re.M | re.S)


def _frontmatter(text: str, path: Path) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        raise SpecError(f"{path}: missing frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise SpecError(f"{path}: unterminated frontmatter")
    meta: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = re.sub(r"\s+#.*$", "", v).strip().strip("'\"")
        meta[k.strip()] = v
    return meta, text[end + 5:]


def _sections(body: str) -> list[tuple[str, str]]:
    out: list[tuple[str, list[str]]] = []
    in_fence = False
    for line in body.splitlines(keepends=True):
        if line.startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith("## "):
            out.append((line[3:].strip(), []))
        elif out:
            out[-1][1].append(line)
    return [(t, "".join(ls)) for t, ls in out]


def parse_tool_file(path: Path) -> ToolFile:
    meta, body = _frontmatter(path.read_text(encoding="utf-8"), path)
    tool = meta.get("tool")
    if not tool:
        raise SpecError(f"{path}: frontmatter has no 'tool'")
    definition: Optional[dict[str, Any]] = None
    scenarios: list[Scenario] = []
    for title, content in _sections(body):
        blocks = _BLOCK_RE.findall(content)
        if title == "Definition":
            tools = [b for kind, b in blocks if kind == "tool"]
            if len(tools) != 1:
                raise SpecError(f"{path}: Definition needs exactly one ```json tool block")
            definition = json.loads(tools[0])
        elif title.startswith("Scenario:"):
            name = title[len("Scenario:"):].strip()
            matches = [b for kind, b in blocks if kind == "match"]
            resps = [(kind, b) for kind, b in blocks if kind in ("response", "error")]
            if len(matches) > 1 or len(resps) != 1:
                raise SpecError(f"{path}: scenario {name!r} needs ≤1 match and exactly 1 response/error")
            try:
                match = json.loads(matches[0]) if matches else None
            except json.JSONDecodeError as e:
                raise SpecError(f"{path}: scenario {name!r} match is invalid JSON: {e}") from None
            scenarios.append(Scenario(name, match, resps[0][1], resps[0][0] == "error"))
    if definition is None:
        raise SpecError(f"{path}: no Definition")
    if definition.get("name") != tool:
        raise SpecError(f"{path}: definition name {definition.get('name')!r} != tool {tool!r}")
    if not scenarios:
        raise SpecError(f"{path}: no scenarios")
    if not any(s.match is None for s in scenarios):
        raise SpecError(f"{path}: no default scenario")
    for s in scenarios:
        try:
            render(s.template, {})
        except Exception as e:  # noqa: BLE001
            raise SpecError(f"{path}: scenario {s.name!r} template invalid: {e}") from None
    return ToolFile(tool, path, definition, scenarios)


def load_all(spec_dir: Path = SPEC_DIR) -> dict[str, ToolFile]:
    files: dict[str, ToolFile] = {}
    for p in sorted((spec_dir / "tools").rglob("*.md")):
        if p.name == "README.md":
            continue
        tf = parse_tool_file(p)
        if tf.tool in files:
            raise SpecError(f"{p}: duplicate tool {tf.tool!r}")
        files[tf.tool] = tf
    return files


# ================================================================ matching (BUILD.md §3)
def _text(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v, separators=(",", ":"), ensure_ascii=False)


def matches(want: Any, got: Any) -> bool:
    if isinstance(want, dict):
        return isinstance(got, dict) and all(k in got and matches(w, got[k]) for k, w in want.items())
    if isinstance(want, list):
        return isinstance(got, list) and all(any(matches(w, g) for g in got) for w in want)
    if isinstance(want, str):
        if got is None:
            return False
        if want == "*":
            return True
        if want.startswith("~"):
            return want[1:].lower() in _text(got).lower()
        if want.startswith("="):
            return _text(got).lower() == want[1:].lower()
        return _text(got).lower() == want.lower()
    if isinstance(want, bool) or isinstance(got, bool):
        return type(want) is type(got) and want == got
    return want == got


def select(tf: ToolFile, args: dict[str, Any], forced: Optional[str]) -> tuple[Scenario, bool]:
    if forced:
        for s in tf.scenarios:
            if s.name.lower() == forced.lower():
                return s, True
    for s in tf.scenarios:
        if s.match is not None and matches(s.match, args):
            return s, False
    return next(s for s in tf.scenarios if s.match is None), False


# ================================================================ placeholders (BUILD.md §3)
_PH_RE = re.compile(r"\{\{\s*(.*?)\s*\}\}")
_DUR_RE = re.compile(r"(\d+)(ms|s|m|h)")
_TIME_RE = re.compile(r"^(now|unixms|unix)([+-][0-9a-z]+)?(?:@([0-9a-z]+))?(?:\|(date|weekday|hhmm|daylabel))?$")


def _duration(s: str) -> timedelta:
    if not s or _DUR_RE.sub("", s):
        raise ValueError(f"bad duration {s!r}")
    unit = {"ms": "milliseconds", "s": "seconds", "m": "minutes", "h": "hours"}
    total = timedelta()
    for n, u in _DUR_RE.findall(s):
        total += timedelta(**{unit[u]: int(n)})
    return total


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _eval(expr: str, args: dict[str, Any]) -> Any:
    if expr == "uuid":
        return str(uuid.uuid4())
    if expr.startswith("rand:"):
        lo, hi = (int(x) for x in expr[5:].split("-", 1))
        return random.randint(lo, hi)
    if expr == "args" or expr.startswith("args."):
        cur: Any = args
        for part in expr.split(".")[1:]:
            if isinstance(cur, dict):
                cur = cur.get(part)
            elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
                cur = cur[int(part)]
            else:
                return None
        return cur
    m = _TIME_RE.match(expr)
    if not m:
        raise ValueError(f"unknown placeholder {{{{{expr}}}}}")
    base, offset, round_to, fmt = m.groups()
    t = datetime.now(timezone.utc).replace(microsecond=0)
    if offset:
        d = _duration(offset[1:])
        t = t + d if offset[0] == "+" else t - d
    if round_to:
        step = int(_duration(round_to).total_seconds())
        t = datetime.fromtimestamp(int(t.timestamp()) // step * step, timezone.utc)
    if base == "unix":
        return int(t.timestamp())
    if base == "unixms":
        return int(t.timestamp()) * 1000
    if fmt:
        local = t.astimezone(DUBAI)
        return {
            "date": local.strftime("%Y-%m-%d"),
            "weekday": local.strftime("%A"),
            "hhmm": local.strftime("%H:%M"),
            "daylabel": f"{local.strftime('%a')}, {_ordinal(local.day)} {local.strftime('%b')}",
        }[fmt]
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def render(template: str, args: dict[str, Any]) -> Any:
    out: list[str] = []
    in_str = False
    i = 0
    while i < len(template):
        if template.startswith("{{", i):
            m = _PH_RE.match(template, i)
            if not m:
                raise ValueError("unterminated placeholder")
            val = _eval(m.group(1), args)
            if in_str:
                txt = val if isinstance(val, str) else json.dumps(val, separators=(",", ":"), ensure_ascii=False)
                out.append(json.dumps(txt, ensure_ascii=False)[1:-1])
            else:
                out.append(json.dumps(val, ensure_ascii=False))
            i = m.end()
            continue
        ch = template[i]
        if ch == "\\" and in_str:
            out.append(template[i:i + 2])
            i += 2
            continue
        if ch == '"':
            in_str = not in_str
        out.append(ch)
        i += 1
    return json.loads("".join(out))


# ================================================================ validation
def apply_defaults(schema: dict[str, Any], value: Any) -> Any:
    if isinstance(value, dict) and isinstance(schema.get("properties"), dict):
        for k, sub in schema["properties"].items():
            if k not in value and isinstance(sub, dict) and "default" in sub:
                value[k] = copy.deepcopy(sub["default"])
            elif k in value:
                value[k] = apply_defaults(sub, value[k])
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        return [apply_defaults(schema["items"], v) for v in value]
    return value


def read_overrides() -> dict[str, str]:
    try:
        data = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# ================================================================ MCP server
def widget_resource_meta() -> dict[str, Any]:
    return {
        "ui": {
            "resourceUri": WIDGET_URI,
            "csp": {
                "connectDomains": [WIDGET_DOMAIN],
                "resourceDomains": [
                    WIDGET_DOMAIN,
                    "https://careem.s3.eu-west-1.amazonaws.com",
                    "https://d1a3f4spazzrp4.cloudfront.net",
                    "https://careem-catalog-media.media.careem.com",
                    "https://partner-mot-prod.media.careem.com",
                ],
                "frameDomains": [],
                "baseUriDomains": [],
            },
        },
        "prefersBorder": False,
        "domain": WIDGET_DOMAIN,
        "annotations": {"ttlMs": 60000, "destructiveHint": False, "openWorldHint": False, "readOnlyHint": True},
    }


def widget_html() -> str:
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        '<meta name="color-scheme" content="light dark">\n<title>Careem MCP App</title>\n'
        f'<link rel="stylesheet" href="{WIDGET_DOMAIN}/assets/index.css">\n</head>\n<body>\n'
        f'<div id="root"></div>\n<script type="module" src="{WIDGET_DOMAIN}/assets/index.js"></script>\n'
        "</body>\n</html>\n"
    )


def _result(value: Any, is_error: bool) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(value, ensure_ascii=False, separators=(",", ":")))],
        structured_content=value if isinstance(value, dict) else None,
        is_error=is_error,
    )


def _error_text(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=True)


def handle_call(defs: dict[str, ToolFile], validators: dict[str, Draft202012Validator],
                name: str, arguments: Optional[dict[str, Any]]) -> types.CallToolResult:
    """Pure tools/call logic (unknown tool is handled by the caller)."""
    tf0 = defs[name]
    args = apply_defaults(tf0.definition.get("inputSchema", {}), copy.deepcopy(arguments or {}))
    err = next(iter(sorted(validators[name].iter_errors(args), key=lambda e: e.path)), None)
    if err is not None:
        log.info("%s  -> invalid arguments  %s", name, _text(args))
        return _error_text(f'validating "arguments": {_validator_message(err)}')
    try:
        tf = parse_tool_file(tf0.path)
    except Exception as e:  # noqa: BLE001
        log.error("%s  -> parse error: %s", name, e)
        return _error_text("internal server error")
    scenario, forced = select(tf, args, read_overrides().get(name))
    try:
        value = render(scenario.template, args)
    except Exception as e:  # noqa: BLE001
        log.error("%s  -> render error in %r: %s", name, scenario.name, e)
        return _error_text("internal server error")
    log.info("%s  -> %s%s  %s", name, scenario.name, " (forced)" if forced else "", _text(args))
    return _result(value, scenario.is_error)


def _validator_message(e: ValidationError) -> str:
    where = "/" + "/".join(str(p) for p in e.absolute_path)
    return f"validating {where}: {e.message}"


def build_server(defs: dict[str, ToolFile]):
    from mcp.server.lowlevel import Server
    from mcp.shared.exceptions import MCPError

    validators = {n: Draft202012Validator(tf.definition.get("inputSchema", {"type": "object"}))
                  for n, tf in defs.items()}
    tools = [types.Tool.model_validate(defs[n].definition) for n in sorted(defs)]

    async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        if params.name not in defs:
            raise MCPError(-32602, f'unknown tool "{params.name}"')
        return handle_call(defs, validators, params.name, params.arguments)

    async def on_list_resources(ctx: Any, params: Any) -> types.ListResourcesResult:
        return types.ListResourcesResult(resources=[types.Resource.model_validate({
            "uri": WIDGET_URI, "name": WIDGET_URI, "description": "Careem Widget",
            "mimeType": "text/html;profile=mcp-app", "_meta": widget_resource_meta(),
        })])

    async def on_read_resource(ctx: Any, params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
        if str(params.uri) != WIDGET_URI:
            raise MCPError(-32602, f"unknown resource {params.uri}")
        return types.ReadResourceResult(contents=[types.TextResourceContents.model_validate({
            "uri": WIDGET_URI, "mimeType": "text/html;profile=mcp-app",
            "_meta": widget_resource_meta(), "text": widget_html(),
        })])

    return Server(
        SERVER_NAME, version=SERVER_VERSION,
        get_tool_input_schema=lambda n: defs[n].definition.get("inputSchema") if n in defs else None,
        on_list_tools=on_list_tools, on_call_tool=on_call_tool,
        on_list_resources=on_list_resources, on_read_resource=on_read_resource,
    )


def http_app(defs: dict[str, ToolFile], host: str):
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "tools": len(defs)})

    return build_server(defs).streamable_http_app(
        streamable_http_path="/mcp", host=host, custom_starlette_routes=[Route("/health", health)],
    )


def main() -> int:
    p = argparse.ArgumentParser(description="Local Careem MCP sandbox")
    p.add_argument("--host", default=os.getenv("SANDBOX_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.getenv("SANDBOX_PORT", "8787")))
    p.add_argument("--stdio", action="store_true", help="serve over stdio instead of HTTP")
    p.add_argument("--check", action="store_true", help="validate every tool file and exit")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(message)s")
    for noisy in ("mcp", "uvicorn.access", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        defs = load_all()
    except SpecError as e:
        print(f"spec error: {e}", file=sys.stderr)
        return 1
    if a.check:
        print(f"ok: {len(defs)} tools, {sum(len(t.scenarios) for t in defs.values())} scenarios", file=sys.stderr)
        return 0
    if a.stdio:
        import anyio
        from mcp.server.stdio import stdio_server

        server = build_server(defs)

        async def run() -> None:
            async with stdio_server() as (r, w):
                await server.run(r, w, server.create_initialization_options())

        anyio.run(run)
        return 0
    import uvicorn

    log.info("careem-mcp listening on http://%s:%d/mcp (%d tools)", a.host, a.port, len(defs))
    uvicorn.run(http_app(defs, a.host), host=a.host, port=a.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
