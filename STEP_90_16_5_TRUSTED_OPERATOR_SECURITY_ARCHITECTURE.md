# VentureBot — Step 90.16.5: Trusted Operator Security Architecture Design

**Document ID:** `STEP_90_16_5_TRUSTED_OPERATOR_SECURITY_ARCHITECTURE.md`  
**Phase:** Phase B — Security Architecture Specification (Design-Only)  
**Repository:** `E:\Project Folder\venturebot`  
**Current Commit Baseline:** `a94a1b920eabfa608e7ddbcefcff0ccea474dfe8`  
**Author:** Vishnu / Antigravity Autonomous Security Architecture  
**Execution Status:** DESIGN ONLY — Zero code modified, zero migrations executed, zero Meta mutations.

---

## 1. Executive Summary

Following the successful implementation, testing, and remote synchronization of **Phase A (Legacy Gateway Hard-Block and Granular Recovery Decomposition)** in Step 90.16.4, this document formally specifies the architecture for **Phase B: Trusted Operator Authentication, Role-Based Access Control, Cryptographic Mutation Capabilities, and Crash-Safe Intent Ledgers**.

### Core Vulnerabilities in the Current Baseline:
1. **Absence of Operator Identity:** VentureBot currently has NO authentication provider, session manager, credential store, or cryptographic identity verification.
2. **Untrusted Identity Flags:** Parameters such as `explicit_auth: bool = True` and `operator_id: str = "human"` are untrusted caller-supplied values that provide zero defense against unauthorized or automated local execution.
3. **Missing Mutation Intent Ledger:** While concurrency locks (`claim_deployment_tier`) prevent concurrent duplicate creation, there is no persistent pre-dispatch intent record. If a process crashes after sending an HTTP request to Meta but before receiving or persisting the response, the system enters an ambiguous state without an auditable two-phase transaction boundary.
4. **Direct Adapter Invocation Exposure:** While `ExecutionDispatchService.dispatch()` has been hard-blocked, `MetaMarketingApiAdapter` methods could theoretically be called directly by internal application scripts if `SAFE_MODE` were lowered.

### Phase B Security Objectives:
- Establish a **cryptographically verified local human operator identity** using memory-hard password hashing and session tokens.
- Implement **single-use, short-lived, operation-bound cryptographic approval tickets (`MutationCapability`)** signed with HMAC-SHA256 / Ed25519.
- Enforce a **crash-safe two-phase mutation state machine** that separates the local database transaction boundary from external network I/O.
- Transition `explicit_auth: bool` to mandatory `MutationCapability` ticket validation across all mutation boundaries.
- Maintain **`VENTUREBOT_SAFE_MODE=true`** as the outer defense-in-depth shield throughout the entire design and implementation lifecycle.

---

## 2. Task 1 — Audit of Existing Security Components

An exhaustive audit of the VentureBot codebase at commit `a94a1b9` identified the following concrete security implementations, boundary points, and gaps:

### 2.1 Existing Authentication Modules
- **Findings:** **NONE.**
- **File Paths Audited:** `backend/venturebot/` contains zero authentication, session management, or user identity files.
- **Database Schema:** `backend/venturebot/database/models.py` contains models for capital transactions, opportunities, experiments, decisions, external executions, and deployment claims, but **zero tables for users, operators, passwords, or sessions**.

### 2.2 Existing Authorization Checks
- **File:** `backend/venturebot/execution/dispatch.py`
- **Mechanism:** Parameterized `explicit_auth: bool` argument present on all recovery and execution endpoints:
  - `deploy_recovery_adset_dormant()` (line 717, defaults to `False`)
  - `deploy_recovery_creative_dormant()` (line 1147, defaults to `False`)
  - `deploy_recovery_ad_dormant()` (line 1421, defaults to `False`)
  - `deploy_recovery_dormant()` (line 1714, defaults to `False`)
  - `activate_recovery_flight()` (line 2411, defaults to `False`)
  - `emergency_pause_adset()` (line 1932, defaults to `False`)
  - `emergency_stop()` (line 549, **defaults to `True`**)
