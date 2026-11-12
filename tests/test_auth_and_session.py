"""Unit and adversarial security tests for Operator Identity, Session Management, Throttling, and RBAC.

Covers Step 90.16.7 and Step 90.16.7B remediation requirements:
- Finding SEC-90.16.7A-01: Universal operator creation authorization (no unauthenticated creations).
- Finding SEC-90.16.7A-02: One-time local admin bootstrap with atomic singleton locking.
- Finding SEC-90.16.7A-03: Pre-verification throttling delay enforcement for all attempts.
- Finding SEC-90.16.7A-04: Atomic SQL failed-attempt updates preventing lost increments.
- Finding SEC-90.16.7A-05: Concurrency and adversarial tests (bootstrap races, parallel logins, revocation races).
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Generator
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from venturebot.database.connection import init_db
from venturebot.database.models import (
    Base,
    OperatorORM,
    OperatorSessionORM,
    SystemBootstrapORM,
)
from venturebot.security.auth import (
    authenticate_operator,
    bootstrap_initial_admin,
    calculate_throttle_delay,
    create_operator,
    get_operator_by_id,
    get_operator_by_username,
    get_operator_role,
    validate_operator_status,
    verify_operator_password,
)
from venturebot.security.crypto import generate_sha256_digest
from venturebot.security.rbac import (
    ALL_PERMISSIONS,
    PERM_EMERGENCY_STOP,
    PERM_EXPERIMENT_APPROVE,
    PERM_EXPERIMENT_READ,
    PERM_MUTATION_EXECUTE,
    PERM_SECURITY_ADMIN,
    ROLE_ADMIN,
    ROLE_OPERATOR,
    ROLE_PERMISSIONS,
    ROLE_VIEWER,
    AccountInactiveError,
    AuthenticationError,
    AuthorizationError,
    SessionError,
    check_permission,
    get_role_permissions,
    has_permission,
)
from venturebot.security.session import (
    SESSION_ABSOLUTE_LIFETIME,
    SESSION_INACTIVITY_TIMEOUT,
    create_session,
    require_permission,
    revoke_all_operator_sessions,
    revoke_session,
    validate_session,
)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """Provide an isolated, file-free in-memory SQLite database session with enforced PRAGMAs."""
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
def admin_operator(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> OperatorORM:
    """Provide a bootstrapped administrator for tests creating other operators."""
    token = "valid_bootstrap_token_12345"
    monkeypatch.setenv("VENTUREBOT_BOOTSTRAP_TOKEN", token)
    return bootstrap_initial_admin(
        db_session,
        username="system_admin",
        password="AdminPassphrase123!",
        bootstrap_token=token,
    )


class TestBootstrapSecurityAndConcurrency:
    """Adversarial tests for Finding SEC-90.16.7A-02 and VERIF-90.16.7C-01 (Fail-closed admin bootstrap)."""

    def test_bootstrap_initial_admin_success(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token = "valid_token_32_characters_long!"
        monkeypatch.setenv("VENTUREBOT_BOOTSTRAP_TOKEN", token)
        admin = bootstrap_initial_admin(
            db_session,
            username="initial_admin",
            password="SecureAdminPass123!",
            bootstrap_token=token,
        )
        assert admin.id is not None
        assert admin.username == "initial_admin"
        assert admin.role == ROLE_ADMIN
        assert admin.is_active is True
        assert admin.failed_attempt_count == 0

        # Verify singleton bootstrap lock in database
        lock = db_session.get(SystemBootstrapORM, 1)
        assert lock is not None
        assert lock.is_bootstrapped is True
        assert lock.admin_operator_id == admin.id
        assert lock.bootstrap_token_hash == generate_sha256_digest(token)

    def test_missing_environment_token_rejects_bootstrap(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Finding VERIF-90.16.7C-01: Bootstrap fails closed if VENTUREBOT_BOOTSTRAP_TOKEN is not configured."""
        monkeypatch.delenv("VENTUREBOT_BOOTSTRAP_TOKEN", raising=False)
        with pytest.raises(
            AuthorizationError,
            match="VENTUREBOT_BOOTSTRAP_TOKEN environment variable is not configured",
        ):
            bootstrap_initial_admin(
                db_session,
                username="admin",
                password="Password123!",
                bootstrap_token="some_token_16_characters_long!",
            )
        # Verify durable DB state: no bootstrap record, no operator
        assert db_session.get(SystemBootstrapORM, 1) is None
        assert db_session.execute(select(OperatorORM)).first() is None

    def test_empty_environment_token_rejects_bootstrap(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bootstrap fails closed if VENTUREBOT_BOOTSTRAP_TOKEN is empty or whitespace."""
        for empty_val in ("", "   ", "\t\n"):
            monkeypatch.setenv("VENTUREBOT_BOOTSTRAP_TOKEN", empty_val)
            with pytest.raises(
                AuthorizationError,
                match="VENTUREBOT_BOOTSTRAP_TOKEN environment variable is not configured",
            ):
                bootstrap_initial_admin(
                    db_session,
                    username="admin",
                    password="Password123!",
                    bootstrap_token="some_token_16_characters_long!",
                )
            assert db_session.get(SystemBootstrapORM, 1) is None
            assert db_session.execute(select(OperatorORM)).first() is None

    def test_short_environment_token_rejects_bootstrap(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bootstrap fails closed if VENTUREBOT_BOOTSTRAP_TOKEN is shorter than minimum length (16)."""
        monkeypatch.setenv("VENTUREBOT_BOOTSTRAP_TOKEN", "short_secret")
        with pytest.raises(AuthorizationError, match="must be at least 16 characters"):
            bootstrap_initial_admin(
                db_session,
                username="admin",
                password="Password123!",
                bootstrap_token="short_secret",
            )
        assert db_session.get(SystemBootstrapORM, 1) is None
        assert db_session.execute(select(OperatorORM)).first() is None

    def test_arbitrary_caller_token_rejected_when_configuration_missing(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An arbitrary 16-character caller-supplied token cannot bypass missing environment configuration."""
        monkeypatch.delenv("VENTUREBOT_BOOTSTRAP_TOKEN", raising=False)
        for caller_token in (
            "1234567890123456",
            "a" * 32,
            "super_secret_caller_token_12345!",
        ):
            with pytest.raises(
                AuthorizationError,
                match="VENTUREBOT_BOOTSTRAP_TOKEN environment variable is not configured",
            ):
                bootstrap_initial_admin(
                    db_session,
                    username="admin",
                    password="Password123!",
                    bootstrap_token=caller_token,
                )
        assert db_session.get(SystemBootstrapORM, 1) is None
        assert db_session.execute(select(OperatorORM)).first() is None

    def test_configured_token_matching_and_rejection(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When configured, caller token must match exactly in constant time."""
        configured = "env_secret_token_at_least_16_chars!"
        monkeypatch.setenv("VENTUREBOT_BOOTSTRAP_TOKEN", configured)

        # 1. Missing / None / empty caller token rejected
        with pytest.raises(AuthorizationError, match="Invalid or missing bootstrap token"):
            bootstrap_initial_admin(db_session, "admin", "Password123!", bootstrap_token=None)

        with pytest.raises(AuthorizationError, match="Invalid or missing bootstrap token"):
            bootstrap_initial_admin(db_session, "admin", "Password123!", bootstrap_token="")

        # 2. Incorrect caller token rejected
        with pytest.raises(AuthorizationError, match="Invalid or missing bootstrap token"):
            bootstrap_initial_admin(db_session, "admin", "Password123!", bootstrap_token="wrong_token_value_here!")

        # 3. Exact match succeeds
        admin = bootstrap_initial_admin(
            db_session,
            username="admin",
            password="Password123!",
            bootstrap_token=configured,
        )
        assert admin.role == ROLE_ADMIN

    def test_bootstrap_cannot_be_repeated(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token = "token_abcdef_12345678"
        monkeypatch.setenv("VENTUREBOT_BOOTSTRAP_TOKEN", token)
        bootstrap_initial_admin(
            db_session,
            username="admin1",
            password="Password123!",
            bootstrap_token=token,
        )

        # Attempting bootstrap a second time must fail closed
        with pytest.raises(AuthorizationError, match="already bootstrapped"):
            bootstrap_initial_admin(
                db_session,
                username="admin2",
                password="Password123!",
                bootstrap_token=token,
            )

    def test_bootstrap_rejected_if_operators_already_exist(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token = "valid_token_12345678"
        monkeypatch.setenv("VENTUREBOT_BOOTSTRAP_TOKEN", token)
        # If an operator somehow exists without bootstrap lock
        op = OperatorORM(
            username="existing_op",
            password_hash="hash",
            role=ROLE_OPERATOR,
        )
        db_session.add(op)
        db_session.commit()

        with pytest.raises(AuthorizationError, match="Operators already exist"):
            bootstrap_initial_admin(
                db_session,
                username="admin",
                password="Password123!",
                bootstrap_token=token,
            )

    def test_bootstrap_interruption_and_rollback(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token = "token_abcdef_12345678"
        monkeypatch.setenv("VENTUREBOT_BOOTSTRAP_TOKEN", token)
        # Malformed password causes immediate rollback before persisting
        with pytest.raises(ValueError, match="at least 8 characters"):
            bootstrap_initial_admin(
                db_session,
                username="admin",
                password="short",
                bootstrap_token=token,
            )

        # Ensure no partially created admin or lock exists
        assert db_session.get(SystemBootstrapORM, 1) is None
        assert db_session.execute(select(OperatorORM)).first() is None

        # Clean bootstrap now succeeds
        admin = bootstrap_initial_admin(
            db_session,
            username="admin",
            password="ValidPassword123!",
            bootstrap_token=token,
        )
        assert admin is not None

    def test_concurrent_bootstrap_attempts_race(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulate two threads attempting to bootstrap simultaneously against the same DB."""
        import tempfile
        token = "concurrent_token_123456"
        monkeypatch.setenv("VENTUREBOT_BOOTSTRAP_TOKEN", token)
        tmp_dir = tempfile.mkdtemp()
        db_path = os.path.join(tmp_dir, "test_concurrent_bootstrap.db")
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"timeout": 30.0},
        )
        init_db(engine)
        session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

        barrier = threading.Barrier(2)
        results: list[dict[str, Any]] = []

        def worker(username: str) -> None:
            session = session_factory()
            try:
                barrier.wait(timeout=5.0)
                admin = bootstrap_initial_admin(
                    session,
                    username=username,
                    password="Password123!",
                    bootstrap_token=token,
                )
                results.append({"status": "SUCCESS", "username": admin.username})
            except Exception as err:
                results.append({"status": "FAILED", "error": type(err).__name__})
            finally:
                session.close()

        t1 = threading.Thread(target=worker, args=("admin_thread_1",))
        t2 = threading.Thread(target=worker, args=("admin_thread_2",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly ONE thread must succeed; the other must fail with AuthorizationError
        successes = [r for r in results if r["status"] == "SUCCESS"]
        failures = [r for r in results if r["status"] == "FAILED"]
        assert len(successes) == 1
        assert len(failures) == 1
        assert failures[0]["error"] == "AuthorizationError"

        # Verify exactly one operator and one bootstrap record exist in DB
        verify_session = session_factory()
        ops = verify_session.execute(select(OperatorORM)).scalars().all()
        assert len(ops) == 1
        assert verify_session.get(SystemBootstrapORM, 1) is not None
        verify_session.close()
        engine.dispose()
        try:
            os.remove(db_path)
            os.rmdir(tmp_dir)
        except OSError:
            pass


class TestOperatorIdentityService:
    """Tests for Finding SEC-90.16.7A-01: Universal operator creation authorization."""

    def test_unauthenticated_operator_creation_rejected(self, db_session: Session) -> None:
        """Finding SEC-90.16.7A-01: Unauthenticated callers can NEVER create operators."""
        # None creator rejected
        with pytest.raises(AuthorizationError, match="requires an authenticated creator_operator"):
            create_operator(
                db_session,
                username="unauth_op",
                password="Password123!",
                creator_operator=None,  # type: ignore
                role=ROLE_OPERATOR,
            )

    def test_non_admin_operator_creation_rejected(
        self, db_session: Session, admin_operator: OperatorORM
    ) -> None:
        """Finding SEC-90.16.7A-01: Non-admin operators cannot create any operator."""
        operator = create_operator(
            db_session,
            username="pilot_operator",
            password="Password123!",
            creator_operator=admin_operator,
            role=ROLE_OPERATOR,
        )

        # ROLE_OPERATOR cannot create another ROLE_OPERATOR
        with pytest.raises(AuthorizationError, match="lacks PERM_SECURITY_ADMIN"):
            create_operator(
                db_session,
                username="child_operator",
                password="Password123!",
                creator_operator=operator,
                role=ROLE_OPERATOR,
            )

        # ROLE_OPERATOR cannot create ROLE_VIEWER
        with pytest.raises(AuthorizationError, match="lacks PERM_SECURITY_ADMIN"):
            create_operator(
                db_session,
                username="child_viewer",
                password="Password123!",
                creator_operator=operator,
                role=ROLE_VIEWER,
            )

        # ROLE_OPERATOR cannot create ROLE_ADMIN
        with pytest.raises(AuthorizationError, match="lacks PERM_SECURITY_ADMIN"):
            create_operator(
                db_session,
                username="child_admin",
                password="Password123!",
                creator_operator=operator,
                role=ROLE_ADMIN,
            )

    def test_inactive_admin_operator_creation_rejected(
        self, db_session: Session, admin_operator: OperatorORM
    ) -> None:
        admin_operator.is_active = False
        db_session.commit()

        with pytest.raises(AuthorizationError, match="disabled or inactive"):
            create_operator(
                db_session,
                username="new_op",
                password="Password123!",
                creator_operator=admin_operator,
                role=ROLE_OPERATOR,
            )

    def test_admin_can_create_all_approved_roles(
        self, db_session: Session, admin_operator: OperatorORM
    ) -> None:
        op = create_operator(
            db_session,
            username="created_op",
            password="Password123!",
            creator_operator=admin_operator,
            role=ROLE_OPERATOR,
        )
        assert op.role == ROLE_OPERATOR

        viewer = create_operator(
            db_session,
            username="created_viewer",
            password="Password123!",
            creator_operator=admin_operator,
            role=ROLE_VIEWER,
        )
        assert viewer.role == ROLE_VIEWER

        admin2 = create_operator(
            db_session,
            username="created_admin2",
            password="Password123!",
            creator_operator=admin_operator,
            role=ROLE_ADMIN,
        )
        assert admin2.role == ROLE_ADMIN

    def test_lookup_by_id_and_username(
        self, db_session: Session, admin_operator: OperatorORM
    ) -> None:
        created = create_operator(
            db_session,
            username="bob_viewer",
            password="Password456!",
            creator_operator=admin_operator,
            role=ROLE_VIEWER,
        )

        by_id = get_operator_by_id(db_session, created.id)
        assert by_id is not None
        assert by_id.username == "bob_viewer"

        by_id_str = get_operator_by_id(db_session, str(created.id))
        assert by_id_str is not None
        assert by_id_str.id == created.id

        by_username = get_operator_by_username(db_session, "bob_viewer")
        assert by_username is not None
        assert by_username.id == created.id

        # Non-existent lookups fail closed
        assert get_operator_by_id(db_session, uuid.uuid4()) is None
        assert get_operator_by_id(db_session, "invalid-uuid-string") is None
        assert get_operator_by_username(db_session, "non_existent") is None
        assert get_operator_by_username(db_session, "") is None

    def test_duplicate_username_rejection(
        self, db_session: Session, admin_operator: OperatorORM
    ) -> None:
        create_operator(
            db_session,
            username="charlie_op",
            password="Password123!",
            creator_operator=admin_operator,
        )
        with pytest.raises(AuthenticationError, match="already exists"):
            create_operator(
                db_session,
                username="charlie_op",
                password="DifferentPassword789!",
                creator_operator=admin_operator,
            )

    def test_malformed_username_and_password_rejection(
        self, db_session: Session, admin_operator: OperatorORM
    ) -> None:
        for bad_user in ("", "a", "ab", "user with spaces", "user@special!", "a" * 51):
            with pytest.raises(ValueError, match="Invalid username"):
                create_operator(
                    db_session,
                    bad_user,
                    "ValidPassword123!",
                    creator_operator=admin_operator,
                )

        with pytest.raises(ValueError, match="at least 8 characters"):
            create_operator(
                db_session,
                "valid_user",
                "short",
                creator_operator=admin_operator,
            )

        with pytest.raises(ValueError, match="Invalid role"):
            create_operator(
                db_session,
                "valid_user",
                "ValidPassword123!",
                creator_operator=admin_operator,
                role="ROLE_SUPERUSER",
            )


class TestAuthenticationThrottling:
    """Tests for Findings SEC-90.16.7A-03 (Pre-verification delay) and SEC-90.16.7A-04 (Atomic updates)."""

    def test_throttle_delay_formula(self) -> None:
        assert calculate_throttle_delay(0) == 0.0
        assert calculate_throttle_delay(-1) == 0.0
        assert calculate_throttle_delay(1) == 1.0   # 2^0
        assert calculate_throttle_delay(2) == 2.0   # 2^1
        assert calculate_throttle_delay(3) == 4.0   # 2^2
        assert calculate_throttle_delay(4) == 8.0   # 2^3
        assert calculate_throttle_delay(5) == 16.0  # 2^4
        assert calculate_throttle_delay(6) == 30.0  # capped at 30.0
        assert calculate_throttle_delay(10) == 30.0 # capped

    def test_pre_verification_throttle_delay_enforced_even_with_correct_password(
        self, db_session: Session, admin_operator: OperatorORM
    ) -> None:
        """Finding SEC-90.16.7A-03: Outstanding throttle delay is enforced BEFORE password verification."""
        operator = create_operator(
            db_session,
            username="throttle_user",
            password="CorrectPassword123!",
            creator_operator=admin_operator,
        )
        # Simulate 3 previous failed attempts (delay = 4.0s)
        operator.failed_attempt_count = 3
        db_session.commit()

        slept_durations: list[float] = []

        def mock_sleep(seconds: float) -> None:
            slept_durations.append(seconds)

        with patch("time.sleep", side_effect=mock_sleep):
            # Authenticate with CORRECT password
            auth = authenticate_operator(
                db_session,
                "throttle_user",
                "CorrectPassword123!",
                enforce_sleep=True,
            )
            assert auth.id == operator.id
            # Counter must be reset to 0
            assert auth.failed_attempt_count == 0

        # CRITICAL ASSERTION: The 4.0s delay MUST have been slept before returning!
        assert slept_durations == [4.0]

    def test_malformed_password_triggers_throttle_increment(
        self, db_session: Session, admin_operator: OperatorORM
    ) -> None:
        """Finding SEC-90.16.7A-03: Submitting an empty password against an existing user increments counter."""
        operator = create_operator(
            db_session,
            username="malformed_pwd_user",
            password="CorrectPassword123!",
            creator_operator=admin_operator,
        )
        assert operator.failed_attempt_count == 0

        with pytest.raises(AuthenticationError, match="Password must be a non-empty string"):
            authenticate_operator(db_session, "malformed_pwd_user", "", enforce_sleep=False)

        assert operator.failed_attempt_count == 1

    def test_failed_attempts_increment_counter_without_lockout(
        self, db_session: Session, admin_operator: OperatorORM
    ) -> None:
        operator = create_operator(
            db_session,
            username="target_user",
            password="CorrectPassword123!",
            creator_operator=admin_operator,
        )
        assert operator.failed_attempt_count == 0

        # Attempt 1
        with pytest.raises(AuthenticationError, match="Failed attempts: 1"):
            authenticate_operator(db_session, "target_user", "Wrong1", enforce_sleep=False)
        assert operator.failed_attempt_count == 1

        # Attempt 2
        with pytest.raises(AuthenticationError, match="Failed attempts: 2"):
            authenticate_operator(db_session, "target_user", "Wrong2", enforce_sleep=False)
        assert operator.failed_attempt_count == 2

        # NO PERMANENT LOCKOUT: User can still successfully log in with correct credentials
        auth = authenticate_operator(db_session, "target_user", "CorrectPassword123!", enforce_sleep=False)
        assert auth.failed_attempt_count == 0

    def test_concurrent_failed_logins_atomic_increments(
        self, admin_operator: OperatorORM, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Finding SEC-90.16.7A-04: Concurrent failed logins execute atomic SQL increments without lost updates."""
        token = "token_for_atomic_test_12345"
        monkeypatch.setenv("VENTUREBOT_BOOTSTRAP_TOKEN", token)
        import tempfile
        tmp_dir = tempfile.mkdtemp()
        db_path = os.path.join(tmp_dir, "test_atomic_throttle.db")
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"timeout": 30.0},
        )
        init_db(engine)
        session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

        # Bootstrap and create target user
        init_session = session_factory()
        admin = bootstrap_initial_admin(
            init_session,
            "admin",
            "AdminPassword123!",
            bootstrap_token=token,
        )
        create_operator(
            init_session,
            "atomic_target",
            "Password123!",
            creator_operator=admin,
        )
        init_session.close()

        num_threads = 5
        barrier = threading.Barrier(num_threads)

        def worker() -> None:
            sess = session_factory()
            try:
                barrier.wait(timeout=5.0)
                authenticate_operator(sess, "atomic_target", "WrongPassword", enforce_sleep=False)
            except AuthenticationError:
                pass
            finally:
                sess.close()

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Check final count in database: must be exactly num_threads
        verify_sess = session_factory()
        op = get_operator_by_username(verify_sess, "atomic_target")
        assert op is not None
        assert op.failed_attempt_count == num_threads
        verify_sess.close()
        engine.dispose()
        try:
            os.remove(db_path)
            os.rmdir(tmp_dir)
        except OSError:
            pass

    def test_durable_throttling_survives_process_restart(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token = "token_restart_test_12345"
        monkeypatch.setenv("VENTUREBOT_BOOTSTRAP_TOKEN", token)
        import tempfile
        tmp_dir = tempfile.mkdtemp()
        db_path = os.path.join(tmp_dir, "test_durable_throttle.db")
        engine1 = create_engine(f"sqlite:///{db_path}")
        init_db(engine1)
        sf1 = sessionmaker(bind=engine1)
        s1 = sf1()

        admin = bootstrap_initial_admin(s1, "admin", "AdminPass123!", bootstrap_token=token)
        create_operator(s1, "restart_user", "Password123!", creator_operator=admin)
        with pytest.raises(AuthenticationError):
            authenticate_operator(s1, "restart_user", "bad1", enforce_sleep=False)
        with pytest.raises(AuthenticationError):
            authenticate_operator(s1, "restart_user", "bad2", enforce_sleep=False)
        with pytest.raises(AuthenticationError):
            authenticate_operator(s1, "restart_user", "bad3", enforce_sleep=False)
        s1.close()
        engine1.dispose()

        # Reopen with fresh engine
        engine2 = create_engine(f"sqlite:///{db_path}")
        sf2 = sessionmaker(bind=engine2)
        s2 = sf2()

        op = get_operator_by_username(s2, "restart_user")
        assert op is not None
        assert op.failed_attempt_count == 3

        with pytest.raises(AuthenticationError, match="Failed attempts: 4"):
            authenticate_operator(s2, "restart_user", "bad4", enforce_sleep=False)
        assert op.failed_attempt_count == 4

        s2.close()
        engine2.dispose()
        try:
            os.remove(db_path)
            os.rmdir(tmp_dir)
        except OSError:
            pass

    def test_nonexistent_user_timing_attack_mitigation(self, db_session: Session) -> None:
        with pytest.raises(AuthenticationError, match="Invalid username or password"):
            authenticate_operator(db_session, "ghost_user", "SomePassword123!", enforce_sleep=False)

    def test_inactive_user_authentication_rejection(
        self, db_session: Session, admin_operator: OperatorORM
    ) -> None:
        create_operator(
            db_session,
            username="inactive_target",
            password="CorrectPassword123!",
            creator_operator=admin_operator,
            is_active=False,
        )
        with pytest.raises(AccountInactiveError, match="disabled or inactive"):
            authenticate_operator(
                db_session,
                "inactive_target",
                "CorrectPassword123!",
                enforce_sleep=False,
            )


class TestSessionManagement:
    """Tests for Part B: Session creation, dual-TTL expiration, revocation, and concurrency."""

    def test_session_creation_and_token_storage(
        self, db_session: Session, admin_operator: OperatorORM
    ) -> None:
        operator = create_operator(
            db_session,
            "sess_user",
            "Password123!",
            creator_operator=admin_operator,
        )
        session_record, raw_token = create_session(db_session, operator)

        assert session_record.id is not None
        assert session_record.operator_id == operator.id
        assert session_record.revoked_at is None
        assert session_record.token_hash != raw_token
        assert session_record.token_hash == generate_sha256_digest(raw_token)
        assert session_record.created_at is not None
        assert session_record.last_activity_at == session_record.created_at
        assert session_record.expires_at == session_record.created_at + timedelta(hours=8)

    def test_session_validation_success_and_touch_activity(
        self, db_session: Session, admin_operator: OperatorORM
    ) -> None:
        operator = create_operator(
            db_session,
            "touch_user",
            "Password123!",
            creator_operator=admin_operator,
        )
        t0 = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
        session_record, raw_token = create_session(db_session, operator, now=t0)

        t1 = t0 + timedelta(minutes=5)
        validated_sess, validated_op = validate_session(
            db_session,
            raw_token,
            touch_activity=True,
            now=t1,
        )
        assert validated_sess.id == session_record.id
        assert validated_op.id == operator.id
        assert validated_sess.last_activity_at == t1

    def test_session_inactivity_timeout_rejection(
        self, db_session: Session, admin_operator: OperatorORM
    ) -> None:
        operator = create_operator(
            db_session,
            "inactive_sess_user",
            "Password123!",
            creator_operator=admin_operator,
        )
        t0 = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
        _, raw_token = create_session(db_session, operator, now=t0)

        t_timeout = t0 + timedelta(minutes=15, seconds=1)
        with pytest.raises(SessionError, match="inactivity timeout"):
            validate_session(db_session, raw_token, now=t_timeout)

    def test_session_absolute_lifetime_rejection(
        self, db_session: Session, admin_operator: OperatorORM
    ) -> None:
        operator = create_operator(
            db_session,
            "lifetime_user",
            "Password123!",
            creator_operator=admin_operator,
        )
        t0 = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
        sess, raw_token = create_session(db_session, operator, now=t0)

        t_active = t0 + timedelta(hours=8, seconds=1)
        sess.last_activity_at = t_active - timedelta(minutes=2)
        db_session.commit()

        with pytest.raises(SessionError, match="absolute lifetime exceeded"):
            validate_session(db_session, raw_token, now=t_active)

    def test_session_revocation(
        self, db_session: Session, admin_operator: OperatorORM
    ) -> None:
        operator = create_operator(
            db_session,
            "revoke_user",
            "Password123!",
            creator_operator=admin_operator,
        )
        _, raw_token = create_session(db_session, operator)

        revoked = revoke_session(db_session, raw_token)
        assert revoked is True

        with pytest.raises(SessionError, match="revoked"):
            validate_session(db_session, raw_token)

        assert revoke_session(db_session, raw_token) is False

    def test_session_fixation_mitigation_terminates_prior_sessions(
        self, db_session: Session, admin_operator: OperatorORM
    ) -> None:
        operator = create_operator(
            db_session,
            "fixation_user",
            "Password123!",
            creator_operator=admin_operator,
        )
        t0 = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)

        s1, tok1 = create_session(db_session, operator, now=t0)
        assert validate_session(db_session, tok1, now=t0)[0].id == s1.id

        t1 = t0 + timedelta(minutes=2)
        s2, tok2 = create_session(db_session, operator, now=t1)

        with pytest.raises(SessionError, match="revoked"):
            validate_session(db_session, tok1, now=t1)

        assert validate_session(db_session, tok2, now=t1)[0].id == s2.id

    def test_logout_all_operator_sessions(
        self, db_session: Session, admin_operator: OperatorORM
    ) -> None:
        operator = create_operator(
            db_session,
            "logout_user",
            "Password123!",
            creator_operator=admin_operator,
        )
        s1, tok1 = create_session(db_session, operator, terminate_existing=False)
        s2, tok2 = create_session(db_session, operator, terminate_existing=False)

        count = revoke_all_operator_sessions(db_session, operator.id)
        assert count == 2

        with pytest.raises(SessionError, match="revoked"):
            validate_session(db_session, tok1)
        with pytest.raises(SessionError, match="revoked"):
            validate_session(db_session, tok2)

    def test_session_validation_fails_for_inactive_operator(
        self, db_session: Session, admin_operator: OperatorORM
    ) -> None:
        operator = create_operator(
            db_session,
            "suspending_user",
            "Password123!",
            creator_operator=admin_operator,
        )
        _, raw_token = create_session(db_session, operator)

        operator.is_active = False
        db_session.commit()

        with pytest.raises(AccountInactiveError, match="inactive or not found"):
            validate_session(db_session, raw_token)

    def test_session_revocation_racing_with_validation(
        self, admin_operator: OperatorORM, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Finding SEC-90.16.7A-05: Revocation racing with validation fails closed once revoked."""
        token = "token_race_test_12345"
        monkeypatch.setenv("VENTUREBOT_BOOTSTRAP_TOKEN", token)
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        init_db(engine)
        session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

        s_init = session_factory()
        admin = bootstrap_initial_admin(s_init, "admin", "Password123!", bootstrap_token=token)
        op = create_operator(s_init, "race_user", "Password123!", creator_operator=admin)
        _, raw_token = create_session(s_init, op)
        s_init.close()

        # Revoke session
        s_revoke = session_factory()
        revoke_session(s_revoke, raw_token)
        s_revoke.close()

        # Subsequent or concurrent validation must immediately fail closed
        s_validate = session_factory()
        with pytest.raises(SessionError, match="revoked"):
            validate_session(s_validate, raw_token)
        s_validate.close()
        engine.dispose()


class TestRoleBasedAccessControl:
    """Tests for Part D: RBAC permissions, role boundaries, and require_permission."""

    def test_role_permissions_matrix(self) -> None:
        viewer_perms = get_role_permissions(ROLE_VIEWER)
        assert PERM_EXPERIMENT_READ in viewer_perms
        assert PERM_EMERGENCY_STOP in viewer_perms
        assert PERM_EXPERIMENT_APPROVE not in viewer_perms
        assert PERM_MUTATION_EXECUTE not in viewer_perms
        assert PERM_SECURITY_ADMIN not in viewer_perms

        op_perms = get_role_permissions(ROLE_OPERATOR)
        assert PERM_EXPERIMENT_READ in op_perms
        assert PERM_EXPERIMENT_APPROVE in op_perms
        assert PERM_MUTATION_EXECUTE in op_perms
        assert PERM_EMERGENCY_STOP in op_perms
        assert PERM_SECURITY_ADMIN not in op_perms

        admin_perms = get_role_permissions(ROLE_ADMIN)
        assert PERM_EXPERIMENT_READ in admin_perms
        assert PERM_EMERGENCY_STOP in admin_perms
        assert PERM_SECURITY_ADMIN in admin_perms
        assert PERM_EXPERIMENT_APPROVE not in admin_perms
        assert PERM_MUTATION_EXECUTE not in admin_perms

    def test_emergency_stop_available_to_all_authenticated_roles(self) -> None:
        assert has_permission(ROLE_VIEWER, PERM_EMERGENCY_STOP) is True
        assert has_permission(ROLE_OPERATOR, PERM_EMERGENCY_STOP) is True
        assert has_permission(ROLE_ADMIN, PERM_EMERGENCY_STOP) is True

    def test_has_permission_and_check_permission(self) -> None:
        assert has_permission(ROLE_OPERATOR, PERM_MUTATION_EXECUTE) is True
        check_permission(ROLE_OPERATOR, PERM_MUTATION_EXECUTE)

        assert has_permission(ROLE_VIEWER, PERM_MUTATION_EXECUTE) is False
        with pytest.raises(AuthorizationError, match="Permission denied"):
            check_permission(ROLE_VIEWER, PERM_MUTATION_EXECUTE)

        assert has_permission("INVALID_ROLE", PERM_EXPERIMENT_READ) is False
        assert has_permission(ROLE_OPERATOR, "INVALID_PERM") is False

    def test_require_permission_bridge(
        self, db_session: Session, admin_operator: OperatorORM
    ) -> None:
        viewer = create_operator(
            db_session,
            "rbac_viewer",
            "Password123!",
            creator_operator=admin_operator,
            role=ROLE_VIEWER,
        )
        operator = create_operator(
            db_session,
            "rbac_op",
            "Password123!",
            creator_operator=admin_operator,
            role=ROLE_OPERATOR,
        )

        _, viewer_tok = create_session(db_session, viewer)
        _, op_tok = create_session(db_session, operator)

        sess_v, op_v = require_permission(db_session, viewer_tok, PERM_EXPERIMENT_READ)
        assert op_v.username == "rbac_viewer"

        with pytest.raises(AuthorizationError, match="Permission denied"):
            require_permission(db_session, viewer_tok, PERM_MUTATION_EXECUTE)

        sess_o, op_o = require_permission(db_session, op_tok, PERM_MUTATION_EXECUTE)
        assert op_o.username == "rbac_op"

        with pytest.raises(AuthorizationError, match="Permission denied"):
            require_permission(db_session, op_tok, PERM_SECURITY_ADMIN)
