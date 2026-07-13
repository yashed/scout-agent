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

from graph import build_graph
from mcp_tools import load_mcp_tools
from tools import NATIVE_TOOLS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("scout-agent")

mcp_tools = asyncio.run(load_mcp_tools())
all_tools = NATIVE_TOOLS + mcp_tools
agent_graph = build_graph(all_tools)
logger.info(
    "Scout agent ready with %d tool(s): %s",
    len(all_tools),
    [getattr(t, "name", str(t)) for t in all_tools],
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
def chat(request: ChatRequest) -> ChatResponse:
    wrapped_message = _wrap_user_message(request.message, request.context)
    try:
        result = agent_graph.invoke(
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
        return ChatResponse(response="", session_id=request.session_id)

    content = messages[-1].content
    if isinstance(content, str):
        response_text = content
    elif isinstance(content, list):
        response_text = "\n".join(str(part) for part in content)
    else:
        response_text = str(content)
    return ChatResponse(response=response_text, session_id=request.session_id)
