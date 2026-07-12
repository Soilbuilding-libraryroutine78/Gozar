"""Auth module ORM models.

Owns the ``auth_operator`` table (module prefix ``auth_`` per the convention in
:mod:`gozar.core.db`). An :class:`Operator` is a person who administers a Gozar
instance; their password is stored only as an Argon2id hash (never in plaintext),
and their :class:`~gozar.auth.rbac.Role` drives fail-closed RBAC.

This module is imported by the Alembic ``env.py`` so the table registers on
``Base.metadata`` for autogenerate/migrations.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from gozar.auth.rbac import Role
from gozar.core.db import Base


class Operator(Base):
    """An administrative operator of a Gozar deployment.

    Columns mirror the design's ``auth_operator`` table: a UUID primary key, a
    unique username, an Argon2id password hash, a role string (validated against
    :class:`~gozar.auth.rbac.Role`), and a creation timestamp in UTC.
    """

    __tablename__ = "auth_operator"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    username: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    # Argon2id encoded hash (PHC string). Never the plaintext password.
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # Stored as the string value of a Role; authorization is fail-closed, so an
    # unrecognized value grants nothing (see gozar.auth.rbac.role_has_permission).
    role: Mapped[str] = mapped_column(String(32), nullable=False, default=Role.ADMIN.value)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Operator(id={self.id!r}, username={self.username!r}, role={self.role!r})"
