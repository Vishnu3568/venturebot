"""Operator Session Management and Lifetime Enforcement for VentureBot.

Implements Phase B Trusted Operator session management per Step 90.16.5B (§10):
- Cryptographically secure 256-bit URL-safe session token generation.
- Persistence of SHA-256 token digests only (raw session tokens are never stored in DB).
- Session fixation mitigation: previous active sessions for the operator are terminated upon login.
- Dual-TTL expiration enforcement:
  * Inactivity timeout: 15 minutes.
  * Absolute session lifetime: 8 hours.
- Timezone-aware UTC timestamp comparisons (handles SQLite tzinfo normalization).
- Fail-closed session validation and immediate revocation.
- Integrated RBAC permission enforcement.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Final
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from venturebot.database.models import OperatorORM, OperatorSessionORM
from venturebot.security.crypto import generate_secure_token, generate_sha256_digest
from venturebot.security.rbac import (
    AccountInactiveError,
    AuthorizationError,
    SessionError,
    check_permission,
)

logger = logging.getLogger(__name__)

# Approved Session Limits (Step 90.16.5A §3.2 & Step 90.16.5B §10)
SESSION_INACTIVITY_TIMEOUT: Final[timedelta] = timedelta(minutes=15)
SESSION_ABSOLUTE_LIFETIME: Final[timedelta] = timedelta(hours=8)
TOKEN_ENTROPY_BYTES: Final[int] = 32  # 256 bits


def _as_utc(dt: datetime | None) -> datetime | None:
    """Normalize a datetime to timezone-aware UTC.

    Handles SQLite driver behavior where tzinfo is stripped upon persistence.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def create_session(
    db_session: Session,
    operator: OperatorORM,
    *,
    terminate_existing: bool = True,
    now: datetime | None = None,
) -> tuple[OperatorSessionORM, str]:
    """Create and persist a new authenticated session for an operator.

    Enforces:
    - Operator active status validation.
    - Session fixation mitigation: Revokes any existing active sessions for this operator.
    - Generation of 256-bit secure URL-safe token.
    - Storage of SHA-256 token digest only; raw token is returned once and never logged.
    - Dual-TTL timestamps initialized:
      * expires_at = created_at + 8 hours
      * last_activity_at = created_at

    Args:
        db_session: Active SQLAlchemy session.
        operator: Authenticated OperatorORM record.
        terminate_existing: If True, terminates prior active sessions for this operator.
        now: Optional override for current UTC timestamp (used for testing).

    Returns:
        tuple[OperatorSessionORM, str]: (session_record, raw_session_token)

    Raises:
        AccountInactiveError: If the operator account is inactive or disabled.
    """
    if not operator.is_active:
        raise AccountInactiveError(
            f"Cannot create session for inactive operator {operator.username!r}."
        )

    current_time = _as_utc(now) or datetime.now(timezone.utc)

    # Session fixation mitigation: terminate prior active sessions
    if terminate_existing:
        revoke_all_operator_sessions(db_session, operator.id, now=current_time)

    raw_token = generate_secure_token(TOKEN_ENTROPY_BYTES)
    token_hash = generate_sha256_digest(raw_token)

    session_record = OperatorSessionORM(
        operator_id=operator.id,
        token_hash=token_hash,
        created_at=current_time,
        last_activity_at=current_time,
        expires_at=current_time + SESSION_ABSOLUTE_LIFETIME,
        revoked_at=None,
    )
    db_session.add(session_record)
    db_session.commit()

    logger.info("Session %s created for operator %s", session_record.id, operator.username)
    return session_record, raw_token


