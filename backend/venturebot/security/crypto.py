"""Cryptographic signing primitives and secure hashing for VentureBot.

This module provides standard-library cryptographic operations for Phase B
Trusted Operator security architecture (Step 90.16.5B design authority):
- CSPRNG random nonces and session tokens
- PHC-compliant scrypt password hashing and constant-time verification
- VentureBot Canonical JSON Specification (VB-CJS) deterministic encoding
- SHA-256 request digest generation
- HMAC-SHA256 capability ticket signing and constant-time verification

Key-Management Assumptions:
- In production, signing keys are 256-bit symmetric secrets stored in a secure local vault
  and loaded into memory during execution. Keys are never hardcoded or committed to git.
- For testing and verification, ephemeral test keys are generated in isolated memory spaces.
- No production keys are generated or exposed within this module.
- Plaintext passwords, raw session tokens, and master keys are never logged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

# Approved scrypt parameters per Step 90.16.5A (§3.1) and Step 90.16.5B (§10)
SCRYPT_N: int = 16384  # 2^14 (CPU/memory cost parameter)
SCRYPT_R: int = 8      # Block size parameter
SCRYPT_P: int = 1      # Parallelization parameter
SCRYPT_MAXMEM: int = 67108864  # 64 MiB limit
SCRYPT_DKLEN: int = 32         # 256-bit derived key length
SCRYPT_SALT_BYTES: int = 32    # 256 bits of salt entropy

# Maximum capability ticket validity duration (Step 90.16.5B §5.1 & §8)
MAX_TICKET_VALIDITY_WINDOW: timedelta = timedelta(minutes=15)

# Canonical ticket field specification (Step 90.16.5B §5.1)
REQUIRED_TICKET_FIELDS: frozenset[str] = frozenset({
    "budget_ceiling_amount",
    "budget_ceiling_currency",
    "exact_operation",
    "expected_parent_id",
    "experiment_id",
    "expires_at",
    "http_method",
    "issued_at",
    "key_id",
    "nonce",
    "operator_id",
    "parameters_digest",
    "replacement_name",
    "target_endpoint",
    "target_tier",
    "ticket_id",
})

VALID_TICKET_TIERS: frozenset[str] = frozenset({"CAMPAIGN", "ADSET", "CREATIVE", "AD"})
VALID_HTTP_METHODS: frozenset[str] = frozenset({"POST", "GET", "DELETE", "PUT", "PATCH"})
RFC3339_REGEX: re.Pattern[str] = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
AMOUNT_REGEX: re.Pattern[str] = re.compile(r"^\d+\.\d{2}$")
HEX64_REGEX: re.Pattern[str] = re.compile(r"^[0-9a-f]{64}$")
HEX32_PLUS_REGEX: re.Pattern[str] = re.compile(r"^[0-9a-f]{32,}$")


def _b64decode_lenient(encoded_str: str) -> bytes:
    """Decode base64 string handling optional missing padding."""
    padding = (4 - len(encoded_str) % 4) % 4
    return base64.b64decode(encoded_str + "=" * padding)


def generate_nonce(length_bytes: int = 16) -> str:
    """Generate a cryptographically secure random hexadecimal nonce.

    Args:
        length_bytes: Minimum 16 bytes (128 bits). Default 16.

    Returns:
        Hexadecimal string of length at least 32 characters.
    """
    if length_bytes < 16:
        raise ValueError("Nonce length must be at least 16 bytes (128 bits)")
    return secrets.token_hex(length_bytes)


def generate_secure_token(nbytes: int = 32) -> str:
    """Generate a cryptographically secure URL-safe random token.

    Args:
        nbytes: Minimum 32 bytes (256 bits). Default 32.

    Returns:
        URL-safe string suitable for session tokens.
    """
    if nbytes < 32:
        raise ValueError("Secure token length must be at least 32 bytes (256 bits)")
    return secrets.token_urlsafe(nbytes)


def generate_sha256_digest(data: bytes | str) -> str:
    """Compute a SHA-256 hexadecimal digest over input bytes or string.

    Args:
        data: Raw bytes or UTF-8 string to hash.

    Returns:
        64-character lowercase hexadecimal SHA-256 digest.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    elif not isinstance(data, bytes):
        raise TypeError(f"Digest input must be bytes or str, got {type(data).__name__}")
    return hashlib.sha256(data).hexdigest()


