"""Mutation Intent Ledger and Pre-Dispatch Safety State Machine.

Step 90.16.9 Implementation per Step 90.16.5B Security Design:
- Provides a durable, database-backed mutation intent ledger.
- Enforces atomic capability ticket consumption and intent creation in a single transaction.
- Governs pre-dispatch mutation lifecycle with a strict fail-closed state machine.
- Provides optimistic concurrency protection against stale or concurrent transitions.
- Supports startup reconciliation discovery of ambiguous/in-flight states without network calls.
- Enforces explicit manual reconciliation protocol for ambiguous outcomes.
- Strictly adheres to zero marketing spend (₹0.00) and zero remote network dispatch.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from venturebot.database.models import MutationCapabilityORM, MutationIntentLedgerORM
from venturebot.security.capability import (
    CapabilityBindingMismatchError,
    CapabilityError,
    CapabilityExpiredError,
    CapabilityInvalidError,
    CapabilityNotFoundError,
    CapabilityReplayError,
    CapabilitySignatureError,
    consume_capability,
)
from venturebot.security.rbac import AuthorizationError, SecurityError

logger = logging.getLogger(__name__)

# --- State Machine Vocabulary (Step 90.16.5B §8 & Step 90.16.9 Section 3) ---

INTENT_STATE_APPROVAL_ISSUED: Final[str] = "APPROVAL_ISSUED"
INTENT_STATE_APPROVAL_CONSUMED: Final[str] = "APPROVAL_CONSUMED"
INTENT_STATE_MUTATION_DISPATCHING: Final[str] = "MUTATION_DISPATCHING"
INTENT_STATE_MUTATION_CONFIRMED: Final[str] = "MUTATION_CONFIRMED"
INTENT_STATE_MUTATION_REJECTED: Final[str] = "MUTATION_REJECTED"
INTENT_STATE_MUTATION_OUTCOME_UNKNOWN: Final[str] = "MUTATION_OUTCOME_UNKNOWN"
INTENT_STATE_RECONCILIATION_REQUIRED: Final[str] = "RECONCILIATION_REQUIRED"

ALL_INTENT_STATES: Final[frozenset[str]] = frozenset({
    INTENT_STATE_APPROVAL_ISSUED,
    INTENT_STATE_APPROVAL_CONSUMED,
    INTENT_STATE_MUTATION_DISPATCHING,
    INTENT_STATE_MUTATION_CONFIRMED,
    INTENT_STATE_MUTATION_REJECTED,
    INTENT_STATE_MUTATION_OUTCOME_UNKNOWN,
    INTENT_STATE_RECONCILIATION_REQUIRED,
})

TERMINAL_INTENT_STATES: Final[frozenset[str]] = frozenset({
    INTENT_STATE_MUTATION_CONFIRMED,
    INTENT_STATE_MUTATION_REJECTED,
})

# States that represent in-flight, ambiguous, or unresolved mutations requiring startup reconciliation discovery
UNRESOLVED_DISCOVERY_STATES: Final[frozenset[str]] = frozenset({
    INTENT_STATE_MUTATION_DISPATCHING,
    INTENT_STATE_MUTATION_OUTCOME_UNKNOWN,
    INTENT_STATE_RECONCILIATION_REQUIRED,
})

# Explicit Transition Graph (Step 90.16.9 Section 3)
VALID_INTENT_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    INTENT_STATE_APPROVAL_ISSUED: frozenset({
        INTENT_STATE_APPROVAL_CONSUMED,
        INTENT_STATE_MUTATION_REJECTED,
    }),
    INTENT_STATE_APPROVAL_CONSUMED: frozenset({
        INTENT_STATE_MUTATION_DISPATCHING,
        INTENT_STATE_MUTATION_REJECTED,
    }),
    INTENT_STATE_MUTATION_DISPATCHING: frozenset({
        INTENT_STATE_MUTATION_CONFIRMED,
        INTENT_STATE_MUTATION_REJECTED,
        INTENT_STATE_MUTATION_OUTCOME_UNKNOWN,
        INTENT_STATE_RECONCILIATION_REQUIRED,
    }),
    INTENT_STATE_MUTATION_OUTCOME_UNKNOWN: frozenset({
        INTENT_STATE_MUTATION_CONFIRMED,
        INTENT_STATE_MUTATION_REJECTED,
        INTENT_STATE_RECONCILIATION_REQUIRED,
    }),
    INTENT_STATE_RECONCILIATION_REQUIRED: frozenset({
        INTENT_STATE_MUTATION_CONFIRMED,
        INTENT_STATE_MUTATION_REJECTED,
    }),
    INTENT_STATE_MUTATION_CONFIRMED: frozenset(),  # Terminal: no outgoing transitions permitted
    INTENT_STATE_MUTATION_REJECTED: frozenset(),   # Terminal: no outgoing transitions permitted
}


# --- Exception Hierarchy ---

class IntentError(SecurityError):
    """Base exception for all mutation intent ledger errors."""


class IntentNotFoundError(IntentError):
    """Raised when a mutation intent ledger record is not found."""


class InvalidStateTransitionError(IntentError):
    """Raised when an illegal or prohibited state machine transition is attempted."""


class IntentReplayError(IntentError, CapabilityReplayError):
    """Raised when a ticket replay attempt occurs against the intent ledger."""


class IntentConcurrentModificationError(IntentError):
    """Raised when a concurrent modification or optimistic concurrency conflict is detected."""


class IntentBindingMismatchError(IntentError, AuthorizationError):
    """Raised when parameter bindings or digest checks fail during intent registration."""


class ReconciliationRequiredError(IntentError):
    """Raised when an operation is blocked because an intent requires reconciliation."""


# --- Data Structures ---

@dataclass(frozen=True)
class IntentSummary:
    """Read-only summary of a mutation intent ledger record."""

    id: uuid.UUID
    ticket_id: uuid.UUID
    experiment_id: uuid.UUID
    tier: str
    state: str
    parameters_digest: str
    remote_resource_id: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class UnresolvedIntentDiagnostic:
    """Diagnostic details for an unresolved intent discovered at startup reconciliation."""

    intent_id: uuid.UUID
    ticket_id: uuid.UUID
    experiment_id: uuid.UUID
    tier: str
    state: str
    parameters_digest: str
    remote_resource_id: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    age_seconds: float


# --- Helper Functions ---

def _as_utc(dt: datetime | None) -> datetime | None:
    """Normalize datetime to timezone-aware UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_uuid(val: uuid.UUID | str, param_name: str = "id") -> uuid.UUID:
    """Safely parse UUID string or instance."""
    if isinstance(val, uuid.UUID):
        return val
    if not isinstance(val, str) or not val.strip():
        raise IntentError(f"Invalid {param_name}: must be a non-empty UUID string or uuid.UUID instance.")
    try:
        return uuid.UUID(val.strip())
    except ValueError as err:
        raise IntentError(f"Invalid {param_name} format {val!r}: must be a valid UUID.") from err


