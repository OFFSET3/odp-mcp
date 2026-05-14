import json
import logging
import os
from contextlib import asynccontextmanager
from json import JSONDecodeError
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.requests import Request
from starlette.responses import JSONResponse

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# USPTO ODP base URLs
# ---------------------------------------------------------------------------
_PATENT_SEARCH_BASE = "https://api.patentsview.org/patents/query"
_PATENT_FULLTEXT_BASE = "https://efts.uspto.gov/LATEST/search-index"
_PEDS_BASE = "https://ped.uspto.gov/api"
_TRADEMARK_SEARCH_BASE = "https://tsdrapi.uspto.gov/ts/cd"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _api_key() -> str:
    key = os.getenv("USPTO_API_KEY", "")
    if not key:
        raise RuntimeError(
            "USPTO_API_KEY is not set. Add it to the environment or Azure Key Vault secret 'kv-offset3/uspto-odp-api-key'."
        )
    return key


def _truthy_env(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_transport_security_settings() -> TransportSecuritySettings | None:
    if not _truthy_env(os.getenv("MCP_ENABLE_DNS_REBINDING_PROTECTION")):
        return None
    raw_hosts = os.getenv("MCP_ALLOWED_HOSTS", "").strip()
    if not raw_hosts:
        return None
    allowed_hosts = [h.strip() for h in raw_hosts.split(",") if h.strip()]
    raw_origins = os.getenv("MCP_ALLOWED_ORIGINS", "").strip()
    allowed_origins = [o.strip() for o in raw_origins.split(",") if o.strip()]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def _headers() -> dict[str, str]:
    return {
        "X-Api-Key": _api_key(),
        "Accept": "application/json",
        "User-Agent": "OFFSET3-odp-mcp/1.0",
    }


async def _get(url: str, params: dict | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, params=params, headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def _post(url: str, payload: dict) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers=_headers())
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# ASGI middleware — identical pattern to taiga-mcp
# ---------------------------------------------------------------------------

class _NormalizeMountedRootPath:
    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") in ("", None):
            scope = dict(scope)
            scope["path"] = "/"
            scope["raw_path"] = b"/"
        await self._app(scope, receive, send)


class _RewriteMountedPaths:
    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path")
            if path == "/mcp":
                scope = dict(scope)
                scope["path"] = "/mcp/"
                scope["raw_path"] = b"/mcp/"
            elif path == "/sse":
                scope = dict(scope)
                scope["path"] = "/sse/"
                scope["raw_path"] = b"/sse/"
        await self._app(scope, receive, send)


class _NormalizeToolNames:
    """ASGI middleware: rewrite dot-notation tool names to underscore notation."""

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        body_chunks: list[bytes] = []
        more_body = True
        while more_body:
            message = await receive()
            body_chunks.append(message.get("body", b""))
            more_body = message.get("more_body", False)

        raw_body = b"".join(body_chunks)
        normalized_body = raw_body

        if raw_body:
            try:
                data = json.loads(raw_body)
                if (
                    isinstance(data, dict)
                    and data.get("method") == "tools/call"
                    and isinstance(data.get("params"), dict)
                    and isinstance(data["params"].get("name"), str)
                    and "." in data["params"]["name"]
                ):
                    original = data["params"]["name"]
                    data["params"]["name"] = original.replace(".", "_")
                    logger.info("_NormalizeToolNames: rewrote '%s' -> '%s'", original, data["params"]["name"])
                    normalized_body = json.dumps(data).encode()
            except (JSONDecodeError, KeyError, TypeError):
                pass

        body_consumed = False

        async def patched_receive():
            nonlocal body_consumed
            if not body_consumed:
                body_consumed = True
                return {"type": "http.request", "body": normalized_body, "more_body": False}
            return await receive()

        await self._app(scope, patched_receive, send)


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    os.getenv("MCP_SERVER_NAME", "USPTO ODP"),
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    sse_path="/",
    streamable_http_path="/",
    transport_security=_get_transport_security_settings(),
)


# ---------------------------------------------------------------------------
# Tool: capabilities
# ---------------------------------------------------------------------------

