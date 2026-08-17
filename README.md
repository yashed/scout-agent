# Scout - Multi-Tool Test Agent

Scout is a small LangGraph + Anthropic Claude agent built to exercise Agent Manager (AMP)
end-to-end: a platform-hosted Chat Agent with **native Python tools** and
**multiple MCP proxies** attached, wired together with LangGraph's tool-calling
loop.

## What it demonstrates

- **LangGraph agent** with an Anthropic chat model (`ChatAnthropic`) bound to tools,
  following the same `agent -> tools -> agent` loop as `hotel-booking-agent`.
- **Native tools** (no MCP, free public APIs, no keys required):
  - `get_weather_tool` - current weather for a city (Open-Meteo)
  - `get_exchange_rate_tool` - currency conversion (open.er-api.com)
  - `get_country_info_tool` - country facts (mledoze/countries dataset)
- **MCP tools**, discovered at startup from proxies AMP injects as env vars:
  - **Fetch** - fetches and reads a web page (`mcp-server-fetch`)
  - **DeepWiki** - answers questions about a public GitHub repo's docs
    (public, hosted, no auth)
- **Standard Chat Agent contract**: `POST /chat` + `GET /health` on port 8000.

## Files

| File | Role |
|---|---|
| `config.py` | Settings: Anthropic config + injected MCP proxy URL/API-key env vars |
| `tools.py` | Native tools (weather, currency, country facts) |
| `mcp_tools.py` | Builds a `MultiServerMCPClient` from whatever proxies are configured and discovers their tools |
| `graph.py` | LangGraph `StateGraph` wiring the LLM and tools together |
| `app.py` | FastAPI entrypoint: `/chat`, `/health` |
| `main.py` | Local runner (`python main.py`) |

## Prerequisites

- An Anthropic API key.
- Access to the Agent Manager console.
- A Render account (free tier) to host the Fetch MCP bridge.

## Step 1: Deploy the Fetch MCP server (Render)

`mcp-server-fetch` only speaks stdio, so it needs a small HTTP bridge to be
reachable as an upstream MCP server. See [`../fetch-mcp-bridge`](../fetch-mcp-bridge)
— a minimal Dockerized wrapper (verified locally: builds, runs, and serves a
working streamable-http MCP endpoint at `/mcp`).

1. Follow [`fetch-mcp-bridge/README.md`](../fetch-mcp-bridge/README.md) to deploy it as a
   Render Web Service (Docker environment, free tier is fine for testing).
2. Once live, your upstream endpoint is `https://<your-service>.onrender.com/mcp`
   — this is what you'll register as an MCP proxy below.

> Render's free tier spins down after ~15 min idle and takes 30-60s to wake
> up on the next request — retry once if the first "Fetch Server Info" call
> in AMP times out.

## Step 2: Register two MCP Proxies in Agent Manager

In the AMP Console, go to the **organization level -> MCP Proxies -> Add MCP Proxy**,
and register each of the following (see
[Register an MCP Proxy](https://wso2.github.io/agent-manager/docs/latest/tutorials/register-mcp-proxy/)):

**Fetch**
- Name: `Fetch`
- Endpoint URL: `https://<your-service>.onrender.com/mcp` (from Step 1)
- No upstream auth header needed

**DeepWiki**
- Name: `DeepWiki`
- Endpoint URL: `https://mcp.deepwiki.com/mcp` (public, no auth; if fetching
  server info fails, try `https://mcp.deepwiki.com/sse` instead)

For each, click **Fetch Server Info** to confirm tools are discovered, select
the environment(s) to serve, then **Add** and **Create**.

## Step 3: Deploy Scout as a platform-hosted agent

This is a standalone project, not part of the `wso2/agent-manager` repo — push
it to your own GitHub repository first, then point Agent Manager at it.

1. In your AMP project, click **Add Agent** -> **Platform-Hosted Agent**.
2. Fill in:

   | Field | Value |
   |---|---|
   | **Display Name** | `Scout` |
   | **Description** | `Multi-tool, multi-MCP LangGraph test agent` |
   | **GitHub Repository** | `<your-repo-url>` (this `scout-agent` project pushed to your own GitHub) |
   | **Branch** | `main` |
   | **App Path** | `.` (repo root) or the subfolder you placed it in |
   | **Language** | `Python` |
   | **Language Version** | `3.11` |
   | **Start Command** | `python main.py` |
   | **Port** | `8000` |

3. Select **Chat Agent** as the agent interface type.
4. Environment variables:

   ```env
   ANTHROPIC_URL=<agent-manager-ai-gateway-url>
   ANTHROPIC_API_KEY=<your-anthropic-api-key>
   ANTHROPIC_MODEL=claude-3-5-sonnet-20241022
   ```

5. Optionally attach both MCP proxies right here under **MCP Proxies
   (Optional)** (see Step 4 for the variable names to use), or attach them
   afterward from the agent's **Configure** page.
6. Click **Deploy** and wait for the build to finish.

## Step 4: Attach the MCP proxies to the agent

1. Open the agent -> **Configure** -> **Tool Configurations** tab -> **Add Tool Configuration**.
2. Select the **Fetch** proxy. In **Environment Variable Names**, set:
   - `url` -> `FETCH_URL`
   - `apikey` -> `FETCH_API_KEY`
3. Repeat for **DeepWiki**:
   - `url` -> `DEEPWIKI_URL`
   - `apikey` -> `DEEPWIKI_API_KEY`

These names must match `config.py`'s `validation_alias`es. If you'd rather
keep AMP's auto-generated names, edit `config.py` instead.

Redeploy the agent (or trigger its next deployment) so it picks up the
injected values — `app.py` calls `load_mcp_tools()` once at process startup.

## Testing

Use the agent's **Try It** / chat interface in the AMP Console, or `curl`
directly if running locally:

```bash
curl -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "What is the weather in Colombo, and how much is 100 USD in LKR?", "session_id": "demo-1"}'
```

Sample prompts covering every tool:

- **Native — weather**: `What's the weather like in Tokyo right now?`
- **Native — currency**: `Convert 250 EUR to GBP.`
- **Native — country facts**: `Tell me the capital and languages of Sri Lanka.`
- **MCP — fetch**: `Fetch https://example.com and summarize it.`
- **MCP — DeepWiki**: `Using DeepWiki, explain how the langchain-ai/langgraph repo structures its checkpointing.`
- **Multi-tool**: `I'm planning a trip to Japan — what's the weather in Tokyo, what's 500 USD in JPY, and what language do they speak there?`

If a proxy isn't attached yet, Scout still starts (see the startup log line
listing loaded tools) — it just answers with native tools only and says so if
asked to fetch a page or query DeepWiki.

## Observing traces

Open the agent in AMP Console -> **Observability** -> **Traces**. Since Scout
uses LangChain/LangGraph directly (no manual instrumentation), leave
auto-instrumentation **on** — AMP's Traceloop-based auto-instrumentation
covers LangChain/LangGraph out of the box, and each `/chat` call produces one
trace showing the LLM call(s), tool-call span(s), and the final answer.
