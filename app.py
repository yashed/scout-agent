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
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field, field_validator

from config import dump_all_env_vars, dump_settings
from graph import build_graph
from mcp_tools import load_mcp_tools
from obo_identity import extract_bearer_token, obo_enabled, subject_token_scope
from tools import NATIVE_TOOLS

# ---------------------------------------------------------------------------
# Logging — controlled by LOG_LEVEL env var (default: DEBUG for this branch).
# Set LOG_LEVEL=INFO in the platform environment to quieten debug output.
# Valid values: DEBUG, INFO, WARNING, ERROR
# ---------------------------------------------------------------------------
_log_level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
_log_level = getattr(logging, _log_level_name, logging.DEBUG)
logging.basicConfig(
    level=_log_level,
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
logger.info("=" * 70)
logger.info("[DEBUG] SCOUT AGENT STARTING UP")
logger.info("[DEBUG] Log level: %s (set LOG_LEVEL env var to change, e.g. LOG_LEVEL=INFO)", _log_level_name)
logger.info("=" * 70)

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
logger.info(
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
async def chat(request: ChatRequest, http_request: Request) -> ChatResponse:
    # The caller's own access token, forwarded by Agent Manager's gateway when
    # this agent's "Invocation security is OAuth" + "Token forwarding" are
    # both enabled (see Agent ID -> Delegated Access in the console). Only
    # its presence is logged, never the token itself — unlike this agent's
    # own static AMP_AGENTID_* secret, this is a live caller credential.
    subject_token = extract_bearer_token(http_request.headers.get("authorization"))
    logger.info(
        "[DEBUG] /chat called — session_id='%s' | message_len=%d | context_keys=%s | "
        "caller_token=%s | obo_enabled=%s",
        request.session_id,
        len(request.message),
        list(request.context.keys()),
        "present" if subject_token else "absent",
        obo_enabled(),
    )
    if obo_enabled() and not subject_token:
        logger.warning(
            "[OBO][WARN] AMP_OBO_ENABLED is true but this request carried no caller Bearer token — "
            "any tool call this turn makes will fail with MissingSubjectTokenError rather than "
            "silently running as this agent's own identity"
        )
    wrapped_message = _wrap_user_message(request.message, request.context)
    try:
        with subject_token_scope(subject_token):
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
        logger.info("[DEBUG] /chat session_id='%s' — graph returned no messages", request.session_id)
        return ChatResponse(response="", session_id=request.session_id)

    content = messages[-1].content
    if isinstance(content, str):
        response_text = content
    elif isinstance(content, list):
        response_text = "\n".join(str(part) for part in content)
    else:
        response_text = str(content)

    logger.info(
        "[DEBUG] /chat session_id='%s' — final response length=%d chars",
        request.session_id,
        len(response_text),
    )
    return ChatResponse(response=response_text, session_id=request.session_id)
