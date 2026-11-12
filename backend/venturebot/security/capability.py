"""Mutation Capability Engine for VentureBot.

Implements Phase B Trusted Operator security architecture per Step 90.16.5B:
- Narrowly scoped, short-lived cryptographic authorization tickets (MutationCapability).
- Authorization verification: active session, ROLE_OPERATOR, PERM_EXPERIMENT_APPROVE.
- Interactive operator passphrase re-authentication bound to the authenticated session.
- Strict payload construction binding operation, tier, parent ID, endpoint, method,
  parameters digest, and maximum budget ceiling (<= ₹200.00).
- Opaque signed ticket serialization (URL-safe canonical JSON payload + HMAC-SHA256 signature).
- Database persistence in `mutation_capabilities` table with status='ISSUED'.
- Atomic database consumption transitioning status from 'ISSUED' to 'CONSUMED' in a single
  SQL update statement, preventing race conditions, replays, and duplicate executions.
- Zero network I/O, zero external mutations, zero Meta API calls.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Final
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from venturebot.database.models import ExperimentORM, MutationCapabilityORM
from venturebot.security.auth import authenticate_operator
from venturebot.security.crypto import (
    AMOUNT_REGEX,
    HEX64_REGEX,
    MAX_TICKET_VALIDITY_WINDOW,
    canonical_json_dumps,
    canonical_json_encode,
    generate_nonce,
    generate_sha256_digest,
    sign_capability_ticket,
    validate_capability_ticket_payload,
    verify_capability_ticket,
)
from venturebot.security.rbac import (
    PERM_EXPERIMENT_APPROVE,
    ROLE_OPERATOR,
    AuthorizationError,
    SecurityError,
    check_permission,
)
from venturebot.security.session import validate_session

logger = logging.getLogger(__name__)

# Security Constants (Step 90.16.5B §5.1 & §8)
MAX_AUTHORIZED_BUDGET: Final[Decimal] = Decimal("200.00")
DEFAULT_TICKET_LIFETIME_SECONDS: Final[int] = 300  # 5 minutes
MAX_TICKET_LIFETIME_SECONDS: Final[int] = 900      # 15 minutes
CLOCK_SKEW_TOLERANCE: Final[timedelta] = timedelta(seconds=60)
DEFAULT_KEY_ID: Final[str] = "k1"


# --- Exception Hierarchy ---

class CapabilityError(SecurityError):
    """Base exception for all capability ticket operations."""


class CapabilityIssuanceError(CapabilityError, AuthorizationError):
    """Raised when capability ticket issuance is rejected."""


class CapabilitySignatureError(CapabilityError, AuthorizationError):
    """Raised when ticket signature verification fails."""


class CapabilityExpiredError(CapabilityError, AuthorizationError):
    """Raised when a ticket is expired or outside valid time window."""


class CapabilityReplayError(CapabilityError, AuthorizationError):
    """Raised when a consumed, revoked, or replayed ticket is submitted."""


class CapabilityBindingMismatchError(CapabilityError, AuthorizationError):
    """Raised when ticket bindings do not match expected operation parameters."""


class CapabilityNotFoundError(CapabilityError):
    """Raised when a ticket is not found in database resting state."""


class CapabilityInvalidError(CapabilityError):
    """Raised when ticket payload structure or parameters are malformed."""


# --- Request Container ---

@dataclass(frozen=True)
class CapabilityRequest:
    """Explicit parameters required to issue a scoped mutation capability ticket.

    All fields are validated against strict schema requirements prior to signing.
    """
    experiment_id: uuid.UUID | str
    target_tier: str
    exact_operation: str
    expected_parent_id: str
    replacement_name: str
    target_endpoint: str
    http_method: str = "POST"
    parameters_digest: str = ""
    budget_ceiling_amount: Decimal | str = Decimal("200.00")
    budget_ceiling_currency: str = "INR"
    key_id: str = DEFAULT_KEY_ID
    validity_duration_seconds: int = DEFAULT_TICKET_LIFETIME_SECONDS


# --- Helper Functions ---

def _as_utc(dt: datetime | None) -> datetime | None:
    """Normalize datetime to timezone-aware UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def get_capability_signing_key(key: bytes | None = None) -> bytes:
    """Retrieve 256-bit symmetric signing key from argument or environment.

    Args:
        key: Optional explicit secret key bytes (at least 32 bytes).

    Returns:
        Validated 256-bit key bytes.

    Raises:
        SecurityError: If key is missing, empty, or less than 32 bytes.
    """
    if key is not None:
        if not isinstance(key, bytes) or len(key) < 32:
            raise SecurityError("Capability signing key must be at least 32 bytes (256 bits).")
        return key

    env_key = os.environ.get("VENTUREBOT_SIGNING_KEY") or os.environ.get("VENTUREBOT_HMAC_KEY")
    if env_key:
        env_key_clean = env_key.strip()
        # Support 64-char hex string or raw bytes
        if len(env_key_clean) == 64 and all(c in "0123456789abcdefABCDEF" for c in env_key_clean):
            return bytes.fromhex(env_key_clean)
        key_bytes = env_key_clean.encode("utf-8")
        if len(key_bytes) >= 32:
            return key_bytes

    raise SecurityError(
        "No capability signing key configured. Set VENTUREBOT_SIGNING_KEY or supply explicit key."
    )