- **Flaw:** Every check evaluates `if not explicit_auth: return ... EXPLICIT_AUTHORIZATION_REQUIRED`. Because this is a standard Python function parameter, any local caller, script, or test can pass `explicit_auth=True` with zero cryptographic proof of identity.

### 2.3 Current `operator_id` Usage
- **File:** `backend/venturebot/execution/dispatch.py`
- **Mechanism:**
  - `record_recovery_version_authorization()` (lines 634–660): Validates that `operator_id` is a non-empty string and records an `ExternalExecutionEvent` with `event_metadata={"operator_id": clean_op, "authorized_version": version}`.
  - `deploy_recovery_adset_dormant()` (lines 867–875):
    ```python
    operator_id = payload.get("operator_id") or "UNKNOWN"
    return RecoveryDormantResult(
        success=False,
        blocked=True,
        reason=(
            f"UNTRUSTED_OPERATOR_AUTHENTICATION: Authorized recovery version {version} exceeds "
            f"maximum operational pilot baseline (version 2). Operator identity '{operator_id}' "
            "is caller-supplied and cannot be verified without a cryptographic identity provider or RBAC subsystem."
        ),
        experiment_id=experiment_id,
    )
    ```
- **Flaw:** For recovery version 2, `operator_id` is recorded in audit logs without verification. For version > 2, it fails closed specifically because the system recognizes `operator_id` as unauthenticated.

### 2.4 Existing Mutation Adapter Methods
- **File:** `backend/venturebot/execution/meta.py`
- **Class:** `MetaMarketingApiAdapter`
- **Mutation Methods:**
  - `create_campaign()` (line 580)
  - `create_adset()` (line 610)
  - `upload_image()` (line 650)
  - `create_creative()` (line 675)
  - `create_ad()` (line 700)
  - `pause_campaign()` (line 725)
  - `pause_adset()` (line 760)
  - `unpause_adset()` (line 780)
  - `pause_ad()` (line 810)
  - `unpause_ad()` (line 825)
  - `unpause_campaign()` (line 800)
  - `update_adset_schedule()` (line 740)

### 2.5 Existing SAFE_MODE Enforcement Points
1. **`backend/venturebot/env.py` (`is_safe_mode()`):**
   - Reads `VENTUREBOT_SAFE_MODE` from environment.
   - Defaults to `True` (fail-closed) if unset, unparseable, or empty.
2. **`backend/venturebot/execution/dispatch.py` (`is_safe_mode_enforced()`):**
   - Lines 206–223: Evaluates environment and ensures caller-supplied `safe_mode=False` cannot override an active environment guard.
   - Checked at entry in `dispatch()`, `deploy_recovery_adset_dormant()`, `deploy_recovery_creative_dormant()`, `deploy_recovery_ad_dormant()`, `activate_recovery_flight()`, `emergency_pause_adset()`, and `_emergency_stop()`.
3. **`backend/venturebot/execution/meta.py` (`_execute_request()`):**
   - Lines 440–460: Unconditional adapter-level interception:
     ```python
     method = req.get_method().upper() if req.get_method() else "GET"
     is_mutation = is_write or method in ("POST", "PUT", "DELETE", "PATCH")
     if is_mutation:
         if is_safe_mode():
             raise MetaApiSafetyError("Live Meta write operations are strictly disabled...")
         if self._transport is None:
             raise MetaApiError("Live Meta write operations are strictly disabled in Step 36...")
     ```

### 2.6 Existing Deployment Claim and Concurrency Logic
- **Files:** `backend/venturebot/database/models.py`, `backend/venturebot/execution/dispatch.py`
- **Primate Primitives:**
  - `DeploymentClaimORM`: Database table `deployment_claims` with `UniqueConstraint("experiment_id", "tier", name="uq_deployment_claims_exp_tier")`.
  - `_TIER_THREAD_LOCKS`: Process-wide in-memory mutexes.
  - `claim_deployment_tier`: Transactional context manager providing mutual exclusion, double-checked locking, and 60-second TTL abandoned claim auto-recovery.