def validate_session(
    db_session: Session,
    session_token: str,
    *,
    touch_activity: bool = True,
    now: datetime | None = None,
) -> tuple[OperatorSessionORM, OperatorORM]:
    """Validate a session token, enforce dual-TTL policies, and touch activity timestamp.

    Enforces:
    - Non-empty token format.
    - Exact SHA-256 token digest lookup.
    - Revocation status (revoked_at is None).
    - Absolute lifetime expiration (now <= expires_at).
    - Inactivity timeout (now - last_activity_at <= 15 minutes).
    - Active operator status.
    - Safe activity touch (updates last_activity_at = now).

    Args:
        db_session: Active SQLAlchemy session.
        session_token: Raw session token provided by client/caller.
        touch_activity: If True, updates last_activity_at upon valid inspection.
        now: Optional override for current UTC timestamp (used for testing).

    Returns:
        tuple[OperatorSessionORM, OperatorORM]: Validated session and operator records.

    Raises:
        SessionError: If token is invalid, revoked, expired, or timed out.
        AccountInactiveError: If operator account is inactive or suspended.
    """
    if not isinstance(session_token, str) or not session_token.strip():
        raise SessionError("Invalid or missing session token.")

    token_hash = generate_sha256_digest(session_token.strip())

    stmt = select(OperatorSessionORM).where(OperatorSessionORM.token_hash == token_hash)
    session_record = db_session.execute(stmt).scalar_one_or_none()

    if session_record is None:
        raise SessionError("Session not found or invalid token.")

    # 1. Check revocation
    if session_record.revoked_at is not None:
        raise SessionError("Session has been revoked.")

    current_time = _as_utc(now) or datetime.now(timezone.utc)

    # 2. Check absolute lifetime expiration (8 hours)
    expires_at = _as_utc(session_record.expires_at)
    if expires_at is None or current_time > expires_at:
        raise SessionError("Session has expired (absolute lifetime exceeded).")

    # 3. Check inactivity timeout (15 minutes)
    last_activity = _as_utc(session_record.last_activity_at)
    if last_activity is None or (current_time - last_activity) > SESSION_INACTIVITY_TIMEOUT:
        raise SessionError("Session expired due to inactivity timeout (15 minutes).")

    # 4. Check operator account status
    operator = db_session.get(OperatorORM, session_record.operator_id)
    if operator is None or not operator.is_active:
        raise AccountInactiveError("Associated operator account is inactive or not found.")

    # 5. Touch activity timestamp if requested
    if touch_activity:
        session_record.last_activity_at = current_time
        db_session.commit()

    return session_record, operator


def revoke_session(
    db_session: Session,
    session_token: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Revoke an active session by token digest.

    Args:
        db_session: Active SQLAlchemy session.
        session_token: Raw session token.
        now: Optional override for current UTC timestamp.

    Returns:
        True if session was found and revoked, False if already revoked or not found.
    """
    if not isinstance(session_token, str) or not session_token.strip():
        return False

    token_hash = generate_sha256_digest(session_token.strip())
    stmt = select(OperatorSessionORM).where(OperatorSessionORM.token_hash == token_hash)
    session_record = db_session.execute(stmt).scalar_one_or_none()

    if session_record is None or session_record.revoked_at is not None:
        return False

    current_time = _as_utc(now) or datetime.now(timezone.utc)
    session_record.revoked_at = current_time
    db_session.commit()

    logger.info("Session %s revoked", session_record.id)
    return True


def revoke_all_operator_sessions(
    db_session: Session,
    operator_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> int:
    """Revoke all active sessions for an operator.

    Args:
        db_session: Active SQLAlchemy session.
        operator_id: Operator UUID.
        now: Optional override for current UTC timestamp.

    Returns:
        Number of sessions revoked.
    """
    current_time = _as_utc(now) or datetime.now(timezone.utc)
    stmt = (
        update(OperatorSessionORM)
        .where(
            OperatorSessionORM.operator_id == operator_id,
            OperatorSessionORM.revoked_at.is_(None),
        )
        .values(revoked_at=current_time)
    )
    result = db_session.execute(stmt)
    db_session.commit()
    return result.rowcount


def require_permission(
    db_session: Session,
    session_token: str,
    required_permission: str,
    *,
    touch_activity: bool = True,
    now: datetime | None = None,
) -> tuple[OperatorSessionORM, OperatorORM]:
    """Validate session and enforce that the operator possesses a required permission.

    Args:
        db_session: Active SQLAlchemy session.
        session_token: Raw session token.
        required_permission: Required permission string.
        touch_activity: Whether to touch session activity timestamp.
        now: Optional current UTC timestamp.

    Returns:
        tuple[OperatorSessionORM, OperatorORM]: Validated session and operator records.

    Raises:
        SessionError: If session is invalid, expired, or revoked.
        AuthorizationError: If operator role lacks the required permission.
    """
    session_record, operator = validate_session(
        db_session,
        session_token,
        touch_activity=touch_activity,
        now=now,
    )
    check_permission(operator.role, required_permission)
    return session_record, operator

