"""Comprehensive test suite for Mutation Intent Ledger and Pre-Dispatch Safety State Machine.

Step 90.16.9 Implementation Verification:
- Section 1: Intent Creation and Binding Verification
- Section 2: Atomic Capability Consumption and Transaction Rollback
- Section 3: Concurrency and Race Condition Mutual Exclusion
- Section 4: State Machine Transitions and Terminal Immutability
- Section 5: Ambiguous Outcome Handling and Manual Reconciliation
- Section 6: Startup Reconciliation Discovery (Read-Only)
- Section 7: Safety Mode and Zero Spend Financial Invariants
"""

from __future__ import annotations

import os
import secrets
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Generator
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from venturebot.env import is_safe_mode
from venturebot.database.connection import init_db
from venturebot.database.models import (
    Base,
    CapitalTransactionORM,
    ExperimentORM,
    MutationCapabilityORM,
    MutationIntentLedgerORM,
    OperatorORM,
    OpportunityORM,
)
from venturebot.security.auth import (
    bootstrap_initial_admin,
    create_operator,
)
from venturebot.security.capability import (
    CapabilityBindingMismatchError,
    CapabilityExpiredError,
    CapabilityInvalidError,
    CapabilityReplayError,
    CapabilityRequest,
    CapabilitySignatureError,
    consume_capability,
    issue_capability,
)
from venturebot.security.crypto import generate_sha256_digest
from venturebot.security.intent import (
    ALL_INTENT_STATES,
    INTENT_STATE_APPROVAL_CONSUMED,
    INTENT_STATE_APPROVAL_ISSUED,
    INTENT_STATE_MUTATION_CONFIRMED,
    INTENT_STATE_MUTATION_DISPATCHING,
    INTENT_STATE_MUTATION_OUTCOME_UNKNOWN,
    INTENT_STATE_MUTATION_REJECTED,
    INTENT_STATE_RECONCILIATION_REQUIRED,
    TERMINAL_INTENT_STATES,
    UNRESOLVED_DISCOVERY_STATES,
    VALID_INTENT_TRANSITIONS,
    IntentBindingMismatchError,
    IntentConcurrentModificationError,
    IntentError,
    IntentNotFoundError,
    IntentReplayError,
    IntentSummary,
    InvalidStateTransitionError,
    ReconciliationRequiredError,
    UnresolvedIntentDiagnostic,
    create_mutation_intent,
    discover_unresolved_intents,
    get_intent_by_ticket_id,
    get_mutation_intent,
    resolve_reconciliation_manual,
    summarize_intent,
    transition_intent_state,
    transition_to_confirmed,
    transition_to_dispatching,
    transition_to_outcome_unknown,
    transition_to_reconciliation_required,
    transition_to_rejected,
)
from venturebot.security.rbac import ROLE_OPERATOR
from venturebot.security.session import create_session


@pytest.fixture
def test_signing_key() -> bytes:
    """Generate an ephemeral 256-bit symmetric signing key."""
    return secrets.token_bytes(32)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """Provide an isolated, file-free in-memory SQLite database session."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture
def test_environment(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Bootstrap admin, create operator, opportunity, and experiment."""
    bootstrap_token = "valid_bootstrap_token_12345"
    monkeypatch.setenv("VENTUREBOT_BOOTSTRAP_TOKEN", bootstrap_token)

    admin = bootstrap_initial_admin(
        db_session,
        username="sec_admin",
        password="AdminPassword123!",
        bootstrap_token=bootstrap_token,
    )

    operator = create_operator(
        db_session,
        username="pilot_operator",
        password="OperatorPassword123!",
        creator_operator=admin,
        role=ROLE_OPERATOR,
    )

    opportunity = OpportunityORM(
        title="Intent Ledger Test Opportunity",
        description="Opportunity for intent testing",
        category="digital",
    )
    db_session.add(opportunity)
    db_session.flush()

    experiment = ExperimentORM(
        opportunity_id=opportunity.id,
        hypothesis="H_intent",
        objective="O_intent",
        channel="meta",
        monetization_method="direct",
        allocated_budget=Decimal("200.00"),
        max_allowed_spend=Decimal("200.00"),
        success_criteria="C_intent",
        failure_criteria="F_intent",
    )
    db_session.add(experiment)
    db_session.commit()

    _, op_token = create_session(db_session, operator)

    return {
        "admin": admin,
        "operator": operator,
        "operator_token": op_token,
        "operator_password": "OperatorPassword123!",
        "experiment": experiment,
    }