### 2.7 Existing Audit / Event Persistence
- **Files:** `backend/venturebot/database/models.py`, `backend/venturebot/database/repositories/external_execution.py`
- **Models:** `ExternalExecutionEventORM` (table `external_execution_events`).
- **Events Recorded:** `DISPATCH_ATTEMPTED`, `RESOURCE_CREATED`, `RESOURCE_ADOPTED`, `RESOURCE_SUPERSEDED`, `STATUS_CHANGED`, `ERROR_ENCOUNTERED`, `LINEAGE_CLASSIFIED`.
- **Property:** Append-only at the application level.

### 2.8 Existing Execution State Models
- **File:** `backend/venturebot/models/external_execution.py`
- **Statuses:** `INITIALIZED`, `PARTIAL_CAMPAIGN`, `PARTIAL_ADSET`, `PARTIAL_CREATIVE`, `DEPLOYED_DORMANT`, `DEPLOYED`, `ACTIVATED`, `PAUSED`, `FAILED`, `TIMEOUT`.

### 2.9 Existing Emergency-Stop Implementation
- **File:** `backend/venturebot/execution/dispatch.py` (`ExecutionDispatchService.emergency_stop()`)
- **Routing:**
  - Evaluates lineage via `classify_execution_lineage()`.
  - For `OPTION_B`: routes to `emergency_pause_adset()` (four-tier pause: Campaign $\rightarrow$ Ad $\rightarrow$ Replacement Ad Set $\rightarrow$ Original Ad Set).
  - For standard: routes to `_emergency_stop()`.
  - Only after external pause succeeds is internal experiment status transitioned to `KILLED` via `ExperimentExecutionService.kill()`.
  - Operates completely independently of the deprecated deployment gateway.

---

## 3. Task 2 — Trusted Local Operator Authentication Design

### 3.1 Comparative Architectural Evaluation

| Evaluation Criteria | Approach A: Argon2id + Local Session Token | Approach B: Windows DPAPI / Credential Manager | Approach C: Hybrid OS Key Wrapping + Argon2id (Recommended) |
| :--- | :--- | :--- | :--- |
| **Cryptographic Strength** | **High:** Memory-hard, side-channel resistant, resistant to GPU/ASIC cracking. | **High:** Backed by Windows LSA and TPM/machine keys. | **Highest:** Memory-hard application passphrase wrapped by OS key protection. |
| **Windows Compatibility** | **Full:** Standard C-extension or pure-Python fallback (`scrypt`). | **Native:** Windows-only API. | **Full:** Adapts to host OS primitives. |
| **Development Complexity** | **Low:** Self-contained, standard library / minimal dependency. | **Moderate:** Requires ctypes / pywin32 API bindings. | **Moderate:** Clean abstraction layer with fallback. |
| **Secret Storage** | Argon2id hash + salt stored in SQLite or config. | Stored in Windows Credential Vault. | Master encryption key in DPAPI/vault; credentials hashed in SQLite. |
| **Session Revocation** | **Immediate:** Server-side session table revocation. | Process-token bound; difficult to revoke granularly. | **Immediate:** Server-side session table with explicit TTL and kill switch. |
| **Account Recovery** | Offline master recovery seed or local passphrase reset. | Windows user password reset. | Offline recovery keyphrase generated at bootstrap. |
| **Brute-Force Protection** | Exponential backoff + account lockout (5 attempts / 15m). | Delegated to Windows logon policies. | Application-level exponential delay + account lockout. |
| **Same-User Process Defense** | **Strong:** Requires explicit passphrase entry in active session. Background malware cannot use credentials without passphrase. | **Weak:** Any process running under the same Windows user token can invoke DPAPI decryption silently. | **Strong:** Password required to unlock session; DPAPI only protects resting key at rest. |
| **Portability to Linux** | **100% Portable:** Identical behavior across OS platforms. | **Zero Portability:** Windows-specific APIs fail on Linux. | **100% Portable:** Core authentication is portable; key-wrapper uses SecretService on Linux. |

