# VentureBot — Step 90.16.5A: Adversarial Security Architecture Review

**Document ID:** `STEP_90_16_5A_ADVERSARIAL_SECURITY_REVIEW.md`  
**Phase:** Phase B — Security Architecture Adversarial Review (Read-Only Analysis)  
**Repository:** `E:\Project Folder\venturebot`  
**Target Document Reviewed:** `STEP_90_16_5_TRUSTED_OPERATOR_SECURITY_ARCHITECTURE.md`  
**Baseline Commit:** `a94a1b920eabfa608e7ddbcefcff0ccea474dfe8`  
**Author:** Vishnu / Antigravity Autonomous Security Architecture  
**Execution Status:** READ-ONLY — Zero application code modified, zero migrations executed, zero Meta mutations.

---

## 1. Executive Verdict

### Verdict: **CONDITIONAL APPROVAL WITH MANDATORY SECURITY CORRECTIONS**

The Step 90.16.5 design provides a strong conceptual foundation by deprecating caller-supplied booleans (`explicit_auth`), introducing cryptographic approval tickets (`MutationCapability`), and designing a two-phase mutation state machine. However, an adversarial review reveals **five critical architectural flaws and implementation gaps** that must be resolved prior to writing application code:

1. **Denial-of-Service via Brute-Force Lockout:** The proposed 5-attempt / 15-minute account lockout in a single-operator local application creates a severe DoS vector where any local script can permanently lock out the human operator.
2. **Standard Library Missing Argon2id:** The proposed reliance on Argon2id introduces an external C-dependency (`argon2-cffi`) on Windows that contradicts VentureBot's zero-dependency standard-library baseline. `hashlib.scrypt` must be the primary standard library primitive.
3. **Flawed Role-Separation Friction:** Requiring mutually exclusive `OPERATOR_DEPLOYER` and `OPERATOR_APPROVER` identities in a single-human pilot environment induces dangerous operator fatigue or credential-sharing workarounds. Separation must be enforced at the **action/workflow boundary (two-step operation with interactive passphrase prompt)** rather than requiring multiple physical accounts.
4. **False Equivalence of HTTP 5xx / 4xx with Mutation Failure:** Classifying network timeouts or HTTP 500 errors as confirmed mutation failures is catastrophic on Meta Marketing API, where remote ad sets can be created even when the HTTP response is 500 or times out.
5. **In-Process Python Boundary Weakness:** A Python `MutationCapability` object is not a security boundary against other code running in the same Python interpreter. The lowest-level adapter (`MetaMarketingApiAdapter._execute_request`) must cryptographically verify an opaque signed ticket string against resting state before opening an HTTP socket.

---

## 2. Critical Design Flaws Identified in Step 90.16.5

### Flaw 1: Lockout Denial-of-Service in Single-Operator Environment (HIGH)
- **Original Proposal:** Account lockout after 5 consecutive failed password attempts for 15 minutes.
- **Vulnerability:** In an enterprise multi-tenant server, account lockout protects passwords. In a single-operator local application running on a developer or server machine, locking the only operator account means an attacker, rogue local script, or buggy background process can lock the operator out of the system at will—preventing time-critical operations such as emergency stop or flight intervention.
- **Correction:** Replace hard account lockout with **exponential per-attempt throttling** (1s, 2s, 4s, 8s, 16s, capped at 30s) computed via memory-hard delay, combined with an offline console recovery unlock.

### Flaw 2: Argon2id Dependency vs. Standard Library Baseline (MEDIUM)
- **Original Proposal:** Argon2id as the primary password hashing mechanism with scrypt as fallback.
- **Vulnerability:** Python 3.11/3.12 stdlib does NOT include Argon2id; it requires `argon2-cffi` (which requires compilation or platform wheels). Having a split algorithm where development runs scrypt and production runs Argon2id creates testing discrepancies, format incompatibilities, and dependency fragility.
- **Correction:** Standardize on **`hashlib.scrypt`** (built into Python stdlib on Windows and Linux since 3.8: N=2^14, r=8, p=1, maxmem=64MB) as the canonical zero-dependency primitive, formatted per standard Modular Crypt Format / PHC string format (`$scrypt$ln=14,r=8,p=1$...`).