# --- Intent Ledger Core Operations ---

def create_mutation_intent(
    db_session: Session,
    signed_ticket: str,
    *,
    expected_parameters_digest: str | None = None,
    expected_experiment_id: uuid.UUID | str | None = None,
    expected_tier: str | None = None,
    expected_operation: str | None = None,
    expected_endpoint: str | None = None,
    expected_http_method: str | None = None,
    expected_max_budget: Decimal | str | None = None,
    signing_key: bytes | None = None,
    initial_state: str = INTENT_STATE_APPROVAL_CONSUMED,
    now: datetime | None = None,
) -> MutationIntentLedgerORM:
    """Atomically consume a capability ticket and register a durable mutation intent.

    Enforces Step 90.16.9 Transaction Atomicity and Safety Guarantees:
    1. Validates initial_state is an authorized pre-dispatch state
       ('APPROVAL_CONSUMED' or 'MUTATION_DISPATCHING').
    2. Atomically consumes capability ticket via consume_capability(auto_commit=False).
    3. Verifies no intent record already exists for the ticket ID (fail-closed replay defense).
    4. Validates parameters digest against ticket payload if expected_parameters_digest supplied.
    5. Creates MutationIntentLedgerORM record bound to the ticket, experiment, tier, and digest.
    6. Commits the transaction atomically.
    7. On ANY failure: rolls back the transaction completely, ensuring neither ticket consumption
       nor intent insertion persists partially.

    Args:
        db_session: Active SQLAlchemy session.
        signed_ticket: Opaque signed capability ticket string.
        expected_parameters_digest: Optional expected request body digest to verify.
        expected_experiment_id: Optional expected experiment ID to bind.
        expected_tier: Optional expected tier to bind.
        expected_operation: Optional expected exact operation to bind.
        expected_endpoint: Optional expected endpoint to bind.
        expected_http_method: Optional expected HTTP method to bind.
        expected_max_budget: Optional maximum allowed budget ceiling to bind.
        signing_key: Optional explicit 256-bit symmetric signing key bytes.
        initial_state: State for newly created intent (default: 'APPROVAL_CONSUMED').
        now: Optional current UTC datetime override.

    Returns:
        MutationIntentLedgerORM: Persisted intent ledger record.

    Raises:
        InvalidStateTransitionError: If initial_state is not an approved entry state.
        IntentReplayError: If an intent already exists for this ticket.
        IntentBindingMismatchError: If parameters digest does not match.
        CapabilityError: If ticket verification or consumption fails.
        IntentError: If a database transaction error occurs.
    """
    # 1. Validate Initial State
    if initial_state not in (INTENT_STATE_APPROVAL_CONSUMED, INTENT_STATE_MUTATION_DISPATCHING):
        raise InvalidStateTransitionError(
            f"Invalid initial intent state: {initial_state!r}. "
            f"Allowed entry states: '{INTENT_STATE_APPROVAL_CONSUMED}', '{INTENT_STATE_MUTATION_DISPATCHING}'."
        )

    current_time = _as_utc(now) or datetime.now(timezone.utc)

    # 2. Atomic Transaction Execution
    try:
        # Step 2a: Consume capability ticket (without commit)
        consumed_ticket = consume_capability(
            db_session,
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
            auto_commit=False,
        )

        ticket_id = consumed_ticket.id

        # Step 2b: Check for existing intent for this ticket (defense against replay)
        existing_stmt = select(MutationIntentLedgerORM).where(MutationIntentLedgerORM.ticket_id == ticket_id)
        existing_intent = db_session.execute(existing_stmt).scalar_one_or_none()
        if existing_intent is not None:
            db_session.rollback()
            raise IntentReplayError(
                f"Intent already exists for capability ticket {ticket_id} (intent id: {existing_intent.id}). "
                f"Replay rejected."
            )

        # Step 2c: Verify parameter digest binding if supplied
        if expected_parameters_digest is not None:
            digest_clean = expected_parameters_digest.strip().lower()
            if consumed_ticket.parameters_digest != digest_clean:
                db_session.rollback()
                raise IntentBindingMismatchError(
                    f"Parameters digest mismatch: ticket binds {consumed_ticket.parameters_digest!r}, "
                    f"expected {digest_clean!r}."
                )

        # Step 2d: Construct durable intent ledger record
        intent_id = uuid.uuid4()
        intent = MutationIntentLedgerORM(
            id=intent_id,
            ticket_id=ticket_id,
            experiment_id=consumed_ticket.experiment_id,
            tier=consumed_ticket.tier,
            state=initial_state,
            parameters_digest=consumed_ticket.parameters_digest,
            remote_resource_id=None,
            last_error=None,
            created_at=current_time,
            updated_at=current_time,
        )
        db_session.add(intent)

        # Step 2e: Atomic Commit
        db_session.commit()
        db_session.refresh(intent)

        logger.info(
            "Mutation intent %s registered for ticket %s (experiment: %s, tier: %s, state: %s)",
            intent_id,
            ticket_id,
            consumed_ticket.experiment_id,
            consumed_ticket.tier,
            initial_state,
        )
        return intent

    except (CapabilityError, IntentError):
        db_session.rollback()
        raise
    except IntegrityError as err:
        db_session.rollback()
        logger.error("Integrity error during intent creation for ticket: %s", err)
        raise IntentReplayError(
            "Database integrity constraint violated during intent registration. Possible concurrent replay."
        ) from err
    except SQLAlchemyError as err:
        db_session.rollback()
        logger.error("Database transaction failure during mutation intent creation: %s", err)
        raise IntentError("Database failure during mutation intent creation.") from err


