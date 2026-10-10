# Scout - Multi-Tool Test Agent

Scout is a small LangGraph + OpenAI agent built to exercise Agent Manager (AMP)
end-to-end: a platform-hosted Chat Agent with **native Python tools** and
**multiple MCP proxies** attached, wired together with LangGraph's tool-calling
loop.

## What it demonstrates

- **LangGraph agent** with an OpenAI chat model (`ChatOpenAI`) bound to tools,
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
| `config.py` | Settings: OpenAI config + injected MCP proxy URL/API-key env vars |
| `tools.py` | Native tools (weather, currency, country facts) |
| `mcp_tools.py` | Builds a `MultiServerMCPClient` from whatever proxies are configured and discovers their tools |
| `graph.py` | LangGraph `StateGraph` wiring the LLM and tools together |
| `app.py` | FastAPI entrypoint: `/chat`, `/health` |
| `main.py` | Local runner (`python main.py`) |
| `agent_identity.py` | Mints/caches this agent's own client_credentials bearer token |
| `obo_identity.py` | RFC 8693 token exchange for delegated (OBO) MCP access — see below |

## Prerequisites

- An OpenAI API key.
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
   OPENAI_API_KEY=<your-openai-api-key>
   OPENAI_MODEL=gpt-4o-mini
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

## Delegated access (OBO)

By default Scout calls MCP proxies acting as its own AgentID
(`client_credentials`, see `agent_identity.py`). Setting `AMP_OBO_ENABLED=true`
switches it to delegated (On-Behalf-Of) access instead: every `/chat` request
forwards the caller's own access token, Scout exchanges it (RFC 8693 token
exchange, implemented from scratch in `obo_identity.py` — no runtime library
dependency) for a token scoped to the specific MCP resource, and calls that
proxy acting as the caller rather than as itself.

This is a security-relevant mode change, so it fails closed: if
`AMP_OBO_ENABLED=true` and a `/chat` request arrives with no caller token
forwarded, Scout does **not** fall back to its own identity — the tool call
raises `MissingSubjectTokenError` instead.

### Setup, in Agent Manager's console

1. **Configure -> Invocation Security**: enable OAuth, and enable token
   forwarding — this is what puts the caller's token on the `/chat` request
   Scout receives.
2. **Agent ID -> Delegated Access (OBO)**: work through the readiness
   checklist (a trusted issuer Connection enabled for the environment, with
   an authorization mapping rule covering your test caller; at least one
   resource/scope added to this agent's Delegation Ceiling), then flip
   **Enable Delegated Access** once every row is satisfied.
3. Set `AMP_OBO_ENABLED=true` and `FETCH_OBO_SCOPES` / `DEEPWIKI_OBO_SCOPES`
   (space-separated) to the scope(s) your authorization mapping's target role
   actually grants for that resource — see the `.env.example` comment for why
   this can't be left blank.
4. Call Scout through something that actually forwards a real end-user
   token (an Application invoking it through the gateway, not the console's
   own Try It panel, which calls as the logged-in admin's own session).

### Troubleshooting

- `MissingSubjectTokenError` — no caller token reached Scout. Check token
  forwarding is enabled on the agent's invocation security config, and that
  whatever is calling Scout is itself presenting a real OAuth access token.
- `DelegationDeniedError` (ThunderID `access_denied` / `invalid_target` /
  `invalid_scope`) — the exchange reached ThunderID and was rejected: check
  the token-exchange grant is enabled on this AgentID, the trusted connection
  is enabled, and its authorization mapping actually covers the caller's
  token claims.
- An exchange that succeeds but the MCP call still gets a 403 — almost always
  `FETCH_OBO_SCOPES`/`DEEPWIKI_OBO_SCOPES` not matching a scope the mapped
  role actually grants (see `obo_identity.py`'s module docstring on why an
  empty `scope` request grants nothing, it's not "all allowed scopes").

## Observing traces

Open the agent in AMP Console -> **Observability** -> **Traces**. Since Scout
uses LangChain/LangGraph directly (no manual instrumentation), leave
auto-instrumentation **on** — AMP's Traceloop-based auto-instrumentation
covers LangChain/LangGraph out of the box, and each `/chat` call produces one
trace showing the LLM call(s), tool-call span(s), and the final answer.
