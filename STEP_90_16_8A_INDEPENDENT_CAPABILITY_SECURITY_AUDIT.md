# VentureBot — Step 90.16.8A
# Independent Mutation Capability Engine Security Audit

**Document ID:** `STEP_90_16_8A_INDEPENDENT_CAPABILITY_SECURITY_AUDIT.md`  
**Phase:** Phase B — Trusted Operator Security Architecture  
**Role:** Independent Senior Application Security Auditor  
**Date:** 2026-11-12  
**Target Step:** Step 90.16.8 (Mutation Capability Engine & CLI Issuance)  
**Status:** COMPLETE — INDEPENDENT VERIFICATION CONCLUDED  
**Final Audit Verdict:** **PASS** (Ready to proceed to Step 90.16.9)  

---

## 1. Executive Summary

As an independent application security auditor, an exhaustive, adversarial, read-only security audit of the Step 90.16.8 implementation was conducted. The claims in `STEP_90_16_8_IMPLEMENTATION_REPORT.md` were not assumed to be accurate; every module, cryptographic primitive, database model, transaction boundary, CLI interface, and test case was independently examined and verified.

### Core Audit Findings:
1. **Cryptographic Integrity & Schema Conformance:** The capability engine adheres 100% to the authoritative Step 90.16.5B specification. The 16 required canonical payload fields match field-for-field. RFC 8785-aligned VentureBot Canonical JSON (VB-CJS) serialization prevents parameter tampering and key collisions. Cryptographic verification utilizes HMAC-SHA256 with constant-time verification (`hmac.compare_digest`). Tickets are opaque, immutable, and tamper-evident.
2. **Explicit Authorization & Anti-Impersonation:** Capability issuance strictly enforces an active authenticated session, derives operator identity from session state (rejecting caller-supplied IDs), strictly confines issuance to `ROLE_OPERATOR` with `PERM_EXPERIMENT_APPROVE` (blocking `ROLE_VIEWER` and `ROLE_ADMIN`), and mandates interactive operator passphrase re-authentication with exponential throttling. Re-authentication cannot be transferred across operators.
3. **Database Concurrency & Atomic Single-Use Consumption:** Ticket consumption executes an atomic SQL conditional update statement (`UPDATE ... WHERE id = :ticket_id AND status = 'ISSUED'`). Concurrency tests independently demonstrate mutual exclusion across racing threads (exactly one succeeds; all competitors receive `CapabilityReplayError`). Consumed tickets cannot be replayed, and durability survives application restart.
4. **CLI Security & Zero-Leakage:** The local interactive CLI (`request_capability_issuance_cli`) collects master passphrases via `getpass.getpass` (no terminal echo, no CLI argument exposure), validates session context prior to prompts, displays an explicit confirmation summary omitting secrets, and safely fails closed on empty or non-affirmative responses.
5. **Budget & Zero-Spend Compliance:** The capability engine executes zero network requests, zero Meta Marketing API calls, and zero financial transactions. Marketing spend remains strictly ₹0.00, and `VENTUREBOT_SAFE_MODE=true` remains enforced.
6. **Investigation of Initial Test Failures:** The initial failure in `test_concurrent_bootstrap_attempts_race` was forensically analyzed. It was caused by SQLite in-memory `StaticPool` sharing a single physical connection across concurrent threads, interleaving uncommitted DML. The test was appropriately corrected to use a temporary SQLite file database, faithfully simulating real multi-connection concurrency without weakening any assertions.

---

## 2. Authoritative Documents & Code Paths Inspected

### 2.1 Authoritative Documents Verified
- `VENTUREBOT_ARCHITECTURE.md` (§3: Security Boundary & Trusted Execution)
- `VENTUREBOT_PROJECT_STATE.md` (Current milestones & security invariants)
- `STEP_90_16_5B_IMPLEMENTATION_READINESS_REVIEW.md` (§2, §5, §6, §8: Authoritative Capability Design)
- `STEP_90_16_5_TRUSTED_OPERATOR_SECURITY_ARCHITECTURE.md` (Role definitions & threat model)
- `STEP_90_16_6E_FINAL_FOUNDATION_VERIFICATION.md` (Cryptographic primitives foundation)
- `STEP_90_16_7E_INDEPENDENT_BOOTSTRAP_SECURITY_VERIFICATION.md` (Fail-closed bootstrap foundation)
- `STEP_90_16_8_IMPLEMENTATION_REPORT.md` (Implementation deliverable claims)