### Flaw 3: Mutually Exclusive Roles in Single-Operator Pilot (HIGH)
- **Original Proposal:** Strict separation between `OPERATOR_DEPLOYER` and `OPERATOR_APPROVER`.
- **Vulnerability:** Because the pilot has exactly one human operator, requiring two separate user accounts forces the human to constantly log in, approve, log out, log in, deploy, and log out. This creates immense friction that encourages developers to disable authentication entirely.
- **Correction:** Model separation of duties as **Two-Step Action Authentication**:
  - A single operator can hold permissions to approve and deploy.
  - However, approving an experiment to generate a `MutationCapability` ticket requires **explicit, interactive re-authentication** (prompting for the master passphrase or PIN).
  - Background processes holding ambient session tokens CANNOT silently self-issue mutation tickets.

### Flaw 4: Meta Marketing API Ambiguous HTTP 500 / Timeout Failures (CRITICAL)
- **Original Proposal:** State machine marked HTTP errors as `MUTATION_REJECTED`.
- **Vulnerability:** Meta Graph API is distributed. A network drop, timeout, or HTTP 500 Internal Server Error frequently occurs *after* Meta has committed the resource to its internal database. Treating this as a failure and re-dispatching creates duplicate campaigns/ad sets and triggers billing overspend.
- **Correction:** ANY network timeout, socket disconnect, or HTTP 500 error during resource creation MUST transition to **`MUTATION_OUTCOME_UNKNOWN`**. The system must halt automated dispatch and invoke the read-only idempotency reconciler before retrying.

### Flaw 5: Python Object In-Memory Forgery (HIGH)
- **Original Proposal:** Gating adapter methods by passing a `MutationCapability` Python object.
- **Vulnerability:** Within a Python interpreter, any code running in the same process can instantiate `MutationCapability(...)` or monkeypatch fields.
- **Correction:** The adapter must accept an **opaque cryptographically signed token string** (`payload.signature`) and verify the signature using `hmac.compare_digest` against the master key *inside* `MetaMarketingApiAdapter._execute_request` immediately before transmitting the raw HTTP request.

---

## 3. Task 1 — Authentication Architecture Corrections

### 3.1 Canonical Password Hashing Specification
To guarantee 100% standard library compliance across Windows and Linux without C-compilers or third-party wheels:
- **Algorithm:** `hashlib.scrypt` (Python stdlib)
- **Parameters:**
  - `n = 16384` ($2^{14}$, CPU/memory cost)
  - `r = 8` (block size)
  - `p = 1` (parallelization)
  - `maxmem = 67108864` (64 MiB limit)
  - `dklen = 32` (256-bit derived key)
  - `salt`: 32 cryptographically random bytes via `secrets.token_bytes(32)`
- **Storage Format:** PHC-compliant string stored in `operators.password_hash`:
  ```text
  $scrypt$ln=14,r=8,p=1$<base64_salt>$<base64_hash>
  ```
- **Verification:** Evaluated via constant-time comparison `hmac.compare_digest(derived, expected)`.

### 3.2 Session Management & Token Protection
- **Session Token Generation:** `secrets.token_urlsafe(32)` (256 bits of entropy, URL-safe base64).
- **Digest Storage:** Raw session tokens are **NEVER** stored in the database. The database table `operator_sessions` stores only `token_hash = hashlib.sha256(raw_token.encode('utf-8')).hexdigest()`.
- **Session Fixation Prevention:** Every successful login terminates all prior active sessions for that operator and generates a completely fresh `session_id` and `token_hash`.
- **Inactivity & Lifespan:**
  - Inactivity Timeout: 15 minutes (`now - last_activity_at > 15m`).
  - Absolute Session Lifetime: 8 hours (`now > expires_at`).
  - Active Pruning: Every session validation cleans up expired records for that operator.