@mcp.tool(
    name="odp_capabilities",
    annotations=ToolAnnotations(openWorldHint=True, readOnlyHint=True, idempotentHint=True),
)
async def odp_capabilities() -> dict[str, Any]:
    """Return server capabilities and available USPTO ODP tool names."""
    return {
        "server": os.getenv("MCP_SERVER_NAME", "USPTO ODP"),
        "api_key_configured": bool(os.getenv("USPTO_API_KEY")),
        "tools": [
            "odp_patent_search",
            "odp_patent_get",
            "odp_patent_fulltext_search",
            "odp_application_status",
            "odp_trademark_status",
            "odp_trademark_search",
        ],
        "transports": {
            "mcp_streamable_http_path": "/mcp",
            "mcp_sse_path": "/sse",
        },
        "docs": "https://developer.uspto.gov/",
    }


# ---------------------------------------------------------------------------
# Tool: Patent search (PatentsView API)
# ---------------------------------------------------------------------------

@mcp.tool(
    name="odp_patent_search",
    annotations=ToolAnnotations(openWorldHint=True, readOnlyHint=True, idempotentHint=True),
)
async def odp_patent_search(
    query: str,
    fields: list[str] | None = None,
    page: int = 1,
    per_page: int = 25,
) -> dict[str, Any]:
    """Search USPTO patents via the PatentsView API.

    Args:
        query: Free-text search query (e.g. "autonomous drone navigation").
        fields: List of fields to return. Defaults to title, patent_number, patent_date, abstract.
        page: Page number (1-indexed).
        per_page: Results per page (max 100).

    Returns:
        Dict with patents list and total_patent_count.
    """
    if fields is None:
        fields = ["patent_number", "patent_title", "patent_date", "patent_abstract", "inventors"]

    payload = {
        "q": {"_text_any": {"patent_title": query, "patent_abstract": query}},
        "f": fields,
        "o": {"page": page, "per_page": min(per_page, 100)},
    }

    try:
        return await _post(_PATENT_SEARCH_BASE, payload)
    except httpx.HTTPStatusError as exc:
        return {"error": str(exc), "status_code": exc.response.status_code}
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: Patent get by number
# ---------------------------------------------------------------------------

