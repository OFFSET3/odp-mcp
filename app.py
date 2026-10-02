import json
import logging
import os
import re
import xml.etree.ElementTree as ET
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
# USPTO / PatentsView upstream endpoints
# ---------------------------------------------------------------------------
# PatentsView legacy api.patentsview.org endpoints were retired. The current
# PatentSearch API is hosted on the Search Platform and uses X-Api-Key.
_PATENT_SEARCH_BASE = os.getenv(
    "PATENTSVIEW_BASE_URL",
    "https://search.patentsview.org/api/v1/patent/",
)

# PEDS was retired. Patent application/file-wrapper data now lives in ODP.
_ODP_APPLICATION_BASE = os.getenv(
    "USPTO_ODP_APPLICATION_BASE_URL",
    "https://api.uspto.gov/api/v1/patent/applications",
)

_TRADEMARK_BASE = os.getenv(
    "USPTO_TSDR_BASE_URL",
    "https://tsdrapi.uspto.gov/ts/cd",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _api_key() -> str:
    key = os.getenv("USPTO_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "USPTO_API_KEY is not set. Add it to the environment or Azure Key Vault secret 'kv-offset3/uspto-odp-api-key'."
        )
    return key


def _patentsview_api_key() -> str:
    key = os.getenv("PATENTSVIEW_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "PATENTSVIEW_API_KEY is not set. Patent search will use the ODP "
            "Patent File Wrapper fallback instead."
        )
    return key


def _tsdr_api_key() -> str:
    """Return the dedicated TSDR API Manager credential.

    Live validation shows the ODP credential is not a usable substitute for
    TSDR. Keep the credential domains separate so a missing TSDR key fails
    explicitly instead of producing a misleading upstream 404.
    """
    key = os.getenv("USPTO_TSDR_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "USPTO_TSDR_API_KEY is not set. TSDR requires a separate USPTO "
            "API Manager credential; configure it before using trademark status."
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


def _patentsview_headers() -> dict[str, str]:
    return {
        "X-Api-Key": _patentsview_api_key(),
        "Accept": "application/json",
        "User-Agent": "OFFSET3-odp-mcp/2.0",
    }


def _odp_headers() -> dict[str, str]:
    return {
        "X-API-KEY": _api_key(),
        "Accept": "application/json",
        "User-Agent": "OFFSET3-odp-mcp/2.0",
    }


def _tsdr_headers() -> dict[str, str]:
    return {
        "USPTO-API-KEY": _tsdr_api_key(),
        "Accept": "application/xml",
        "User-Agent": "OFFSET3-odp-mcp/2.0",
    }


async def _get(
    url: str,
    params: dict | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, params=params, headers=headers or _odp_headers())
        resp.raise_for_status()
        return resp.json()


async def _post(
    url: str,
    payload: dict,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers=headers or _odp_headers())
        resp.raise_for_status()
        return resp.json()


def _xml_element_to_value(element: ET.Element) -> Any:
    children = list(element)
    if not children:
        return (element.text or "").strip()

    value: dict[str, Any] = {}
    for child in children:
        key = child.tag.rsplit("}", 1)[-1]
        child_value = _xml_element_to_value(child)
        existing = value.get(key)
        if existing is None:
            value[key] = child_value
        elif isinstance(existing, list):
            existing.append(child_value)
        else:
            value[key] = [existing, child_value]

    if element.attrib:
        value["_attributes"] = dict(element.attrib)
    return value


async def _get_xml(
    url: str,
    *,
    headers: dict[str, str],
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        root_name = root.tag.rsplit("}", 1)[-1]
        return {root_name: _xml_element_to_value(root)}


def _upstream_error(exc: httpx.HTTPStatusError, *, service: str) -> dict[str, Any]:
    status_code = exc.response.status_code
    if service == "TSDR" and status_code == 401:
        return {
            "error": (
                "TSDR rejected the configured credential. TSDR uses the "
                "USPTO-API-KEY header and may require a separate TSDR API key. "
                "Set USPTO_TSDR_API_KEY if your ODP key is not authorized for TSDR."
            ),
            "status_code": 401,
            "service": service,
        }
    return {"error": str(exc), "status_code": status_code, "service": service}


def _normalize_odp_application(record: dict[str, Any]) -> dict[str, Any]:
    # ODP has returned both nested Patent File Wrapper records and flattened
    # patentBag search records across revisions. Accept both shapes.
    meta = record.get("applicationMetaData") or record
    patent_number = meta.get("patentNumber") or record.get("patentNumber")
    return {
        "patent_id": patent_number,
        "patent_number": patent_number,
        "patent_title": meta.get("inventionTitle"),
        "patent_date": meta.get("grantDate") or meta.get("patentIssueDate"),
        "application_number": (
            record.get("applicationNumberText")
            or meta.get("applicationNumberText")
            or meta.get("applicationNumber")
        ),
        "application_status": (
            meta.get("applicationStatusDescriptionText")
            or meta.get("applicationStatusCode")
        ),
        "filing_date": meta.get("filingDate"),
        "publication_number": (
            meta.get("publicationNumber")
            or meta.get("applicationPublicationNumber")
            or meta.get("earliestPublicationNumber")
        ),
        "publication_date": (
            meta.get("publicationDate")
            or meta.get("applicationPublicationDate")
            or meta.get("earliestPublicationDate")
        ),
    }


async def _odp_patent_search_fallback(
    query: str,
    *,
    page: int,
    per_page: int,
) -> dict[str, Any]:
    params = {
        "q": query,
        "offset": (page - 1) * per_page,
        "limit": per_page,
    }
    result = await _get(
        f"{_ODP_APPLICATION_BASE}/search",
        params=params,
        headers=_odp_headers(),
    )
    applications = (
        result.get("patentBag")
        or result.get("patentFileWrapperDataBag")
        or []
    )
    return {
        "patents": [_normalize_odp_application(item) for item in applications],
        "applications": applications,
        "count": len(applications),
        "total_patent_count": result.get("count"),
        "page": page,
        "per_page": per_page,
        "source": "USPTO ODP Patent File Wrapper",
    }


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
        "patentsview_api_key_configured": bool(os.getenv("PATENTSVIEW_API_KEY")),
        "tsdr_api_key_configured": bool(os.getenv("USPTO_TSDR_API_KEY")),
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
        "docs": "https://data.uspto.gov/apis/getting-started",
        "upstreams": {
            "patents": _PATENT_SEARCH_BASE,
            "applications": _ODP_APPLICATION_BASE,
            "trademark_status": _TRADEMARK_BASE,
        },
        "limitations": {
            "odp_patent_fulltext_search": "legacy EFTS public API retired; returns a stable unsupported response",
            "odp_trademark_search": "TSDR has no documented free-text mark-search endpoint",
            "odp_trademark_status": "requires a separately provisioned USPTO_TSDR_API_KEY credential",
        },
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
        fields = ["patent_id", "patent_title", "patent_date", "patent_abstract"]
    else:
        fields = ["patent_id" if field == "patent_number" else field for field in fields]

    page = max(page, 1)
    per_page = max(1, min(per_page, 100))

    # Prefer PatentsView when its distinct credential is configured. Otherwise
    # keep the connector useful with the ODP key by searching Patent File
    # Wrapper application data.
    if not os.getenv("PATENTSVIEW_API_KEY", "").strip():
        try:
            return await _odp_patent_search_fallback(
                query,
                page=page,
                per_page=per_page,
            )
        except httpx.HTTPStatusError as exc:
            return _upstream_error(exc, service="USPTO ODP Patent File Wrapper")
        except Exception as exc:
            return {"error": str(exc), "service": "USPTO ODP Patent File Wrapper"}

    requested_size = page * per_page
    if requested_size > 1000:
        return {
            "error": "PatentsView supports at most 1000 results per query; reduce page or per_page.",
            "status_code": 400,
            "service": "PatentsView",
        }

    payload = {
        "q": {
            "_or": [
                {"_text_any": {"patent_title": query}},
                {"_text_any": {"patent_abstract": query}},
            ]
        },
        "f": fields,
        "o": {"size": requested_size},
    }

    try:
        result = await _post(_PATENT_SEARCH_BASE, payload, headers=_patentsview_headers())
        patents = result.get("patents", [])
        start = (page - 1) * per_page
        end = start + per_page
        paged = patents[start:end]
        for patent in paged:
            if "patent_id" in patent and "patent_number" not in patent:
                patent["patent_number"] = patent["patent_id"]
        result["patents"] = paged
        result["count"] = len(paged)
        result["page"] = page
        result["per_page"] = per_page
        result["source"] = "PatentsView PatentSearch"
        return result
    except httpx.HTTPStatusError as exc:
        return _upstream_error(exc, service="PatentsView")
    except Exception as exc:
        return {"error": str(exc), "service": "PatentsView"}


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
        fields = ["patent_id", "patent_title", "patent_date", "patent_abstract"]
    else:
        fields = ["patent_id" if field == "patent_number" else field for field in fields]

    normalized = patent_number.strip().upper().replace(",", "").replace(" ", "")
    if normalized.startswith("US"):
        normalized = normalized[2:]
    # Strip common USPTO kind codes (A1, B1, B2, S1, etc.) while preserving
    # design identifiers such as D345393.
    normalized = re.sub(r"([A-Z]\d?)$", "", normalized)

    if not os.getenv("PATENTSVIEW_API_KEY", "").strip():
        try:
            result = await _odp_patent_search_fallback(
                normalized,
                page=1,
                per_page=10,
            )
            exact = [
                patent
                for patent in result.get("patents", [])
                if str(patent.get("patent_number") or "").upper() == normalized
            ]
            result["patents"] = exact
            result["count"] = len(exact)
            result["requested_patent_number"] = normalized
            if not exact:
                result["not_found"] = True
            return result
        except httpx.HTTPStatusError as exc:
            return _upstream_error(exc, service="USPTO ODP Patent File Wrapper")
        except Exception as exc:
            return {"error": str(exc), "service": "USPTO ODP Patent File Wrapper"}

    payload = {
        "q": {"patent_id": normalized},
        "f": fields,
        "o": {"size": 1},
    }

    try:
        result = await _post(_PATENT_SEARCH_BASE, payload, headers=_patentsview_headers())
        for patent in result.get("patents", []):
            if "patent_id" in patent and "patent_number" not in patent:
                patent["patent_number"] = patent["patent_id"]
        result["source"] = "PatentsView PatentSearch"
        return result
    except httpx.HTTPStatusError as exc:
        return _upstream_error(exc, service="PatentsView")
    except Exception as exc:
        return {"error": str(exc), "service": "PatentsView"}


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
    # The public EFTS host used by this connector is no longer a supported
    # USPTO API surface. Do not disguise DNS/transport failures as search
    # results. Keep the tool contract stable and return an actionable result.
    return {
        "error": (
            "USPTO EFTS full-text API is no longer available at the legacy "
            "public endpoint used by this connector. Use odp_patent_search "
            "for grant title/abstract search and odp_application_status for "
            "ODP Patent File Wrapper application data."
        ),
        "status_code": 410,
        "service": "USPTO EFTS",
        "supported": False,
        "query": query,
        "date_range_start": date_range_start,
        "date_range_end": date_range_end,
        "rows": rows,
        "start": start,
    }


# ---------------------------------------------------------------------------
# Tool: Patent application status (ODP Patent File Wrapper)
# ---------------------------------------------------------------------------

@mcp.tool(
    name="odp_application_status",
    annotations=ToolAnnotations(openWorldHint=True, readOnlyHint=True, idempotentHint=True),
)
async def odp_application_status(
    application_number: str,
) -> dict[str, Any]:
    """Retrieve patent application data/status from USPTO ODP Patent File Wrapper.

    Args:
        application_number: USPTO application number (e.g. "16123456" or "16/123,456").

    Returns:
        Patent File Wrapper application metadata, status, parties, and event data.
    """
    normalized = application_number.strip().replace("/", "").replace(",", "").replace(" ", "")
    url = f"{_ODP_APPLICATION_BASE}/{normalized}"

    try:
        return await _get(url, headers=_odp_headers())
    except httpx.HTTPStatusError as exc:
        return _upstream_error(exc, service="USPTO ODP Patent File Wrapper")
    except Exception as exc:
        return {"error": str(exc), "service": "USPTO ODP Patent File Wrapper"}


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
    normalized = serial_number.strip().replace("-", "").replace(" ", "")
    url = f"{_TRADEMARK_BASE}/casestatus/sn{normalized}/info.xml"

    try:
        result = await _get_xml(url, headers=_tsdr_headers())
        result["serial_number"] = normalized
        result["source"] = "USPTO TSDR"
        return result
    except RuntimeError as exc:
        return {
            "error": str(exc),
            "status_code": 424,
            "service": "TSDR",
            "configuration_required": True,
        }
    except httpx.HTTPStatusError as exc:
        return _upstream_error(exc, service="TSDR")
    except ET.ParseError as exc:
        return {
            "error": f"TSDR returned malformed XML: {exc}",
            "status_code": 502,
            "service": "TSDR",
        }
    except Exception as exc:
        return {"error": str(exc), "service": "TSDR"}


# ---------------------------------------------------------------------------
# Tool: Trademark search
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
    """Report the current support status for trademark free-text search.

    TSDR is an identifier-based status/document API; it does not provide the
    free-text mark-search endpoint previously assumed by this connector.
    """
    return {
        "error": (
            "USPTO TSDR does not provide a documented free-text trademark "
            "search endpoint. The previous /trademark/search route was invalid. "
            "Use odp_trademark_status when you have a serial number, or use the "
            "USPTO Trademark Search web service for mark-name discovery."
        ),
        "status_code": 501,
        "service": "USPTO Trademark Search",
        "supported": False,
        "mark_name": mark_name,
        "status": status,
        "page": page,
        "rows": rows,
    }


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
