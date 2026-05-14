# odp-mcp

MCP server for the USPTO Open Data Portal (ODP). Exposes patent and trademark search as MCP tools consumable by ChatGPT, Claude, or any MCP-compatible agent.

## MCP Tools

| Tool | Description |
|------|-------------|
| `odp_capabilities` | List available tools and server config |
| `odp_patent_search` | Search patents by keyword (PatentsView API) |
| `odp_patent_get` | Retrieve a patent by number |
| `odp_patent_fulltext_search` | Full-text search across patent grants + applications (EFTS) |
| `odp_application_status` | Patent application prosecution status (PEDS) |
| `odp_trademark_status` | Trademark status by serial number (TSDR) |
| `odp_trademark_search` | Search trademarks by mark name (TSDR) |

## Transport Endpoints

| Path | Protocol |
|------|----------|
| `/mcp` | Streamable HTTP (MCP 2025-03-26) — **use this in ChatGPT** |
| `/sse` | Server-Sent Events (legacy MCP) |
| `/health` | Health check |

## Configuration

| Variable | Source | Description |
|----------|--------|-------------|
| `USPTO_API_KEY` | Azure Key Vault `kv-offset3/uspto-odp-api-key` | USPTO ODP API key |
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

## ChatGPT Custom MCP Setup

1. Go to **ChatGPT → Settings → Agents → Custom MCP**
2. Name: `USPTO ODP`
3. MCP Server URL: `https://odp-mcp.<env-fqdn>.azurecontainerapps.io/mcp`
4. Authentication: **None** (the API key is server-side only)
5. Click **Create**

The deployed FQDN is printed in the `Print MCP URL` step of each GitHub Actions run.
