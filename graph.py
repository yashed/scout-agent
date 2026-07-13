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
    llm = ChatOpenAI(
        model=settings.openai_model,
        api_key=settings.openai_api_key,
        timeout=settings.openai_timeout,
        max_retries=settings.openai_max_retries,
    ).bind_tools(tools)

    def agent_node(state: AgentState) -> AgentState:
        messages = [SystemMessage(content=SYSTEM_PROMPT)] + state["messages"]
        response = llm.invoke(messages)
        tool_calls = getattr(response, "tool_calls", None) or []
        if tool_calls:
            logger.debug(
                "agent_node decided to call tools: %s",
                [call.get("name") for call in tool_calls if isinstance(call, dict)],
            )
        return {"messages": [response]}

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", ToolNode(tools))
    graph.add_conditional_edges("agent", tools_condition)
    graph.add_edge("tools", "agent")
    graph.set_entry_point("agent")

    checkpointer = InMemorySaver()
    return graph.compile(checkpointer=checkpointer)
