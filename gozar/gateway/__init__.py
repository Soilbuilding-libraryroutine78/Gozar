"""Proxy_Gateway: OpenAI-compatible HTTP data-path.

Public surface:

* :data:`router` -- the FastAPI router mounting Chat Completions and Embeddings.
* :func:`complete_chat_completion` -- the non-streaming orchestration pipeline that
  wires authentication, limit enforcement, routing/fallback, translation, the
  upstream call, and usage/trace recording together.
"""

from gozar.gateway.embeddings import EmbeddingUpstreamCaller, complete_embedding
from gozar.gateway.pipeline import (
    MaterialAcquirer,
    UpstreamCaller,
    complete_chat_completion,
)
from gozar.gateway.router import router

__all__ = [
    "router",
    "complete_chat_completion",
    "complete_embedding",
    "UpstreamCaller",
    "EmbeddingUpstreamCaller",
    "MaterialAcquirer",
]
