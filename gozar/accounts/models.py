"""Account_Manager ORM models.

Owns the ``acct_`` tables (module prefix per the convention in
:mod:`gozar.core.db`): the upstream credential record, its one-to-one encrypted
secret tables (subscription bundle and API key), and the per-account usage limit.

Tables
------
* :class:`UpstreamCredential` -- ``acct_upstream_credential``: one row per connected
  Subscription_Account or API_Key_Account. Carries the unique account identifier,
  Provider, kind, label, lifecycle status, the provider-side account reference, the
  connection timestamp, the reauthorization reason, and a ``deleted_at`` soft-delete
  marker (Requirements 1.4, 3.3, 5.1, 5.3).
* :class:`SubscriptionSecret` -- ``acct_subscription_secret``: one-to-one encrypted
  subscription token bundle. Columns mirror
  :class:`gozar.core.crypto.EncryptedRecord` (``ciphertext``, ``nonce``,
  ``wrapped_dek``) plus an ``expires_at`` driving the renewal window
  (Requirements 1.2, 3.1, 3.2, 16.2).
* :class:`ApiKeySecret` -- ``acct_api_key_secret``: one-to-one encrypted API key,
  same envelope-encryption columns (Requirements 2.2, 16.2).
* :class:`AccountUsageLimit` -- ``acct_usage_limit``: the per-account Usage_Limit,
  column-shaped to map to/from :class:`gozar.usage.limits.UsageLimitSpec`
  (Requirements 4.1).

All secret material is stored only as ciphertext produced by the envelope-encryption
scheme; this module never holds plaintext. The hard-delete of secret rows on account
deletion (while the credential row and its usage history are retained) is implemented
in the Account_Manager lifecycle layer (task 5.2).

This module is imported by the Alembic ``env.py`` so the tables register on
``Base.metadata`` for autogenerate/migrations.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import (
    DateTime,
    Enum as SAEnum,
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
from gozar.usage.limits import LimitMetric, LimitWindow


class CredentialKind(str, Enum):
    """How an Upstream_Credential authenticates to its Provider.

    ``SUBSCRIPTION`` credentials authenticate with a subscription token bundle
    (OAuth); ``API_KEY`` credentials authenticate with a conventional metered key.
    Stored as text in the ``kind`` column.
    """

    SUBSCRIPTION = "subscription"
    API_KEY = "api_key"


class CredentialStatus(str, Enum):
    """Lifecycle status of an Upstream_Credential.

    ``ACTIVE`` credentials are eligible for routing; ``DISABLED`` ones are turned off
    by the Operator (Requirement 5.1); ``REQUIRES_REAUTH`` ones failed a token
    refresh and must be reconnected (Requirement 3.3). Stored as text in the
    ``status`` column.
    """

    ACTIVE = "active"
    DISABLED = "disabled"
    REQUIRES_REAUTH = "requires_reauth"


# Stored-as-text enum columns: VARCHAR + CHECK constraint (no native PG enum type),
# persisting the lowercase ``.value`` of each member to stay consistent with the
# rest of the schema and with UsageLimitSpec serialization.
def _text_enum(enum_cls: type) -> SAEnum:
    return SAEnum(
        enum_cls,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda e: [member.value for member in e],
    )


class UpstreamCredential(Base):
    """A connected Subscription_Account or API_Key_Account (``acct_upstream_credential``).

    The ``id`` is the unique account identifier recorded at connection time
    (Requirement 1.4). ``provider`` is indexed because routing and analytics filter
    by Provider. ``deleted_at`` implements soft-delete: the Flow_Controller treats a
    non-null ``deleted_at`` as "deleted -> skip" (Requirement 11.2) while usage
    history is retained for reporting (Requirement 5.3).
    """

    __tablename__ = "acct_upstream_credential"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    kind: Mapped[CredentialKind] = mapped_column(
        _text_enum(CredentialKind), nullable=False
    )
    label: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[CredentialStatus] = mapped_column(
        _text_enum(CredentialStatus),
        nullable=False,
        default=CredentialStatus.ACTIVE,
    )
    provider_account_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    requires_reauth_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    # Soft-delete marker. NULL means the credential is live; non-NULL records when it
    # was deleted (secret material is hard-deleted, the row is retained).
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"UpstreamCredential(id={self.id!r}, provider={self.provider!r}, "
            f"kind={self.kind!r}, status={self.status!r})"
        )


class SubscriptionSecret(Base):
    """Encrypted subscription token bundle (``acct_subscription_secret``).

    One-to-one with :class:`UpstreamCredential`. The three envelope-encryption
    columns mirror :class:`gozar.core.crypto.EncryptedRecord`. ``expires_at`` is the
    bundle's access-token expiry and drives the refresh/renewal window
    (Requirement 3.1).
    """

    __tablename__ = "acct_subscription_secret"

    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("acct_upstream_credential.id", ondelete="CASCADE"),
        primary_key=True,
    )
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SubscriptionSecret(account_id={self.account_id!r})"


class ApiKeySecret(Base):
    """Encrypted API key (``acct_api_key_secret``).

    One-to-one with :class:`UpstreamCredential`. Uses the same envelope-encryption
    columns as :class:`SubscriptionSecret`; an API key has no expiry, so there is no
    ``expires_at`` column.
    """

    __tablename__ = "acct_api_key_secret"

    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("acct_upstream_credential.id", ondelete="CASCADE"),
        primary_key=True,
    )
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ApiKeySecret(account_id={self.account_id!r})"


class AccountUsageLimit(Base):
    """Per-account Usage_Limit (``acct_usage_limit``).

    Column-shaped to map directly to/from
    :class:`gozar.usage.limits.UsageLimitSpec`: ``metric`` and ``window`` reuse the
    :class:`~gozar.usage.limits.LimitMetric` and
    :class:`~gozar.usage.limits.LimitWindow` enums, ``limit_value`` is the threshold,
    and ``capacity`` is the denominator required when ``metric`` is ``percentage``
    (Requirements 4.1, 4.3). ``subject_kind``/``subject_id`` keep the row consistent
    with the uniform Usage_Limit model in the design; for this account-side table
    ``subject_kind`` is ``account`` and ``subject_id`` is the credential id.
    """

    __tablename__ = "acct_usage_limit"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    # ``account`` for this account-side table; kept for parity with the uniform
    # Usage_Limit model so a row can be mapped to the shared spec.
    subject_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="account"
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("acct_upstream_credential.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    metric: Mapped[LimitMetric] = mapped_column(_text_enum(LimitMetric), nullable=False)
    limit_value: Mapped[float] = mapped_column(Numeric, nullable=False)
    capacity: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    window: Mapped[LimitWindow] = mapped_column(
        _text_enum(LimitWindow),
        nullable=False,
        default=LimitWindow.NONE,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"AccountUsageLimit(id={self.id!r}, subject_id={self.subject_id!r}, "
            f"metric={self.metric!r}, window={self.window!r})"
        )
