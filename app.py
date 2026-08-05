"""FastAPI entrypoint for the Scout sample agent.

Implements the AM chat-agent contract: ``POST /chat`` on port 8000 accepting
``{message, session_id, context}`` and returning ``{response, session_id}``.
``GET /health`` is provided for local and platform health checks.

MCP tools are discovered once at startup (see ``mcp_tools.load_mcp_tools``)
from whatever proxies Agent Manager has injected via Tool Configurations, and
merged with the native tools before the LangGraph agent is built.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, status
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field, field_validator

from config import dump_all_env_vars, dump_settings
from graph import build_graph
from mcp_tools import load_mcp_tools
from tools import NATIVE_TOOLS

# ---------------------------------------------------------------------------
# Logging — set to DEBUG so all [DEBUG] lines in this branch are visible.
# Change back to INFO before shipping to production.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Quieten noisy third-party loggers so our debug lines are easy to spot
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

logger = logging.getLogger("scout-agent")

# ---------------------------------------------------------------------------
# Startup debug dump — runs before anything else
# ---------------------------------------------------------------------------
logger.debug("=" * 70)
logger.debug("[DEBUG] SCOUT AGENT STARTING UP — DEBUG LOGGING ENABLED")
logger.debug("=" * 70)

# 1. Dump ALL OS environment variables (including secrets)
dump_all_env_vars()

# 2. Dump parsed settings with validation checks
dump_settings()

# ---------------------------------------------------------------------------
# MCP tool discovery + agent graph build
# ---------------------------------------------------------------------------
mcp_tools = asyncio.run(load_mcp_tools())
all_tools = NATIVE_TOOLS + mcp_tools
agent_graph = build_graph(all_tools)

logger.info(
    "Scout agent ready with %d tool(s): %s",
    len(all_tools),
    [getattr(t, "name", str(t)) for t in all_tools],
)
logger.debug(
    "[DEBUG] Native tools: %s | MCP tools: %s",
    [getattr(t, "name", str(t)) for t in NATIVE_TOOLS],
    [getattr(t, "name", str(t)) for t in mcp_tools],
)


class ChatRequest(BaseModel):
    message: str
    session_id: str
    context: dict[str, Any] = Field(default_factory=dict)

    @field_validator("session_id", mode="before")
    @classmethod
    def validate_session_id(cls, value: Any) -> str:
        if value is None:
            raise ValueError("session_id must be a non-empty string")
        trimmed = str(value).strip()
        if not trimmed:
            raise ValueError("session_id must be a non-empty string")
        return trimmed


class ChatResponse(BaseModel):
    response: str
    session_id: str


app = FastAPI(title="Scout - Multi-Tool Test Agent")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _wrap_user_message(user_message: str, context: dict[str, Any]) -> str:
    now = datetime.now(timezone.utc).isoformat()
    context_json = json.dumps(context, default=str, ensure_ascii=True)
    return (
        f"Request Context JSON:\n{context_json}\n"
        f"UTC Time now:\n{now}\n\n"
        f"User Query:\n{user_message}"
    )


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    logger.debug(
        "[DEBUG] /chat called — session_id='%s' | message_len=%d | context_keys=%s",
        request.session_id,
        len(request.message),
        list(request.context.keys()),
    )
    wrapped_message = _wrap_user_message(request.message, request.context)
    try:
        result = await agent_graph.ainvoke(
            {"messages": [HumanMessage(content=wrapped_message)]},
            config={
                "recursion_limit": 50,
                "configurable": {"thread_id": request.session_id},
            },
        )
    except Exception:
        logger.exception("chat invoke failed: session_id=%s", request.session_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )

    messages = result.get("messages") if isinstance(result, dict) else None
    if not messages:
        logger.debug("[DEBUG] /chat session_id='%s' — graph returned no messages", request.session_id)
        return ChatResponse(response="", session_id=request.session_id)

    content = messages[-1].content
    if isinstance(content, str):
        response_text = content
    elif isinstance(content, list):
        response_text = "\n".join(str(part) for part in content)
    else:
        response_text = str(content)

    logger.debug(
        "[DEBUG] /chat session_id='%s' — final response length=%d chars",
        request.session_id,
        len(response_text),
    )
    return ChatResponse(response=response_text, session_id=request.session_id)
