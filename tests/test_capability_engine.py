"""Comprehensive test suite for Mutation Capability Engine and CLI Issuance (Step 90.16.8).

Covers:
- Section 3: Authorization (unauthenticated, expired session, viewer, admin, operator re-auth, passphrase transfer)
- Section 4: Ticket Integrity (valid, modified sig, modified payload, bindings, expired, excessive lifetime, nonce uniqueness)
- Section 5: Persistence and Atomic Consumption (first consume, second consume replay, concurrent race, durable state, restart)
- Section 6: CLI Issuance (safe summary, interactive confirmation, no-echo passphrase, decline handling)
- Section 7: Budget Enforcement (<= 200.00 ceiling, negative, non-numeric, currency)
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

from venturebot.database.connection import init_db
from venturebot.database.models import (
    Base,
    ExperimentORM,
    MutationCapabilityORM,
    OperatorORM,
    OpportunityORM,
)
from venturebot.security.auth import (
    authenticate_operator,
    bootstrap_initial_admin,
    create_operator,
)
from venturebot.security.capability import (
    MAX_AUTHORIZED_BUDGET,
    CapabilityBindingMismatchError,
    CapabilityError,
    CapabilityExpiredError,
    CapabilityInvalidError,
    CapabilityIssuanceError,
    CapabilityNotFoundError,
    CapabilityReplayError,
    CapabilityRequest,
    CapabilitySignatureError,
    consume_capability,
    deserialize_signed_ticket,
    get_capability_signing_key,
    issue_capability,
    serialize_signed_ticket,
    validate_capability_ticket,
)
from venturebot.security.cli import request_capability_issuance_cli
from venturebot.security.crypto import (
    generate_nonce,
    generate_sha256_digest,
    sign_capability_ticket,
)
from venturebot.security.rbac import (
    ROLE_ADMIN,
    ROLE_OPERATOR,
    ROLE_VIEWER,
    AuthenticationError,
    AuthorizationError,
)
from venturebot.security.session import create_session


@pytest.fixture
def test_signing_key() -> bytes:
    """Generate an ephemeral 256-bit symmetric signing key for hermetic test execution."""
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
    """Bootstrap admin, create viewer, operator, opportunity, and experiment."""
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

    viewer = create_operator(
        db_session,
        username="read_viewer",
        password="ViewerPassword123!",
        creator_operator=admin,
        role=ROLE_VIEWER,
    )

    opportunity = OpportunityORM(
        title="Test Opportunity",
        description="Opportunity for capability test",
        category="digital",
    )
    db_session.add(opportunity)
    db_session.flush()

    experiment = ExperimentORM(
        opportunity_id=opportunity.id,
        hypothesis="H1",
        objective="O1",
        channel="meta",
        monetization_method="direct",
        allocated_budget=Decimal("200.00"),
        max_allowed_spend=Decimal("200.00"),
        success_criteria="C1",
        failure_criteria="F1",
    )
    db_session.add(experiment)
    db_session.commit()

    # Create active sessions
    _, op_token = create_session(db_session, operator)
    _, admin_token = create_session(db_session, admin)
    _, viewer_token = create_session(db_session, viewer)

    return {
        "admin": admin,
        "operator": operator,
        "viewer": viewer,
        "experiment": experiment,
        "operator_token": op_token,
        "admin_token": admin_token,
        "viewer_token": viewer_token,
        "operator_password": "OperatorPassword123!",
    }


def make_sample_request(experiment_id: Any) -> CapabilityRequest:
    """Helper to create a standard valid CapabilityRequest."""
    return CapabilityRequest(
        experiment_id=experiment_id,
        target_tier="ADSET",
        exact_operation="DEPLOY_RECOVERY_ADSET_DORMANT",
        expected_parent_id="120252176243860380",
        replacement_name="VB-EXP-SAMPLE-ADSET-v1",
        target_endpoint="/act_12345/adsets",
        http_method="POST",
        parameters_digest=generate_sha256_digest(b'{"name":"VB-EXP-SAMPLE-ADSET-v1","status":"PAUSED"}'),
        budget_ceiling_amount=Decimal("200.00"),
        budget_ceiling_currency="INR",
        validity_duration_seconds=300,
    )


# ===========================================================================
# 1. Authorization Tests (Section 3 & Section 7)
# ===========================================================================

class TestCapabilityAuthorization:
    """Verifies that capability ticket issuance requires strict session, role, and re-auth."""

    def test_unauthenticated_issuance_rejected(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        req = make_sample_request(test_environment["experiment"].id)
        with pytest.raises(CapabilityIssuanceError, match="Session validation failed"):
            issue_capability(
                db_session,
                "invalid_or_fake_session_token",
                test_environment["operator_password"],
                req,
                signing_key=test_signing_key,
            )

    def test_expired_session_rejected(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        req = make_sample_request(test_environment["experiment"].id)
        t0 = datetime(2026, 11, 11, 10, 0, 0, tzinfo=timezone.utc)
        _, expired_token = create_session(db_session, test_environment["operator"], now=t0)
        # Advance time by 20 minutes (exceeds 15m inactivity timeout)
        t_expired = t0 + timedelta(minutes=20)
        with pytest.raises(CapabilityIssuanceError, match="Session expired due to inactivity timeout"):
            issue_capability(
                db_session,
                expired_token,
                test_environment["operator_password"],
                req,
                signing_key=test_signing_key,
                now=t_expired,
            )

    def test_viewer_issuance_rejected(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        req = make_sample_request(test_environment["experiment"].id)
        with pytest.raises(CapabilityIssuanceError, match="Only 'ROLE_OPERATOR' possesses capability issuance authority"):
            issue_capability(
                db_session,
                test_environment["viewer_token"],
                "ViewerPassword123!",
                req,
                signing_key=test_signing_key,
            )

    def test_admin_issuance_rejected(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        req = make_sample_request(test_environment["experiment"].id)
        with pytest.raises(CapabilityIssuanceError, match="Only 'ROLE_OPERATOR' possesses capability issuance authority"):
            issue_capability(
                db_session,
                test_environment["admin_token"],
                "AdminPassword123!",
                req,
                signing_key=test_signing_key,
            )

    def test_operator_issuance_succeeds_with_reauth(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        req = make_sample_request(test_environment["experiment"].id)
        cap_orm, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        assert cap_orm.id is not None
        assert cap_orm.status == "ISSUED"
        assert cap_orm.operator_id == test_environment["operator"].id
        assert cap_orm.experiment_id == test_environment["experiment"].id
        assert cap_orm.budget_ceiling_amount == Decimal("200.00")
        assert cap_orm.consumed_at is None
        assert isinstance(signed_ticket, str)
        assert "." in signed_ticket

    def test_incorrect_passphrase_rejected_and_throttled(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        req = make_sample_request(test_environment["experiment"].id)
        operator = test_environment["operator"]
        assert operator.failed_attempt_count == 0

        with pytest.raises(AuthenticationError, match="Invalid username or password"):
            issue_capability(
                db_session,
                test_environment["operator_token"],
                "WrongPassphrase123!",
                req,
                signing_key=test_signing_key,
            )

        db_session.refresh(operator)
        assert operator.failed_attempt_count == 1

    def test_reauth_cannot_be_transferred_between_operators(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """The passphrase entered must match the authenticated session operator, not another operator."""
        req = make_sample_request(test_environment["experiment"].id)
        # Operator tries to reauth using Admin's password
        with pytest.raises(AuthenticationError):
            issue_capability(
                db_session,
                test_environment["operator_token"],
                "AdminPassword123!",
                req,
                signing_key=test_signing_key,
            )

    def test_nonexistent_experiment_rejected(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        fake_exp_id = str(uuid.uuid4())
        req = make_sample_request(fake_exp_id)
        with pytest.raises(CapabilityInvalidError, match="not found in database"):
            issue_capability(
                db_session,
                test_environment["operator_token"],
                test_environment["operator_password"],
                req,
                signing_key=test_signing_key,
            )


# ===========================================================================
# 2. Ticket Integrity & Validation Tests (Section 4 & Section 7)
# ===========================================================================

class TestCapabilityTicketIntegrity:
    """Verifies cryptographic signature, payload immutability, and binding validation."""

    def test_valid_ticket_acceptance(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        req = make_sample_request(test_environment["experiment"].id)
        cap_orm, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        payload = validate_capability_ticket(
            signed_ticket,
            signing_key=test_signing_key,
            expected_experiment_id=test_environment["experiment"].id,
            expected_tier="ADSET",
            expected_operation="DEPLOY_RECOVERY_ADSET_DORMANT",
        )
        assert payload["ticket_id"] == str(cap_orm.id)
        assert payload["budget_ceiling_amount"] == "200.00"

    def test_modified_signature_rejected(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        req = make_sample_request(test_environment["experiment"].id)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        b64_payload, _ = signed_ticket.split(".")
        bad_sig_ticket = f"{b64_payload}.{'0' * 64}"

        with pytest.raises(CapabilitySignatureError, match="signature verification failed"):
            validate_capability_ticket(bad_sig_ticket, signing_key=test_signing_key)

    def test_modified_payload_rejected(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        req = make_sample_request(test_environment["experiment"].id)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        payload, sig = deserialize_signed_ticket(signed_ticket)
        # Attacker tampers budget ceiling from 200.00 to 50.00 or 999.00
        payload["budget_ceiling_amount"] = "100.00"
        tampered_ticket = serialize_signed_ticket(payload, sig)

        with pytest.raises(CapabilitySignatureError, match="signature verification failed"):
            validate_capability_ticket(tampered_ticket, signing_key=test_signing_key)

    def test_wrong_experiment_binding_rejected(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        req = make_sample_request(test_environment["experiment"].id)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        with pytest.raises(CapabilityBindingMismatchError, match="Experiment binding mismatch"):
            validate_capability_ticket(
                signed_ticket,
                signing_key=test_signing_key,
                expected_experiment_id=uuid.uuid4(),
            )

    def test_wrong_tier_binding_rejected(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        req = make_sample_request(test_environment["experiment"].id)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        with pytest.raises(CapabilityBindingMismatchError, match="Tier binding mismatch"):
            validate_capability_ticket(
                signed_ticket,
                signing_key=test_signing_key,
                expected_tier="CAMPAIGN",
            )

    def test_wrong_operation_binding_rejected(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        req = make_sample_request(test_environment["experiment"].id)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        with pytest.raises(CapabilityBindingMismatchError, match="Operation binding mismatch"):
            validate_capability_ticket(
                signed_ticket,
                signing_key=test_signing_key,
                expected_operation="UNAUTHORIZED_OPERATION",
            )

    def test_wrong_endpoint_binding_rejected(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        req = make_sample_request(test_environment["experiment"].id)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        with pytest.raises(CapabilityBindingMismatchError, match="Endpoint binding mismatch"):
            validate_capability_ticket(
                signed_ticket,
                signing_key=test_signing_key,
                expected_endpoint="/act_12345/campaigns",
            )

    def test_wrong_http_method_binding_rejected(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        req = make_sample_request(test_environment["experiment"].id)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        with pytest.raises(CapabilityBindingMismatchError, match="HTTP method binding mismatch"):
            validate_capability_ticket(
                signed_ticket,
                signing_key=test_signing_key,
                expected_http_method="DELETE",
            )

    def test_wrong_body_digest_binding_rejected(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        req = make_sample_request(test_environment["experiment"].id)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        with pytest.raises(CapabilityBindingMismatchError, match="Parameters digest mismatch"):
            validate_capability_ticket(
                signed_ticket,
                signing_key=test_signing_key,
                expected_parameters_digest="0" * 64,
            )

    def test_wrong_budget_binding_rejected(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        req = make_sample_request(test_environment["experiment"].id)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        # Ticket binds 200.00; expected caller asks for max 100.00
        with pytest.raises(CapabilityBindingMismatchError, match="Budget mismatch"):
            validate_capability_ticket(
                signed_ticket,
                signing_key=test_signing_key,
                expected_max_budget=Decimal("100.00"),
            )

    def test_expired_ticket_rejected(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        t0 = datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)
        req = make_sample_request(test_environment["experiment"].id)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
            now=t0,
        )
        # 6 minutes later (expires in 5 minutes)
        t_after = t0 + timedelta(minutes=6)
        with pytest.raises(CapabilityExpiredError, match="Capability ticket expired"):
            validate_capability_ticket(signed_ticket, signing_key=test_signing_key, now=t_after)

    def test_future_issued_ticket_outside_clock_skew_rejected(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        t_issue = datetime(2026, 11, 11, 12, 10, 0, tzinfo=timezone.utc)
        req = make_sample_request(test_environment["experiment"].id)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
            now=t_issue,
        )
        # Validating at 12:00:00 (10 minutes before issuance, outside 60s tolerance)
        t_check = datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)
        with pytest.raises(CapabilityExpiredError, match="in the future beyond clock tolerance"):
            validate_capability_ticket(signed_ticket, signing_key=test_signing_key, now=t_check)

    def test_excessive_lifetime_rejected(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """Attempting to request a lifetime > 900 seconds (15 minutes) is rejected immediately."""
        bad_req = CapabilityRequest(
            experiment_id=test_environment["experiment"].id,
            target_tier="ADSET",
            exact_operation="DEPLOY_RECOVERY_ADSET_DORMANT",
            expected_parent_id="120252176243860380",
            replacement_name="VB-EXP-TEST",
            target_endpoint="/act_12345/adsets",
            validity_duration_seconds=901,  # 15 minutes + 1 second
        )
        with pytest.raises(CapabilityInvalidError, match="exceeds maximum allowed ticket lifetime"):
            issue_capability(
                db_session,
                test_environment["operator_token"],
                test_environment["operator_password"],
                bad_req,
                signing_key=test_signing_key,
            )

    def test_nonce_uniqueness_across_issuances(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        req = make_sample_request(test_environment["experiment"].id)
        _, ticket1 = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        _, ticket2 = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        p1, _ = deserialize_signed_ticket(ticket1)
        p2, _ = deserialize_signed_ticket(ticket2)
        assert p1["ticket_id"] != p2["ticket_id"]
        assert p1["nonce"] != p2["nonce"]


# ===========================================================================
# 3. Persistence and Atomic Consumption Tests (Section 5 & Section 7)
# ===========================================================================

class TestCapabilityAtomicConsumption:
    """Verifies atomic state transitions ('ISSUED' -> 'CONSUMED') and concurrency safety."""

    def test_first_consumption_succeeds(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        req = make_sample_request(test_environment["experiment"].id)
        cap_orm, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        assert cap_orm.status == "ISSUED"

        consumed = consume_capability(db_session, signed_ticket, signing_key=test_signing_key)
        assert consumed.id == cap_orm.id
        assert consumed.status == "CONSUMED"
        assert consumed.consumed_at is not None

        # Verify durable DB state
        in_db = db_session.get(MutationCapabilityORM, cap_orm.id)
        assert in_db is not None
        assert in_db.status == "CONSUMED"

    def test_second_consumption_fails_replay(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        req = make_sample_request(test_environment["experiment"].id)
        _, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        # First consumption
        consume_capability(db_session, signed_ticket, signing_key=test_signing_key)

        # Replay attempt
        with pytest.raises(CapabilityReplayError, match="has already been consumed"):
            consume_capability(db_session, signed_ticket, signing_key=test_signing_key)

    def test_concurrent_consumers_atomic_mutual_exclusion(
        self, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """Finding SEC-90.16.5B §2: Two or more concurrent consumers race; exactly ONE succeeds."""
        import tempfile
        tmp_dir = tempfile.mkdtemp()
        db_path = os.path.join(tmp_dir, "test_atomic_consume.db")
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 30.0})
        init_db(engine)
        session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

        init_sess = session_factory()
        # Set up admin, operator, and experiment in shared DB file
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
        cap_orm, signed_ticket = issue_capability(
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
                consumed = consume_capability(sess, signed_ticket, signing_key=test_signing_key)
                results.append({"status": "SUCCESS", "thread": thread_idx})
            except CapabilityReplayError:
                results.append({"status": "REPLAY_REJECTED", "thread": thread_idx})
            except Exception as err:
                results.append({"status": "ERROR", "error": type(err).__name__})
            finally:
                sess.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if r["status"] == "SUCCESS"]
        replays = [r for r in results if r["status"] == "REPLAY_REJECTED"]

        # CRITICAL ASSERTION: Exactly one thread succeeded; all 4 others failed closed with replay rejection
        assert len(successes) == 1
        assert len(replays) == num_threads - 1

        # Check durable resting state
        verify_sess = session_factory()
        resting = verify_sess.get(MutationCapabilityORM, cap_orm.id)
        assert resting is not None
        assert resting.status == "CONSUMED"
        verify_sess.close()
        engine.dispose()

        try:
            os.remove(db_path)
            os.rmdir(tmp_dir)
        except OSError:
            pass

    def test_failed_validation_does_not_consume_valid_ticket(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """If caller passes wrong expected parameters, validation fails and ticket remains ISSUED."""
        req = make_sample_request(test_environment["experiment"].id)
        cap_orm, signed_ticket = issue_capability(
            db_session,
            test_environment["operator_token"],
            test_environment["operator_password"],
            req,
            signing_key=test_signing_key,
        )
        # Attempt consume with mismatched expected operation
        with pytest.raises(CapabilityBindingMismatchError):
            consume_capability(
                db_session,
                signed_ticket,
                signing_key=test_signing_key,
                expected_operation="DIFFERENT_OPERATION",
            )

        # Ticket must still be in 'ISSUED' state in database!
        db_session.refresh(cap_orm)
        assert cap_orm.status == "ISSUED"
        assert cap_orm.consumed_at is None

        # Clean consumption now succeeds
        consumed = consume_capability(
            db_session,
            signed_ticket,
            signing_key=test_signing_key,
            expected_operation=req.exact_operation,
        )
        assert consumed.status == "CONSUMED"

    def test_restart_preserves_consumed_status(
        self, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        """Process restart does not restore consumed capability tickets."""
        import tempfile
        tmp_dir = tempfile.mkdtemp()
        db_path = os.path.join(tmp_dir, "test_restart_consume.db")
        engine1 = create_engine(f"sqlite:///{db_path}")
        init_db(engine1)
        sf1 = sessionmaker(bind=engine1)
        s1 = sf1()

        os.environ["VENTUREBOT_BOOTSTRAP_TOKEN"] = "token123456789012"
        admin = bootstrap_initial_admin(s1, "admin", "AdminPass123!", bootstrap_token="token123456789012")
        op = create_operator(s1, "test_op", "OpPass123!", creator_operator=admin, role=ROLE_OPERATOR)
        opp = OpportunityORM(title="T", description="D", category="c")
        s1.add(opp)
        s1.flush()
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
        s1.add(exp)
        s1.commit()
        _, op_token = create_session(s1, op)

        req = make_sample_request(exp.id)
        cap_orm, signed_ticket = issue_capability(
            s1,
            op_token,
            "OpPass123!",
            req,
            signing_key=test_signing_key,
        )
        consume_capability(s1, signed_ticket, signing_key=test_signing_key)
        s1.close()
        engine1.dispose()

        # Reopen with fresh engine (simulating process restart)
        engine2 = create_engine(f"sqlite:///{db_path}")
        sf2 = sessionmaker(bind=engine2)
        s2 = sf2()

        # Replay attempt after restart must fail
        with pytest.raises(CapabilityReplayError):
            consume_capability(s2, signed_ticket, signing_key=test_signing_key)

        s2.close()
        engine2.dispose()
        try:
            os.remove(db_path)
            os.rmdir(tmp_dir)
        except OSError:
            pass


# ===========================================================================
# 4. Budget Enforcement Tests (Section 4 & Section 7)
# ===========================================================================

class TestCapabilityBudgetEnforcement:
    """Verifies that the ₹200.00 maximum authorized budget ceiling is strictly enforced."""

    def test_budget_above_200_rejected(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        for bad_amount in (Decimal("200.01"), Decimal("201.00"), Decimal("500.00"), "250.00"):
            req = CapabilityRequest(
                experiment_id=test_environment["experiment"].id,
                target_tier="ADSET",
                exact_operation="DEPLOY_RECOVERY_ADSET_DORMANT",
                expected_parent_id="120252176243860380",
                replacement_name="VB-EXP-TEST",
                target_endpoint="/act_12345/adsets",
                budget_ceiling_amount=bad_amount,
            )
            with pytest.raises(CapabilityInvalidError, match="exceeds maximum authorized ceiling of ₹200.00"):
                issue_capability(
                    db_session,
                    test_environment["operator_token"],
                    test_environment["operator_password"],
                    req,
                    signing_key=test_signing_key,
                )

    def test_negative_or_zero_budget_rejected(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        for bad_amount in (Decimal("0.00"), Decimal("-1.00"), Decimal("-200.00"), "0.00", "-50.00"):
            req = CapabilityRequest(
                experiment_id=test_environment["experiment"].id,
                target_tier="ADSET",
                exact_operation="DEPLOY_RECOVERY_ADSET_DORMANT",
                expected_parent_id="120252176243860380",
                replacement_name="VB-EXP-TEST",
                target_endpoint="/act_12345/adsets",
                budget_ceiling_amount=bad_amount,
            )
            with pytest.raises(CapabilityInvalidError, match="must be greater than zero"):
                issue_capability(
                    db_session,
                    test_environment["operator_token"],
                    test_environment["operator_password"],
                    req,
                    signing_key=test_signing_key,
                )

    def test_invalid_numeric_budget_rejected(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        for bad_val in ("invalid", "NaN", "Inf", "", "200.000"):
            req = CapabilityRequest(
                experiment_id=test_environment["experiment"].id,
                target_tier="ADSET",
                exact_operation="DEPLOY_RECOVERY_ADSET_DORMANT",
                expected_parent_id="120252176243860380",
                replacement_name="VB-EXP-TEST",
                target_endpoint="/act_12345/adsets",
                budget_ceiling_amount=bad_val,  # type: ignore
            )
            with pytest.raises(CapabilityInvalidError):
                issue_capability(
                    db_session,
                    test_environment["operator_token"],
                    test_environment["operator_password"],
                    req,
                    signing_key=test_signing_key,
                )

    def test_unsupported_currency_rejected(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        req = CapabilityRequest(
            experiment_id=test_environment["experiment"].id,
            target_tier="ADSET",
            exact_operation="DEPLOY_RECOVERY_ADSET_DORMANT",
            expected_parent_id="120252176243860380",
            replacement_name="VB-EXP-TEST",
            target_endpoint="/act_12345/adsets",
            budget_ceiling_currency="USD",
        )
        with pytest.raises(CapabilityInvalidError, match="only 'INR' is authorized"):
            issue_capability(
                db_session,
                test_environment["operator_token"],
                test_environment["operator_password"],
                req,
                signing_key=test_signing_key,
            )


# ===========================================================================
# 5. Local CLI Issuance Tests (Section 6 & Section 7)
# ===========================================================================

class TestCapabilityCLIIssuance:
    """Verifies the interactive local CLI capability issuance interface."""

    def test_cli_successful_issuance(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        req = make_sample_request(test_environment["experiment"].id)
        output_lines: list[str] = []

        def mock_getpass(prompt: str) -> str:
            return test_environment["operator_password"]

        def mock_confirm(prompt: str) -> str:
            return "y"

        def mock_output(line: str) -> None:
            output_lines.append(line)

        signed_ticket = request_capability_issuance_cli(
            db_session,
            test_environment["operator_token"],
            req,
            signing_key=test_signing_key,
            get_passphrase_fn=mock_getpass,
            confirm_fn=mock_confirm,
            output_fn=mock_output,
        )

        assert isinstance(signed_ticket, str)
        assert "." in signed_ticket

        # Verify summary output did NOT leak passwords or keys
        full_output = "\n".join(output_lines)
        assert test_environment["operator_password"] not in full_output
        assert "CAPABILITY TICKET SUCCESSFULLY ISSUED" in full_output
        assert "VENTUREBOT MUTATION CAPABILITY AUTHORIZATION REQUEST" in full_output

    def test_cli_issuance_declined_by_operator(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        req = make_sample_request(test_environment["experiment"].id)

        def mock_confirm(prompt: str) -> str:
            return "n"

        with pytest.raises(AuthorizationError, match="confirmation declined by operator"):
            request_capability_issuance_cli(
                db_session,
                test_environment["operator_token"],
                req,
                signing_key=test_signing_key,
                confirm_fn=mock_confirm,
            )

    def test_cli_empty_passphrase_aborts(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        req = make_sample_request(test_environment["experiment"].id)

        def mock_getpass(prompt: str) -> str:
            return ""

        def mock_confirm(prompt: str) -> str:
            return "yes"

        with pytest.raises(AuthorizationError, match="Passphrase cannot be empty"):
            request_capability_issuance_cli(
                db_session,
                test_environment["operator_token"],
                req,
                signing_key=test_signing_key,
                get_passphrase_fn=mock_getpass,
                confirm_fn=mock_confirm,
            )

    def test_cli_unauthorized_role_rejected(
        self, db_session: Session, test_environment: dict[str, Any], test_signing_key: bytes
    ) -> None:
        req = make_sample_request(test_environment["experiment"].id)
        with pytest.raises(AuthorizationError, match="Only 'ROLE_OPERATOR' is permitted"):
            request_capability_issuance_cli(
                db_session,
                test_environment["viewer_token"],
                req,
                signing_key=test_signing_key,
            )