def serialize_signed_ticket(payload: dict[str, Any], signature: str) -> str:
    """Serialize ticket payload and signature into an opaque, tamper-evident token string.

    Format: <base64url(canonical_json(payload))>.<hex_signature>

    Args:
        payload: Canonical 16-field capability ticket payload dictionary.
        signature: 64-character HMAC-SHA256 hex string.

    Returns:
        Opaque URL-safe signed ticket string.
    """
    canonical_bytes = canonical_json_encode(payload)
    b64_payload = base64.urlsafe_b64encode(canonical_bytes).decode("ascii").rstrip("=")
    return f"{b64_payload}.{signature}"


def deserialize_signed_ticket(signed_ticket: str) -> tuple[dict[str, Any], str]:
    """Parse an opaque signed ticket string into its payload dictionary and signature.

    Args:
        signed_ticket: Opaque signed ticket string (<b64_payload>.<signature>).

    Returns:
        tuple[dict[str, Any], str]: (payload_dict, signature_hex)

    Raises:
        CapabilityInvalidError: If ticket format is malformed.
    """
    if not isinstance(signed_ticket, str) or not signed_ticket.strip():
        raise CapabilityInvalidError("Signed ticket must be a non-empty string.")

    parts = signed_ticket.strip().split(".")
    if len(parts) != 2:
        raise CapabilityInvalidError("Malformed signed ticket: expected '<payload>.<signature>'.")

    b64_payload, signature = parts[0], parts[1]

    # Decode base64url payload with padding restoration
    padding = (4 - len(b64_payload) % 4) % 4
    try:
        payload_bytes = base64.urlsafe_b64decode(b64_payload + "=" * padding)
        import json
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception as err:
        raise CapabilityInvalidError("Failed to decode signed ticket payload.") from err

    if not isinstance(payload, dict):
        raise CapabilityInvalidError("Ticket payload must decode to a dictionary.")

    return payload, signature


# --- Core Capability Engine Operations ---