def get_mutation_intent(
    db_session: Session,
    intent_id: uuid.UUID | str,
) -> MutationIntentLedgerORM:
    """Retrieve a mutation intent record by ID.

    Args:
        db_session: Active SQLAlchemy session.
        intent_id: UUID of the intent.

    Returns:
        MutationIntentLedgerORM: Resting intent record.

    Raises:
        IntentNotFoundError: If the intent does not exist.
        IntentError: If UUID parsing or query fails.
    """
    uid = _parse_uuid(intent_id, "intent_id")
    try:
        record = db_session.get(MutationIntentLedgerORM, uid)
    except SQLAlchemyError as err:
        raise IntentError(f"Database error while querying intent {uid}: {err}") from err

    if record is None:
        raise IntentNotFoundError(f"Mutation intent {uid} not found.")
    return record


def get_intent_by_ticket_id(
    db_session: Session,
    ticket_id: uuid.UUID | str,
) -> MutationIntentLedgerORM | None:
    """Retrieve a mutation intent record by capability ticket ID.

    Args:
        db_session: Active SQLAlchemy session.
        ticket_id: UUID of the capability ticket.

    Returns:
        MutationIntentLedgerORM | None: Resting intent record or None if not found.
    """
    uid = _parse_uuid(ticket_id, "ticket_id")
    stmt = select(MutationIntentLedgerORM).where(MutationIntentLedgerORM.ticket_id == uid)
    try:
        return db_session.execute(stmt).scalar_one_or_none()
    except SQLAlchemyError as err:
        raise IntentError(f"Database error while querying intent by ticket {uid}: {err}") from err