def hash_password(
    password: str,
    *,
    n: int = SCRYPT_N,
    r: int = SCRYPT_R,
    p: int = SCRYPT_P,
    maxmem: int = SCRYPT_MAXMEM,
    dklen: int = SCRYPT_DKLEN,
    salt: bytes | None = None,
) -> str:
    """Hash an operator password using standard-library scrypt and PHC string format.

    Format: $scrypt$ln=<ln>,r=<r>,p=<p>$<base64_salt>$<base64_hash>

    Args:
        password: Plaintext password string.
        n: CPU/memory cost parameter (power of 2, default 16384 = 2^14).
        r: Block size (default 8).
        p: Parallelization (default 1).
        maxmem: Memory limit in bytes (default 64 MiB).
        dklen: Derived key length in bytes (default 32).
        salt: Optional salt bytes (minimum 16 bytes; default 32 random bytes).

    Returns:
        PHC-formatted string suitable for database storage.
    """
    if not isinstance(password, str) or not password:
        raise ValueError("Password must be a non-empty string")

    if salt is None:
        salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    elif len(salt) < 16:
        raise ValueError("Salt must be at least 16 bytes")

    if n <= 1 or (n & (n - 1)) != 0:
        raise ValueError(f"scrypt N parameter must be a power of 2 greater than 1, got {n}")

    ln = int(math.log2(n))
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        maxmem=maxmem,
        dklen=dklen,
    )

    salt_b64 = base64.b64encode(salt).decode("ascii")
    hash_b64 = base64.b64encode(derived).decode("ascii")

    return f"$scrypt$ln={ln},r={r},p={p}${salt_b64}${hash_b64}"


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext password against a stored PHC scrypt hash in constant time.

    Fails closed (returns False) on any formatting error, unsupported parameter,
    or mismatch.

    Args:
        password: Candidate password string.
        password_hash: Stored PHC string ($scrypt$ln=...,r=...,p=...$salt$hash).

    Returns:
        True if password matches, False otherwise.
    """
    if not isinstance(password, str) or not isinstance(password_hash, str):
        return False
    if not password or not password_hash:
        return False

    parts = password_hash.split("$")
    # Expected: ["", "scrypt", "ln=14,r=8,p=1", "<salt>", "<hash>"]
    if len(parts) != 5 or parts[0] != "" or parts[1] != "scrypt":
        return False

    try:
        param_dict: dict[str, int] = {}
        for item in parts[2].split(","):
            k, v = item.split("=", 1)
            param_dict[k.strip()] = int(v.strip())

        ln = param_dict["ln"]
        r = param_dict["r"]
        p = param_dict["p"]

        # Enforce safe parameter bounds: N in [1024, 65536], r >= 1, p >= 1
        if ln < 10 or ln > 16 or r < 1 or p < 1:
            return False

        n = 1 << ln
        salt = _b64decode_lenient(parts[3])
        expected_hash = _b64decode_lenient(parts[4])

        if len(salt) < 16 or len(expected_hash) < 16:
            return False

        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            maxmem=SCRYPT_MAXMEM,
            dklen=len(expected_hash),
        )

        return hmac.compare_digest(derived, expected_hash)
    except Exception:
        return False


def _canonicalize_value(val: Any) -> Any:
    """Recursively canonicalize values according to VentureBot Canonical JSON Specification (VB-CJS).

    Specification Details (VB-CJS):
    1. Object Key Ordering: Lexicographical sorting of keys by Unicode scalar values (code points).
       Note on RFC 8785 divergence: Standard RFC 8785 §3.2.3 specifies UTF-16 code unit ordering;
       VB-CJS uses standard Python code point sorting, matching UTF-16 for the Basic Multilingual Plane (<= U+FFFF).
    2. Separators: Exact separators (',', ':') with zero whitespace.
    3. Number Handling: Floating-point numbers are strictly prohibited (raise TypeError) to eliminate
       precision and rounding ambiguity across architectures. Fixed-point Decimal or strings are enforced.
       Note on RFC 8785 divergence: Standard RFC 8785 §3.2.2.3 permits IEEE-754 floats; VB-CJS forbids them.
    4. String Normalization: All strings are proactively normalized to Unicode Normalization Form C (NFC).
       If distinct object keys normalize to the same NFC representation, canonicalization strictly raises
       ValueError to prevent silent key collision or value erasure.
       Note on RFC 8785 divergence: Standard RFC 8785 §3.2.1 does not perform Unicode normalization.
    5. Datetimes: Timezone-aware UTC datetimes formatted as RFC 3339 strings ending in 'Z'.
       Naive datetimes are strictly rejected (raise ValueError).
    6. Identifiers & Decimals: Decimals and UUIDs are formatted deterministically as strings.
    """
    if isinstance(val, float):
        raise TypeError(
            f"Floating-point values are strictly prohibited in canonical JSON: {val!r}. "
            "Use Decimal or fixed-point strings."
        )
    if isinstance(val, Decimal):
        return str(val)
    if isinstance(val, datetime):
        if val.tzinfo is None:
            raise ValueError(
                f"Naive datetime values are forbidden in canonical JSON: {val!r}. "
                "Must be timezone-aware UTC."
            )
        val_utc = val.astimezone(timezone.utc)
        if val_utc.microsecond == 0:
            return val_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"{val_utc.strftime('%Y-%m-%dT%H:%M:%S')}.{val_utc.microsecond:06d}Z"
    if isinstance(val, uuid.UUID):
        return str(val)
    if isinstance(val, str):
        return unicodedata.normalize("NFC", val)
    if isinstance(val, dict):
        normalized_map: dict[str, tuple[str, Any]] = {}
        for k, v in val.items():
            if not isinstance(k, str):
                raise TypeError(
                    f"Dictionary keys in canonical JSON must be strings, got {type(k).__name__}: {k!r}"
                )
            norm_k = unicodedata.normalize("NFC", k)
            if norm_k in normalized_map:
                orig_k, _ = normalized_map[norm_k]
                raise ValueError(
                    f"Duplicate object key after NFC normalization: {norm_k!r} "
                    f"(collision between distinct keys {orig_k!r} and {k!r})"
                )
            normalized_map[norm_k] = (k, _canonicalize_value(v))
        return {k: v for k, (_, v) in sorted(normalized_map.items(), key=lambda item: item[0])}
    if isinstance(val, (list, tuple)):
        return [_canonicalize_value(item) for item in val]
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val
    if val is None:
        return None
    raise TypeError(f"Unsupported type for canonical JSON serialization: {type(val).__name__}")


def canonical_json_encode(payload: Any) -> bytes:
    """Encode a Python structure to deterministic VentureBot Canonical JSON Specification (VB-CJS) bytes.

    VB-CJS Rules:
    1. Lexicographical key sorting by Unicode code point.
    2. Exact separators: (',', ':') with zero whitespace.
    3. Floating-point numbers strictly prohibited (TypeError).
    4. Strings normalized to Unicode Normalization Form C (NFC).
    5. Datetimes formatted as RFC 3339 UTC strings ending in 'Z'.
    6. Returns UTF-8 encoded bytes.
    """
    canonical_obj = _canonicalize_value(payload)
    json_str = json.dumps(
        canonical_obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return json_str.encode("utf-8")


def canonical_json_dumps(payload: Any) -> str:
    """Encode a Python structure to deterministic VentureBot Canonical JSON Specification (VB-CJS) string."""
    return canonical_json_encode(payload).decode("utf-8")


def sign_hmac_sha256(key: bytes, data: bytes | str) -> str:
    """Compute HMAC-SHA256 signature over data using a 256-bit symmetric secret key.

    Args:
        key: Secret key bytes (minimum 32 bytes / 256 bits).
        data: Bytes or string to sign.

    Returns:
        64-character lowercase hexadecimal signature.
    """
    if not isinstance(key, bytes):
        raise TypeError(f"Signing key must be bytes, got {type(key).__name__}")
    if len(key) < 32:
        raise ValueError("Signing key must be at least 32 bytes (256 bits)")

    if isinstance(data, str):
        data = data.encode("utf-8")
    elif not isinstance(data, bytes):
        raise TypeError(f"Payload to sign must be bytes or str, got {type(data).__name__}")

    return hmac.new(key, data, hashlib.sha256).hexdigest()


def verify_hmac_sha256(key: bytes, data: bytes | str, expected_signature: str) -> bool:
    """Verify HMAC-SHA256 signature in constant time.

    Args:
        key: Secret key bytes (minimum 32 bytes).
        data: Bytes or string to verify.
        expected_signature: Hexadecimal signature string to verify against.

    Returns:
        True if signature is valid, False otherwise.
    """
    if not isinstance(expected_signature, str) or not expected_signature:
        return False
    try:
        computed = sign_hmac_sha256(key, data)
        return hmac.compare_digest(computed, expected_signature)
    except Exception:
        return False


def validate_capability_ticket_payload(payload: dict[str, Any]) -> None:
    """Validate that a capability ticket payload strictly conforms to canonical schema.

    Enforces:
    - Presence of all 16 required canonical fields.
    - Total absence of unexpected or speculative fields.
    - Valid data types and formatting (UUIDs, timestamps, amounts, endpoints).

    Raises:
        ValueError or TypeError on schema deviation.
    """
    if not isinstance(payload, dict):
        raise TypeError(f"Ticket payload must be a dict, got {type(payload).__name__}")

    present_keys = set(payload.keys())
    missing_keys = REQUIRED_TICKET_FIELDS - present_keys
    if missing_keys:
        raise ValueError(f"Missing required ticket field(s): {sorted(missing_keys)}")

    unexpected_keys = present_keys - REQUIRED_TICKET_FIELDS
    if unexpected_keys:
        raise ValueError(f"Unexpected field(s) in capability ticket payload: {sorted(unexpected_keys)}")

    # 1. UUID validations
    for uuid_field in ("ticket_id", "operator_id", "experiment_id"):
        val = payload[uuid_field]
        if not isinstance(val, str):
            raise TypeError(f"Field '{uuid_field}' must be a UUID string, got {type(val).__name__}")
        try:
            uuid.UUID(val)
        except ValueError as err:
            raise ValueError(f"Field '{uuid_field}' is not a valid UUID: {val!r}") from err

    # 2. Tier validation
    tier = payload["target_tier"]
    if tier not in VALID_TICKET_TIERS:
        raise ValueError(f"Invalid target_tier {tier!r}. Must be one of {sorted(VALID_TICKET_TIERS)}")

    # 3. HTTP method validation
    method = payload["http_method"]
    if method not in VALID_HTTP_METHODS:
        raise ValueError(f"Invalid http_method {method!r}. Must be one of {sorted(VALID_HTTP_METHODS)}")

    # 4. Budget ceiling validations
    amount = payload["budget_ceiling_amount"]
    if not isinstance(amount, str) or not AMOUNT_REGEX.match(amount):
        raise ValueError(
            f"Field 'budget_ceiling_amount' must be a fixed-point decimal string ('0.00'), got {amount!r}"
        )
    if Decimal(amount) <= Decimal("0.00"):
        raise ValueError(f"Field 'budget_ceiling_amount' must be greater than zero, got {amount!r}")

    currency = payload["budget_ceiling_currency"]
    if not isinstance(currency, str) or len(currency) < 3:
        raise ValueError(f"Field 'budget_ceiling_currency' must be a valid currency string, got {currency!r}")

    # 5. Endpoint and replacement name
    endpoint = payload["target_endpoint"]
    if not isinstance(endpoint, str) or not endpoint.startswith("/"):
        raise ValueError(f"Field 'target_endpoint' must start with '/', got {endpoint!r}")

    replacement_name = payload["replacement_name"]
    if not isinstance(replacement_name, str) or not replacement_name.strip():
        raise ValueError("Field 'replacement_name' must be a non-empty string")

    exact_op = payload["exact_operation"]
    if not isinstance(exact_op, str) or not exact_op.strip():
        raise ValueError("Field 'exact_operation' must be a non-empty string")

    parent_id = payload["expected_parent_id"]
    if not isinstance(parent_id, str) or not parent_id.isdigit():
        raise ValueError(f"Field 'expected_parent_id' must be numeric ID string, got {parent_id!r}")

    # 6. Nonce and digest validations
    nonce = payload["nonce"]
    if not isinstance(nonce, str) or not HEX32_PLUS_REGEX.match(nonce):
        raise ValueError(f"Field 'nonce' must be a lowercase hex string of at least 32 chars, got {nonce!r}")

    digest = payload["parameters_digest"]
    if not isinstance(digest, str) or not HEX64_REGEX.match(digest):
        raise ValueError(f"Field 'parameters_digest' must be a 64-char lowercase SHA-256 hex string, got {digest!r}")

    key_id = payload["key_id"]
    if not isinstance(key_id, str) or not key_id.strip():
        raise ValueError("Field 'key_id' must be a non-empty string")

    # 7. Timestamp validations and duration bounds
    parsed_timestamps: dict[str, datetime] = {}
    for ts_field in ("issued_at", "expires_at"):
        val = payload[ts_field]
        if not isinstance(val, str) or not RFC3339_REGEX.match(val):
            raise ValueError(f"Field '{ts_field}' must be an RFC 3339 UTC string ending in 'Z', got {val!r}")
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        except ValueError as err:
            raise ValueError(f"Field '{ts_field}' is not a valid ISO datetime: {val!r}") from err
        if dt.tzinfo is None:
            raise ValueError(f"Field '{ts_field}' must be timezone-aware UTC, got naive datetime")
        parsed_timestamps[ts_field] = dt.astimezone(timezone.utc)

    dt_issued = parsed_timestamps["issued_at"]
    dt_expires = parsed_timestamps["expires_at"]

    if dt_expires <= dt_issued:
        raise ValueError(
            f"Ticket 'expires_at' ({payload['expires_at']}) must be strictly after "
            f"'issued_at' ({payload['issued_at']})"
        )

    validity_window = dt_expires - dt_issued
    if validity_window > MAX_TICKET_VALIDITY_WINDOW:
        raise ValueError(
            f"Ticket validity window of {validity_window} exceeds maximum allowed period of {MAX_TICKET_VALIDITY_WINDOW}"
        )


def sign_capability_ticket(key: bytes, payload: dict[str, Any]) -> str:
    """Validate ticket payload, serialize canonically, and compute HMAC-SHA256 signature."""
    validate_capability_ticket_payload(payload)
    canonical_bytes = canonical_json_encode(payload)
    return sign_hmac_sha256(key, canonical_bytes)


def verify_capability_ticket(key: bytes, payload: dict[str, Any], signature: str) -> bool:
    """Validate ticket payload, serialize canonically, and verify HMAC-SHA256 signature."""
    try:
        validate_capability_ticket_payload(payload)
        canonical_bytes = canonical_json_encode(payload)
        return verify_hmac_sha256(key, canonical_bytes, signature)
    except Exception:
        return False