def issue_capability(
    db_session: Session,
    session_token: str,
    passphrase: str,
    request: CapabilityRequest,
    *,
    signing_key: bytes | None = None,
    now: datetime | None = None,
) -> tuple[MutationCapabilityORM, str]:
    """Issue a single-use signed mutation capability ticket with interactive re-authentication.

    Enforces Step 90.16.8 Authorization and Security Requirements:
    1. Validates active session token (derive operator identity).
    2. Enforces operator.role == ROLE_OPERATOR (strictly blocks ROLE_VIEWER and ROLE_ADMIN).
    3. Enforces PERM_EXPERIMENT_APPROVE on operator role.
    4. Re-authenticates operator via master passphrase (enforcing throttling and counter updates).
    5. Confirms experiment exists in database.
    6. Enforces budget ceiling <= ₹200.00, non-negative, and valid currency.
    7. Enforces ticket validity duration <= 15 minutes (900 seconds).
    8. Generates cryptographically secure ticket ID and 128-bit random nonce.
    9. Constructs canonical 16-field payload and computes HMAC-SHA256 signature.
    10. Persists MutationCapabilityORM record with status='ISSUED'.
    11. Returns (capability_orm, opaque_signed_ticket_string).

    Args:
        db_session: Active SQLAlchemy session.
        session_token: Active authenticated session token of calling operator.
        passphrase: Plaintext master passphrase of the calling operator for re-authentication.
        request: Explicit CapabilityRequest defining the scoped mutation parameters.
        signing_key: Optional explicit 256-bit symmetric signing key bytes.
        now: Optional UTC datetime override (used in hermetic tests).

    Returns:
        tuple[MutationCapabilityORM, str]: Persisted ORM record and opaque signed ticket string.

    Raises:
        CapabilityIssuanceError: If authorization, session, or role validation fails.
        AuthenticationError: If passphrase re-authentication fails.
        CapabilityInvalidError: If request parameters or budget limits are violated.
    """
    current_time = _as_utc(now) or datetime.now(timezone.utc)
    key = get_capability_signing_key(signing_key)

    # 1. Validate Session & Derive Operator Identity
    try:
        session_record, operator = validate_session(db_session, session_token, now=current_time)
    except Exception as err:
        raise CapabilityIssuanceError(f"Session validation failed: {err}") from err

    # 2. Enforce Role: strictly ROLE_OPERATOR (ROLE_VIEWER and ROLE_ADMIN are blocked)
    if operator.role != ROLE_OPERATOR:
        raise CapabilityIssuanceError(
            f"Role {operator.role!r} is not authorized to issue mutation capabilities. "
            f"Only {ROLE_OPERATOR!r} possesses capability issuance authority."
        )

    # 3. Enforce RBAC Permission: PERM_EXPERIMENT_APPROVE
    try:
        check_permission(operator.role, PERM_EXPERIMENT_APPROVE)
    except Exception as err:
        raise CapabilityIssuanceError(f"Insufficient permissions: {err}") from err

    # 4. Interactive Re-Authentication: Verify operator passphrase with throttling
    if not isinstance(passphrase, str) or not passphrase:
        raise CapabilityIssuanceError("Operator passphrase is required for capability authorization.")

    # Re-authentication is strictly bound to the authenticated operator's username
    authenticate_operator(db_session, operator.username, passphrase, enforce_sleep=True)

    # 5. Validate Experiment Association
    exp_id_str = str(request.experiment_id)
    try:
        exp_uuid = uuid.UUID(exp_id_str)
    except (ValueError, TypeError) as err:
        raise CapabilityInvalidError(f"Invalid experiment_id {exp_id_str!r}: must be a valid UUID.") from err

    experiment = db_session.get(ExperimentORM, exp_uuid)
    if experiment is None:
        raise CapabilityInvalidError(f"Experiment {exp_id_str!r} not found in database.")

    # 6. Validate Budget Ceiling (Architecture maximum: ₹200.00)
    try:
        if isinstance(request.budget_ceiling_amount, str):
            raw_amount_str = request.budget_ceiling_amount.strip()
            amount_decimal = Decimal(raw_amount_str)
        elif isinstance(request.budget_ceiling_amount, (int, float, Decimal)):
            amount_decimal = Decimal(str(request.budget_ceiling_amount))
        else:
            raise CapabilityInvalidError(
                f"Invalid budget_ceiling_amount type: {type(request.budget_ceiling_amount).__name__}"
            )
    except (InvalidOperation, TypeError, ValueError) as err:
        raise CapabilityInvalidError(
            f"Invalid budget_ceiling_amount {request.budget_ceiling_amount!r}: must be a valid numeric amount."
        ) from err

    if amount_decimal.is_nan() or amount_decimal.is_infinite():
        raise CapabilityInvalidError("budget_ceiling_amount must be a finite number.")

    if amount_decimal <= Decimal("0.00"):
        raise CapabilityInvalidError(
            f"budget_ceiling_amount {amount_decimal} must be greater than zero."
        )

    if amount_decimal > MAX_AUTHORIZED_BUDGET:
        raise CapabilityInvalidError(
            f"budget_ceiling_amount ₹{amount_decimal:.2f} exceeds maximum authorized ceiling "
            f"of ₹{MAX_AUTHORIZED_BUDGET:.2f}."
        )

    if isinstance(request.budget_ceiling_amount, str) and not AMOUNT_REGEX.match(request.budget_ceiling_amount.strip()):
        raise CapabilityInvalidError(
            f"Malformed budget ceiling amount format: {request.budget_ceiling_amount!r}. Must match '0.00' format."
        )

    amount_str = f"{amount_decimal:.2f}"
    if not AMOUNT_REGEX.match(amount_str):
        raise CapabilityInvalidError(f"Malformed budget ceiling amount format: {amount_str!r}")

    currency = request.budget_ceiling_currency.strip().upper()
    if currency != "INR":
        raise CapabilityInvalidError(f"Unsupported budget currency {currency!r}: only 'INR' is authorized.")

    # 7. Validate Validity Duration (Max 15 minutes)
    if request.validity_duration_seconds <= 0:
        raise CapabilityInvalidError("validity_duration_seconds must be greater than zero.")

    if request.validity_duration_seconds > MAX_TICKET_LIFETIME_SECONDS:
        raise CapabilityInvalidError(
            f"validity_duration_seconds ({request.validity_duration_seconds}s) exceeds maximum allowed "
            f"ticket lifetime ({MAX_TICKET_LIFETIME_SECONDS}s)."
        )

    expires_time = current_time + timedelta(seconds=request.validity_duration_seconds)

    # 8. Parameters Digest Validation
    digest = request.parameters_digest.strip().lower()
    if not digest:
        # If empty, calculate standard empty digest
        digest = generate_sha256_digest(b"")
    elif not HEX64_REGEX.match(digest):
        raise CapabilityInvalidError(
            f"parameters_digest must be a 64-character lowercase SHA-256 hex string, got {digest!r}"
        )

    # 9. Generate Nonce and Ticket ID
    ticket_id = uuid.uuid4()
    nonce = generate_nonce(16)

    # Format RFC3339 UTC timestamps ending in 'Z'
    issued_at_str = current_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    expires_at_str = expires_time.strftime("%Y-%m-%dT%H:%M:%SZ")

    # 10. Construct Canonical Payload
    payload: dict[str, Any] = {
        "budget_ceiling_amount": amount_str,
        "budget_ceiling_currency": currency,
        "exact_operation": request.exact_operation.strip(),
        "expected_parent_id": request.expected_parent_id.strip(),
        "experiment_id": exp_id_str,
        "expires_at": expires_at_str,
        "http_method": request.http_method.strip().upper(),
        "issued_at": issued_at_str,
        "key_id": request.key_id.strip(),
        "nonce": nonce,
        "operator_id": str(operator.id),
        "parameters_digest": digest,
        "replacement_name": request.replacement_name.strip(),
        "target_endpoint": request.target_endpoint.strip(),
        "target_tier": request.target_tier.strip().upper(),
        "ticket_id": str(ticket_id),
    }

    # Validate strict schema & sign
    try:
        signature = sign_capability_ticket(key, payload)
    except Exception as err:
        raise CapabilityInvalidError(f"Failed to sign capability ticket payload: {err}") from err

    # 11. Persist MutationCapabilityORM
    capability_orm = MutationCapabilityORM(
        id=ticket_id,
        operator_id=operator.id,
        experiment_id=exp_uuid,
        tier=payload["target_tier"],
        exact_operation=payload["exact_operation"],
        budget_ceiling_amount=amount_decimal,
        budget_ceiling_currency=currency,
        expected_parent_id=payload["expected_parent_id"],
        replacement_name=payload["replacement_name"],
        target_endpoint=payload["target_endpoint"],
        http_method=payload["http_method"],
        parameters_digest=digest,
        nonce=nonce,
        key_id=payload["key_id"],
        signature=signature,
        status="ISSUED",
        issued_at=current_time,
        expires_at=expires_time,
        consumed_at=None,
    )

    try:
        db_session.add(capability_orm)
        db_session.commit()
    except SQLAlchemyError as err:
        db_session.rollback()
        logger.error("Failed to persist capability ticket %s: %s", ticket_id, err)
        raise CapabilityError("Database transaction failure during capability ticket issuance.") from err

    signed_ticket_str = serialize_signed_ticket(payload, signature)
    logger.info(
        "Mutation capability ticket %s issued for operator %s (experiment: %s, tier: %s, budget: ₹%s)",
        ticket_id,
        operator.username,
        exp_id_str,
        payload["target_tier"],
        amount_str,
    )

    return capability_orm, signed_ticket_str