def transition_intent_state(
    db_session: Session,
    intent_id: uuid.UUID | str,
    target_state: str,
    *,
    expected_current_state: str | None = None,
    remote_resource_id: str | None = None,
    last_error: str | None = None,
    now: datetime | None = None,
) -> MutationIntentLedgerORM:
    """Execute a strict state machine transition on a mutation intent record.

    Enforces Step 90.16.9 Transition Rules:
    1. Validates target_state exists in approved state vocabulary.
    2. Validates resting record exists.
    3. Fails closed if resting record is already in a TERMINAL state.
    4. Enforces expected_current_state check if supplied (optimistic concurrency).
    5. Validates transition against VALID_INTENT_TRANSITIONS table.
    6. Executes atomic update with WHERE state = current_state condition.
    7. Fails closed if 0 rows updated (concurrent modification detected).

    Args:
        db_session: Active SQLAlchemy session.
        intent_id: UUID of intent to transition.
        target_state: Target state name.
        expected_current_state: Optional expected pre-state.
        remote_resource_id: Optional remote identifier (e.g. from Meta upon confirmation).
        last_error: Optional error description or rejection reason.
        now: Optional current UTC datetime override.

    Returns:
        MutationIntentLedgerORM: Updated intent ledger record.

    Raises:
        InvalidStateTransitionError: If transition is illegal, prohibited, or from terminal state.
        IntentNotFoundError: If intent does not exist.
        IntentConcurrentModificationError: If concurrent modification is detected.
        IntentError: On database error.
    """
    clean_target = str(target_state).strip().upper() if target_state else ""
    if clean_target not in ALL_INTENT_STATES:
        raise InvalidStateTransitionError(
            f"Invalid target state: {target_state!r}. Must be one of: {sorted(ALL_INTENT_STATES)}."
        )

    current_time = _as_utc(now) or datetime.now(timezone.utc)
    intent = get_mutation_intent(db_session, intent_id)
    current_state = intent.state

    # Check terminal state protection
    if current_state in TERMINAL_INTENT_STATES:
        raise InvalidStateTransitionError(
            f"Cannot transition intent {intent.id} from terminal state {current_state!r} to {clean_target!r}. "
            f"Terminal states are immutable."
        )

    # Check expected current state (optimistic concurrency guard)
    if expected_current_state is not None:
        expected_clean = expected_current_state.strip().upper()
        if current_state != expected_clean:
            raise IntentConcurrentModificationError(
                f"State mismatch on intent {intent.id}: expected {expected_clean!r}, "
                f"found resting state {current_state!r}."
            )

    # Validate transition against state machine graph
    allowed_transitions = VALID_INTENT_TRANSITIONS.get(current_state, frozenset())
    if clean_target not in allowed_transitions:
        raise InvalidStateTransitionError(
            f"Illegal state transition from {current_state!r} to {clean_target!r} on intent {intent.id}. "
            f"Permitted transitions from {current_state!r}: {sorted(allowed_transitions) if allowed_transitions else 'None (Terminal)'}."
        )

    # Prepare update payload
    update_values: dict[str, Any] = {
        "state": clean_target,
        "updated_at": current_time,
    }
    if remote_resource_id is not None:
        update_values["remote_resource_id"] = remote_resource_id.strip()
    if last_error is not None:
        update_values["last_error"] = last_error.strip()

    # Execute conditional update for atomic concurrency safety
    stmt = (
        update(MutationIntentLedgerORM)
        .where(
            MutationIntentLedgerORM.id == intent.id,
            MutationIntentLedgerORM.state == current_state,
        )
        .values(**update_values)
    )

    try:
        result = db_session.execute(stmt)
        if result.rowcount == 0:
            db_session.rollback()
            raise IntentConcurrentModificationError(
                f"Concurrent modification detected on intent {intent.id}. "
                f"State was modified before transition from {current_state!r} to {clean_target!r} could commit."
            )
        db_session.commit()
        db_session.refresh(intent)
    except (IntentConcurrentModificationError, InvalidStateTransitionError):
        raise
    except SQLAlchemyError as err:
        db_session.rollback()
        logger.error("Database error transitioning intent %s to %s: %s", intent.id, clean_target, err)
        raise IntentError(f"Database error during intent transition to {clean_target!r}.") from err

    logger.info(
        "Mutation intent %s transitioned from %s to %s",
        intent.id,
        current_state,
        clean_target,
    )
    return intent