### 3.3 Anti-Brute-Force Throttling (Replacing Account Lockout)
To prevent Denial-of-Service while maintaining resistance to offline/online dictionary attacks:
- On failed password verification:
  - Increment `failed_attempt_count` in `operators` table.
  - Enforce exponential delay in memory before returning HTTP/CLI response:
    $$\text{delay} = \min(30.0, 2^{\text{failed\_attempt\_count} - 1}) \text{ seconds}$$
  - Log `SECURITY_AUTHENTICATION_FAILURE` audit event with timestamp.
- On successful password verification:
  - Reset `failed_attempt_count = 0`.

---

## 4. Task 2 — Corrected RBAC and Permission Matrix

### 4.1 Permission Definitions
- `PERM_EXPERIMENT_READ`: Inspect experiments, metrics, and audit ledger.
- `PERM_EXPERIMENT_APPROVE`: Review specifications, approve budget allocations, and issue mutation tickets.
- `PERM_MUTATION_EXECUTE`: Consume valid mutation tickets to execute dormant recovery deployments.
- `PERM_EMERGENCY_STOP`: Trigger immediate external campaign pause and internal experiment termination.
- `PERM_SECURITY_ADMIN`: Manage operator accounts, rotate signing keys, inspect security audit logs.

### 4.2 Explicit Role Permission Matrix

| Role Name | Description | Allowed Permissions | Disallowed Permissions |
| :--- | :--- | :--- | :--- |
| **`ROLE_VIEWER`** | Read-only auditor or stakeholder | `PERM_EXPERIMENT_READ` | All mutation, approval, or admin actions |
| **`ROLE_OPERATOR`** (Standard Pilot Role) | Primary human operator managing pilot experiments | `PERM_EXPERIMENT_READ`<br>`PERM_EXPERIMENT_APPROVE` (requires interactive passphrase confirmation)<br>`PERM_MUTATION_EXECUTE`<br>`PERM_EMERGENCY_STOP` | `PERM_SECURITY_ADMIN` |
| **`ROLE_ADMIN`** | Security administrator managing keys and operators | `PERM_EXPERIMENT_READ`<br>`PERM_EMERGENCY_STOP`<br>`PERM_SECURITY_ADMIN` | `PERM_EXPERIMENT_APPROVE`<br>`PERM_MUTATION_EXECUTE` |

### 4.3 Two-Step Separation of Duties Workflow
For the pilot single-operator environment (`ROLE_OPERATOR`):
1. **Step 1: Ticket Issuance (`authorise_mutation`):**
   - The operator inspects the experiment and invokes `issue_mutation_capability(experiment_id, tier)`.
   - The system prompts for the operator's **master passphrase** (interactive confirmation).
   - Upon verification, issues a short-lived (5-minute TTL) signed `MutationCapability` ticket in `ISSUED` status.
2. **Step 2: Deployment Execution (`execute_mutation`):**
   - The deployment tier consumes the ticket.
   - The ticket transitions to `CONSUMED` and dispatches the mutation.
   - **Security Guarantee:** Background processes or automated agent loops cannot forge Step 1 because Step 1 requires interactive master passphrase entry.

---

## 5. Task 3 — Key Management and Signature Specification

### 5.1 Canonical Serialization (RFC 8785 Principles)
To prevent signature mismatch across platforms or JSON parser implementations:
1. Keys must be sorted lexicographically by Unicode code point.
2. No whitespace allowed between key-value separators (exact separators: `(',', ':')`).
3. Monetary amounts must be formatted as fixed-point Decimal strings with exactly two decimal places (e.g. `"200.00"`).
4. Timestamps must be formatted as RFC 3339 UTC ISO-8601 strings with trailing `"Z"` (e.g. `"2026-11-06T20:00:00Z"`).
5. All text strings must be normalized to Unicode Normalization Form C (`unicodedata.normalize('NFC', text)`).

### 5.2 HMAC-SHA256 vs. Ed25519 Cryptographic Reality