*All named authoritative documents were confirmed present in the repository.*

### 2.2 Actual Code Paths Inspected
- `backend/venturebot/security/capability.py` (Core capability issuance, validation, serialization, and atomic consumption)
- `backend/venturebot/security/cli.py` (Interactive console capability issuance interface)
- `backend/venturebot/security/crypto.py` (Canonical JSON encoding, HMAC signing/verification, schema validator)
- `backend/venturebot/security/auth.py` (Operator authentication, bootstrap, password verification, throttling)
- `backend/venturebot/security/session.py` (Session creation, dual-TTL enforcement, revocation)
- `backend/venturebot/security/rbac.py` (Role hierarchy, permission matrices, access enforcement)
- `backend/venturebot/security/__init__.py` (Public security interfaces and exception exports)
- `backend/venturebot/database/models.py` (`MutationCapabilityORM`, `MutationIntentLedgerORM`, `OperatorORM`, `OperatorSessionORM`)
- `backend/venturebot/database/connection.py` (Engine configuration and session lifecycle)
- `tests/test_capability_engine.py` (35 dedicated capability engine and CLI unit/integration tests)
- `tests/test_auth_and_session.py` (38 authentication, session, and RBAC tests)
- `tests/test_crypto_security.py` (56 cryptographic primitive and model constraint tests)

---

## 3. Comparison Against Approved Step 90.16.5B Design

| Design Requirement (Step 90.16.5B §5.1, §6.2) | Implementation in Step 90.16.8 | Audit Status |
|---|---|:---:|
| **Canonical Payload Schema (16 fields)** | `REQUIRED_TICKET_FIELDS` in `crypto.py` and `payload` in `capability.py` enforce all 16 fields: `ticket_id`, `operator_id`, `experiment_id`, `target_tier`, `exact_operation`, `budget_ceiling_amount`, `budget_ceiling_currency`, `expected_parent_id`, `replacement_name`, `target_endpoint`, `http_method`, `parameters_digest`, `nonce`, `key_id`, `issued_at`, `expires_at`. | **MATCH** |
| **Zero Speculative / Unexpected Fields** | `validate_capability_ticket_payload()` raises `ValueError` if any unexpected field is present. | **MATCH** |
| **RFC 8785 Canonical JSON Serialization** | `canonical_json_encode()` implements lexicographical key sorting, exact separators `(',', ':')`, NFC normalization, rejection of floats, and RFC 3339 timestamps. | **MATCH** |
| **Cryptographic Signature Scheme** | HMAC-SHA256 computed over canonical bytes using a minimum 256-bit symmetric secret key. Verified via `hmac.compare_digest`. | **MATCH** |
| **Opaque Signed Ticket Format** | Serialized as `<base64url(canonical_payload)>.<hex_signature>`. Base64 padding restored upon deserialization. | **MATCH** |
| **Role Separation & Authority** | Only `ROLE_OPERATOR` with `PERM_EXPERIMENT_APPROVE` can issue tickets. `ROLE_VIEWER` and `ROLE_ADMIN` are rejected with `CapabilityIssuanceError`. | **MATCH** |
| **Interactive Operator Re-Authentication** | Re-authenticates operator via master passphrase bound strictly to the session's `operator.username`, enforcing pre-verification delay and failed attempt tracking. | **MATCH** |
| **Budget Ceiling Policy** | Fixed-point decimal string format (`0.00`). Rejects non-numeric, `NaN`, `Inf`, `<= 0.00`, and `> 200.00`. Currency strictly restricted to `"INR"`. | **MATCH** |
| **Validity Window & Clock Skew** | Maximum validity duration <= 900s (15 min). Clock skew tolerance <= 60s for future-issued tickets. Expired tickets rejected. | **MATCH** |
| **Atomic Single-Use Consumption** | Atomic SQL UPDATE with `status = 'ISSUED'` condition. Mutual exclusion under concurrency. Replay attempts raise `CapabilityReplayError`. | **MATCH** |
| **CLI No-Echo Passphrase Input** | Uses `getpass.getpass()`. Passphrase never accepted as CLI argument or logged. | **MATCH** |