### 3.2 Recommended Authentication Architecture: Approach C (Hybrid Protected Password Authentication)

#### Architectural Justification:
The primary threat in a single-operator local execution environment is **unauthorized background invocation** (e.g. an automated script, background agent, or malware running under the same OS user account invoking Python APIs directly).
- Relying purely on Windows DPAPI (Approach B) fails against this threat because any process running under the user's login session can silently invoke DPAPI and decrypt keys without operator interaction.
- Approach C requires an **explicit human interactive unlock** (passphrase entry into an authenticated session) using Argon2id hashing. The session token is held in-memory and validated against a local session repository with a strict inactivity TTL.

#### Key Authentication Components:
1. **Bootstrap Initialization (`OperatorBootstrapService`):**
   - First-time setup prompts operator for a secure passphrase (minimum 12 characters, entropy check).
   - Salt generated using `os.urandom(32)`.
   - Passphrase hashed using **Argon2id** (memory cost: 64MB, time cost: 3 iterations, parallelism: 4 lanes) or **`hashlib.scrypt`** (N=2^14, r=8, p=1) as zero-dependency stdlib fallback.
   - Master record persisted to `operators` table.
   - 24-word BIP-39 recovery seed displayed once for disaster recovery.
2. **Session Lifecycle (`OperatorSessionService`):**
   - On login, operator presents passphrase.
   - Upon verification, a cryptographically secure 256-bit random session token is generated (`secrets.token_urlsafe(32)`).
   - Session record persisted to `operator_sessions` table with:
     - `session_id`: UUIDv4
     - `operator_id`: UUIDv4
     - `token_hash`: SHA-256 of session token (raw token is never stored in DB)
     - `created_at`: UTC timestamp
     - `expires_at`: UTC timestamp (absolute maximum 8 hours)
     - `last_activity_at`: UTC timestamp (inactivity timeout: 15 minutes)
     - `revoked_at`: Nullable UTC timestamp
3. **Brute-Force & Rate Limiting Guard:**
   - 5 consecutive failed attempts trigger a 15-minute lockout.
   - Failed attempts logged to `security_audit_events` with timestamp and IP/caller context.

---

## 4. Task 3 — Authorization and Role-Based Access Control (RBAC) Design

### 4.1 Fixed Security Roles

| Role | Allowed Actions | Disallowed Actions |
| :--- | :--- | :--- |
| **`OPERATOR_VIEWER`** | Read experiment metrics, view campaign status, inspect execution ledger. | All mutation, deployment, emergency-stop, or approval actions. |
| **`OPERATOR_DEPLOYER`** | Execute dormant deployments, emergency-stop active campaigns. | Issue approval tickets, modify allocated capital, bypass verification. |
| **`OPERATOR_APPROVER`** | Review experiment specifications, approve capital allocation, sign `MutationCapability` tickets. | Execute deployments directly (requires deployer session or dual-auth). |
| **`SECURITY_ADMIN`** | User management, key rotation, emergency pause, audit log inspection. | Cannot unilaterally execute unapproved live Meta flights. |

### 4.2 Dual-Control vs. Single-Operator Separation of Duties
In a single-operator local pilot environment, requiring two physically distinct human operators ("four-eyes principle") is practically impossible. However, the architecture must prevent **automated self-approval loops**:
- **Separation of Actions:** The action of *approving* an experiment and generating a mutation ticket must be separated from the action of *executing* the remote mutation.
- **Explicit Re-Authentication for Approvals:** Issuing a `MutationCapability` ticket requires re-entering the operator passphrase or confirming an interactive terminal prompt. A background process cannot use an existing ambient read session to forge an approval.