| Property | HMAC-SHA256 (Symmetric) | Ed25519 (Asymmetric) |
| :--- | :--- | :--- |
| **Integrity & Tamper Detection** | **Guaranteed:** Any altered byte invalidates the HMAC digest. | **Guaranteed:** Any altered byte invalidates signature. |
| **Authentication** | **Guaranteed:** Proves holder of shared secret generated the MAC. | **Guaranteed:** Proves holder of private key signed it. |
| **Non-Repudiation** | **NONE:** Cannot prove whether the operator or the backend server generated the MAC, because both possess the shared secret. | **TRUE NON-REPUDIATION:** Only the operator holding the private key could have signed the ticket. |
| **Standard Library** | **Native** (`hmac`, `hashlib`). Zero dependencies. | Requires external `cryptography` package. |
| **Piloting Verdict** | **Approved as Phase B Baseline:** Provides airtight local integrity and anti-tampering within the local security boundary. | **Deferred to Phase C:** For distributed or multi-host deployments. |

### 5.3 Constant-Time Comparison
All signature verifications **MUST** use `hmac.compare_digest(computed_sig, presented_sig)` to eliminate timing side-channel leakage.

---

## 6. Task 4 — Mutation State Machine & Failure Classification

### 6.1 Adversarial Scenario Matrix

| # | Failure Scenario | True Remote State | Correct State Classification | Safe Action / Reconciliation Rule |
| :-: | :--- | :--- | :---: | :--- |
| **1** | Process crashes after ticket consumption but before HTTP dispatch | No resource created | **DEFINITE FAILURE** (`MUTATION_REJECTED`) | Startup reconciler verifies remote resource does not exist. Ticket remains `CONSUMED` (prevents replay). Operator re-issues ticket. |
| **2** | Process crashes while HTTP request is in-flight | Unknown (Meta may have created it) | **MUTATION_OUTCOME_UNKNOWN** | **HALT DISPATCH.** Do NOT retry POST. Run read-only lookup for `VB-EXP-{id}-ADSET-v2`. If exact match found: adopt. If absent: mark rejected. |
| **3** | Meta receives request, creates ad set, but TCP connection resets | Resource created in `PAUSED` status | **MUTATION_OUTCOME_UNKNOWN** | Adapter catches `ConnectionResetError` $\rightarrow$ marks `MUTATION_OUTCOME_UNKNOWN`. Reconciler queries Meta, detects existing ad set, adopts cleanly without duplicate POST. |
| **4** | Meta returns HTTP 500 / 502 / 503 | Unknown (Meta occasionally commits before 500) | **MUTATION_OUTCOME_UNKNOWN** | **DO NOT TREAT HTTP 500 AS DEFINITE FAILURE.** Set `RECONCILIATION_REQUIRED`. Lookup deterministic name on Meta. |
| **5** | Meta returns HTTP 429 (Rate Limit) | No resource created | **DEFINITE FAILURE** (`MUTATION_REJECTED`) | Ticket consumed; intent logged with `RATE_LIMIT_EXCEEDED`; pause automated dispatch until rate limit reset. |
| **6** | Meta returns HTTP 200, but local SQLite database commit crashes | Resource created on Meta | **MUTATION_OUTCOME_UNKNOWN** | Local DB shows `MUTATION_DISPATCHING`. On restart, reconciler queries Meta, finds resource, writes missing local DB records, advances to `DEPLOYED`. |
| **7** | Two processes submit the same ticket concurrently | At most one can dispatch | **RACE ELIMINATED** | Atomic DB update: `UPDATE mutation_capabilities SET status='CONSUMED' WHERE id=:id AND status='ISSUED'`. Exactly 1 process updates row; second fails closed. |
| **8** | SQLite `database is locked` during ticket consumption | No request sent | **DEFINITE FAILURE** | Fails closed before network I/O. Ticket rolls back to `ISSUED`. |
| **9** | Remote replication lag: Meta lookup yields 404 immediately after creation | Resource created but not replicated | **TRANSIENT UNKNOWN** | Reconciler executes bounded exponential backoff (0s, 2s, 5s, 10s) before declaring resource absent. |
| **10** | Process restarts while `MUTATION_DISPATCHING` is recorded | Pending resolution | **RECONCILIATION_REQUIRED** | Mandatory startup audit scans for pending intents; forces reconciliation before permitting new operations. |

---

## 7. Task 5 — Mutation Boundary and Direct Adapter Exposure