# --- Convenience Transition Methods ---

def transition_to_dispatching(
    db_session: Session,
    intent_id: uuid.UUID | str,
    *,
    now: datetime | None = None,
) -> MutationIntentLedgerORM:
    """Transition an intent from 'APPROVAL_CONSUMED' to 'MUTATION_DISPATCHING'.

    Marks that the system is entering the remote dispatch window.
    """
    return transition_intent_state(
        db_session,
        intent_id,
        INTENT_STATE_MUTATION_DISPATCHING,
        expected_current_state=INTENT_STATE_APPROVAL_CONSUMED,
        now=now,
    )


def transition_to_confirmed(
    db_session: Session,
    intent_id: uuid.UUID | str,
    remote_resource_id: str,
    *,
    now: datetime | None = None,
) -> MutationIntentLedgerORM:
    """Transition an intent to terminal 'MUTATION_CONFIRMED' with remote resource identifier.

    Requires a non-empty remote_resource_id.
    """
    if not isinstance(remote_resource_id, str) or not remote_resource_id.strip():
        raise IntentError("remote_resource_id must be a non-empty string when confirming an intent.")

    return transition_intent_state(
        db_session,
        intent_id,
        INTENT_STATE_MUTATION_CONFIRMED,
        remote_resource_id=remote_resource_id.strip(),
        now=now,
    )


