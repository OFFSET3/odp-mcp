# odp-mcp

MCP server for the USPTO Open Data Portal (ODP). Exposes patent and trademark search as MCP tools consumable by ChatGPT, Claude, or any MCP-compatible agent.

## MCP Tools

| Tool | Description |
|------|-------------|
| `odp_capabilities` | List available tools and server config |
| `odp_patent_search` | Search patents by keyword; PatentsView when configured, otherwise ODP Patent File Wrapper fallback |
| `odp_patent_get` | Retrieve a patent by number; PatentsView when configured, otherwise ODP fallback |
| `odp_patent_fulltext_search` | Compatibility tool; legacy EFTS API is retired and returns an explicit unsupported response |
| `odp_application_status` | Patent application status/data via ODP Patent File Wrapper |
| `odp_trademark_status` | Trademark status by serial number via TSDR |
| `odp_trademark_search` | Compatibility tool; TSDR has no documented free-text mark-search API |

## Transport Endpoints

| Path | Protocol |
|------|----------|
| `/mcp` | Streamable HTTP (MCP 2025-03-26) — **use this in ChatGPT** |
| `/sse` | Server-Sent Events (legacy MCP) |
| `/health` | Health check |

## Configuration

| Variable | Source | Description |
|----------|--------|-------------|
| `USPTO_API_KEY` | Azure Key Vault `kv-offset3/uspto-odp-api-key` | USPTO ODP API key; required |
| `PATENTSVIEW_API_KEY` | env / secret | Optional PatentsView Search Platform key; when absent, patent search/get fall back to ODP |
| `USPTO_TSDR_API_KEY` | env / secret | Dedicated TSDR API Manager key; required for `odp_trademark_status`. The ODP key is not used as a fallback. |
| `PATENTSVIEW_BASE_URL` | env | Optional PatentsView endpoint override |
| `USPTO_ODP_APPLICATION_BASE_URL` | env | Optional ODP Patent File Wrapper endpoint override |
| `USPTO_TSDR_BASE_URL` | env | Optional TSDR endpoint override |
| `MCP_SERVER_NAME` | env | Display name (default: `USPTO ODP`) |
| `MCP_ENABLE_DNS_REBINDING_PROTECTION` | env | Set `true` + `MCP_ALLOWED_HOSTS` for custom domains |

## Local Development

```bash
cp .env.example .env
# Edit .env and add your USPTO_API_KEY
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

Health check: http://localhost:8000/health  
MCP endpoint: http://localhost:8000/mcp

## Deployment

Push to `main` → GitHub Actions builds the Docker image, pushes to `aresnetacr.azurecr.io/odp-mcp`, and deploys/updates the `odp-mcp` Azure Container App.

**Pre-requisite:** Store the USPTO API key in Key Vault before first deploy:

```bash
az keyvault secret set \
  --vault-name kv-offset3 \
  --name uspto-odp-api-key \
  --value "<your-key>"
```

The Container App's system-assigned managed identity is granted `get`/`list` on Key Vault automatically by the workflow.

### Optional TSDR trademark-status support

`odp_trademark_status` requires a separate TSDR API Manager credential. The
existing ODP credential is intentionally not reused. Until a TSDR credential
is provisioned and injected as `USPTO_TSDR_API_KEY`, that tool returns a
structured `424 configuration_required` response without making an upstream
request. Patent tools and ODP application-status tools do not depend on TSDR.

The dedicated TSDR credential is stored in Key Vault as
`kv-offset3/uspto-tsdr-api-key`. The deployment workflow injects it into the
Container App as the secret-backed environment variable `USPTO_TSDR_API_KEY`.
Both USPTO credential values are masked during deployment and must never be
written to source, issues, pull requests, Taiga, or logs.

## ChatGPT Custom MCP Setup

1. Go to **ChatGPT → Settings → Agents → Custom MCP**
2. Name: `USPTO ODP`
3. MCP Server URL: `https://odp-mcp.<env-fqdn>.azurecontainerapps.io/mcp`
4. Authentication: **None** (the API key is server-side only)
5. Click **Create**

The deployed FQDN is printed in the `Print MCP URL` step of each GitHub Actions run.