### 7.1 Analysis of Adapter Mutation Entry Points
All 12 mutating methods on `MetaMarketingApiAdapter` were audited:
- `create_campaign`, `create_adset`, `upload_image`, `create_creative`, `create_ad`
- `pause_campaign`, `pause_adset`, `pause_ad`
- `unpause_campaign`, `unpause_adset`, `unpause_ad`
- `update_adset_schedule`

### 7.2 The Bypass Threat
In Python, any script or subagent running in the same process can import:
```python
from venturebot.execution.meta import MetaMarketingApiAdapter
adapter = MetaMarketingApiAdapter(ad_account_id=..., access_token=...)
adapter.create_adset(...)  # BYPASSES ExecutionDispatchService entirely!
```
While `MetaMarketingApiAdapter._execute_request()` currently blocks writes if `is_safe_mode()` is True, **if SAFE_MODE is un-gated for a legitimate action, direct adapter calls could execute unrestricted mutations!**

### 7.3 Mandatory Low-Level Adapter Enforcement
To establish true defense-in-depth, `MetaMarketingApiAdapter._execute_request()` must be updated:
1. Every write method (`create_*`, `update_*`, `unpause_*`) must accept a mandatory parameter `mutation_ticket: str`.
2. Inside `_execute_request()`, before creating any HTTP socket:
   - Verify that `mutation_ticket` is present and non-empty.
   - Verify HMAC signature against the master key using `hmac.compare_digest`.
   - Verify that ticket `expires_at >= now_utc`.
   - Verify that ticket `exact_operation` matches the requested endpoint.
3. If verification fails, raise `MetaApiSafetyError("UNAUTHORIZED_MUTATION_ATTEMPT: Invalid or missing mutation ticket.")`.

---

## 8. Task 6 — Emergency-Stop Architecture Verification

### 8.1 Principles for Safe Emergency Stop
1. **Zero Approval Friction:** Emergency stop must **NEVER** require generating a `MutationCapability` ticket. An operator discovering a runaway campaign cannot wait 5 minutes for approval ticket creation.
2. **Permitted for All Roles:** Any authenticated session (`ROLE_VIEWER`, `ROLE_OPERATOR`, `ROLE_ADMIN`) can trigger emergency stop.
3. **Audit Immutability:** An immutable `EMERGENCY_STOP_TRIGGERED` audit event is recorded immediately with operator identity and timestamp.
4. **Idempotency:** Emergency stop is 100% idempotent. Calling it multiple times on an already paused/killed campaign returns success without error.
5. **SAFE_MODE Behavior:**
   - In production with `VENTUREBOT_SAFE_MODE=true`, automated external mutations are blocked.
   - The CLI must provide clear, human-actionable instructions to pause the campaign directly via the **Meta Ads Manager Dashboard**.
   - For automated emergency pause during un-gated operations, an explicit `--force-emergency-pause` flag requires re-entering the operator passphrase to un-gate ONLY the pause endpoint for that single call.

---

## 9. Task 7 — Revised Threat Model Matrix

