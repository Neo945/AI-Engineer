"""LLM provider abstraction and context management (Phase 5).

A provider-agnostic interface with adapters for Anthropic, OpenAI, and
OpenAI-compatible local backends (vLLM, Ollama), plus token budgeting and
context compaction.
"""

from __future__ import annotations

from app.llm.clients.anthropic import AnthropicClient
from app.llm.clients.openai import OpenAIClient
from app.llm.factory import build_llm_client
from app.llm.messages import ChatMessage, ChatRole, ToolRequest
from app.llm.protocol import LLMProvider, LLMResponse, LLMStreamEvent, LLMUsage

__all__ = [
    "AnthropicClient",
    "ChatMessage",
    "ChatRole",
    "LLMProvider",
    "LLMResponse",
    "LLMStreamEvent",
    "LLMUsage",
    "OpenAIClient",
    "ToolRequest",
    "build_llm_client",
]