@mcp.tool(
    name="odp_patent_get",
    annotations=ToolAnnotations(openWorldHint=True, readOnlyHint=True, idempotentHint=True),
)
async def odp_patent_get(
    patent_number: str,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    """Retrieve a specific patent by patent number from PatentsView.

    Args:
        patent_number: USPTO patent number (e.g. "10123456" or "US10123456B2").
        fields: Fields to return. Defaults to full bibliographic set.

    Returns:
        Patent record dict.
    """
    if fields is None:
        fields = [
            "patent_number", "patent_title", "patent_date", "patent_abstract",
            "inventors", "assignees", "cpcs", "claims",
        ]

    # Normalize: strip leading "US" and kind code if present
    normalized = patent_number.strip().upper()
    if normalized.startswith("US"):
        normalized = normalized[2:]
    normalized = normalized.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"[:0])  # keep digits + kind
    # PatentsView expects numeric or alphanumeric patent_number
    payload = {
        "q": {"patent_number": patent_number.strip()},
        "f": fields,
    }

    try:
        return await _post(_PATENT_SEARCH_BASE, payload)
    except httpx.HTTPStatusError as exc:
        return {"error": str(exc), "status_code": exc.response.status_code}
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: Full-text patent search (USPTO EFTS)
# ---------------------------------------------------------------------------

@mcp.tool(
    name="odp_patent_fulltext_search",
    annotations=ToolAnnotations(openWorldHint=True, readOnlyHint=True, idempotentHint=True),
)
async def odp_patent_fulltext_search(
    query: str,
    date_range_start: str | None = None,
    date_range_end: str | None = None,
    rows: int = 20,
    start: int = 0,
) -> dict[str, Any]:
    """Full-text search across USPTO patent grants and applications (EFTS).

    Args:
        query: Lucene-style query string (e.g. "autonomous drone" OR "field:clm.en:machine learning").
        date_range_start: Filter by issue date start (YYYY-MM-DD).
        date_range_end: Filter by issue date end (YYYY-MM-DD).
        rows: Number of results (max 500).
        start: Offset for pagination.

    Returns:
        Dict with hits list (patent_id, title, patent_number, date, snippet).
    """
    params: dict[str, Any] = {
        "q": query,
        "rows": min(rows, 500),
        "start": start,
        "fl": "patent_title,patent_number,patent_date,patent_abstract",
    }
    if date_range_start and date_range_end:
        params["dateRangeData"] = f"[{date_range_start} TO {date_range_end}]"

    try:
        result = await _get(_PATENT_FULLTEXT_BASE, params=params)
        return result
    except httpx.HTTPStatusError as exc:
        return {"error": str(exc), "status_code": exc.response.status_code}
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: Patent application status (PEDS)
# ---------------------------------------------------------------------------

@mcp.tool(
    name="odp_application_status",
    annotations=ToolAnnotations(openWorldHint=True, readOnlyHint=True, idempotentHint=True),
)
async def odp_application_status(
    application_number: str,
) -> dict[str, Any]:
    """Retrieve patent application status from USPTO PEDS (Patent Examination Data System).

    Args:
        application_number: USPTO application number (e.g. "16123456" or "16/123,456").

    Returns:
        Application status, filing date, examiner, and prosecution history summary.
    """
    # Normalize: strip slashes and commas
    normalized = application_number.strip().replace("/", "").replace(",", "").replace(" ", "")

    url = f"{_PEDS_BASE}/queries"
    payload = {
        "searchText": f"applId:{normalized}",
        "fq": [],
        "fl": "*",
        "facet": False,
        "sort": "applId asc",
        "start": 0,
        "rows": 1,
        "highlighting": True,
        "mm": "100%",
    }

    try:
        return await _post(url, payload)
    except httpx.HTTPStatusError as exc:
        return {"error": str(exc), "status_code": exc.response.status_code}
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: Trademark status (TSDR)
# ---------------------------------------------------------------------------

@mcp.tool(
    name="odp_trademark_status",
    annotations=ToolAnnotations(openWorldHint=True, readOnlyHint=True, idempotentHint=True),
)
async def odp_trademark_status(
    serial_number: str,
) -> dict[str, Any]:
    """Retrieve trademark status from USPTO TSDR by serial number.

    Args:
        serial_number: USPTO trademark serial number (e.g. "97123456").

    Returns:
        Trademark status, owner, goods/services, and prosecution history.
    """
    normalized = serial_number.strip().replace("-", "")
    url = f"{_TRADEMARK_SEARCH_BASE}/casestatus/{normalized}/info"

    try:
        return await _get(url)
    except httpx.HTTPStatusError as exc:
        return {"error": str(exc), "status_code": exc.response.status_code}
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: Trademark search (TESS via ODP)
# ---------------------------------------------------------------------------

@mcp.tool(
    name="odp_trademark_search",
    annotations=ToolAnnotations(openWorldHint=True, readOnlyHint=True, idempotentHint=True),
)
async def odp_trademark_search(
    mark_name: str,
    status: str = "live",
    page: int = 1,
    rows: int = 25,
) -> dict[str, Any]:
    """Search USPTO trademarks by mark name via the TSDR ODP API.

    Args:
        mark_name: Trademark text to search (e.g. "AresNet").
        status: Filter by status: "live", "dead", or "all".
        page: Page number (1-indexed).
        rows: Results per page (max 100).

    Returns:
        List of matching trademarks with serial numbers, owner, status, and IC classes.
    """
    params: dict[str, Any] = {
        "searchText": mark_name,
        "rows": min(rows, 100),
        "start": (page - 1) * rows,
    }
    if status.lower() in ("live", "dead"):
        params["status"] = status.lower()

    url = f"{_TRADEMARK_SEARCH_BASE}/trademark/search"

    try:
        return await _get(url, params=params)
    except httpx.HTTPStatusError as exc:
        return {"error": str(exc), "status_code": exc.response.status_code}
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

async def _health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "odp-mcp"})


# ---------------------------------------------------------------------------
# App assembly
# ---------------------------------------------------------------------------

sse_starlette_app = mcp.sse_app()
sse_subapp = _NormalizeMountedRootPath(sse_starlette_app)

streamable_http_starlette_app = mcp.streamable_http_app()
streamable_http_starlette_app.router.redirect_slashes = False
streamable_http_subapp = _NormalizeMountedRootPath(streamable_http_starlette_app)


@asynccontextmanager
async def lifespan(app):
    logger.info("odp-mcp starting — USPTO ODP MCP server")
    async with mcp.session_manager.run():
        yield
    logger.info("odp-mcp shutdown")


_routes = [
    Route("/health", _health, methods=["GET"]),
    Mount("/sse", app=sse_subapp),
    Mount("/mcp", app=streamable_http_subapp),
]

starlette_app = Starlette(routes=_routes, lifespan=lifespan)
starlette_app.router.redirect_slashes = False

app = _NormalizeToolNames(_RewriteMountedPaths(starlette_app))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