| Threat Vector | Attack Scenario | Status in Baseline (Post-Phase A) | Status in Proposed Phase B | Evidence & Justification |
| :--- | :--- | :---: | :---: | :--- |
| **1. Forged Operator Identity** | Caller passes `operator_id="admin"` | **NOT MITIGATED** | **MITIGATED** | Post-Phase A: Caller-supplied string. Phase B: Extracted exclusively from validated `OperatorSessionORM`. |
| **2. Session Theft** | Local process reads session token | **NOT MITIGATED** | **PARTIALLY MITIGATED** | Post-Phase A: No sessions exist. Phase B: Tokens stored as SHA-256 hashes in DB, in-memory 15m inactivity timeout. |
| **3. Brute-Force Password** | Script brute-forces login | **NOT MITIGATED** | **MITIGATED** | Post-Phase A: No passwords exist. Phase B: Memory-hard scrypt + exponential delay per attempt. |
| **4. Ticket Replay** | Attacker replays consumed ticket | **NOT MITIGATED** | **MITIGATED** | Post-Phase A: No tickets exist. Phase B: Atomic DB state `APPROVAL_CONSUMED` + unique nonce check. |
| **5. Ticket Tampering** | Attacker modifies budget limit in ticket | **NOT MITIGATED** | **MITIGATED** | Post-Phase A: No tickets exist. Phase B: Canonical RFC 8785 JSON + HMAC-SHA256 signature verification. |
| **6. Concurrent Mutations** | Parallel threads create duplicate ad sets | **MITIGATED** | **MITIGATED** | Implemented in Step 90.15.4: `_TIER_THREAD_LOCKS` + `DeploymentClaimORM` with unique constraint. |
| **7. Process Crash Window** | Crash occurs during HTTP request | **PARTIALLY MITIGATED** | **MITIGATED** | Post-Phase A: Idempotency adoption exists, but lacks pre-intent record. Phase B: Two-phase state machine with startup reconciler. |
| **8. Database Tampering** | Attacker edits SQLite DB directly | **NOT MITIGATED** | **PARTIALLY MITIGATED** | Cryptographic ticket signatures detect tampering. Full protection against local root/admin is outside boundary. |
| **9. Local Admin Compromise** | Windows Administrator compromises OS | **OUTSIDE BOUNDARY** | **OUTSIDE BOUNDARY** | An OS root user can inspect live RAM or hook Python binaries. Operating system integrity is an assumed trust root. |
| **10. Direct Adapter Bypass** | Script calls `adapter.create_adset()` | **NOT MITIGATED** | **MITIGATED** | Post-Phase A: Adapter only checks SAFE_MODE. Phase B: Adapter verifies signed ticket before sending HTTP request. |
| **11. SAFE_MODE Bypass** | Caller passes `safe_mode=False` | **MITIGATED** | **MITIGATED** | Implemented: `is_safe_mode_enforced()` forces environment variable precedence; adapter checks `is_safe_mode()`. |
| **12. Emergency-Stop Failure** | Emergency stop blocked or broken | **MITIGATED** | **MITIGATED** | Implemented: Operates independently of deployment gateway; routes to 4-tier pause; manual fallback preserved. |

---

## 10. Corrected Architecture & Implementation Blockers

### 10.1 Key Decisions Finalized Before Implementation:
1. **Zero External Dependencies:** Use `hashlib.scrypt` (stdlib) for password hashing and `hmac` / `hashlib.sha256` (stdlib) for ticket signing. Zero external C-bindings required on Windows.
2. **Action-Based Separation of Duties:** Single operator account with mandatory interactive passphrase prompt for Step 1 (`issue_mutation_capability`).
3. **No Lockout Denial-of-Service:** Exponential backoff delays instead of account lockouts.
4. **Adapter-Level Gate:** `MetaMarketingApiAdapter` requires an opaque signed ticket string parameter for all write operations.
5. **Reconciliation-First Crash Recovery:** Network timeouts, resets, and HTTP 500 errors transition to `MUTATION_OUTCOME_UNKNOWN` and strictly require read-only lookup before declaring failure.

### 10.2 Recommended Implementation Sequence (Phase B Sub-Steps):
- **Step 90.16.6:** Database Schemas (`operators`, `operator_sessions`, `mutation_capabilities`, `mutation_intent_ledger`) & Cryptographic Signing Primitives (`backend/venturebot/security/crypto.py`).
- **Step 90.16.7:** Local Operator Identity & Session Management Service (`backend/venturebot/security/auth.py`, `session.py`).
- **Step 90.16.8:** Mutation Capability (Approval Ticket) Engine & CLI Issuance Tool (`backend/venturebot/security/ticket.py`).
- **Step 90.16.9:** Pre-Dispatch Intent Ledger & Crash-Safe State Machine (`backend/venturebot/execution/state_machine.py`).
- **Step 90.16.10:** Adapter Gating, Granular Tier Integration, and Failure-Injection Test Suite (`tests/test_operator_security.py`).

---

**ADVERSARIAL SECURITY REVIEW COMPLETE — NO APPLICATION CODE MODIFIED, NO DATABASE MUTATIONS, NO GIT WRITES, NO META API MUTATIONS.**