def transition_to_rejected(
    db_session: Session,
    intent_id: uuid.UUID | str,
    reason: str,
    *,
    now: datetime | None = None,
) -> MutationIntentLedgerORM:
    """Transition an intent to terminal 'MUTATION_REJECTED' recording the rejection reason."""
    clean_reason = reason.strip() if isinstance(reason, str) and reason.strip() else "Operation rejected"
    return transition_intent_state(
        db_session,
        intent_id,
        INTENT_STATE_MUTATION_REJECTED,
        last_error=clean_reason,
        now=now,
    )


def transition_to_outcome_unknown(
    db_session: Session,
    intent_id: uuid.UUID | str,
    error_message: str,
    *,
    now: datetime | None = None,
) -> MutationIntentLedgerORM:
    """Transition an intent to 'MUTATION_OUTCOME_UNKNOWN' upon ambiguous network outcome.

    Recorded when an HTTP response is ambiguous (timeout, connection reset, 5xx).
    Blocks further automatic mutations until reconciled.
    """
    clean_error = error_message.strip() if isinstance(error_message, str) and error_message.strip() else "Ambiguous network outcome"
    return transition_intent_state(
        db_session,
        intent_id,
        INTENT_STATE_MUTATION_OUTCOME_UNKNOWN,
        last_error=clean_error,
        now=now,
    )


def transition_to_reconciliation_required(
    db_session: Session,
    intent_id: uuid.UUID | str,
    reason: str,
    *,
    now: datetime | None = None,
) -> MutationIntentLedgerORM:
    """Transition an intent to 'RECONCILIATION_REQUIRED'.

    Explicitly halts automated execution and requires manual human review.
    """
    clean_reason = reason.strip() if isinstance(reason, str) and reason.strip() else "Reconciliation required"
    return transition_intent_state(
        db_session,
        intent_id,
        INTENT_STATE_RECONCILIATION_REQUIRED,
        last_error=clean_reason,
        now=now,
    )


def resolve_reconciliation_manual(
    db_session: Session,
    intent_id: uuid.UUID | str,
    resolution: str,
    *,
    remote_resource_id: str | None = None,
    notes: str | None = None,
    now: datetime | None = None,
) -> MutationIntentLedgerORM:
    """Execute explicit operator reconciliation on an ambiguous or unresolved intent.

    Resolution MUST be either:
    - 'MUTATION_CONFIRMED': If verified that the remote resource was created. Requires remote_resource_id.
    - 'MUTATION_REJECTED': If verified that the remote mutation never took effect or is aborted.

    Transition is only allowed from 'RECONCILIATION_REQUIRED' or 'MUTATION_OUTCOME_UNKNOWN'.

    Args:
        db_session: Active SQLAlchemy session.
        intent_id: UUID of intent to resolve.
        resolution: 'MUTATION_CONFIRMED' or 'MUTATION_REJECTED'.
        remote_resource_id: Required if resolution == 'MUTATION_CONFIRMED'.
        notes: Audit notes explaining the human reconciliation decision.
        now: Optional current UTC datetime override.

    Returns:
        MutationIntentLedgerORM: Reconciled intent record.
    """
    clean_resolution = str(resolution).strip().upper() if resolution else ""
    if clean_resolution not in (INTENT_STATE_MUTATION_CONFIRMED, INTENT_STATE_MUTATION_REJECTED):
        raise InvalidStateTransitionError(
            f"Invalid reconciliation resolution: {resolution!r}. "
            f"Manual reconciliation resolution must be either '{INTENT_STATE_MUTATION_CONFIRMED}' or '{INTENT_STATE_MUTATION_REJECTED}'."
        )

    if clean_resolution == INTENT_STATE_MUTATION_CONFIRMED:
        if not isinstance(remote_resource_id, str) or not remote_resource_id.strip():
            raise IntentError("remote_resource_id is required when confirming an intent via manual reconciliation.")

    intent = get_mutation_intent(db_session, intent_id)
    if intent.state not in (INTENT_STATE_RECONCILIATION_REQUIRED, INTENT_STATE_MUTATION_OUTCOME_UNKNOWN):
        raise InvalidStateTransitionError(
            f"Manual reconciliation cannot be performed on intent {intent.id} in state {intent.state!r}. "
            f"Reconciliation is only valid from 'RECONCILIATION_REQUIRED' or 'MUTATION_OUTCOME_UNKNOWN'."
        )

    audit_entry = f"Manual reconciliation: {clean_resolution}"
    if notes and notes.strip():
        audit_entry = f"{audit_entry} | Notes: {notes.strip()}"
    if intent.last_error:
        audit_entry = f"{intent.last_error} | {audit_entry}"

    return transition_intent_state(
        db_session,
        intent.id,
        clean_resolution,
        expected_current_state=intent.state,
        remote_resource_id=remote_resource_id.strip() if remote_resource_id else None,
        last_error=audit_entry,
        now=now,
    )