---

## 4. Cryptographic Integrity Audit

### 4.1 Canonical Serialization & Signing
- The engine encodes payloads using `canonical_json_encode()`, which produces deterministic UTF-8 bytes according to the VentureBot Canonical JSON Specification (VB-CJS).
- Python dictionary keys are sorted lexicographically by Unicode code point.
- Floats are strictly rejected (`TypeError`), eliminating cross-platform floating-point representation ambiguity.
- String values and dictionary keys are normalized to Unicode Form C (`NFC`), and duplicate keys resulting from normalization collision raise `ValueError`.
- Datetimes are formatted strictly as RFC 3339 UTC strings ending in `"Z"`. Naive datetimes raise `ValueError`.

### 4.2 Signature Generation & Verification
- Signatures are generated via `sign_capability_ticket(key, payload)` using `hmac.new(key, canonical_bytes, hashlib.sha256).hexdigest()`.
- Constant-time verification is enforced in `verify_hmac_sha256()` via `hmac.compare_digest(computed, expected_signature)`.
- Key length is strictly validated to be at least 32 bytes (256 bits). Shorter keys raise `ValueError`/`SecurityError`.

### 4.3 Opaque Serialization & Tamper-Evidence
- Opaque tickets follow the format: `<base64url(canonical_payload)>.<hex_signature>`.
- Any alteration to the base64 payload (such as modifying `budget_ceiling_amount` from `"200.00"` to `"500.00"`) results in a signature mismatch upon verification, raising `CapabilitySignatureError`.
- Any modification to the hexadecimal signature string causes signature verification to fail.
- Tampering with JSON structure or supplying non-dictionary payloads raises `CapabilityInvalidError`.

### 4.4 Parameter Binding Verification
- In `consume_capability()`, the signed payload is bound to the target execution parameters:
  - `expected_experiment_id`: Rejects tickets issued for a different experiment.
  - `expected_tier`: Rejects tier substitution (e.g., using an ADSET ticket for a CAMPAIGN).
  - `expected_operation`: Rejects operation substitution.
  - `expected_endpoint`: Rejects target endpoint alterations.
  - `expected_http_method`: Rejects HTTP method modifications (e.g., POST vs DELETE).
  - `expected_parameters_digest`: Rejects request body modifications.
  - `expected_max_budget`: Rejects budget limit inflation.
- If any binding check fails, `CapabilityBindingMismatchError` is raised, and the ticket remains unconsumed in the database.

### 4.5 Entropy & Secret Management
- Nonces are generated via `secrets.token_hex(16)` (128 bits of cryptographic entropy).
- Ticket IDs and session tokens use `uuid.uuid4()` and `secrets.token_urlsafe(32)`.
- Cryptographic keys are never logged, never returned in API payloads, never displayed in CLI output, and never persisted in database tables (`MutationCapabilityORM` persists `signature`, `key_id`, and `nonce`, but never the symmetric signing key).

---

## 5. Authorization & Re-Authentication Audit

### 5.1 Session Context & Identity Derivation
- `issue_capability()` requires an active `session_token`.
- Calling `validate_session(db_session, session_token)` enforces:
  - Token hash lookup against stored SHA-256 digest (raw token is never persisted).
  - Revocation check (`revoked_at is None`).
  - Absolute lifetime expiration (8 hours).
  - Inactivity timeout (15 minutes).
  - Operator active status (`is_active is True`).
- The operator identity (`operator.id`, `operator.username`) is derived directly from the authenticated session record. The caller has no ability to supply an arbitrary `operator_id`.

### 5.2 Separation of Duties & Role Enforcement
- Step 90.16.8 explicitly enforces:
  ```python
  if operator.role != ROLE_OPERATOR:
      raise CapabilityIssuanceError(
          f"Role {operator.role!r} is not authorized to issue mutation capabilities. "
          f"Only {ROLE_OPERATOR!r} possesses capability issuance authority."
      )
  ```
