"""Core/shared ORM models.

Owns tables with the ``core_`` prefix: cross-cutting runtime configuration that is
not secret material and does not belong to a single account, token, route, or usage
module.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, JSON, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from gozar.core.db import Base


class ProviderModelCatalog(Base):
    """Runtime fallback model list for one provider.

    ``GOZAR_PROVIDER_MODELS`` remains the bootstrap/default fallback. Rows in this
    table let an operator update those fallback lists from the console/API without
    restarting the process. Providers with live model listing still prefer their
    live ``/models`` response; this row is used when live listing is unavailable.
    """

    __tablename__ = "core_provider_model_catalog"

    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    model_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ProviderModelCatalog(provider={self.provider!r})"

    @property
    def safe_model_ids(self) -> list[str]:
        """Return the stored ids when the JSON payload is shaped correctly."""
        if not isinstance(self.model_ids, list):
            return []
        return [str(model_id) for model_id in self.model_ids if model_id]
