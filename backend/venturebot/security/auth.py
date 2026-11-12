"""Operator Identity Service, Secure Bootstrap, and Authentication Throttling for VentureBot.

Implements Phase B Trusted Operator authentication and remediation per Steps 90.16.7B and 90.16.7D:
- Strictly fail-closed local administrator bootstrap requiring VENTUREBOT_BOOTSTRAP_TOKEN (VERIF-90.16.7C-01).
- One-time secure local administrator bootstrap with atomic singleton database locking (SEC-90.16.7A-02).
- Universal operator creation authorization: All operator creation strictly requires an active
  creator_operator possessing PERM_SECURITY_ADMIN (SEC-90.16.7A-01).
- Correct throttle enforcement: Outstanding exponential delays are enforced prior to password
  verification, even on attempts with correct passwords (SEC-90.16.7A-03).
- Atomic SQL failed-attempt updates eliminating lost-update concurrency races (SEC-90.16.7A-04).
- PHC-compliant scrypt password hashing and constant-time verification.
- Operator lookup and active status enforcement.
- Fail-closed security boundaries on all operations.

Process Restart & Throttling Architecture:
- Throttling state (failed_attempt_count) is stored durably in the database (`operators` table).
- Process restarts do NOT clear or bypass failed attempt counters.
- Throttling is local to this single-node SQLite instance and does not provide distributed
  rate-limiting across multi-tenant distributed clusters.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from venturebot.database.models import OperatorORM, SystemBootstrapORM, VALID_OPERATOR_ROLES
from venturebot.security.crypto import generate_sha256_digest, hash_password, verify_password
from venturebot.security.rbac import (
    AccountInactiveError,
    AuthenticationError,
    AuthorizationError,
    PERM_SECURITY_ADMIN,
    ROLE_ADMIN,
    ROLE_OPERATOR,
    has_permission,
)

logger = logging.getLogger(__name__)

# Username validation: alphanumeric, underscore, dot, hyphen; 3-50 characters
USERNAME_REGEX: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_\-\.]{3,50}$")
MIN_PASSWORD_LENGTH: int = 8
MIN_BOOTSTRAP_TOKEN_LENGTH: int = 16


def calculate_throttle_delay(failed_attempts: int) -> float:
    """Calculate exponential throttling delay in seconds per Step 90.16.5A (§3.3).

    Formula: delay = min(30.0, 2 ** (attempts - 1)) if attempts >= 1 else 0.0

    Progression:
        attempts = 1 -> 1.0s
        attempts = 2 -> 2.0s
        attempts = 3 -> 4.0s
        attempts = 4 -> 8.0s
        attempts = 5 -> 16.0s
        attempts >= 6 -> 30.0s (capped)

    Args:
        failed_attempts: Count of consecutive failed authentication attempts.

    Returns:
        Delay in seconds (float between 0.0 and 30.0).
    """
    if failed_attempts <= 0:
        return 0.0
    return min(30.0, float(2 ** (failed_attempts - 1)))


def validate_operator_status(operator: OperatorORM) -> None:
    """Verify that an operator is active and enabled.

    Args:
        operator: Operator ORM record.

    Raises:
        AccountInactiveError: If the operator account is marked inactive.
    """
    if not operator.is_active:
        raise AccountInactiveError(
            f"Operator account {operator.username!r} is disabled or inactive."
        )


def get_operator_role(operator: OperatorORM) -> str:
    """Retrieve the normalized role of an operator.

    Args:
        operator: Operator ORM record.

    Returns:
        Role string (e.g. 'ROLE_OPERATOR').
    """
    return operator.role


def get_operator_by_id(db_session: Session, operator_id: uuid.UUID | str) -> OperatorORM | None:
    """Look up an operator by primary key UUID.

    Args:
        db_session: Active SQLAlchemy session.
        operator_id: UUID or UUID string.

    Returns:
        OperatorORM or None if not found or malformed input.
    """
    if isinstance(operator_id, str):
        try:
            operator_id = uuid.UUID(operator_id)
        except (ValueError, TypeError):
            return None
    elif not isinstance(operator_id, uuid.UUID):
        return None

    return db_session.get(OperatorORM, operator_id)


def get_operator_by_username(db_session: Session, username: str) -> OperatorORM | None:
    """Look up an operator by exact username.

    Args:
        db_session: Active SQLAlchemy session.
        username: Operator username string.

    Returns:
        OperatorORM or None if not found or malformed input.
    """
    if not isinstance(username, str) or not username.strip():
        return None

    stmt = select(OperatorORM).where(OperatorORM.username == username.strip())
    return db_session.execute(stmt).scalar_one_or_none()


def bootstrap_initial_admin(
    db_session: Session,
    username: str,
    password: str,
    bootstrap_token: str | None = None,
) -> OperatorORM:
    """One-time trusted local initialization of the initial administrator (Step 90.16.7D).

    Remediates finding VERIF-90.16.7C-01:
    - Enforces strictly fail-closed initial administrator bootstrap.
    - Requires VENTUREBOT_BOOTSTRAP_TOKEN to be configured and non-empty in the process environment.
    - If VENTUREBOT_BOOTSTRAP_TOKEN is missing, empty, or whitespace, raises AuthorizationError immediately.
    - Never accepts a caller-supplied token as an alternative when configuration is missing.
    - Performs constant-time comparison (hmac.compare_digest) between caller-supplied token and configured environment token.
    - Single-use database-level singleton lock (`SystemBootstrapORM` row id=1).
    - Validates username and password strength.
    - Confirms zero operators exist in the database.
    - Atomically commits the initial administrator and the singleton bootstrap record.
    - Cannot be repeated; subsequent calls raise `AuthorizationError`.
    - Rollback on failure ensures no partially privileged state is persisted.
    - Never logs or exposes either token.

    Args:
        db_session: Active SQLAlchemy session.
        username: Desired administrator username.
        password: Plaintext password string.
        bootstrap_token: High-entropy bootstrap secret token matching VENTUREBOT_BOOTSTRAP_TOKEN.

    Returns:
        Persisted initial administrator OperatorORM record.

    Raises:
        AuthorizationError: If environment token is not configured, token is invalid,
            system is already bootstrapped, or operators already exist.
        ValueError: On invalid username or password.
    """
    # 1. Check if bootstrap lock already exists in DB
    existing_lock = db_session.get(SystemBootstrapORM, 1)
    if existing_lock is not None:
        raise AuthorizationError("System is already bootstrapped. Bootstrap cannot be repeated.")

    # 2. Check if any operator already exists in DB
    any_op = db_session.execute(select(OperatorORM.id)).first()
    if any_op is not None:
        raise AuthorizationError("Operators already exist in system. Bootstrap is permanently closed.")

    # 3. Validate bootstrap token: strictly fail-closed (VERIF-90.16.7C-01 remediation)
    env_token = os.environ.get("VENTUREBOT_BOOTSTRAP_TOKEN")
    if not env_token or not env_token.strip():
        raise AuthorizationError(
            "Bootstrap is disabled: VENTUREBOT_BOOTSTRAP_TOKEN environment variable is not configured."
        )

    configured_token = env_token.strip()
    if len(configured_token) < MIN_BOOTSTRAP_TOKEN_LENGTH:
        raise AuthorizationError(
            f"Bootstrap is disabled: VENTUREBOT_BOOTSTRAP_TOKEN must be at least {MIN_BOOTSTRAP_TOKEN_LENGTH} characters."
        )

    if (
        not isinstance(bootstrap_token, str)
        or not bootstrap_token
        or not hmac.compare_digest(bootstrap_token, configured_token)
    ):
        raise AuthorizationError("Invalid or missing bootstrap token.")

    # 4. Validate username and password
    if not isinstance(username, str) or not USERNAME_REGEX.match(username):
        raise ValueError(
            f"Invalid username {username!r}. Must be 3-50 alphanumeric characters, underscores, dots, or hyphens."
        )

    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long.")

    pwd_hash = hash_password(password)
    token_hash = generate_sha256_digest(bootstrap_token)
    now = datetime.now(timezone.utc)

    admin_op = OperatorORM(
        username=username,
        password_hash=pwd_hash,
        role=ROLE_ADMIN,
        failed_attempt_count=0,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db_session.add(admin_op)

    try:
        db_session.flush()  # Populates admin_op.id

        bootstrap_lock = SystemBootstrapORM(
            id=1,
            is_bootstrapped=True,
            bootstrapped_at=now,
            admin_operator_id=admin_op.id,
            bootstrap_token_hash=token_hash,
        )
        db_session.add(bootstrap_lock)
        db_session.commit()
    except IntegrityError as err:
        db_session.rollback()
        logger.error("Bootstrap conflict or race condition detected: %s", err)
        raise AuthorizationError("Bootstrap failed: another initialization process already completed.") from err
    except SQLAlchemyError as err:
        db_session.rollback()
        logger.error("Bootstrap database error: %s", err)
        raise AuthorizationError("Bootstrap failed due to database transaction error.") from err

    logger.info("System successfully bootstrapped with initial admin %s", admin_op.username)
    return admin_op


def create_operator(
    db_session: Session,
    username: str,
    password: str,
    creator_operator: OperatorORM,
    role: str = ROLE_OPERATOR,
    is_active: bool = True,
) -> OperatorORM:
    """Create and persist a new trusted operator.

    Remediates finding SEC-90.16.7A-01:
    - Every operator creation strictly requires an active creator_operator possessing PERM_SECURITY_ADMIN.
    - Unauthenticated callers can NEVER create any operator (ROLE_VIEWER, ROLE_OPERATOR, or ROLE_ADMIN).
    - Initial administrator setup must use `bootstrap_initial_admin()`.
    - Enforces username and password strength constraints.
    - Prevents duplicate usernames.
    - Stores scrypt password hash only (never plaintext password).

    Args:
        db_session: Active SQLAlchemy session.
        username: Desired unique username.
        password: Plaintext password string.
        creator_operator: Mandatory authenticated operator record with PERM_SECURITY_ADMIN.
        role: Operator role (default 'ROLE_OPERATOR').
        is_active: Initial account status (default True).

    Returns:
        Persisted OperatorORM record.

    Raises:
        AuthorizationError: If creator_operator is missing, inactive, or lacks PERM_SECURITY_ADMIN.
        ValueError: On malformed input (empty password, invalid role, invalid username format).
        AuthenticationError: If username already exists.
    """
    # 1. Enforce Mandatory Authorization Context (Finding SEC-90.16.7A-01)
    if creator_operator is None or not isinstance(creator_operator, OperatorORM):
        raise AuthorizationError(
            "Operator creation requires an authenticated creator_operator."
        )

    if not creator_operator.is_active:
        raise AuthorizationError(
            f"Creator operator {creator_operator.username!r} is disabled or inactive."
        )

    if not has_permission(creator_operator.role, PERM_SECURITY_ADMIN):
        raise AuthorizationError(
            f"Role {creator_operator.role!r} lacks PERM_SECURITY_ADMIN required to create operators."
        )

    # 2. Validate input fields
    if not isinstance(username, str) or not USERNAME_REGEX.match(username):
        raise ValueError(
            f"Invalid username {username!r}. Must be 3-50 alphanumeric characters, underscores, dots, or hyphens."
        )

    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long.")

    if role not in VALID_OPERATOR_ROLES:
        raise ValueError(f"Invalid role {role!r}. Must be one of {sorted(VALID_OPERATOR_ROLES)}.")

    # 3. Check for existing operator with same username
    existing = get_operator_by_username(db_session, username)
    if existing is not None:
        raise AuthenticationError(f"Operator with username {username!r} already exists.")

    pwd_hash = hash_password(password)
    now = datetime.now(timezone.utc)

    operator = OperatorORM(
        username=username,
        password_hash=pwd_hash,
        role=role,
        failed_attempt_count=0,
        is_active=is_active,
        created_at=now,
        updated_at=now,
    )
    db_session.add(operator)
    db_session.commit()
    logger.info("Operator %s (%s) created by %s", username, role, creator_operator.username)
    return operator


def _increment_failed_attempts(db_session: Session, operator: OperatorORM) -> None:
    """Atomically increment failed_attempt_count in SQL to prevent lost updates (SEC-90.16.7A-04)."""
    try:
        stmt = (
            update(OperatorORM)
            .where(OperatorORM.id == operator.id)
            .values(
                failed_attempt_count=OperatorORM.failed_attempt_count + 1,
                updated_at=datetime.now(timezone.utc),
            )
        )
        db_session.execute(stmt)
        db_session.commit()
        db_session.refresh(operator)
    except Exception as err:
        db_session.rollback()
        logger.error("Failed to increment failed_attempt_count for operator %s: %s", operator.username, err)
        raise AuthenticationError("Authentication service error during credential validation.") from err


def _reset_failed_attempts(db_session: Session, operator: OperatorORM) -> None:
    """Atomically reset failed_attempt_count to zero in SQL upon successful authentication."""
    try:
        stmt = (
            update(OperatorORM)
            .where(OperatorORM.id == operator.id)
            .values(
                failed_attempt_count=0,
                updated_at=datetime.now(timezone.utc),
            )
        )
        db_session.execute(stmt)
        db_session.commit()
        db_session.refresh(operator)
    except Exception as err:
        db_session.rollback()
        logger.error("Failed to reset failed_attempt_count for operator %s: %s", operator.username, err)
        raise AuthenticationError("Authentication service error during credential validation.") from err


def verify_operator_password(operator: OperatorORM, password: str) -> bool:
    """Verify candidate password against operator's stored scrypt hash in constant time.

    Args:
        operator: Operator ORM record.
        password: Candidate password string.

    Returns:
        True if password matches, False otherwise.
    """
    if not isinstance(password, str) or not password:
        return False
    return verify_password(password, operator.password_hash)


def authenticate_operator(
    db_session: Session,
    username: str,
    password: str,
    *,
    enforce_sleep: bool = True,
) -> OperatorORM:
    """Authenticate an operator with pre-verification exponential throttling.

    Remediates findings SEC-90.16.7A-03 and SEC-90.16.7A-04:
    1. Look up operator by username.
       If not found: perform dummy password verification to eliminate timing leakage, then raise.
    2. Validate operator status (must be active).
    3. Pre-Verification Throttling: Enforce outstanding delay BEFORE password verification,
       even on attempts with correct passwords (SEC-90.16.7A-03).
    4. Malformed Password Handling: If password is empty or invalid, increment failed attempts.
    5. Password Verification:
       - On failure: Atomically increment failed_attempt_count in SQL (SEC-90.16.7A-04) and raise.
       - On success: Atomically reset failed_attempt_count to 0 only after required delay has elapsed.

    Args:
        db_session: Active SQLAlchemy session.
        username: Candidate username string.
        password: Candidate password string.
        enforce_sleep: Whether to sleep for throttling delay (set False in fast unit tests).

    Returns:
        Authenticated OperatorORM record.

    Raises:
        AuthenticationError: On credential mismatch or throttling delay.
        AccountInactiveError: If account is inactive.
    """
    if not isinstance(username, str) or not username:
        raise AuthenticationError("Username must be a non-empty string.")

    operator = get_operator_by_username(db_session, username)

    # Timing-attack mitigation: perform a dummy verify_password if user does not exist
    if operator is None:
        dummy_hash = "$scrypt$ln=14,r=8,p=1$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
        verify_password("dummy_password", dummy_hash)
        raise AuthenticationError("Invalid username or password.")

    validate_operator_status(operator)

    # Pre-Verification Throttling Enforcement (Finding SEC-90.16.7A-03):
    # Enforce any outstanding delay BEFORE verifying password, including for correct passwords.
    if operator.failed_attempt_count > 0:
        outstanding_delay = calculate_throttle_delay(operator.failed_attempt_count)
        if enforce_sleep and outstanding_delay > 0.0:
            logger.info(
                "Enforcing pre-verification throttle delay of %.1fs for operator %s (failed attempts: %d)",
                outstanding_delay,
                operator.username,
                operator.failed_attempt_count,
            )
            time.sleep(outstanding_delay)

    # Handle malformed/empty password as a failed attempt
    if not isinstance(password, str) or not password:
        _increment_failed_attempts(db_session, operator)
        new_delay = calculate_throttle_delay(operator.failed_attempt_count)
        raise AuthenticationError(
            f"Password must be a non-empty string. (Failed attempts: {operator.failed_attempt_count}, "
            f"next throttle delay: {new_delay:.1f}s)"
        )

    # Perform constant-time scrypt password verification
    is_valid = verify_operator_password(operator, password)

    if not is_valid:
        _increment_failed_attempts(db_session, operator)
        new_delay = calculate_throttle_delay(operator.failed_attempt_count)

        logger.warning(
            "Authentication failed for operator %s (attempt %d, next throttle delay: %.1fs)",
            operator.username,
            operator.failed_attempt_count,
            new_delay,
        )

        raise AuthenticationError(
            f"Invalid username or password. (Failed attempts: {operator.failed_attempt_count}, "
            f"next throttle delay: {new_delay:.1f}s)"
        )

    # Success: reset failed attempt counter atomically only after required delay has elapsed
    if operator.failed_attempt_count > 0:
        _reset_failed_attempts(db_session, operator)

    return operator