def make_sample_request(
    experiment_id: uuid.UUID,
    tier: str = "ADSET",
    op: str = "DEPLOY_ADSET_DORMANT",
    body: bytes = b'{"name": "VB-EXP-1-ADSET-v2"}',
) -> CapabilityRequest:
    """Helper to construct a valid CapabilityRequest."""
    digest = generate_sha256_digest(body)
    return CapabilityRequest(
        experiment_id=experiment_id,
        target_tier=tier,
        exact_operation=op,
        expected_parent_id="120252176243860380",
        replacement_name="VB-EXP-1-ADSET-v2",
        target_endpoint="/act_12345/adsets",
        http_method="POST",
        parameters_digest=digest,
        budget_ceiling_amount=Decimal("200.00"),
        budget_ceiling_currency="INR",
        validity_duration_seconds=300,
    )


# ==============================================================================
# SECTION 1: INTENT CREATION & BINDING VERIFICATION
# ==============================================================================

class TestMutationIntentCreation:
    """Verify durable intent creation, bindings, and initial state validation."""

    def test_create_intent_success_default_state(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """Valid ticket creates an intent in 'APPROVAL_CONSUMED' state atomically."""
        req = make_sample_request(test_environment["experiment"].id)
        cap_orm, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )

        intent = create_mutation_intent(
            db_session,
            signed_ticket,
            signing_key=test_signing_key,
        )

        assert intent is not None
        assert intent.ticket_id == cap_orm.id
        assert intent.experiment_id == test_environment["experiment"].id
        assert intent.tier == "ADSET"
        assert intent.state == INTENT_STATE_APPROVAL_CONSUMED
        assert intent.parameters_digest == req.parameters_digest
        assert intent.remote_resource_id is None
        assert intent.last_error is None

        # Verify capability ticket was consumed in DB
        db_session.refresh(cap_orm)
        assert cap_orm.status == "CONSUMED"
        assert cap_orm.consumed_at is not None

        # Verify lookup helper
        found = get_mutation_intent(db_session, intent.id)
        assert found.id == intent.id
        by_ticket = get_intent_by_ticket_id(db_session, cap_orm.id)
        assert by_ticket is not None
        assert by_ticket.id == intent.id

    def test_create_intent_with_dispatching_state(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """Intent can be created directly in 'MUTATION_DISPATCHING' state if requested."""
        req = make_sample_request(test_environment["experiment"].id)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )

        intent = create_mutation_intent(
            db_session,
            signed_ticket,
            signing_key=test_signing_key,
            initial_state=INTENT_STATE_MUTATION_DISPATCHING,
        )
        assert intent.state == INTENT_STATE_MUTATION_DISPATCHING

    def test_create_intent_rejects_invalid_initial_state(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """Creating intent in a disallowed entry state fails closed."""
        req = make_sample_request(test_environment["experiment"].id)
        cap_orm, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )

        with pytest.raises(InvalidStateTransitionError, match="Invalid initial intent state"):
            create_mutation_intent(
                db_session,
                signed_ticket,
                signing_key=test_signing_key,
                initial_state=INTENT_STATE_MUTATION_CONFIRMED,
            )

        # Ticket must remain ISSUED
        db_session.refresh(cap_orm)
        assert cap_orm.status == "ISSUED"

    def test_create_intent_verifies_parameter_digest(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """Matching expected_parameters_digest succeeds; mismatched digest fails closed."""
        req = make_sample_request(test_environment["experiment"].id)
        cap_orm, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )

        # Mismatched digest fails
        wrong_digest = "0" * 64
        with pytest.raises(CapabilityBindingMismatchError):
            create_mutation_intent(
                db_session,
                signed_ticket,
                expected_parameters_digest=wrong_digest,
                signing_key=test_signing_key,
            )

        # Ticket remains unconsumed
        db_session.refresh(cap_orm)
        assert cap_orm.status == "ISSUED"

        # Matching digest succeeds
        intent = create_mutation_intent(
            db_session,
            signed_ticket,
            expected_parameters_digest=req.parameters_digest,
            signing_key=test_signing_key,
        )
        assert intent.parameters_digest == req.parameters_digest

    def test_create_intent_verifies_parameter_bindings(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """Binding mismatches (experiment, tier, operation) reject intent creation fail-closed."""
        req = make_sample_request(test_environment["experiment"].id)
        cap_orm, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )

        # Mismatched tier
        with pytest.raises(CapabilityBindingMismatchError):
            create_mutation_intent(
                db_session,
                signed_ticket,
                expected_tier="AD",
                signing_key=test_signing_key,
            )

        # Mismatched operation
        with pytest.raises(CapabilityBindingMismatchError):
            create_mutation_intent(
                db_session,
                signed_ticket,
                expected_operation="UNAUTHORIZED_OP",
                signing_key=test_signing_key,
            )

        # Ticket remains ISSUED
        db_session.refresh(cap_orm)
        assert cap_orm.status == "ISSUED"

    def test_create_intent_rejects_forged_ticket(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """Tampered or invalid signature ticket is rejected with zero DB writes."""
        req = make_sample_request(test_environment["experiment"].id)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )

        # Tamper with signature
        payload_part, sig_part = signed_ticket.split(".")
        tampered_sig = ("0" if sig_part[0] != "0" else "1") + sig_part[1:]
        tampered_ticket = f"{payload_part}.{tampered_sig}"

        with pytest.raises(CapabilitySignatureError):
            create_mutation_intent(
                db_session,
                tampered_ticket,
                signing_key=test_signing_key,
            )

        # Verify zero intent records exist
        intents = db_session.execute(select(MutationIntentLedgerORM)).scalars().all()
        assert len(intents) == 0

    def test_create_intent_rejects_expired_ticket(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """Ticket past expiration is rejected fail-closed."""
        req = make_sample_request(test_environment["experiment"].id)
        past_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
            now=past_time,
        )

        with pytest.raises(CapabilityExpiredError):
            create_mutation_intent(
                db_session,
                signed_ticket,
                signing_key=test_signing_key,
                now=datetime.now(timezone.utc),
            )