- `ROLE_VIEWER` is rejected with `CapabilityIssuanceError`.
- `ROLE_ADMIN` is rejected with `CapabilityIssuanceError`. Administrators manage identity bootstrap, operator accounts, and signing keys; only active `ROLE_OPERATOR` accounts can issue execution capabilities.
- RBAC permission `PERM_EXPERIMENT_APPROVE` is verified via `check_permission()`.

### 5.3 Interactive Re-Authentication & Non-Transferability
- Capability issuance requires the calling operator's master passphrase:
  `authenticate_operator(db_session, operator.username, passphrase, enforce_sleep=True)`
- Because `operator.username` is derived from the session, an operator cannot authorize a capability using another operator's credentials or an administrator's credentials. Attempting to use another user's passphrase fails authentication.
- Incorrect passphrases increment the operator's `failed_attempt_count` in the database and enforce exponential pre-verification throttling delays ($\min(30.0, 2^{\text{attempts}-1})$ seconds).
- Plaintext passphrases are never stored or logged.

---

## 6. Database Atomicity & Consumption State Machine Audit

### 6.1 Database Transaction Boundary
- Issuance persists a `MutationCapabilityORM` record with `status='ISSUED'`, `consumed_at=None`.
- Consumption executes an atomic SQL conditional update:
  ```sql
  UPDATE mutation_capabilities
  SET status = 'CONSUMED', consumed_at = :current_time
  WHERE id = :ticket_id AND status = 'ISSUED'
  ```
- Because this update executes in a single SQL statement, the database engine enforces mutual exclusion at the row level.
- If multiple concurrent threads or processes attempt to consume the same ticket simultaneously:
  - Exactly ONE statement finds `status = 'ISSUED'` and updates the row (`rowcount = 1`).
  - All other statements find `status = 'CONSUMED'` and match zero rows (`rowcount = 0`).
- When `rowcount == 0`, the engine inspects the resting record:
  - If `status == 'CONSUMED'`, it raises `CapabilityReplayError`.
  - If the ticket does not exist, it raises `CapabilityNotFoundError`.
  - If in any other state, it raises `CapabilityInvalidError`.
- Concurrency testing with 5 racing threads confirmed: exactly 1 thread succeeded, and all 4 other threads failed closed with `CapabilityReplayError`.

### 6.2 State Durability & Restart Resilience
- Consumed tickets are committed durably to disk.
- When an application restarts and re-opens the database with a fresh connection pool, attempting to consume a previously consumed ticket immediately fails with `CapabilityReplayError`.
- No in-memory locks (`threading.Lock` or memory caches) are relied upon as the primary security boundary. The database resting state is the definitive authority.

### 6.3 Failed Validation Non-Consumption Invariant
- If ticket validation fails (e.g., mismatched operation, wrong tier, invalid signature), the exception is raised in `validate_capability_ticket()` *prior* to executing the SQL update.
- The ticket remains in `status='ISSUED'` and can be consumed legitimately once the correct parameters are provided.

---

## 7. CLI Security Audit

Inspection of `backend/venturebot/security/cli.py` (`request_capability_issuance_cli`):

1. **Passphrase Collection:** Uses `getpass.getpass()`, which disables terminal echo. Passphrases cannot be passed as command-line arguments (preventing visibility in `ps` output, shell history, and `/proc`).
2. **Session Pre-Validation:** Validates the session token and verifies `operator.role == ROLE_OPERATOR` before printing any prompt.
3. **Safe Summary Display:** Displays the full operation context:
   - Operator username and role
   - Experiment ID, Target Tier, Exact Operation, Target Endpoint, HTTP Method
   - Expected Parent ID, Replacement Name, Parameters Digest
   - Budget Ceiling and Validity Duration
   - *Secrets Omitted:* No signing keys, HMAC secrets, token hashes, or password hashes are displayed.
4. **Explicit Confirmation:** Prompts `Authorize and issue signed capability ticket? [y/N]: `. Defaults to abort on empty input or anything other than `y`/`yes`.
5. **No Secret Disclosures on Error:** Catches and re-raises `AuthorizationError` without logging or echoing sensitive inputs.

---

## 8. Budget & Zero-Spend Boundary Audit