def validate_capability_ticket(
    signed_ticket: str,
    *,
    signing_key: bytes | None = None,
    expected_experiment_id: uuid.UUID | str | None = None,
    expected_tier: str | None = None,
    expected_operation: str | None = None,
    expected_endpoint: str | None = None,
    expected_http_method: str | None = None,
    expected_parameters_digest: str | None = None,
    expected_max_budget: Decimal | str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate ticket signature, strict payload schema, expiration, and parameter bindings.

    Performs read-only validation without modifying database state.

    Args:
        signed_ticket: Opaque signed ticket string.
        signing_key: Optional explicit 256-bit symmetric signing key bytes.
        expected_experiment_id: Optional expected experiment ID to bind.
        expected_tier: Optional expected deployment tier to bind.
        expected_operation: Optional expected exact operation to bind.
        expected_endpoint: Optional expected endpoint to bind.
        expected_http_method: Optional expected HTTP method to bind.
        expected_parameters_digest: Optional expected request body digest to bind.
        expected_max_budget: Optional maximum allowed budget ceiling to bind.
        now: Optional current UTC datetime override.

    Returns:
        dict[str, Any]: Validated canonical payload dictionary.

    Raises:
        CapabilitySignatureError: If HMAC signature is invalid or tampered.
        CapabilityExpiredError: If ticket has expired or is issued in the future.
        CapabilityBindingMismatchError: If ticket bindings do not match expected parameters.
        CapabilityInvalidError: If schema or payload is malformed.
    """
    key = get_capability_signing_key(signing_key)
    payload, signature = deserialize_signed_ticket(signed_ticket)

    # 1. Cryptographic HMAC Signature Verification
    if not verify_capability_ticket(key, payload, signature):
        raise CapabilitySignatureError(
            "Cryptographic signature verification failed: ticket payload has been altered or key mismatch."
        )

    current_time = _as_utc(now) or datetime.now(timezone.utc)

    # 2. Expiration and Clock-Skew Validation
    try:
        dt_issued = datetime.fromisoformat(payload["issued_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
        dt_expires = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception as err:
        raise CapabilityInvalidError("Failed to parse ticket ISO timestamps.") from err

    # Check clock skew: ticket issued in the future outside tolerance
    if dt_issued > current_time + CLOCK_SKEW_TOLERANCE:
        raise CapabilityExpiredError(
            f"Ticket issued_at ({payload['issued_at']}) is in the future beyond clock tolerance."
        )

    # Check expiry: current time is after expiration
    if current_time > dt_expires:
        raise CapabilityExpiredError(
            f"Capability ticket expired at {payload['expires_at']} (current time: {current_time.isoformat()})."
        )

    # Check validity window does not exceed approved maximum (15 minutes)
    if (dt_expires - dt_issued) > MAX_TICKET_VALIDITY_WINDOW:
        raise CapabilityInvalidError(
            f"Ticket validity window exceeds maximum allowable period of {MAX_TICKET_VALIDITY_WINDOW}."
        )

    # 3. Budget Ceiling Policy Check
    ticket_budget = Decimal(payload["budget_ceiling_amount"])
    if ticket_budget > MAX_AUTHORIZED_BUDGET:
        raise CapabilityInvalidError(
            f"Ticket budget ceiling ₹{ticket_budget:.2f} exceeds system maximum ₹{MAX_AUTHORIZED_BUDGET:.2f}."
        )

    # 4. Operation Binding Checks (if explicitly supplied)
    if expected_experiment_id is not None:
        if payload["experiment_id"] != str(expected_experiment_id):
            raise CapabilityBindingMismatchError(
                f"Experiment binding mismatch: ticket is for {payload['experiment_id']!r}, "
                f"expected {str(expected_experiment_id)!r}."
            )

    if expected_tier is not None:
        if payload["target_tier"] != expected_tier.strip().upper():
            raise CapabilityBindingMismatchError(
                f"Tier binding mismatch: ticket authorizes {payload['target_tier']!r}, "
                f"expected {expected_tier!r}."
            )

    if expected_operation is not None:
        if payload["exact_operation"] != expected_operation.strip():
            raise CapabilityBindingMismatchError(
                f"Operation binding mismatch: ticket authorizes {payload['exact_operation']!r}, "
                f"expected {expected_operation!r}."
            )

    if expected_endpoint is not None:
        if payload["target_endpoint"] != expected_endpoint.strip():
            raise CapabilityBindingMismatchError(
                f"Endpoint binding mismatch: ticket authorizes {payload['target_endpoint']!r}, "
                f"expected {expected_endpoint!r}."
            )

    if expected_http_method is not None:
        if payload["http_method"] != expected_http_method.strip().upper():
            raise CapabilityBindingMismatchError(
                f"HTTP method binding mismatch: ticket authorizes {payload['http_method']!r}, "
                f"expected {expected_http_method!r}."
            )

    if expected_parameters_digest is not None:
        expected_digest_clean = expected_parameters_digest.strip().lower()
        if payload["parameters_digest"] != expected_digest_clean:
            raise CapabilityBindingMismatchError(
                f"Parameters digest mismatch: ticket binds {payload['parameters_digest']!r}, "
                f"request body digest is {expected_digest_clean!r}."
            )

    if expected_max_budget is not None:
        budget_limit = Decimal(str(expected_max_budget))
        if ticket_budget > budget_limit:
            raise CapabilityBindingMismatchError(
                f"Budget mismatch: ticket authorizes ₹{ticket_budget:.2f}, "
                f"which exceeds maximum authorized limit of ₹{budget_limit:.2f}."
            )

    return payload


def consume_capability(
    db_session: Session,
    signed_ticket: str,
    *,
    signing_key: bytes | None = None,
    expected_experiment_id: uuid.UUID | str | None = None,
    expected_tier: str | None = None,
    expected_operation: str | None = None,
    expected_endpoint: str | None = None,
    expected_http_method: str | None = None,
    expected_parameters_digest: str | None = None,
    expected_max_budget: Decimal | str | None = None,
    now: datetime | None = None,
) -> MutationCapabilityORM:
    """Atomically consume a capability ticket in the database.

    Enforces Step 90.16.8 Atomic Consumption Requirements:
    1. Cryptographically validates signature, strict schema, expiry, and parameter bindings.
    2. Executes an atomic SQL UPDATE statement:
       UPDATE mutation_capabilities
       SET status = 'CONSUMED', consumed_at = :now
       WHERE id = :ticket_id AND status = 'ISSUED'
    3. If rowcount == 0, inspects database state to accurately report:
       - CapabilityNotFoundError if ticket does not exist.
       - CapabilityReplayError if ticket has already been consumed.
       - CapabilityInvalidError if ticket is in an invalid state (EXPIRED/REVOKED).
    4. Guarantees that two concurrent consumer threads cannot both succeed.
    5. Leaves valid tickets unconsumed if validation fails before database execution.

    Args:
        db_session: Active SQLAlchemy session.
        signed_ticket: Opaque signed ticket string.
        signing_key: Optional explicit 256-bit symmetric signing key bytes.
        expected_experiment_id: Optional expected experiment ID to bind.
        expected_tier: Optional expected deployment tier to bind.
        expected_operation: Optional expected exact operation to bind.
        expected_endpoint: Optional expected endpoint to bind.
        expected_http_method: Optional expected HTTP method to bind.
        expected_parameters_digest: Optional expected request body digest to bind.
        expected_max_budget: Optional maximum allowed budget ceiling to bind.
        now: Optional current UTC datetime override.

    Returns:
        MutationCapabilityORM: Durably consumed capability record.

    Raises:
        CapabilitySignatureError: On signature mismatch or payload tampering.
        CapabilityExpiredError: If ticket is expired.
        CapabilityBindingMismatchError: On parameter binding discrepancy.
        CapabilityReplayError: If ticket has already been consumed (replay prevention).
        CapabilityNotFoundError: If ticket does not exist in resting state.
        CapabilityInvalidError: On schema or state invalidity.
    """
    current_time = _as_utc(now) or datetime.now(timezone.utc)

    # 1. Cryptographic and Binding Validation (Zero DB writes if this fails)
    payload = validate_capability_ticket(
        signed_ticket,
        signing_key=signing_key,
        expected_experiment_id=expected_experiment_id,
        expected_tier=expected_tier,
        expected_operation=expected_operation,
        expected_endpoint=expected_endpoint,
        expected_http_method=expected_http_method,
        expected_parameters_digest=expected_parameters_digest,
        expected_max_budget=expected_max_budget,
        now=current_time,
    )

    ticket_id = uuid.UUID(payload["ticket_id"])

    # 2. Atomic SQL State Transition: 'ISSUED' -> 'CONSUMED'
    stmt = (
        update(MutationCapabilityORM)
        .where(
            MutationCapabilityORM.id == ticket_id,
            MutationCapabilityORM.status == "ISSUED",
        )
        .values(
            status="CONSUMED",
            consumed_at=current_time,
        )
    )

    try:
        result = db_session.execute(stmt)
        db_session.commit()
    except SQLAlchemyError as err:
        db_session.rollback()
        logger.error("Database error during atomic capability consumption for ticket %s: %s", ticket_id, err)
        raise CapabilityError("Database error during capability ticket consumption.") from err

    # 3. Evaluate Atomic Update Outcome
    if result.rowcount == 0:
        # Investigate resting record to provide precise, secure failure reason
        resting = db_session.get(MutationCapabilityORM, ticket_id)
        if resting is None:
            raise CapabilityNotFoundError(f"Capability ticket {ticket_id} does not exist in database.")
        if resting.status == "CONSUMED":
            consumed_at_str = resting.consumed_at.isoformat() if resting.consumed_at else "unknown"
            logger.warning("Replay rejected: capability ticket %s was already consumed at %s", ticket_id, consumed_at_str)
            raise CapabilityReplayError(
                f"Capability ticket {ticket_id} has already been consumed (at {consumed_at_str}). Replay rejected."
            )
        raise CapabilityInvalidError(
            f"Capability ticket {ticket_id} cannot be consumed: current status is {resting.status!r} (expected 'ISSUED')."
        )

    # Fetch updated record to return
    consumed_record = db_session.get(MutationCapabilityORM, ticket_id)
    if consumed_record is None:
        raise CapabilityNotFoundError(f"Capability ticket {ticket_id} not found after commit.")

    logger.info(
        "Capability ticket %s atomically consumed at %s for operation %s",
        ticket_id,
        current_time.isoformat(),
        payload["exact_operation"],
    )

    return consumed_record
