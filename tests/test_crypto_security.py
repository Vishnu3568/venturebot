"""Unit and integration security tests for Step 90.16.6.

Covers:
- Standard-library scrypt password hashing and constant-time verification
- Password rejection on mismatch, corruption, and invalid parameters
- Random nonce and session token generation and uniqueness
- Deterministic RFC 8785 canonical JSON serialization (NFC normalization, float prohibition)
- SHA-256 request digest stability
- HMAC-SHA256 signature generation and constant-time verification
- Capability ticket schema validation, signing, tampering detection, and missing/extra field rejection
- Database ORM models: uniqueness constraints, foreign keys, single-use ticket lifecycle, and durable intent states
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from venturebot.database.models import (
    VALID_CAPABILITY_STATUSES,
    VALID_MUTATION_INTENT_STATES,
    VALID_OPERATOR_ROLES,
    Base,
    ExperimentORM,
    MutationCapabilityORM,
    MutationIntentLedgerORM,
    OperatorORM,
    OperatorSessionORM,
    OpportunityORM,
)
from venturebot.security.crypto import (
    REQUIRED_TICKET_FIELDS,
    canonical_json_dumps,
    canonical_json_encode,
    generate_nonce,
    generate_secure_token,
    generate_sha256_digest,
    hash_password,
    sign_capability_ticket,
    sign_hmac_sha256,
    validate_capability_ticket_payload,
    verify_capability_ticket,
    verify_hmac_sha256,
    verify_password,
)


# ---------------------------------------------------------------------------
# Test Fixtures & Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def test_key_256() -> bytes:
    """Generate an isolated, ephemeral 256-bit symmetric test key."""
    return secrets.token_bytes(32)


@pytest.fixture
def db_session() -> Session:
    """Create an isolated in-memory SQLite database session with foreign keys enabled."""
    engine = create_engine("sqlite:///:memory:")
    # Enable SQLite foreign key constraint enforcement
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys = ON;")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def create_sample_ticket_payload(
    experiment_id: str | None = None,
    operator_id: str | None = None,
    ticket_id: str | None = None,
    nonce: str | None = None,
) -> dict[str, Any]:
    """Generate a canonical Step 90.16.5B capability ticket payload."""
    return {
        "budget_ceiling_amount": "200.00",
        "budget_ceiling_currency": "INR",
        "exact_operation": "DEPLOY_RECOVERY_ADSET_DORMANT",
        "expected_parent_id": "120252176243860380",
        "experiment_id": experiment_id or str(uuid.uuid4()),
        "expires_at": "2026-11-06T20:05:00Z",
        "http_method": "POST",
        "issued_at": "2026-11-06T20:00:00Z",
        "key_id": "k1",
        "nonce": nonce or generate_nonce(16),
        "operator_id": operator_id or str(uuid.uuid4()),
        "parameters_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "replacement_name": "VB-EXP-49fde874-9387-5056-934c-51a9cfca164f-ADSET-v2",
        "target_endpoint": "/act_1985595022114520/adsets",
        "target_tier": "ADSET",
        "ticket_id": ticket_id or str(uuid.uuid4()),
    }


# ---------------------------------------------------------------------------
# Part 1: Scrypt Password Hashing and Verification
# ---------------------------------------------------------------------------

class TestPasswordHashing:
    """Verifies standard-library scrypt password hashing and constant-time verification."""

    def test_password_hash_format_and_parameters(self) -> None:
        raw_pw = "CorrectHorseBatteryStaple#2026!"
        phc_hash = hash_password(raw_pw)

        # Assert format: $scrypt$ln=14,r=8,p=1$<salt>$<hash>
        assert phc_hash.startswith("$scrypt$ln=14,r=8,p=1$")
        parts = phc_hash.split("$")
        assert len(parts) == 5
        assert parts[1] == "scrypt"
        assert parts[2] == "ln=14,r=8,p=1"

        # Check salt and hash are valid base64
        salt_bytes = base64.b64decode(parts[3])
        hash_bytes = base64.b64decode(parts[4])
        assert len(salt_bytes) == 32
        assert len(hash_bytes) == 32

    def test_password_verification_success(self) -> None:
        raw_pw = "SuperSecretOperatorPassphrase!99"
        phc_hash = hash_password(raw_pw)

        assert verify_password(raw_pw, phc_hash) is True

    def test_incorrect_password_rejection(self) -> None:
        raw_pw = "ValidPassword123!"
        phc_hash = hash_password(raw_pw)

        assert verify_password("WrongPassword123!", phc_hash) is False
        assert verify_password("validpassword123!", phc_hash) is False
        assert verify_password("", phc_hash) is False

    def test_empty_or_invalid_inputs_fail_closed(self) -> None:
        assert verify_password("", "") is False
        assert verify_password("test", "") is False
        assert verify_password("", "$scrypt$ln=14,r=8,p=1$aaaa$bbbb") is False
        assert verify_password("test", "not_a_valid_hash") is False
        assert verify_password("test", "$argon2id$v=19$m=65536...") is False

    def test_corrupted_hash_or_salt_rejection(self) -> None:
        raw_pw = "TestPassword#1"
        phc_hash = hash_password(raw_pw)
        parts = phc_hash.split("$")

        # Corrupt salt
        corrupt_salt = parts[3][:-4] + "AAAA"
        corrupt_hash_str = f"${parts[1]}${parts[2]}${corrupt_salt}${parts[4]}"
        assert verify_password(raw_pw, corrupt_hash_str) is False

        # Corrupt hash
        corrupt_digest = parts[4][:-4] + "ZZZZ"
        corrupt_hash_str2 = f"${parts[1]}${parts[2]}${parts[3]}${corrupt_digest}"
        assert verify_password(raw_pw, corrupt_hash_str2) is False

    def test_hash_password_rejects_empty_password(self) -> None:
        with pytest.raises(ValueError, match="Password must be a non-empty string"):
            hash_password("")


# ---------------------------------------------------------------------------
# Part 2: Nonce and Token Generation & Uniqueness
# ---------------------------------------------------------------------------

class TestRandomGeneration:
    """Verifies CSPRNG random nonces and secure session tokens."""

    def test_nonce_uniqueness_and_length(self) -> None:
        nonces = [generate_nonce(16) for _ in range(500)]
        assert len(nonces) == 500
        assert len(set(nonces)) == 500  # Zero collisions

        for n in nonces:
            assert len(n) == 32  # 16 bytes = 32 hex chars
            assert all(c in "0123456789abcdef" for c in n)

    def test_nonce_minimum_length_enforcement(self) -> None:
        with pytest.raises(ValueError, match="Nonce length must be at least 16 bytes"):
            generate_nonce(15)

    def test_secure_token_uniqueness_and_entropy(self) -> None:
        tokens = [generate_secure_token(32) for _ in range(500)]
        assert len(tokens) == 500
        assert len(set(tokens)) == 500  # Zero collisions

        for t in tokens:
            assert len(t) >= 42  # 32 bytes base64-encoded is ~43 chars

    def test_secure_token_minimum_length_enforcement(self) -> None:
        with pytest.raises(ValueError, match="Secure token length must be at least 32 bytes"):
            generate_secure_token(31)


# ---------------------------------------------------------------------------
# Part 3: Canonical JSON Serialization (RFC 8785)
# ---------------------------------------------------------------------------

class TestCanonicalJsonSerialization:
    """Verifies deterministic canonical JSON serialization following VB-CJS."""

    def test_key_sorting_determinism(self) -> None:
        dict_a = {"z": 1, "a": 2, "m": 3}
        dict_b = {"a": 2, "m": 3, "z": 1}
        dict_c = {"m": 3, "z": 1, "a": 2}

        encoded_a = canonical_json_encode(dict_a)
        encoded_b = canonical_json_encode(dict_b)
        encoded_c = canonical_json_encode(dict_c)

        assert encoded_a == encoded_b == encoded_c
        assert encoded_a == b'{"a":2,"m":3,"z":1}'

    def test_whitespace_free_separators(self) -> None:
        payload = {"key": "value", "items": [1, 2, 3]}
        encoded = canonical_json_dumps(payload)
        assert " " not in encoded
        assert "\n" not in encoded
        assert "\t" not in encoded
        assert encoded == '{"items":[1,2,3],"key":"value"}'

    def test_unicode_nfc_normalization(self) -> None:
        # e with acute accent: composed (NFC) vs decomposed (NFD: 'e' + combining acute)
        composed = "caf\u00e9"
        decomposed = "cafe\u0301"
        assert composed != decomposed  # Different code points

        payload_1 = {"name": composed}
        payload_2 = {"name": decomposed}

        encoded_1 = canonical_json_encode(payload_1)
        encoded_2 = canonical_json_encode(payload_2)

        assert encoded_1 == encoded_2  # Normalized to identical NFC bytes

    def test_floats_strictly_prohibited(self) -> None:
        with pytest.raises(TypeError, match="Floating-point values are strictly prohibited"):
            canonical_json_encode({"amount": 200.0})

        with pytest.raises(TypeError, match="Floating-point values are strictly prohibited"):
            canonical_json_encode({"nested": {"budget": 1.5}})

    def test_decimals_and_uuids_formatted_deterministically(self) -> None:
        test_uuid = uuid.UUID("49fde874-9387-5056-934c-51a9cfca164f")
        payload = {
            "amount": Decimal("200.00"),
            "id": test_uuid,
        }
        encoded = canonical_json_dumps(payload)
        assert encoded == '{"amount":"200.00","id":"49fde874-9387-5056-934c-51a9cfca164f"}'

    def test_datetimes_formatted_rfc3339_utc(self) -> None:
        dt_utc = datetime(2026, 11, 6, 20, 0, 0, tzinfo=timezone.utc)
        payload = {"timestamp": dt_utc}
        encoded = canonical_json_dumps(payload)
        assert encoded == '{"timestamp":"2026-11-06T20:00:00Z"}'

        # Naive datetime must be rejected
        dt_naive = datetime(2026, 11, 6, 20, 0, 0)
        with pytest.raises(ValueError, match="Naive datetime values are forbidden"):
            canonical_json_encode({"timestamp": dt_naive})

    def test_distinct_keys_normalizing_to_same_nfc_rejected(self) -> None:
        # Two distinct original keys: composed vs decomposed
        composed = "caf\u00e9"
        decomposed = "cafe\u0301"
        assert composed != decomposed

        # Forward insertion order
        colliding_dict_1 = {composed: "val1", decomposed: "val2"}
        with pytest.raises(ValueError, match="Duplicate object key after NFC normalization"):
            canonical_json_encode(colliding_dict_1)

        # Reverse insertion order
        colliding_dict_2 = {decomposed: "val2", composed: "val1"}
        with pytest.raises(ValueError, match="Duplicate object key after NFC normalization"):
            canonical_json_encode(colliding_dict_2)

    def test_valid_pre_normalized_key_accepted(self) -> None:
        composed = "caf\u00e9"
        payload = {composed: "espresso", "tea": "earl_grey"}
        encoded = canonical_json_dumps(payload)
        assert encoded == '{"café":"espresso","tea":"earl_grey"}'

    def test_valid_decomposed_key_without_collision_accepted(self) -> None:
        # Decomposed key without competing collision normalizes cleanly to NFC
        decomposed = "cafe\u0301"
        payload = {decomposed: "latte"}
        encoded = canonical_json_dumps(payload)
        assert encoded == '{"café":"latte"}'

    def test_supplementary_unicode_characters(self) -> None:
        # Characters with code points > U+FFFF (astral plane)
        smile = "\U0001F600"
        rocket = "\U0001F680"
        payload = {
            "alpha": 1,
            smile + "_mood": "happy",
            rocket + "_tier": "launch",
            "omega": 99,
        }
        encoded = canonical_json_dumps(payload)
        # Verify deterministic code point ordering: 'alpha' < 'omega' < smile < rocket
        assert encoded == '{"alpha":1,"omega":99,"😀_mood":"happy","🚀_tier":"launch"}'

    def test_nested_dictionary_collision_rejection(self) -> None:
        composed = "caf\u00e9"
        decomposed = "cafe\u0301"
        payload = {
            "outer": {
                "safe": 1,
                "nested": {
                    composed: "a",
                    decomposed: "b",
                },
            }
        }
        with pytest.raises(ValueError, match="Duplicate object key after NFC normalization"):
            canonical_json_encode(payload)

    def test_non_string_dictionary_key_rejection(self) -> None:
        with pytest.raises(TypeError, match="Dictionary keys in canonical JSON must be strings"):
            canonical_json_encode({123: "int_key"})

        with pytest.raises(TypeError, match="Dictionary keys in canonical JSON must be strings"):
            canonical_json_encode({None: "none_key"})

        with pytest.raises(TypeError, match="Dictionary keys in canonical JSON must be strings"):
            canonical_json_encode({(1, 2): "tuple_key"})


# ---------------------------------------------------------------------------
# Part 4: SHA-256 Digest Generation
# ---------------------------------------------------------------------------

class TestSha256Digest:
    """Verifies deterministic SHA-256 request digest calculation."""

    def test_known_test_vectors(self) -> None:
        # Standard SHA-256 empty string digest
        empty_digest = generate_sha256_digest(b"")
        assert empty_digest == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

        # String input behaves identically to UTF-8 bytes
        assert generate_sha256_digest("") == empty_digest

    def test_deterministic_request_body_digest(self) -> None:
        body = b'{"name":"VB-EXP-123","status":"PAUSED"}'
        digest_1 = generate_sha256_digest(body)
        digest_2 = generate_sha256_digest(body)
        assert digest_1 == digest_2
        assert len(digest_1) == 64

    def test_invalid_type_rejection(self) -> None:
        with pytest.raises(TypeError, match="Digest input must be bytes or str"):
            generate_sha256_digest(12345)  # type: ignore


# ---------------------------------------------------------------------------
# Part 5: HMAC-SHA256 Signing and Verification
# ---------------------------------------------------------------------------

class TestHmacSha256Signing:
    """Verifies HMAC-SHA256 signing, constant-time verification, and tamper detection."""

    def test_valid_signature_acceptance(self, test_key_256: bytes) -> None:
        data = b'{"operation":"DEPLOY","tier":"ADSET"}'
        signature = sign_hmac_sha256(test_key_256, data)

        assert len(signature) == 64
        assert verify_hmac_sha256(test_key_256, data, signature) is True

    def test_invalid_signature_rejection(self, test_key_256: bytes) -> None:
        data = b'{"operation":"DEPLOY","tier":"ADSET"}'
        signature = sign_hmac_sha256(test_key_256, data)

        # Corrupted signature
        bad_sig = "a" * 64
        assert verify_hmac_sha256(test_key_256, data, bad_sig) is False
        assert verify_hmac_sha256(test_key_256, data, "") is False

    def test_different_key_rejection(self, test_key_256: bytes) -> None:
        other_key = secrets.token_bytes(32)
        data = b'{"operation":"DEPLOY","tier":"ADSET"}'
        signature = sign_hmac_sha256(test_key_256, data)

        assert verify_hmac_sha256(other_key, data, signature) is False

    def test_payload_tampering_rejection(self, test_key_256: bytes) -> None:
        original_data = b'{"budget":"200.00"}'
        tampered_data = b'{"budget":"2000.00"}'

        signature = sign_hmac_sha256(test_key_256, original_data)
        assert verify_hmac_sha256(test_key_256, original_data, signature) is True
        assert verify_hmac_sha256(test_key_256, tampered_data, signature) is False

    def test_key_length_enforcement(self) -> None:
        too_short_key = b"short_key_under_32_bytes"
        with pytest.raises(ValueError, match="Signing key must be at least 32 bytes"):
            sign_hmac_sha256(too_short_key, b"data")


# ---------------------------------------------------------------------------
# Part 6: Capability Ticket Schema Validation & Verification
# ---------------------------------------------------------------------------

class TestCapabilityTicketValidation:
    """Verifies schema validation and cryptographic verification for MutationCapability tickets."""

    def test_valid_ticket_signing_and_verification(self, test_key_256: bytes) -> None:
        payload = create_sample_ticket_payload()
        signature = sign_capability_ticket(test_key_256, payload)

        assert len(signature) == 64
        assert verify_capability_ticket(test_key_256, payload, signature) is True

    def test_missing_fields_rejection(self, test_key_256: bytes) -> None:
        payload = create_sample_ticket_payload()
        for field in REQUIRED_TICKET_FIELDS:
            corrupt_payload = dict(payload)
            del corrupt_payload[field]
            with pytest.raises(ValueError, match="Missing required ticket field"):
                validate_capability_ticket_payload(corrupt_payload)
            with pytest.raises(ValueError, match="Missing required ticket field"):
                sign_capability_ticket(test_key_256, corrupt_payload)
            assert verify_capability_ticket(test_key_256, corrupt_payload, "sig") is False

    def test_unexpected_fields_rejection(self, test_key_256: bytes) -> None:
        payload = create_sample_ticket_payload()
        payload["speculative_unauthorized_field"] = "malicious_payload"

        with pytest.raises(ValueError, match="Unexpected field"):
            validate_capability_ticket_payload(payload)

        with pytest.raises(ValueError, match="Unexpected field"):
            sign_capability_ticket(test_key_256, payload)

    def test_invalid_uuid_fields_rejection(self) -> None:
        payload = create_sample_ticket_payload()
        payload["ticket_id"] = "not-a-uuid"
        with pytest.raises(ValueError, match="not a valid UUID"):
            validate_capability_ticket_payload(payload)

    def test_invalid_tier_rejection(self) -> None:
        payload = create_sample_ticket_payload()
        payload["target_tier"] = "INVALID_TIER"
        with pytest.raises(ValueError, match="Invalid target_tier"):
            validate_capability_ticket_payload(payload)

    def test_invalid_amount_format_rejection(self) -> None:
        payload = create_sample_ticket_payload()
        # Missing decimal places
        payload["budget_ceiling_amount"] = "200"
        with pytest.raises(ValueError, match="fixed-point decimal string"):
            validate_capability_ticket_payload(payload)

        # Zero or negative amount
        payload["budget_ceiling_amount"] = "0.00"
        with pytest.raises(ValueError, match="greater than zero"):
            validate_capability_ticket_payload(payload)

    def test_invalid_endpoint_rejection(self) -> None:
        payload = create_sample_ticket_payload()
        payload["target_endpoint"] = "https://graph.facebook.com/act_123/adsets"
        with pytest.raises(ValueError, match="must start with '/'"):
            validate_capability_ticket_payload(payload)

    def test_invalid_timestamp_rejection(self) -> None:
        payload = create_sample_ticket_payload()
        payload["expires_at"] = "2026-11-06 20:05:00"  # Missing T and Z
        with pytest.raises(ValueError, match="RFC 3339 UTC string"):
            validate_capability_ticket_payload(payload)

    def test_tampered_payload_fails_verification(self, test_key_256: bytes) -> None:
        payload = create_sample_ticket_payload()
        signature = sign_capability_ticket(test_key_256, payload)

        # Attacker attempts to change budget ceiling from 200.00 to 999.00
        tampered_payload = dict(payload)
        tampered_payload["budget_ceiling_amount"] = "999.00"

        assert verify_capability_ticket(test_key_256, tampered_payload, signature) is False

    def test_ticket_reversed_timestamp_rejection(self) -> None:
        payload = create_sample_ticket_payload()
        # expires_at is before issued_at
        payload["issued_at"] = "2026-11-06T20:10:00Z"
        payload["expires_at"] = "2026-11-06T20:05:00Z"
        with pytest.raises(ValueError, match="must be strictly after"):
            validate_capability_ticket_payload(payload)

    def test_ticket_zero_duration_rejection(self) -> None:
        payload = create_sample_ticket_payload()
        # expires_at is identical to issued_at (0-second validity)
        payload["issued_at"] = "2026-11-06T20:00:00Z"
        payload["expires_at"] = "2026-11-06T20:00:00Z"
        with pytest.raises(ValueError, match="must be strictly after"):
            validate_capability_ticket_payload(payload)

    def test_ticket_excessive_duration_rejection(self) -> None:
        payload = create_sample_ticket_payload()
        # 16 minutes validity (exceeds 15m maximum)
        payload["issued_at"] = "2026-11-06T20:00:00Z"
        payload["expires_at"] = "2026-11-06T20:16:00Z"
        with pytest.raises(ValueError, match="exceeds maximum allowed period"):
            validate_capability_ticket_payload(payload)

        # 1 year validity
        payload["expires_at"] = "2027-11-06T20:00:00Z"
        with pytest.raises(ValueError, match="exceeds maximum allowed period"):
            validate_capability_ticket_payload(payload)

    def test_ticket_valid_duration_acceptance(self, test_key_256: bytes) -> None:
        for minutes in (1, 5, 10, 15):
            payload = create_sample_ticket_payload()
            payload["issued_at"] = "2026-11-06T20:00:00Z"
            payload["expires_at"] = f"2026-11-06T20:{minutes:02d}:00Z"
            # Must validate and sign cleanly
            validate_capability_ticket_payload(payload)
            sig = sign_capability_ticket(test_key_256, payload)
            assert verify_capability_ticket(test_key_256, payload, sig) is True

    def test_ticket_malformed_timestamp_formats_rejection(self) -> None:
        payload = create_sample_ticket_payload()
        malformed_cases = [
            "",
            "2026-11-06T20:05:00",  # Missing Z
            "2026-11-06 20:05:00Z",  # Space instead of T
            "2026-02-31T20:05:00Z",  # Invalid calendar day
            "2026-13-01T20:05:00Z",  # Invalid month
            "not-a-timestamp",
        ]
        for bad_ts in malformed_cases:
            payload_copy = dict(payload)
            payload_copy["expires_at"] = bad_ts
            with pytest.raises(ValueError):
                validate_capability_ticket_payload(payload_copy)

    def test_failed_canonicalization_cannot_produce_signature(self, test_key_256: bytes) -> None:
        # 1. Direct canonicalization failure (float prohibited by VB-CJS)
        with pytest.raises(TypeError, match="Floating-point values are strictly prohibited"):
            canonical_json_encode({"amount": 200.0})

        # 2. Canonicalization failure due to duplicate NFC keys
        collision_dict = {"café": 1, "cafe\u0301": 2}
        with pytest.raises(ValueError, match="Duplicate object key after NFC normalization"):
            canonical_json_encode(collision_dict)

        # 3. Neither can produce an HMAC signature
        with pytest.raises(TypeError):
            sign_hmac_sha256(test_key_256, canonical_json_encode({"amount": 200.0}))

        # 4. In sign_capability_ticket, invalid payload raises and cannot produce signature
        payload = create_sample_ticket_payload()
        corrupt_payload = dict(payload, budget_ceiling_amount="invalid_amount")
        with pytest.raises(ValueError):
            sign_capability_ticket(test_key_256, corrupt_payload)

        # 5. Verification on corrupt payload fails closed (returns False)
        assert verify_capability_ticket(test_key_256, corrupt_payload, "a" * 64) is False

    def test_signing_and_verification_use_identical_vbcjs_behavior(
        self, test_key_256: bytes
    ) -> None:
        payload = create_sample_ticket_payload()
        # Verify valid signature round-trip
        sig = sign_capability_ticket(test_key_256, payload)
        assert verify_capability_ticket(test_key_256, payload, sig) is True

        # Tampering any field in the payload fails verification immediately
        tampered_1 = dict(payload, target_tier="CREATIVE")
        assert verify_capability_ticket(test_key_256, tampered_1, sig) is False

        tampered_2 = dict(payload, expected_parent_id="999999999999999999")
        assert verify_capability_ticket(test_key_256, tampered_2, sig) is False

        tampered_3 = dict(payload, parameters_digest="0" * 64)
        assert verify_capability_ticket(test_key_256, tampered_3, sig) is False


# ---------------------------------------------------------------------------
# Part 7: Database Models, Uniqueness Constraints, and Durability
# ---------------------------------------------------------------------------

class TestDatabaseModelsAndConstraints:
    """Verifies ORM models, unique constraints, foreign keys, and single-use semantics."""

    def test_operator_uniqueness_and_storage(self, db_session: Session) -> None:
        pw_hash = hash_password("OperatorPass123!")
        operator_1 = OperatorORM(
            username="trusted_operator",
            password_hash=pw_hash,
            role="ROLE_OPERATOR",
            is_active=True,
        )
        db_session.add(operator_1)
        db_session.commit()

        assert operator_1.id is not None
        assert operator_1.failed_attempt_count == 0

        # Attempt to insert duplicate username
        operator_duplicate = OperatorORM(
            username="trusted_operator",
            password_hash=hash_password("OtherPass456!"),
            role="ROLE_VIEWER",
        )
        db_session.add(operator_duplicate)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

    def test_operator_session_token_hash_uniqueness(self, db_session: Session) -> None:
        operator = OperatorORM(
            username="session_operator",
            password_hash=hash_password("Pass123!"),
            role="ROLE_OPERATOR",
        )
        db_session.add(operator)
        db_session.commit()

        raw_token = generate_secure_token(32)
        token_hash = generate_sha256_digest(raw_token)

        now = datetime.now(timezone.utc)
        session = OperatorSessionORM(
            operator_id=operator.id,
            token_hash=token_hash,
            expires_at=now + timedelta(hours=8),
        )
        db_session.add(session)
        db_session.commit()

        # Duplicate token_hash must fail
        duplicate_session = OperatorSessionORM(
            operator_id=operator.id,
            token_hash=token_hash,
            expires_at=now + timedelta(hours=8),
        )
        db_session.add(duplicate_session)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

    def test_capability_nonce_uniqueness_and_lifecycle(
        self, db_session: Session, test_key_256: bytes
    ) -> None:
        # Create prerequisite operator and opportunity/experiment
        operator = OperatorORM(
            username="capability_operator",
            password_hash=hash_password("Pass123!"),
            role="ROLE_OPERATOR",
        )
        opportunity = OpportunityORM(
            title="Opp 1",
            description="Test opp",
            category="digital",
        )
        db_session.add_all([operator, opportunity])
        db_session.commit()

        experiment = ExperimentORM(
            opportunity_id=opportunity.id,
            hypothesis="Test hypothesis",
            objective="Test objective",
            channel="meta",
            monetization_method="direct",
            allocated_budget=Decimal("200.00"),
            max_allowed_spend=Decimal("200.00"),
            success_criteria="C1",
            failure_criteria="F1",
        )
        db_session.add(experiment)
        db_session.commit()

        nonce = generate_nonce(16)
        ticket_payload = create_sample_ticket_payload(
            experiment_id=str(experiment.id),
            operator_id=str(operator.id),
            nonce=nonce,
        )
        signature = sign_capability_ticket(test_key_256, ticket_payload)

        now = datetime.now(timezone.utc)
        capability = MutationCapabilityORM(
            id=uuid.UUID(ticket_payload["ticket_id"]),
            operator_id=operator.id,
            experiment_id=experiment.id,
            tier="ADSET",
            exact_operation="DEPLOY_RECOVERY_ADSET_DORMANT",
            budget_ceiling_amount=Decimal("200.00"),
            budget_ceiling_currency="INR",
            expected_parent_id="120252176243860380",
            replacement_name=ticket_payload["replacement_name"],
            target_endpoint="/act_1985595022114520/adsets",
            http_method="POST",
            parameters_digest=ticket_payload["parameters_digest"],
            nonce=nonce,
            key_id="k1",
            signature=signature,
            status="ISSUED",
            expires_at=now + timedelta(minutes=5),
        )
        db_session.add(capability)
        db_session.commit()

        # 1. Nonce uniqueness: Attempting to insert another capability with identical nonce fails
        duplicate_capability = MutationCapabilityORM(
            operator_id=operator.id,
            experiment_id=experiment.id,
            tier="ADSET",
            exact_operation="DEPLOY_RECOVERY_ADSET_DORMANT",
            budget_ceiling_amount=Decimal("200.00"),
            expected_parent_id="120252176243860380",
            replacement_name="VB-EXP-OTHER-ADSET-v2",
            target_endpoint="/act_1985595022114520/adsets",
            parameters_digest=ticket_payload["parameters_digest"],
            nonce=nonce,  # Replayed nonce!
            signature=signature,
            expires_at=now + timedelta(minutes=5),
        )
        db_session.add(duplicate_capability)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

        # 2. Single-use status transition: ISSUED -> CONSUMED
        loaded_cap = db_session.get(MutationCapabilityORM, capability.id)
        assert loaded_cap is not None
        assert loaded_cap.status == "ISSUED"
        assert loaded_cap.consumed_at is None

        consumed_time = datetime.now(timezone.utc)
        loaded_cap.status = "CONSUMED"
        loaded_cap.consumed_at = consumed_time
        db_session.commit()

        refreshed_cap = db_session.get(MutationCapabilityORM, capability.id)
        assert refreshed_cap is not None
        assert refreshed_cap.status == "CONSUMED"
        assert refreshed_cap.consumed_at is not None
        # SQLite stores naive datetime strings; normalize tzinfo for comparison
        loaded_consumed = refreshed_cap.consumed_at
        if loaded_consumed.tzinfo is None:
            loaded_consumed = loaded_consumed.replace(tzinfo=timezone.utc)
        assert loaded_consumed == consumed_time

    def test_durable_intent_state_constraints_and_uniqueness(
        self, db_session: Session, test_key_256: bytes
    ) -> None:
        operator = OperatorORM(
            username="intent_operator",
            password_hash=hash_password("Pass123!"),
            role="ROLE_OPERATOR",
        )
        opportunity = OpportunityORM(title="Opp 2", description="Test opp 2", category="digital")
        db_session.add_all([operator, opportunity])
        db_session.commit()

        experiment = ExperimentORM(
            opportunity_id=opportunity.id,
            hypothesis="H2",
            objective="O2",
            channel="meta",
            monetization_method="direct",
            allocated_budget=Decimal("200.00"),
            max_allowed_spend=Decimal("200.00"),
            success_criteria="C2",
            failure_criteria="F2",
        )
        db_session.add(experiment)
        db_session.commit()

        ticket_payload = create_sample_ticket_payload(
            experiment_id=str(experiment.id),
            operator_id=str(operator.id),
        )
        sig = sign_capability_ticket(test_key_256, ticket_payload)

        capability = MutationCapabilityORM(
            id=uuid.UUID(ticket_payload["ticket_id"]),
            operator_id=operator.id,
            experiment_id=experiment.id,
            tier="ADSET",
            exact_operation="DEPLOY_RECOVERY_ADSET_DORMANT",
            budget_ceiling_amount=Decimal("200.00"),
            expected_parent_id="120252176243860380",
            replacement_name=ticket_payload["replacement_name"],
            target_endpoint="/act_1985595022114520/adsets",
            parameters_digest=ticket_payload["parameters_digest"],
            nonce=ticket_payload["nonce"],
            signature=sig,
            status="CONSUMED",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        db_session.add(capability)
        db_session.commit()

        # 1. Insert intent ledger row
        intent = MutationIntentLedgerORM(
            ticket_id=capability.id,
            experiment_id=experiment.id,
            tier="ADSET",
            state="MUTATION_DISPATCHING",
            parameters_digest=ticket_payload["parameters_digest"],
        )
        db_session.add(intent)
        db_session.commit()

        # 2. Unique constraint on ticket_id (1:1 mapping between ticket and intent)
        duplicate_intent = MutationIntentLedgerORM(
            ticket_id=capability.id,
            experiment_id=experiment.id,
            tier="ADSET",
            state="MUTATION_DISPATCHING",
            parameters_digest=ticket_payload["parameters_digest"],
        )
        db_session.add(duplicate_intent)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

        # 3. Verify all approved durable states can be durably persisted
        loaded_intent = db_session.get(MutationIntentLedgerORM, intent.id)
        assert loaded_intent is not None

        for approved_state in VALID_MUTATION_INTENT_STATES:
            loaded_intent.state = approved_state
            db_session.commit()
            refreshed = db_session.get(MutationIntentLedgerORM, intent.id)
            assert refreshed is not None
            assert refreshed.state == approved_state

    def test_foreign_key_cascade_and_restrict(
        self, db_session: Session, test_key_256: bytes
    ) -> None:
        # Operator cascade delete on sessions
        operator = OperatorORM(
            username="cascade_operator",
            password_hash=hash_password("Pass123!"),
            role="ROLE_OPERATOR",
        )
        db_session.add(operator)
        db_session.commit()

        session = OperatorSessionORM(
            operator_id=operator.id,
            token_hash=generate_sha256_digest(generate_secure_token(32)),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
        )
        db_session.add(session)
        db_session.commit()

        # Deleting operator cascades to session
        db_session.delete(operator)
        db_session.commit()

        assert db_session.get(OperatorSessionORM, session.id) is None

    def test_experiment_with_capability_audit_cannot_be_deleted(
        self, db_session: Session, test_key_256: bytes
    ) -> None:
        operator = OperatorORM(username="op_audit_1", password_hash=hash_password("Pass1!"))
        opportunity = OpportunityORM(title="Opp Audit 1", description="desc", category="digital")
        db_session.add_all([operator, opportunity])
        db_session.commit()

        experiment = ExperimentORM(
            opportunity_id=opportunity.id,
            hypothesis="H",
            objective="O",
            channel="meta",
            monetization_method="direct",
            allocated_budget=Decimal("200.00"),
            max_allowed_spend=Decimal("200.00"),
            success_criteria="S",
            failure_criteria="F",
        )
        db_session.add(experiment)
        db_session.commit()

        ticket_payload = create_sample_ticket_payload(
            experiment_id=str(experiment.id),
            operator_id=str(operator.id),
        )
        sig = sign_capability_ticket(test_key_256, ticket_payload)
        cap = MutationCapabilityORM(
            id=uuid.UUID(ticket_payload["ticket_id"]),
            operator_id=operator.id,
            experiment_id=experiment.id,
            tier="ADSET",
            exact_operation="DEPLOY_RECOVERY_ADSET_DORMANT",
            budget_ceiling_amount=Decimal("200.00"),
            expected_parent_id="120252176243860380",
            replacement_name=ticket_payload["replacement_name"],
            target_endpoint="/act_1985595022114520/adsets",
            parameters_digest=ticket_payload["parameters_digest"],
            nonce=ticket_payload["nonce"],
            signature=sig,
            status="ISSUED",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        db_session.add(cap)
        db_session.commit()

        # Attempt to delete experiment: MUST fail closed with IntegrityError due to ondelete="RESTRICT"
        db_session.delete(experiment)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

        # Verify capability and experiment records still exist intact
        assert db_session.get(MutationCapabilityORM, cap.id) is not None
        assert db_session.get(ExperimentORM, experiment.id) is not None

    def test_opportunity_with_audited_experiment_cannot_be_deleted(
        self, db_session: Session, test_key_256: bytes
    ) -> None:
        operator = OperatorORM(username="op_audit_2", password_hash=hash_password("Pass1!"))
        opportunity = OpportunityORM(title="Opp Audit 2", description="desc", category="digital")
        db_session.add_all([operator, opportunity])
        db_session.commit()

        experiment = ExperimentORM(
            opportunity_id=opportunity.id,
            hypothesis="H",
            objective="O",
            channel="meta",
            monetization_method="direct",
            allocated_budget=Decimal("200.00"),
            max_allowed_spend=Decimal("200.00"),
            success_criteria="S",
            failure_criteria="F",
        )
        db_session.add(experiment)
        db_session.commit()

        ticket_payload = create_sample_ticket_payload(
            experiment_id=str(experiment.id),
            operator_id=str(operator.id),
        )
        sig = sign_capability_ticket(test_key_256, ticket_payload)
        cap = MutationCapabilityORM(
            id=uuid.UUID(ticket_payload["ticket_id"]),
            operator_id=operator.id,
            experiment_id=experiment.id,
            tier="ADSET",
            exact_operation="DEPLOY_RECOVERY_ADSET_DORMANT",
            budget_ceiling_amount=Decimal("200.00"),
            expected_parent_id="120252176243860380",
            replacement_name=ticket_payload["replacement_name"],
            target_endpoint="/act_1985595022114520/adsets",
            parameters_digest=ticket_payload["parameters_digest"],
            nonce=ticket_payload["nonce"],
            signature=sig,
            status="ISSUED",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        db_session.add(cap)
        db_session.commit()

        # Attempt to delete parent opportunity: MUST fail closed with IntegrityError
        db_session.delete(opportunity)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

        # Verify opportunity, experiment, and capability still exist intact
        assert db_session.get(OpportunityORM, opportunity.id) is not None
        assert db_session.get(ExperimentORM, experiment.id) is not None
        assert db_session.get(MutationCapabilityORM, cap.id) is not None

    def test_unrelated_unaudited_records_deletion_succeeds(
        self, db_session: Session
    ) -> None:
        # Opportunity with a draft experiment that has NO capabilities or intents
        opportunity = OpportunityORM(title="Draft Opp", description="desc", category="digital")
        db_session.add(opportunity)
        db_session.commit()

        exp_draft = ExperimentORM(
            opportunity_id=opportunity.id,
            hypothesis="Draft H",
            objective="Draft O",
            channel="meta",
            monetization_method="direct",
            allocated_budget=Decimal("200.00"),
            max_allowed_spend=Decimal("200.00"),
            success_criteria="S",
            failure_criteria="F",
        )
        db_session.add(exp_draft)
        db_session.commit()

        draft_exp_id = exp_draft.id
        draft_opp_id = opportunity.id

        # Deleting unaudited draft experiment directly succeeds
        db_session.delete(exp_draft)
        db_session.commit()
        assert db_session.get(ExperimentORM, draft_exp_id) is None

        # Re-add another unaudited experiment and delete opportunity
        exp_draft_2 = ExperimentORM(
            opportunity_id=opportunity.id,
            hypothesis="Draft H2",
            objective="Draft O2",
            channel="meta",
            monetization_method="direct",
            allocated_budget=Decimal("200.00"),
            max_allowed_spend=Decimal("200.00"),
            success_criteria="S",
            failure_criteria="F",
        )
        db_session.add(exp_draft_2)
        db_session.commit()

        # Deleting parent opportunity cascades to unaudited experiment cleanly
        db_session.delete(opportunity)
        db_session.commit()
        assert db_session.get(OpportunityORM, draft_opp_id) is None
        assert db_session.get(ExperimentORM, exp_draft_2.id) is None

    def test_operator_with_capabilities_cannot_be_deleted(
        self, db_session: Session, test_key_256: bytes
    ) -> None:
        operator = OperatorORM(username="op_protect", password_hash=hash_password("Pass1!"))
        opportunity = OpportunityORM(title="Opp Protect", description="desc", category="digital")
        db_session.add_all([operator, opportunity])
        db_session.commit()

        experiment = ExperimentORM(
            opportunity_id=opportunity.id,
            hypothesis="H",
            objective="O",
            channel="meta",
            monetization_method="direct",
            allocated_budget=Decimal("200.00"),
            max_allowed_spend=Decimal("200.00"),
            success_criteria="S",
            failure_criteria="F",
        )
        db_session.add(experiment)
        db_session.commit()

        ticket_payload = create_sample_ticket_payload(
            experiment_id=str(experiment.id),
            operator_id=str(operator.id),
        )
        sig = sign_capability_ticket(test_key_256, ticket_payload)
        cap = MutationCapabilityORM(
            id=uuid.UUID(ticket_payload["ticket_id"]),
            operator_id=operator.id,
            experiment_id=experiment.id,
            tier="ADSET",
            exact_operation="DEPLOY_RECOVERY_ADSET_DORMANT",
            budget_ceiling_amount=Decimal("200.00"),
            expected_parent_id="120252176243860380",
            replacement_name=ticket_payload["replacement_name"],
            target_endpoint="/act_1985595022114520/adsets",
            parameters_digest=ticket_payload["parameters_digest"],
            nonce=ticket_payload["nonce"],
            signature=sig,
            status="ISSUED",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        db_session.add(cap)
        db_session.commit()

        # Deleting operator must fail closed with IntegrityError
        db_session.delete(operator)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

        assert db_session.get(OperatorORM, operator.id) is not None
        assert db_session.get(MutationCapabilityORM, cap.id) is not None

    def test_temporary_sqlite_database_migration_simulation(
        self, tmp_path: Any, test_key_256: bytes
    ) -> None:
        import os
        from sqlalchemy import event, text
        db_file = os.path.join(str(tmp_path), "test_migration.db")
        temp_engine = create_engine(f"sqlite:///{db_file}")

        @event.listens_for(temp_engine, "connect")
        def _set_fk(dbapi_conn, _):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys = ON;")
            cursor.close()

        # 1. Initialize schema
        Base.metadata.create_all(bind=temp_engine)

        # 2. Verify PRAGMAs
        with temp_engine.connect() as conn:
            fk_status = conn.execute(text("PRAGMA foreign_keys;")).scalar()
            assert fk_status == 1

        # 3. Insert and verify data persistence in isolated SQLite file
        temp_session = sessionmaker(bind=temp_engine)()
        operator = OperatorORM(username="migrated_op", password_hash=hash_password("Pass1!"))
        opp = OpportunityORM(title="Migrated Opp", description="desc", category="digital")
        temp_session.add_all([operator, opp])
        temp_session.commit()

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
        temp_session.add(exp)
        temp_session.commit()

        # Deleting audited exp is blocked
        payload = create_sample_ticket_payload(experiment_id=str(exp.id), operator_id=str(operator.id))
        sig = sign_capability_ticket(test_key_256, payload)
        cap = MutationCapabilityORM(
            id=uuid.UUID(payload["ticket_id"]),
            operator_id=operator.id,
            experiment_id=exp.id,
            tier="ADSET",
            exact_operation="DEPLOY_RECOVERY_ADSET_DORMANT",
            budget_ceiling_amount=Decimal("200.00"),
            expected_parent_id="120252176243860380",
            replacement_name=payload["replacement_name"],
            target_endpoint="/act_1985595022114520/adsets",
            parameters_digest=payload["parameters_digest"],
            nonce=payload["nonce"],
            signature=sig,
            status="ISSUED",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        temp_session.add(cap)
        temp_session.commit()

        temp_session.delete(exp)
        with pytest.raises(IntegrityError):
            temp_session.commit()
        temp_session.rollback()

        temp_session.close()
        temp_engine.dispose()