# ==============================================================================
# SECTION 2: TRANSACTION ATOMICITY & ROLLBACK
# ==============================================================================

class TestMutationIntentAtomicity:
    """Verify single-transaction ACID semantics across capability and intent records."""

    def test_intent_insertion_failure_rolls_back_capability_consumption(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """If intent insertion fails, capability consumption is rolled back to 'ISSUED'."""
        req = make_sample_request(test_environment["experiment"].id)
        cap_orm, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        assert cap_orm.status == "ISSUED"

        # Simulate database failure during commit of intent
        with patch.object(db_session, "commit", side_effect=IntentError("Simulated commit error")):
            with pytest.raises(IntentError, match="Simulated commit error"):
                create_mutation_intent(
                    db_session,
                    signed_ticket,
                    signing_key=test_signing_key,
                )

        # Capability MUST still be ISSUED, NOT consumed!
        db_session.refresh(cap_orm)
        assert cap_orm.status == "ISSUED"
        assert cap_orm.consumed_at is None

        # Zero intent records must exist
        intents = db_session.execute(select(MutationIntentLedgerORM)).scalars().all()
        assert len(intents) == 0

    def test_replay_ticket_fails_closed(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """Attempting to consume an already-consumed ticket fails with replay rejection."""
        req = make_sample_request(test_environment["experiment"].id)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )

        # First consumption succeeds
        intent1 = create_mutation_intent(db_session, signed_ticket, signing_key=test_signing_key)
        assert intent1 is not None

        # Replay attempt fails closed
        with pytest.raises(CapabilityReplayError, match="has already been consumed"):
            create_mutation_intent(db_session, signed_ticket, signing_key=test_signing_key)

        # Exactly 1 intent record in DB
        intents = db_session.execute(select(MutationIntentLedgerORM)).scalars().all()
        assert len(intents) == 1


# ==============================================================================
# SECTION 3: CONCURRENCY & MUTUAL EXCLUSION
# ==============================================================================

class TestMutationIntentConcurrency:
    """Verify mutual exclusion across concurrent threads racing on the same ticket."""

    def test_concurrent_intent_creation_mutual_exclusion(
        self, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """Multiple threads race to create an intent from the same ticket; exactly ONE succeeds."""
        tmp_dir = tempfile.mkdtemp()
        db_path = os.path.join(tmp_dir, "test_intent_race.db")
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 30.0})
        init_db(engine)
        session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

        init_sess = session_factory()
        bootstrap_token = "valid_bootstrap_token_12345"
        os.environ["VENTUREBOT_BOOTSTRAP_TOKEN"] = bootstrap_token
        admin = bootstrap_initial_admin(init_sess, "admin", "AdminPass123!", bootstrap_token=bootstrap_token)
        op = create_operator(init_sess, "test_op", "OpPass123!", creator_operator=admin, role=ROLE_OPERATOR)
        opp = OpportunityORM(title="T", description="D", category="c")
        init_sess.add(opp)
        init_sess.flush()
        exp = ExperimentORM(
            opportunity_id=opp.id,
            hypothesis="H",
            objective="O",
            channel="meta",
            monetization_method="direct",
            allocated_budget=Decimal("200.00"),
            max_allowed_spend=Decimal("200.00"),
            success_criteria="S",
            failure_criteria="F",
        )
        init_sess.add(exp)
        init_sess.commit()
        _, op_token = create_session(init_sess, op)

        req = make_sample_request(exp.id)
        _, signed_ticket = issue_capability(
            init_sess,
            op_token,
            "OpPass123!",
            req,
            signing_key=test_signing_key,
        )
        init_sess.close()

        num_threads = 5
        barrier = threading.Barrier(num_threads)
        results: list[dict[str, Any]] = []

        def worker(thread_idx: int) -> None:
            sess = session_factory()
            try:
                barrier.wait(timeout=5.0)
                intent = create_mutation_intent(sess, signed_ticket, signing_key=test_signing_key)
                results.append({"status": "SUCCESS", "thread": thread_idx, "intent_id": intent.id})
            except (CapabilityReplayError, IntentReplayError):
                results.append({"status": "REPLAY_REJECTED", "thread": thread_idx})
            except Exception as err:
                results.append({"status": "ERROR", "error": type(err).__name__, "thread": thread_idx})
            finally:
                sess.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if r["status"] == "SUCCESS"]
        replays = [r for r in results if r["status"] == "REPLAY_REJECTED"]

        # Exactly ONE thread succeeded; all other 4 threads were rejected
        assert len(successes) == 1
        assert len(replays) == 4

        # Verify DB state: exactly 1 intent record exists
        verify_sess = session_factory()
        intents = verify_sess.execute(select(MutationIntentLedgerORM)).scalars().all()
        assert len(intents) == 1
        verify_sess.close()
        engine.dispose()

    def test_concurrent_state_transition_optimistic_conflict(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """Simulate two threads attempting state transition with stale expected_current_state."""
        req = make_sample_request(test_environment["experiment"].id)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        intent = create_mutation_intent(
            db_session,
            signed_ticket,
            signing_key=test_signing_key,
            initial_state=INTENT_STATE_MUTATION_DISPATCHING,
        )

        # First transition succeeds: DISPATCHING -> CONFIRMED
        transition_to_confirmed(db_session, intent.id, "123456789")
        assert intent.state == INTENT_STATE_MUTATION_CONFIRMED

        # Second transition attempt assuming it's still in DISPATCHING must fail
        with pytest.raises(InvalidStateTransitionError, match="Cannot transition intent .* from terminal state"):
            transition_intent_state(
                db_session,
                intent.id,
                INTENT_STATE_MUTATION_REJECTED,
                expected_current_state=INTENT_STATE_MUTATION_DISPATCHING,
            )


# ==============================================================================
# SECTION 4: STATE MACHINE TRANSITIONS & TERMINAL IMMUTABILITY
# ==============================================================================

class TestMutationIntentStateTransitions:
    """Verify strict adherence to approved state transition graph."""

    def test_full_successful_lifecycle(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """APPROVAL_CONSUMED -> MUTATION_DISPATCHING -> MUTATION_CONFIRMED (terminal)."""
        req = make_sample_request(test_environment["experiment"].id)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        intent = create_mutation_intent(db_session, signed_ticket, signing_key=test_signing_key)
        assert intent.state == INTENT_STATE_APPROVAL_CONSUMED

        # 1. Transition to dispatching
        t_dispatch = transition_to_dispatching(db_session, intent.id)
        assert t_dispatch.state == INTENT_STATE_MUTATION_DISPATCHING

        # 2. Transition to confirmed
        t_confirm = transition_to_confirmed(db_session, intent.id, "remote_12345")
        assert t_confirm.state == INTENT_STATE_MUTATION_CONFIRMED
        assert t_confirm.remote_resource_id == "remote_12345"

        # 3. Terminal state cannot transition anywhere
        with pytest.raises(InvalidStateTransitionError, match="terminal state"):
            transition_intent_state(db_session, intent.id, INTENT_STATE_MUTATION_DISPATCHING)

    def test_rejection_from_approval_consumed(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """APPROVAL_CONSUMED -> MUTATION_REJECTED (terminal)."""
        req = make_sample_request(test_environment["experiment"].id)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        intent = create_mutation_intent(db_session, signed_ticket, signing_key=test_signing_key)

        rej = transition_to_rejected(db_session, intent.id, "Pre-dispatch safety check failed")
        assert rej.state == INTENT_STATE_MUTATION_REJECTED
        assert rej.last_error == "Pre-dispatch safety check failed"

        # Terminal immutability
        with pytest.raises(InvalidStateTransitionError, match="terminal state"):
            transition_to_dispatching(db_session, intent.id)

    def test_rejection_from_dispatching(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """MUTATION_DISPATCHING -> MUTATION_REJECTED (terminal)."""
        req = make_sample_request(test_environment["experiment"].id)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        intent = create_mutation_intent(
            db_session,
            signed_ticket,
            signing_key=test_signing_key,
            initial_state=INTENT_STATE_MUTATION_DISPATCHING,
        )

        rej = transition_to_rejected(db_session, intent.id, "Remote API returned 400 Bad Request")
        assert rej.state == INTENT_STATE_MUTATION_REJECTED
        assert rej.last_error == "Remote API returned 400 Bad Request"

    def test_prohibited_transitions_fail_closed(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """Prohibited transitions (direct shortcuts) are rejected fail-closed."""
        req = make_sample_request(test_environment["experiment"].id)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        intent = create_mutation_intent(db_session, signed_ticket, signing_key=test_signing_key)
        assert intent.state == INTENT_STATE_APPROVAL_CONSUMED

        # Prohibited: APPROVAL_CONSUMED -> MUTATION_CONFIRMED (cannot confirm without dispatching)
        with pytest.raises(InvalidStateTransitionError, match="Illegal state transition"):
            transition_intent_state(db_session, intent.id, INTENT_STATE_MUTATION_CONFIRMED)

        # Prohibited: APPROVAL_CONSUMED -> MUTATION_OUTCOME_UNKNOWN
        with pytest.raises(InvalidStateTransitionError, match="Illegal state transition"):
            transition_intent_state(db_session, intent.id, INTENT_STATE_MUTATION_OUTCOME_UNKNOWN)

    def test_invalid_state_name_rejected(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """Unknown state names are rejected fail-closed."""
        req = make_sample_request(test_environment["experiment"].id)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        intent = create_mutation_intent(db_session, signed_ticket, signing_key=test_signing_key)

        with pytest.raises(InvalidStateTransitionError, match="Invalid target state"):
            transition_intent_state(db_session, intent.id, "INVALID_STATE_NAME")


# ==============================================================================
# SECTION 5: AMBIGUOUS OUTCOMES & MANUAL RECONCILIATION
# ==============================================================================

class TestMutationIntentAmbiguousOutcomes:
    """Verify handling of MUTATION_OUTCOME_UNKNOWN and RECONCILIATION_REQUIRED."""

    def test_dispatching_to_outcome_unknown_and_reconciliation(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """MUTATION_DISPATCHING -> MUTATION_OUTCOME_UNKNOWN -> RECONCILIATION_REQUIRED."""
        req = make_sample_request(test_environment["experiment"].id)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        intent = create_mutation_intent(
            db_session,
            signed_ticket,
            signing_key=test_signing_key,
            initial_state=INTENT_STATE_MUTATION_DISPATCHING,
        )

        # Socket timeout during dispatch
        t_unknown = transition_to_outcome_unknown(db_session, intent.id, "Socket timeout after HTTP POST")
        assert t_unknown.state == INTENT_STATE_MUTATION_OUTCOME_UNKNOWN
        assert t_unknown.last_error == "Socket timeout after HTTP POST"

        # Reconciler flags manual reconciliation required
        t_recon = transition_to_reconciliation_required(db_session, intent.id, "Remote inspect returned 404")
        assert t_recon.state == INTENT_STATE_RECONCILIATION_REQUIRED

    def test_manual_reconciliation_resolve_confirmed(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """Operator confirms remote mutation manually after verifying adset ID on Meta."""
        req = make_sample_request(test_environment["experiment"].id)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        intent = create_mutation_intent(
            db_session,
            signed_ticket,
            signing_key=test_signing_key,
            initial_state=INTENT_STATE_MUTATION_DISPATCHING,
        )
        transition_to_reconciliation_required(db_session, intent.id, "Timeout during execution")

        # Operator manually confirms with remote ID and audit notes
        resolved = resolve_reconciliation_manual(
            db_session,
            intent.id,
            resolution=INTENT_STATE_MUTATION_CONFIRMED,
            remote_resource_id="120252176254130380",
            notes="Verified via Meta Ads Manager console that ad set was created in PAUSED state",
        )
        assert resolved.state == INTENT_STATE_MUTATION_CONFIRMED
        assert resolved.remote_resource_id == "120252176254130380"
        assert "Manual reconciliation: MUTATION_CONFIRMED" in resolved.last_error

    def test_manual_reconciliation_resolve_rejected(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """Operator rejects ambiguous mutation after verifying it never reached Meta."""
        req = make_sample_request(test_environment["experiment"].id)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        intent = create_mutation_intent(
            db_session,
            signed_ticket,
            signing_key=test_signing_key,
            initial_state=INTENT_STATE_MUTATION_DISPATCHING,
        )
        transition_to_outcome_unknown(db_session, intent.id, "Connection reset by peer")

        resolved = resolve_reconciliation_manual(
            db_session,
            intent.id,
            resolution=INTENT_STATE_MUTATION_REJECTED,
            notes="Confirmed via Meta Graph API audit log that request was never received",
        )
        assert resolved.state == INTENT_STATE_MUTATION_REJECTED
        assert "Manual reconciliation: MUTATION_REJECTED" in resolved.last_error

    def test_manual_reconciliation_requires_remote_id_for_confirmed(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """Confirming an intent without remote_resource_id fails closed."""
        req = make_sample_request(test_environment["experiment"].id)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        intent = create_mutation_intent(
            db_session,
            signed_ticket,
            signing_key=test_signing_key,
            initial_state=INTENT_STATE_MUTATION_DISPATCHING,
        )
        transition_to_reconciliation_required(db_session, intent.id, "Ambiguous outcome")

        with pytest.raises(IntentError, match="remote_resource_id is required"):
            resolve_reconciliation_manual(
                db_session,
                intent.id,
                resolution=INTENT_STATE_MUTATION_CONFIRMED,
                remote_resource_id="",
            )

    def test_manual_reconciliation_prohibited_from_clean_states(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """Attempting manual reconciliation on APPROVAL_CONSUMED fails closed."""
        req = make_sample_request(test_environment["experiment"].id)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        intent = create_mutation_intent(db_session, signed_ticket, signing_key=test_signing_key)
        assert intent.state == INTENT_STATE_APPROVAL_CONSUMED

        with pytest.raises(InvalidStateTransitionError, match="Manual reconciliation cannot be performed"):
            resolve_reconciliation_manual(
                db_session,
                intent.id,
                resolution=INTENT_STATE_MUTATION_CONFIRMED,
                remote_resource_id="123",
            )


# ==============================================================================
# SECTION 6: STARTUP RECONCILIATION DISCOVERY
# ==============================================================================

class TestStartupReconciliationDiscovery:
    """Verify discovery of in-flight and ambiguous intents at system initialization."""

    def test_discovery_empty_db_returns_empty_list(self, db_session: Session) -> None:
        """Clean database returns zero unresolved intents."""
        diagnostics = discover_unresolved_intents(db_session)
        assert diagnostics == []

    def test_discovery_ignores_terminal_and_clean_states(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """Terminal states (CONFIRMED, REJECTED) and clean state (APPROVAL_CONSUMED) are not unresolved."""
        # 1. Clean intent in APPROVAL_CONSUMED
        req1 = make_sample_request(test_environment["experiment"].id, op="OP_1")
        _, ticket1 = issue_capability(db_session, test_environment["operator_token"], test_environment["operator_password"], req1, signing_key=test_signing_key)
        create_mutation_intent(db_session, ticket1, signing_key=test_signing_key)

        # 2. Confirmed intent
        req2 = make_sample_request(test_environment["experiment"].id, op="OP_2")
        _, ticket2 = issue_capability(db_session, test_environment["operator_token"], test_environment["operator_password"], req2, signing_key=test_signing_key)
        intent2 = create_mutation_intent(db_session, ticket2, signing_key=test_signing_key, initial_state=INTENT_STATE_MUTATION_DISPATCHING)
        transition_to_confirmed(db_session, intent2.id, "res_2")

        # 3. Rejected intent
        req3 = make_sample_request(test_environment["experiment"].id, op="OP_3")
        _, ticket3 = issue_capability(db_session, test_environment["operator_token"], test_environment["operator_password"], req3, signing_key=test_signing_key)
        intent3 = create_mutation_intent(db_session, ticket3, signing_key=test_signing_key, initial_state=INTENT_STATE_MUTATION_DISPATCHING)
        transition_to_rejected(db_session, intent3.id, "Intentional rejection")

        diagnostics = discover_unresolved_intents(db_session)
        assert diagnostics == []

    def test_discovery_identifies_all_unresolved_states(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """Intents in DISPATCHING, OUTCOME_UNKNOWN, and RECONCILIATION_REQUIRED are discovered."""
        now = datetime.now(timezone.utc)

        # 1. Intent left in MUTATION_DISPATCHING (e.g. power loss during socket transmission)
        req1 = make_sample_request(test_environment["experiment"].id, op="OP_DISPATCH")
        _, ticket1 = issue_capability(db_session, test_environment["operator_token"], test_environment["operator_password"], req1, signing_key=test_signing_key)
        intent1 = create_mutation_intent(db_session, ticket1, signing_key=test_signing_key, initial_state=INTENT_STATE_MUTATION_DISPATCHING)

        # 2. Intent in MUTATION_OUTCOME_UNKNOWN
        req2 = make_sample_request(test_environment["experiment"].id, op="OP_UNKNOWN")
        _, ticket2 = issue_capability(db_session, test_environment["operator_token"], test_environment["operator_password"], req2, signing_key=test_signing_key)
        intent2 = create_mutation_intent(db_session, ticket2, signing_key=test_signing_key, initial_state=INTENT_STATE_MUTATION_DISPATCHING)
        transition_to_outcome_unknown(db_session, intent2.id, "Connection dropped")

        # 3. Intent in RECONCILIATION_REQUIRED
        req3 = make_sample_request(test_environment["experiment"].id, op="OP_RECON")
        _, ticket3 = issue_capability(db_session, test_environment["operator_token"], test_environment["operator_password"], req3, signing_key=test_signing_key)
        intent3 = create_mutation_intent(db_session, ticket3, signing_key=test_signing_key, initial_state=INTENT_STATE_MUTATION_DISPATCHING)
        transition_to_reconciliation_required(db_session, intent3.id, "Ambiguous status")

        diagnostics = discover_unresolved_intents(db_session, now=now + timedelta(seconds=120))
        assert len(diagnostics) == 3

        discovered_ids = {d.intent_id for d in diagnostics}
        assert intent1.id in discovered_ids
        assert intent2.id in discovered_ids
        assert intent3.id in discovered_ids

        # Check diagnostic metadata
        for d in diagnostics:
            assert d.age_seconds >= 100.0
            assert d.tier == "ADSET"
            assert d.experiment_id == test_environment["experiment"].id

    def test_discovery_is_read_only_with_zero_side_effects(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """Discovery does not modify database records or invoke network requests."""
        req = make_sample_request(test_environment["experiment"].id)
        _, ticket = issue_capability(db_session, test_environment["operator_token"], test_environment["operator_password"], req, signing_key=test_signing_key)
        intent = create_mutation_intent(db_session, ticket, signing_key=test_signing_key, initial_state=INTENT_STATE_MUTATION_DISPATCHING)

        diagnostics = discover_unresolved_intents(db_session)
        assert len(diagnostics) == 1

        # Verify state is completely unchanged
        db_session.refresh(intent)
        assert intent.state == INTENT_STATE_MUTATION_DISPATCHING


# ==============================================================================
# SECTION 7: SAFETY MODE & ZERO FINANCIAL SPEND INVARIANTS
# ==============================================================================

class TestSafetyAndFinancialInvariants:
    """Verify system safety boundaries and financial ledger invariants."""

    def test_safe_mode_strictly_preserved(self) -> None:
        """VENTUREBOT_SAFE_MODE=true is active and verified."""
        assert is_safe_mode() is True

    def test_zero_capital_transactions_created(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """Intent ledger operations create zero CapitalTransactionORM rows and incur zero spend."""
        req = make_sample_request(test_environment["experiment"].id)
        _, ticket = issue_capability(db_session, test_environment["operator_token"], test_environment["operator_password"], req, signing_key=test_signing_key)
        intent = create_mutation_intent(db_session, ticket, signing_key=test_signing_key)
        transition_to_dispatching(db_session, intent.id)
        transition_to_confirmed(db_session, intent.id, "12345")

        transactions = db_session.execute(select(CapitalTransactionORM)).scalars().all()
        assert len(transactions) == 0

    def test_summary_serialization(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """summarize_intent creates a valid immutable IntentSummary dataclass."""
        req = make_sample_request(test_environment["experiment"].id)
        _, ticket = issue_capability(db_session, test_environment["operator_token"], test_environment["operator_password"], req, signing_key=test_signing_key)
        intent = create_mutation_intent(db_session, ticket, signing_key=test_signing_key)

        summary = summarize_intent(intent)
        assert isinstance(summary, IntentSummary)
        assert summary.id == intent.id
        assert summary.ticket_id == intent.ticket_id
        assert summary.state == INTENT_STATE_APPROVAL_CONSUMED
