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
from fastapi.responses import JSONResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field, field_validator

from config import dump_all_env_vars, dump_settings
from graph import build_graph
from mcp_tools import load_mcp_tools
from obo_identity import (
    DelegationDeniedError,
    MissingSubjectTokenError,
    TokenExchangeError,
    extract_bearer_token,
    last_obo_failure,
    obo_enabled,
    subject_token_scope,
)
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


def _obo_failure_response(failure: BaseException) -> JSONResponse | None:
    """Translate an OBO-layer failure into the plan's required stable
    "authorization_required" / "access_denied" result (see the design doc's
    "Runtime exchange" requirements), instead of letting the caller see only
    whatever prose the model wrote narrating ToolNode's swallowed exception —
    see obo_identity.last_obo_failure's doc comment for why that's otherwise
    the only place this failure would be visible. Returns None for anything
    that isn't an OBO-layer failure, so the caller falls through to the
    normal chat response.
    """
    if isinstance(failure, MissingSubjectTokenError):
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": "authorization_required", "message": str(failure)},
        )
    if isinstance(failure, DelegationDeniedError):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"error": "access_denied", "message": str(failure)},
        )
    if isinstance(failure, TokenExchangeError):
        # Not a denial — the exchange itself couldn't complete (timeout,
        # malformed response, endpoint unreachable). Kept distinct from
        # access_denied so a caller knows retrying later might help, unlike
        # a genuine denial.
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"error": "token_exchange_failed", "message": str(failure)},
        )
    return None


def _log_inbound_headers(http_request: Request) -> None:
    """Dumps every header this request actually arrived with — diagnostic for
    the "is the gateway really stripping Authorization, or forwarding it
    under a name/casing this agent isn't looking for" question. Starlette's
    Headers are already lower-cased and merged internally, so this is exactly
    what extract_bearer_token() itself sees, not a separate/earlier view of
    the request. Authorization's own VALUE is never logged in full, only its
    length and a short prefix — enough to confirm whether a real-looking
    token is attached, without printing a live caller credential, matching
    this module's existing "presence only" convention for the parsed token.
    Every OTHER header is logged in full: none of them are expected to carry
    caller-sensitive data, and seeing exactly what the gateway attaches
    (x-envoy-*, x-forwarded-*, etc.) is the whole point of this dump.
    """
    logger.info("=" * 70)
    logger.info("[DEBUG] /chat INBOUND REQUEST HEADERS (raw, as received by this pod)")
    logger.info("=" * 70)
    for key, value in http_request.headers.items():
        if key.lower() == "authorization":
            logger.info("  %s: (len=%d, starts_with=%r)", key, len(value), value[:16])
        else:
            logger.info("  %s: %s", key, value)
    logger.info("=" * 70)


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, http_request: Request) -> ChatResponse | JSONResponse:
    _log_inbound_headers(http_request)

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
    obo_failure: BaseException | None = None
    try:
        with subject_token_scope(subject_token) as request_key:
            result = await agent_graph.ainvoke(
                {"messages": [HumanMessage(content=wrapped_message)]},
                config={
                    "recursion_limit": 50,
                    "configurable": {"thread_id": request.session_id},
                },
            )
            # Keyed by request_key, not session_id: a client can (and did, in
            # testing) submit the same session_id twice concurrently, and each
            # call's own failure must stay its own — see the module-level
            # comment on obo_identity's request-key dict for why a ContextVar
            # alone can't carry this value back out of here at all (the actual
            # MCP call, and so record_obo_failure, runs inside a TaskGroup
            # LangGraph spawns internally).
            obo_failure = last_obo_failure(request_key)
    except Exception:
        logger.exception("chat invoke failed: session_id=%s", request.session_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )

    if obo_failure is not None:
        failure_response = _obo_failure_response(obo_failure)
        if failure_response is not None:
            logger.warning(
                "[OBO][WARN] /chat session_id='%s' — overriding chat response with a stable OBO failure "
                "result instead of the model's narrated tool-error text: %s: %s",
                request.session_id, type(obo_failure).__name__, obo_failure,
            )
            return failure_response

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