# --- Startup Reconciliation Discovery ---

def discover_unresolved_intents(
    db_session: Session,
    *,
    now: datetime | None = None,
) -> list[UnresolvedIntentDiagnostic]:
    """Scan database for unresolved or ambiguous mutation intents at system startup.

    Enforces Step 90.16.5B §6.4 & Step 90.16.9 Section 6:
    - Scans for intents in: 'MUTATION_DISPATCHING', 'MUTATION_OUTCOME_UNKNOWN', 'RECONCILIATION_REQUIRED'.
    - Computes diagnostic metadata including age in seconds.
    - Zero external network dispatch. Zero HTTP sockets opened.
    - Returns structured diagnostic objects for operator inspection and tier safety locking.

    Args:
        db_session: Active SQLAlchemy session.
        now: Optional current UTC datetime override.

    Returns:
        list[UnresolvedIntentDiagnostic]: List of unresolved intent diagnostics, ordered by created_at.
    """
    current_time = _as_utc(now) or datetime.now(timezone.utc)

    stmt = (
        select(MutationIntentLedgerORM)
        .where(MutationIntentLedgerORM.state.in_(list(UNRESOLVED_DISCOVERY_STATES)))
        .order_by(MutationIntentLedgerORM.created_at.asc())
    )

    try:
        records = db_session.execute(stmt).scalars().all()
    except SQLAlchemyError as err:
        logger.error("Failed to query unresolved mutation intents: %s", err)
        raise IntentError(f"Database error during unresolved intent discovery: {err}") from err

    diagnostics: list[UnresolvedIntentDiagnostic] = []
    for rec in records:
        rec_created_at = _as_utc(rec.created_at) or current_time
        rec_updated_at = _as_utc(rec.updated_at) or current_time
        age = (current_time - rec_created_at).total_seconds()
        diagnostics.append(
            UnresolvedIntentDiagnostic(
                intent_id=rec.id,
                ticket_id=rec.ticket_id,
                experiment_id=rec.experiment_id,
                tier=rec.tier,
                state=rec.state,
                parameters_digest=rec.parameters_digest,
                remote_resource_id=rec.remote_resource_id,
                last_error=rec.last_error,
                created_at=rec_created_at,
                updated_at=rec_updated_at,
                age_seconds=max(0.0, age),
            )
        )

    if diagnostics:
        logger.warning(
            "Startup discovery identified %d unresolved mutation intents requiring reconciliation: %s",
            len(diagnostics),
            [f"{d.intent_id} ({d.state})" for d in diagnostics],
        )
    else:
        logger.info("Startup discovery: zero unresolved mutation intents found. State is clean.")

    return diagnostics


def summarize_intent(intent: MutationIntentLedgerORM) -> IntentSummary:
    """Generate an immutable summary dataclass from a resting intent ORM record."""
    return IntentSummary(
        id=intent.id,
        ticket_id=intent.ticket_id,
        experiment_id=intent.experiment_id,
        tier=intent.tier,
        state=intent.state,
        parameters_digest=intent.parameters_digest,
        remote_resource_id=intent.remote_resource_id,
        last_error=intent.last_error,
        created_at=_as_utc(intent.created_at) or intent.created_at,
        updated_at=_as_utc(intent.updated_at) or intent.updated_at,
    )