### 4.3 Server-Side Permission Enforcement Model
Permissions are enforced strictly within backend service layer boundaries, never on client/caller input:
```python
def require_permission(session: Session, session_token: str, required_permission: Permission) -> OperatorORM:
    # 1. Hash incoming token and query operator_sessions
    token_hash = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
    session_record = session.query(OperatorSessionORM).filter_by(token_hash=token_hash).first()
    
    # 2. Validate session validity and TTL
    if not session_record or session_record.revoked_at is not None:
        raise SecurityAuthenticationError("INVALID_OR_REVOKED_SESSION")
    if session_record.expires_at < datetime.now(timezone.utc):
        raise SecurityAuthenticationError("SESSION_EXPIRED")
    if datetime.now(timezone.utc) - session_record.last_activity_at > timedelta(minutes=15):
        raise SecurityAuthenticationError("SESSION_INACTIVITY_TIMEOUT")
        
    # 3. Retrieve operator and evaluate role permissions
    operator = session.get(OperatorORM, session_record.operator_id)
    if not operator or not operator.is_active:
        raise SecurityAuthorizationError("OPERATOR_INACTIVE_OR_SUSPENDED")
    if required_permission not in ROLE_PERMISSIONS[operator.role]:
        raise SecurityAuthorizationError(f"PERMISSION_DENIED: {required_permission.value}")
        
    # 4. Touch last_activity_at
    session_record.last_activity_at = datetime.now(timezone.utc)
    return operator
```

---

## 5. Task 4 — Cryptographic Mutation Capability (`MutationCapability`)

### 5.1 Ticket Schema Specification
To eliminate caller-controlled boolean trust flags (`explicit_auth=True`), every granular deployment tier strictly requires an operation-bound cryptographic ticket:

```json
{
  "ticket_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
  "operator_id": "c1a2b3c4-d5e6-7f8a-9b0c-1d2e3f4a5b6c",
  "experiment_id": "49fde874-9387-5056-934c-51a9cfca164f",
  "target_tier": "ADSET",
  "exact_operation": "DEPLOY_RECOVERY_ADSET_DORMANT",
  "budget_ceiling_currency": "INR",
  "budget_ceiling_amount": "200.00",
  "parameters_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "expected_parent_id": "120252176243860380",
  "replacement_name": "VB-EXP-49fde874-9387-5056-934c-51a9cfca164f-ADSET-v2",
  "nonce": "a7f3b89012cd4e5f6a7b8c9d0e1f2a3b",
  "issued_at": "2026-11-06T20:00:00Z",
  "expires_at": "2026-11-06T20:05:00Z"
}
```

### 5.2 Cryptographic Signature Comparison

| Dimension | HMAC-SHA256 (Symmetric) | Ed25519 (Asymmetric) |
| :--- | :--- | :--- |
| **Algorithm Type** | Symmetric Message Authentication Code | Asymmetric Digital Signature (Edwards-curve DSA) |
| **Key Architecture** | Single shared 256-bit secret key. | 256-bit private key (signing) / public key (verifying). |
| **Python Standard Library** | **Native** (`hmac`, `hashlib`). Zero extra packages. | Requires third-party `cryptography` package or Python 3.13+. |
| **Execution Performance** | Sub-microsecond (< 1 µs). Negligible overhead. | Fast (< 100 µs), but higher CPU than symmetric HMAC. |
| **Tamper Resistance** | **Absolute:** Any alteration of ticket payload invalidates MAC. | **Absolute:** Any alteration invalidates digital signature. |
| **Non-Repudiation** | **None:** Server and signer share the same secret; does not prove which holder created it. | **True Non-Repudiation:** Only the private key holder could have generated the signature. |
| **Recommendation for VentureBot Pilot** | **Primary baseline:** Zero-dependency, highly secure for local boundaries. | **Future enhancement:** When external microservices or distributed signers are introduced. |

### 5.3 Key Management & Nonce Verification
1. **Master Signing Key (`HMAC_SECRET_KEY`):**
   - 256-bit cryptographically secure key generated via `secrets.token_bytes(32)`.
   - Stored in a dedicated local secure vault file (`.venturebot_keys`) with strict OS permissions (`0600`), encrypted using Windows DPAPI or master operator passphrase.
   - Never committed to git; never logged in debug outputs.
