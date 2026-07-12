"""Translation_Layer: format adapters per provider (pure functions).

This package defines the canonical OpenAI Chat Completions types, the
:class:`ProviderAdapter` protocol every adapter implements, and the concrete
adapters. The pass-through :class:`OpenAICompatAdapter` is exported here; the
Codex and Anthropic adapters are added in subsequent tasks.
"""

from .anthropic import AnthropicAdapter
from .codex import CodexAdapter
from .openai_compat import OpenAICompatAdapter
from .types import (
    OpenAIChatMessage,
    OpenAIChatRequest,
    OpenAIChatResponse,
    OpenAIResponseChoice,
    OpenAIStreamChoice,
    OpenAIStreamChunk,
    ProviderAdapter,
    ProviderChunk,
    ProviderRequest,
    ProviderResponse,
    UsageCounts,
)

__all__ = [
    "OpenAIChatMessage",
    "OpenAIChatRequest",
    "OpenAIChatResponse",
    "OpenAIResponseChoice",
    "OpenAIStreamChoice",
    "OpenAIStreamChunk",
    "ProviderAdapter",
    "ProviderChunk",
    "ProviderRequest",
    "ProviderResponse",
    "UsageCounts",
    "OpenAICompatAdapter",
    "AnthropicAdapter",
    "CodexAdapter",
]
