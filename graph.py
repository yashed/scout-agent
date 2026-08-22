from __future__ import annotations

import logging
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage, SystemMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are Scout, a helpful research and travel-planning assistant.

You have two kinds of tools:
- Native tools for weather, currency conversion, and country facts.
- Tools discovered from external MCP servers (fetching web pages, answering
  questions about public GitHub repos via DeepWiki), when configured.

Instructions:
- Use a tool whenever a question needs current, external, or numeric information instead of guessing.
- Prefer the native tools for weather/currency/country questions.
- Use the fetch tool to read a specific URL, and the DeepWiki tool to answer questions about a public GitHub repository's documentation.
- Present only information a tool actually returned. If a tool errors or a capability isn't available, say so plainly instead of inventing an answer.
- Keep responses concise and in markdown.
"""


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


def build_graph(tools: list[BaseTool]):
    logger.info("=" * 70)
    logger.info("[DEBUG] BUILDING LANGGRAPH AGENT")
    logger.info("  llm mode        = %s", "agent-manager LLM proxy" if settings.use_llm_provider else "OpenAI direct")
    logger.info("  model           = %s", settings.openai_model)
    logger.info("  llm_provider_url= %s", settings.llm_provider_url if settings.use_llm_provider else "(direct OpenAI)")
    logger.info("  timeout         = %s", settings.openai_timeout)
    logger.info("  max_retries     = %s", settings.openai_max_retries)
    logger.info("  tools bound     = %d", len(tools))
    logger.info("  tool names      = %s", [getattr(t, "name", str(t)) for t in tools])
    logger.info("=" * 70)

    if settings.use_llm_provider:
        # Route through the Agent Manager LLM proxy / AI gateway. The gateway is
        # OpenAI-compatible and authenticates via the "API-Key" header; the key
        # is gateway-scoped (NOT a raw OpenAI key). Blank out "Authorization" so
        # the OpenAI SDK's default "Bearer <api_key>" header doesn't override it.
        llm_kwargs = {
            "model": settings.openai_model,
            "base_url": settings.llm_provider_url,
            "api_key": "not-used",
            "timeout": settings.openai_timeout,
            "max_retries": settings.openai_max_retries,
            "default_headers": {
                "API-Key": settings.llm_provider_key,
                "Authorization": "",
            },
        }
    else:
        # Call OpenAI directly with the configured OPENAI_API_KEY.
        llm_kwargs = {
            "model": settings.openai_model,
            "api_key": settings.openai_api_key or "dummy",
            "timeout": settings.openai_timeout,
            "max_retries": settings.openai_max_retries,
        }

    llm = ChatOpenAI(**llm_kwargs).bind_tools(tools)

    _invoke_count = {"n": 0}

    async def agent_node(state: AgentState) -> AgentState:
        _invoke_count["n"] += 1
        invoke_num = _invoke_count["n"]
        messages = [SystemMessage(content=SYSTEM_PROMPT)] + state["messages"]

        logger.info(
            "[DEBUG] agent_node invoke #%d — sending %d message(s) to OpenAI model '%s'",
            invoke_num,
            len(messages),
            settings.openai_model,
        )
        logger.info(
            "[DEBUG] Last user message content: %s",
            getattr(messages[-1], "content", str(messages[-1]))[:500],  # truncate long msgs
        )

        try:
            response = await llm.ainvoke(messages)
        except Exception as exc:
            logger.exception(
                "[FAIL] OpenAI token generation FAILED on invoke #%d — model='%s' | error: %s",
                invoke_num,
                settings.openai_model,
                exc,
            )
            raise

        # Log the generated token / response
        logger.info(
            "[OK]   OpenAI token generation PASSED on invoke #%d", invoke_num
        )
        logger.info(
            "[DEBUG] Generated response type: %s | id: %s",
            type(response).__name__,
            getattr(response, "id", "N/A"),
        )
        logger.info(
            "[DEBUG] Generated token/response content: %s",
            getattr(response, "content", str(response)),
        )

        # Log usage metadata (token counts) if available
        usage = getattr(response, "usage_metadata", None) or getattr(response, "response_metadata", {}).get("token_usage")
        if usage:
            logger.info("[DEBUG] Token usage: %s", usage)
        else:
            logger.info("[DEBUG] Token usage metadata not available in response")

        tool_calls = getattr(response, "tool_calls", None) or []
        if tool_calls:
            logger.info(
                "[DEBUG] agent_node invoke #%d — model decided to call %d tool(s): %s",
                invoke_num,
                len(tool_calls),
                [call.get("name") if isinstance(call, dict) else getattr(call, "name", str(call)) for call in tool_calls],
            )
        else:
            logger.info(
                "[DEBUG] agent_node invoke #%d — model returned a final answer (no tool calls)",
                invoke_num,
            )

        return {"messages": [response]}

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", ToolNode(tools))
    graph.add_conditional_edges("agent", tools_condition)
    graph.add_edge("tools", "agent")
    graph.set_entry_point("agent")

    checkpointer = InMemorySaver()
    compiled = graph.compile(checkpointer=checkpointer)
    logger.info("[DEBUG] LangGraph agent compiled successfully")
    return compiled