2. **Signature Generation:**
   ```python
   canonical_payload = json.dumps(ticket_payload, sort_keys=True, separators=(',', ':'))
   signature = hmac.new(master_key, canonical_payload.encode('utf-8'), hashlib.sha256).hexdigest()
   ```
3. **Single-Use Replay Prevention:**
   - Every ticket includes a 128-bit random `nonce`.
   - Upon consumption, the ticket record in `mutation_capabilities` table transitions from `ISSUED` to `CONSUMED` inside an atomic database transaction *before* network dispatch.
   - Any attempt to reuse the same ticket or nonce is rejected immediately with `MUTATION_CAPABILITY_REPLAY_REJECTED`.
4. **Key Rotation Protocol:**
   - Keys are tagged with a `key_id` (e.g. `k1`, `k2`).
   - The ticket payload includes `key_id`. Verification looks up the active key or graceful grace-period keys.

---

## 6. Task 5 — Crash-Safe Mutation State Machine

A critical flaw in standard API integrations is holding database transactions open during remote network requests (causing lock contention) or updating the database only *after* remote requests complete (causing orphaned remote resources on process crash).

### 6.1 State Machine Lifecycle & Transitions

```
[ APPROVAL_ISSUED ]
        │
        ▼ (Operator passes ticket to ExecutionService)
[ APPROVAL_CONSUMED ] ── (Atomic DB Transaction 1)
        │
        ▼ (Pre-dispatch intent ledger entry written)
[ MUTATION_DISPATCHING ] ── (Atomic DB Transaction 2, release DB locks)
        │
        ├────────────────────────────────────────────────────────┐
        ▼ (Network I/O: Meta API HTTP POST - NO OPEN DB LOCKS)    │
[ HTTP Response Received ]                                       │
        │                                                        │
        ├── HTTP 200 OK ──────────────┐                          │ (Socket Timeout, Crash,
        ├── HTTP Error (4xx/5xx) ─────┤                          │  Power Failure)
        │                             │                          │
        ▼                             ▼                          ▼
[ MUTATION_CONFIRMED ]       [ MUTATION_REJECTED ]      [ MUTATION_OUTCOME_UNKNOWN ]
        │                             │                          │
        ▼ (Atomic DB Trans 3)         ▼ (Atomic DB Trans 3)      ▼ (Automatic / Operator Restart)
[ ExternalExecution DEPLOYED ]   [ Execution FAILED ]   [ RECONCILIATION_REQUIRED ]
```

### 6.2 Failure Scenario Resolutions

| Scenario | State at Failure | Resolution & Recovery Architecture |
| :--- | :--- | :--- |
| **Crash before network request** | `MUTATION_DISPATCHING` (no remote ID recorded). | On system startup, startup reconciler inspects Meta for deterministic replacement name. Not found $\rightarrow$ transitions cleanly to `MUTATION_REJECTED`. No orphaned remote ad set. |
| **Network timeout after Meta receives POST** | `MUTATION_DISPATCHING` | Adapter timeout raises `MetaApiTimeoutError`. System transitions to `MUTATION_OUTCOME_UNKNOWN`. **NEVER automatically retries POST.** Flags experiment as `RECONCILIATION_REQUIRED`. |
| **Remote success but local process crashes before DB commit** | `MUTATION_DISPATCHING` (remote resource created in `PAUSED` status). | On restart, reconciler performs read-only lookup on Meta. Finds exact matching replacement ad set. Adopts existing remote resource, persists ID, and transitions to `MUTATION_CONFIRMED`. Zero duplicate creation. |
| **Duplicate dispatch request with same ticket** | `APPROVAL_CONSUMED` | Attempt to acquire ticket fails closed: `TICKET_ALREADY_CONSUMED`. Execution blocked. |
| **Expired approval ticket** | `APPROVAL_ISSUED` | Time elapsed > 5 minutes (`now > expires_at`). Verification fails closed: `MUTATION_CAPABILITY_EXPIRED`. Operator must re-approve. |
| **Database lock contention** | Lock acquisition | Database operations are short, discrete transactions (< 5ms). Zero database connections held open during external Meta HTTP calls. |

---