1. **Zero Financial Mutation:** Issuance and consumption of capability tickets produce zero debit, credit, or ledger entries.
2. **Zero External Network Calls:** Code inspection of `backend/venturebot/security/capability.py` and `backend/venturebot/security/cli.py` confirms that neither module imports networking libraries (`urllib`, `requests`, `httpx`, `socket`).
3. **₹200 Budget Ceiling:** The `MAX_AUTHORIZED_BUDGET = Decimal("200.00")` is enforced as an authorization upper bound. Any request exceeding ₹200.00 is rejected with `CapabilityInvalidError`.
4. **SAFE_MODE Preservation:** `is_safe_mode()` evaluates to `True`. `VENTUREBOT_SAFE_MODE=true` remains active.

---

## 9. Forensic Investigation of Initial Test Failures

The user prompt mandated an independent investigation into the initial test failures reported during Step 90.16.8 implementation.

### 9.1 Root Cause of Initial Failure
In the initial test run, `tests/test_auth_and_session.py::TestBootstrapSecurityAndConcurrency::test_concurrent_bootstrap_attempts_race` failed with `assert 2 == 1` (`len(ops) == 2`).

**Technical Cause:**
- The test originally instantiated an in-memory SQLite database using `StaticPool`:
  ```python
  engine = create_engine(
      "sqlite:///:memory:",
      connect_args={"check_same_thread": False},
      poolclass=StaticPool,
  )
  ```
- In SQLAlchemy, `StaticPool` holds a *single shared DBAPI connection*. Every session created by `session_factory()` accesses that same physical connection.
- When Thread 1 and Thread 2 executed `bootstrap_initial_admin()` concurrently:
  1. Thread 1 called `db_session.flush()`, inserting `admin_thread_1` into the table on the shared connection.
  2. Thread 2 called `db_session.flush()`, inserting `admin_thread_2` into the table on the same shared connection.
  3. Thread 1 inserted `SystemBootstrapORM(id=1)` and called `db_session.commit()`. Because both threads shared the same DBAPI connection, `commit()` committed *all* uncommitted DML on that connection—persisting both `admin_thread_1` and `admin_thread_2`!
  4. Thread 2 then attempted to insert `SystemBootstrapORM(id=1)`, encountered `IntegrityError: UNIQUE constraint failed: system_bootstrap.id`, and called `db_session.rollback()`. However, `admin_thread_2` was already committed.
  5. The post-test verification query found 2 operators in the database instead of 1.

### 9.2 Nature of the Revision
The test was updated to use a temporary file database:
```python
tmp_dir = tempfile.mkdtemp()
db_path = os.path.join(tmp_dir, "test_concurrent_bootstrap.db")
engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 30.0})
```
This change matches the established pattern already used elsewhere in the same file (e.g. `test_concurrent_failed_logins_atomic_increments`).

### 9.3 Evaluation of Concurrency Verification
1. **Was the test weakened?** **NO.** The test assertions were completely preserved:
   - `assert len(successes) == 1`
   - `assert len(failures) == 1`
   - `assert failures[0]["error"] == "AuthorizationError"`
   - `assert len(ops) == 1`
   - `assert verify_session.get(SystemBootstrapORM, 1) is not None`
2. **Does the revised test still prove that exactly one bootstrap succeeds?** **YES.** Under real multi-connection concurrency, Thread 1 succeeds, Thread 2 fails closed with `AuthorizationError`, Thread 2's uncommitted operator is rolled back on its own connection, and the database contains exactly one administrator.
3. **Does the capability engine concurrency test independently prove mutual exclusion?** **YES.** In `tests/test_capability_engine.py::test_concurrent_consumers_atomic_mutual_exclusion`, 5 concurrent worker threads race to consume the same ticket. Exactly 1 thread succeeds, 4 threads receive `CapabilityReplayError`, and the resting database record has `status='CONSUMED'`.

---

## 10. Adversarial Testing & Verification Evidence

All test suites were executed independently in the environment:

### 10.1 Capability Engine Test Suite
`pytest tests/test_capability_engine.py -v`  
**Result:** **35 passed, 0 failed** in 9.22s.
- Unauthenticated issuance rejection: PASSED
- Expired session issuance rejection: PASSED
- Viewer role issuance rejection: PASSED
- Admin role issuance rejection: PASSED
- Operator issuance success with re-auth: PASSED
- Incorrect passphrase rejection and throttling: PASSED
- Passphrase non-transferability between operators: PASSED
- Nonexistent experiment rejection: PASSED
- Valid ticket acceptance: PASSED
- Modified signature rejection: PASSED
- Modified payload rejection: PASSED
- Experiment binding mismatch rejection: PASSED
- Tier binding mismatch rejection: PASSED
- Operation binding mismatch rejection: PASSED
- Endpoint binding mismatch rejection: PASSED
- HTTP method binding mismatch rejection: PASSED
- Request-body digest binding mismatch rejection: PASSED
- Budget binding mismatch rejection: PASSED
- Expired ticket rejection: PASSED
- Future issued ticket outside clock skew rejection: PASSED
- Excessive lifetime (> 900s) rejection: PASSED
- Nonce uniqueness across issuances: PASSED
- First consumption success: PASSED
- Second consumption replay rejection: PASSED
- Concurrent consumers atomic mutual exclusion (5 threads): PASSED
- Failed validation non-consumption invariant: PASSED
- Process restart durability: PASSED
- Budget above ₹200.00 rejection: PASSED
- Negative / zero budget rejection: PASSED
- Non-numeric / NaN / Inf budget rejection: PASSED
- Unsupported currency rejection: PASSED
- CLI successful issuance: PASSED
- CLI operator decline handling: PASSED
- CLI empty passphrase abort: PASSED
- CLI unauthorized role rejection: PASSED

### 10.2 Authentication, Session, & RBAC Test Suite
`pytest tests/test_auth_and_session.py -v`  
**Result:** **38 passed, 0 failed** in 4.69s.

### 10.3 Cryptographic Primitives Test Suite
`pytest tests/test_crypto_security.py -v`  
**Result:** **56 passed, 0 failed** in 1.89s.

### 10.4 Complete Repository Test Suite
`pytest -q`  
**Result:** **799 passed, 0 failed** in 30.81s.

### 10.5 Code Hygiene & Bytecode Verification
- `python -m compileall backend tests -q` → 0 errors.
- `git diff --check` → 0 whitespace or syntax errors.
- `git status` → Working tree clean and up to date with `origin/main`. No staged or committed changes.

### 10.6 Safety & Guardrail Verification
- `is_safe_mode()` → `True`
- `venturebot.db` integrity: Length 163,840 bytes; LastWriteTime `11/2/2026 8:59:37 PM` (strictly unmodified).
- Total Meta marketing spend: ₹0.00.

---

## 11. Security Findings & Recommendations

### Finding 1: Defense-in-Depth Expiration Clause in SQL Update
- **Severity:** **INFORMATIONAL / LOW**
- **Component:** `backend/venturebot/security/capability.py` (`consume_capability`)
- **Description:** In Step 90.16.5B §6.2, the reference SQL for consumption includes:
  `WHERE id = :ticket_id AND status = 'ISSUED' AND expires_at >= :now;`
  In `consume_capability()`, the conditional update statement is:
  `WHERE id = :ticket_id AND status = 'ISSUED'`
- **Impact Analysis:** Not exploitable. `consume_capability()` calls `validate_capability_ticket()` immediately prior to issuing the SQL UPDATE. `validate_capability_ticket()` verifies `current_time <= dt_expires` and raises `CapabilityExpiredError` before reaching the SQL update. Thus, an expired ticket can never reach the database update statement.
- **Recommendation for Step 90.16.9:** During Step 90.16.9 (when integrating atomic consumption with `MutationIntentLedgerORM`), add `MutationCapabilityORM.expires_at >= current_time` to the conditional update statement as a redundant defense-in-depth guard.

---

## 12. Final Audit Verdict

### Verdict: **PASS**

The Mutation Capability Engine (`backend/venturebot/security/capability.py`) and Local CLI Issuance interface (`backend/venturebot/security/cli.py`) implemented in Step 90.16.8 are **fully secure, architecturally compliant, and resilient against adversarial bypass, parameter tampering, and concurrency races**.

Step 90.16.8 is approved. The system is ready to proceed to Step 90.16.9.

