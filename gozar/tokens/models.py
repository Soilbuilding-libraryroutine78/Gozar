"""Token_Authority ORM models.

Owns the ``tok_`` tables (module prefix per the convention in
:mod:`gozar.core.db`):

* :class:`ClientToken` (``tok_client_token``) -- a Gozar-issued credential a
  Client_Application presents to authenticate proxy requests. Verification uses a
  non-reversible HMAC-SHA256 digest (keyed with the server-side pepper) plus a
  non-secret ``id_prefix`` lookup key embedded in the token string. The presentable
  secret is also stored with the same envelope encryption used for upstream
  credentials so an operator can reveal the same API key after password confirmation,
  without rotating or revoking the existing key.
* :class:`TokenUsageLimit` (``tok_usage_limit``) -- the uniform Usage_Limit shape
  attached to a Client_Token (Requirements 9.1). The same shape is reused for
  upstream credentials by the accounts module (``acct_usage_limit``).

This module is imported by the Alembic ``env.py`` so the tables register on
``Base.metadata`` for autogenerate/migrations.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import (
    DateTime,
    ForeignKey,
    LargeBinary,
    Numeric,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from gozar.core.db import Base


class TokenStatus(str, Enum):
    """Lifecycle status of a Client_Token.

    A token authorizes proxy requests only while :data:`ACTIVE`; :data:`DISABLED`
    (Requirement 9.3/9.4) and :data:`REVOKED` (Requirement 8.4) both deny access.
    Revocation is terminal, whereas a disabled token may be re-enabled.
    """

    ACTIVE = "active"
    DISABLED = "disabled"
    REVOKED = "revoked"


class ClientToken(Base):
    """A Gozar-issued client token (``tok_client_token``).

    Columns mirror the design's ``tok_client_token`` table. ``token_hash`` holds the
    keyed HMAC-SHA256 digest of the secret (never the secret itself), and
    ``id_prefix`` is the non-secret lookup key embedded in the ``gz-<id_prefix>-<secret>``
    token string so a presented token can be located in constant work before the
    constant-time hash comparison.
    """

    __tablename__ = "tok_client_token"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    # Non-secret lookup key embedded in the token string; unique so it indexes one row.
    id_prefix: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    # HMAC-SHA256(secret, server pepper). Non-reversible (Requirement 8.2).
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Envelope-encrypted full presentable token string. Nullable for tokens created
    # before reveal-at-rest existed; those legacy secrets cannot be recovered.
    secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    secret_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    secret_wrapped_dek: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=TokenStatus.ACTIVE.value,
    )
    # Optional explicit routing override: when set, requests authenticated by this
    # token use the selected fallback chain before model-selector auto routing.
    assigned_chain_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("route_fallback_chain.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # Never include token_hash or any secret-derived material.
        return (
            f"ClientToken(id={self.id!r}, id_prefix={self.id_prefix!r}, "
            f"status={self.status!r})"
        )


class TokenUsageLimit(Base):
    """A Usage_Limit attached to a Client_Token (``tok_usage_limit``).

    Uniform Usage_Limit shape (design "acct_usage_limit and tok_usage_limit"):
    ``subject_kind`` is always ``"token"`` here, ``subject_id`` references the owning
    :class:`ClientToken`, and the remaining columns mirror
    :class:`~gozar.usage.limits.UsageLimitSpec`. There is at most one limit row per
    token (enforced by a unique constraint on ``subject_id``).
    """

    __tablename__ = "tok_usage_limit"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    subject_kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="token",
    )
    # References tok_client_token.id; not a hard FK because the uniform shape is
    # subject-polymorphic across modules. Unique so a token has a single limit.
    subject_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        unique=True,
        nullable=False,
    )
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    limit_value: Mapped[float] = mapped_column(Numeric, nullable=False)
    capacity: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    window: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"TokenUsageLimit(subject_id={self.subject_id!r}, metric={self.metric!r}, "
            f"limit_value={self.limit_value!r}, window={self.window!r})"
        )