## 7. Task 6 — Comprehensive Threat Model

### Security Posture Matrix:

| Threat Vector | Attack Scenario | Mitigation Status | Technical Mitigation Mechanism |
| :--- | :--- | :---: | :--- |
| **1. Forged Operator Identity** | Unauthenticated local script supplies `operator_id="admin"`. | **MITIGATED** | `operator_id` extracted exclusively from validated `OperatorSessionORM` verified via Argon2id password hash. Caller parameters ignored. |
| **2. Session Theft** | Local process reads active session token from memory or disk. | **PARTIALLY MITIGATED** | Session tokens stored as SHA-256 hashes in database; in-memory tokens have 15-minute inactivity timeouts. (Protected against resting disk read, vulnerable to live memory scraper with same-user OS rights). |
| **3. Brute-Force Authentication** | Attacker scripts millions of password attempts against local CLI. | **MITIGATED** | Memory-hard Argon2id parameters enforce severe computational cost; account lockout after 5 failed attempts (15-minute lockout). |
| **4. Ticket Replay** | Attacker captures valid `MutationCapability` and resubmits it. | **MITIGATED** | Single-use nonce and atomic database transition to `APPROVAL_CONSUMED` before dispatch. Resubmission rejected with `REPLAY_REJECTED`. |
| **5. Ticket Tampering** | Attacker alters budget ceiling from ₹200 to ₹20,000 in ticket payload. | **MITIGATED** | HMAC-SHA256 signature verification over canonical JSON representation fails immediately upon any single-bit payload alteration. |
| **6. Concurrent Execution** | Parallel processes invoke dormant deployment simultaneously. | **MITIGATED** | Two-layer double-checked locking: `_TIER_THREAD_LOCKS` (in-process mutex) + `DeploymentClaimORM` (cross-process database lock with unique constraint and TTL). |
| **7. Process Crash Window** | Power failure or crash occurs during HTTP POST. | **MITIGATED** | Two-phase state machine records `MUTATION_DISPATCHING` pre-intent; startup reconciler evaluates remote Meta state before resuming or clearing. |
| **8. Database Tampering** | Attacker opens `venturebot.db` directly in SQLite editor and alters records. | **PARTIALLY MITIGATED** | HMAC signatures on tickets detect tampering; append-only audit event hashes. (Direct SQL modification cannot be completely blocked against a host administrator). |
| **9. Local Admin Compromise** | Root / Administrator on Windows host machine compromises system. | **OUTSIDE BOUNDARY** | An OS superuser can inspect process memory, hook Python runtime, or patch bytecode. Operating system integrity is an assumed trust boundary. |
| **10. Direct Adapter Invocation** | Python script bypasses `dispatch.py` and calls `adapter.create_adset()` directly. | **MITIGATED** | `MetaMarketingApiAdapter._execute_request()` requires a validated `MutationCapability` ticket object to unlock write operations. |
| **11. SAFE_MODE Bypass** | Caller passes `safe_mode=False` to function arguments. | **MITIGATED** | `is_safe_mode_enforced()` forces environment variable precedence; `MetaMarketingApiAdapter` unconditionally checks `is_safe_mode()` regardless of arguments. |
| **12. Emergency-Stop Failure** | Operator cannot stop runaway campaign due to authentication lockout. | **MITIGATED** | Fail-safe emergency stop: emergency pause does not require ticket generation; permitted for any authenticated operator role; manual Meta Ads Manager fallback preserved. |

---

## 8. Task 7 — Implementation Roadmap (Phase B Sub-Steps)

Phase B must be executed in small, independently reviewable, hermetically tested increments:

```
Step 90.16.5.1: Database Schemas & Cryptographic Primitives
       │
       ▼
Step 90.16.5.2: Operator Identity & Session Management Service
       │
       ▼
Step 90.16.5.3: Mutation Capability (Approval Ticket) Engine
       │
       ▼
Step 90.16.5.4: Pre-Dispatch Intent Ledger & Crash-Safe State Machine
       │
       ▼
Step 90.16.5.5: Adapter Gating & End-to-End Failure-Injection Tests
```

### Detailed Sub-Step Breakdown:

#### Step 90.16.5.1: Database Schemas & Cryptographic Primitives
- **Goal:** Add ORM models for operators, sessions, mutation tickets, and pre-dispatch intent records. Implement key generation and HMAC signing primitives.
- **Files Affected:**
  - `backend/venturebot/database/models.py`
  - `backend/venturebot/security/crypto.py` (new)
- **Database Migrations:** Yes (add `operators`, `operator_sessions`, `mutation_capabilities`, `mutation_intent_ledger` tables).
- **Test Strategy:** Hermetic unit tests for signature generation, verification, tamper detection, and key rotation.
- **Acceptance Criteria:** 100% of cryptographic operations verify correctly; zero external network access.

#### Step 90.16.5.2: Operator Identity & Session Management Service
- **Goal:** Implement password hashing, bootstrap setup, authentication, session issuance, expiration, and revocation.
- **Files Affected:**
  - `backend/venturebot/security/auth.py` (new)
  - `backend/venturebot/security/session.py` (new)
- **Database Migrations:** No.
- **Test Strategy:** Unit tests for Argon2id hashing, lockout after 5 failed attempts, session expiry after 15m inactivity, session revocation.
- **Acceptance Criteria:** Unauthenticated callers cannot generate valid session tokens.

#### Step 90.16.5.3: Mutation Capability (Approval Ticket) Engine
- **Goal:** Implement the ticket issuance and single-use consumption engine.
- **Files Affected:**
  - `backend/venturebot/security/ticket.py` (new)
- **Database Migrations:** No.
- **Test Strategy:** Tests verifying ticket issuance, budget ceiling binding, nonce uniqueness, single-use consumption, and replay rejection.
- **Acceptance Criteria:** Replaying a consumed ticket fails closed with `REPLAY_REJECTED`.

#### Step 90.16.5.4: Pre-Dispatch Intent Ledger & Crash-Safe State Machine
- **Goal:** Implement the two-phase state machine separating DB transactions from remote HTTP calls.
- **Files Affected:**
  - `backend/venturebot/execution/state_machine.py` (new)
  - `backend/venturebot/execution/dispatch.py`
- **Database Migrations:** No.
- **Test Strategy:** Failure injection tests: simulate crash before POST, simulate socket timeout, simulate crash after POST. Verify startup reconciler cleanly adopts or marks failed.
- **Acceptance Criteria:** Zero open DB transactions during mock transport sleep; zero duplicate remote resources on simulated crash.

#### Step 90.16.5.5: Adapter Gating & End-to-End Failure-Injection Tests
- **Goal:** Integrate `MutationCapability` ticket checking directly into `MetaMarketingApiAdapter` and replace all caller-supplied `explicit_auth` booleans across granular deployment tiers.
- **Files Affected:**
  - `backend/venturebot/execution/meta.py`
  - `backend/venturebot/execution/dispatch.py`
  - `tests/test_operator_security.py` (new)
  - `tests/test_granular_recovery.py` (update to pass tickets)
- **Database Migrations:** No.
- **Test Strategy:** Full regression suite run; all 670+ existing tests must pass; new security tests must achieve 100% branch coverage.
- **Acceptance Criteria:** Calling any mutation method without a valid signed ticket raises `SecurityAuthorizationError`.

---

## 9. Explicit SAFE_MODE Preservation

Throughout the design and upcoming implementation of Phase B:
- **`VENTUREBOT_SAFE_MODE=true` remains unconditionally active and enforced.**
- No security feature, authentication module, or test suite will disable or bypass SAFE_MODE.
- All Phase B testing will execute exclusively against hermetic mock transports with mock adapters.
- Live Meta API mutations remain **strictly prohibited** until Phase B implementation is fully completed, audited, and explicitly authorized by the human operator.

---

**AUDIT & DESIGN COMPLETE — NO APPLICATION CODE MODIFIED, NO DATABASE MUTATIONS, NO GIT WRITES, NO META API MUTATIONS.**
