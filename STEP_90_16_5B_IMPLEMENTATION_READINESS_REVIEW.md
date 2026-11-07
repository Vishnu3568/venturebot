# VentureBot — Step 90.16.5B: Security Correction and Implementation Readiness Review

**Document ID:** `STEP_90_16_5B_IMPLEMENTATION_READINESS_REVIEW.md`  
**Phase:** Phase B — Security Architecture Second Adversarial Review & Implementation Readiness  
**Repository:** `E:\Project Folder\venturebot`  
**Baseline Commit:** `a94a1b920eabfa608e7ddbcefcff0ccea474dfe8`  
**Author:** Vishnu / Antigravity Autonomous Security Architecture  
**Execution Status:** READ-ONLY — Zero application code modified, zero migrations executed, zero Meta mutations.

---

## 1. Executive Summary & Implementation Readiness Verdict

### Final Verdict: **CONDITIONALLY APPROVED FOR STEP 90.16.6 IMPLEMENTATION**

This second adversarial review resolves the remaining edge cases, timing vulnerabilities, and crash-boundary ambiguities identified in Step 90.16.5 and Step 90.16.5A.

### Core Architecture Resolutions Established in this Review:
1. **Crash-Safe Transaction / Network Separation:** The database transaction boundary is strictly decoupled from network I/O. Atomic ticket consumption and intent creation occur in a single local database transaction *prior* to network socket creation, ensuring that tickets cannot be replayed across process crashes.
2. **Four-State Remote Dispatch Model:** Formalized explicit states: `NOT_DISPATCHED`, `DISPATCH_POSSIBLE`, `OUTCOME_UNKNOWN`, and `CONFIRMED`. No process crash is ever classified as a definite failure without remote verification.
3. **Reconciliation-First Meta Absence Invariant:** A bounded series of 404 responses is **NOT** treated as proof of absence due to geo-distributed Meta replication lag. Unresolved 404 states permanently block downstream mutation dispatch until authenticated operator resolution.
4. **Adapter Cryptographic Parameter Binding:** The HMAC-SHA256 signature binds the exact HTTP method, target Graph API URL endpoint, and SHA-256 digest of the request body, verified inside `MetaMarketingApiAdapter._execute_request` before any TCP socket is opened.
5. **Explicit In-Process Trust Boundaries:** Documented that HMAC protects against caller identity forgery, parameter tampering, and replay, but cannot serve as a sandbox against malicious code executing within the same Python process memory space.

---

## 2. Crash-Safe Dispatch Boundary Specification

### 2.1 The Seven-Step Durable Dispatch Sequence

```
Step 1: Capability Validation
        │ (Read-only validation of ticket signature, expiration, and experiment scope)
Step 2: Atomic Consumption & Intent Ledger Commit (DB Transaction 1)
        │ (UPDATE mutation_capabilities ... ; INSERT INTO mutation_intent_ledger ... COMMIT)
Step 3: In-Process & Cross-Process Mutual Exclusion
        │ (Acquire _TIER_THREAD_LOCKS mutex and DeploymentClaimORM lease)
Step 4: Transition to DISPATCHING (DB Transaction 2)
        │ (Intent state marked MUTATION_DISPATCHING; release all DB locks)
Step 5: Network Request Transmission (Zero Open DB Transactions)
        │ (urllib.request / Meta Marketing API POST)
Step 6: Remote Response Receipt & Status Evaluation
        │ (Evaluate HTTP status code and response payload)
Step 7: Local Persistence of Result (DB Transaction 3)
          (Update ExternalExecution, ExternalExecutionEvents, mark intent CONFIRMED/REJECTED)
```

### 2.2 Boundary-by-Boundary Crash Analysis

| Crash Boundary | What is Durably Stored? | Could Meta Have Received Request? | Can System Safely Retry? | Required Reconciliation Protocol | Dispatch Classification |
| :--- | :--- | :---: | :---: | :--- | :---: |
| **B1: Crash during Step 1 (Validation)** | Nothing changed. Ticket is `ISSUED`. | **NO** | **YES** | None. Ticket remains valid until `expires_at`. | `NOT_DISPATCHED` |
| **B2: Crash during Step 2 (Atomic Consume + Intent)** | Ticket is `CONSUMED`, Intent is `INTENT_CREATED`. | **NO** | **NO** | On restart, reconciler verifies no remote resource exists. Transitions intent to `NOT_DISPATCHED_ABORTED`. Operator must re-authorize. | `NOT_DISPATCHED` |
| **B3: Crash during Step 3/4 (Lock lease / Pre-Dispatch)** | Ticket is `CONSUMED`, Intent is `MUTATION_DISPATCHING`. | **NO** | **NO** | On restart, claim is expired by TTL. Reconciler verifies Meta absence. Marks intent `MUTATION_ABORTED_PRE_DISPATCH`. | `NOT_DISPATCHED` |
| **B4: Crash during Step 5 (In-Flight HTTP Request)** | Ticket is `CONSUMED`, Intent is `MUTATION_DISPATCHING`. | **YES** | **NEVER** | **HALT DISPATCH.** Transition to `OUTCOME_UNKNOWN`. Reconciler queries Meta for deterministic name. If found: adopt. If 404: block. | `DISPATCH_POSSIBLE` |
| **B5: Crash during Step 6 (Response in memory, pre-DB)** | Ticket is `CONSUMED`, Intent is `MUTATION_DISPATCHING`. | **YES (Confirmed)** | **NEVER** | Remote resource exists on Meta in `PAUSED` status. On restart, reconciler finds exact match, adopts ID, persists `ExternalExecution`. | `OUTCOME_UNKNOWN` |
| **B6: Crash during Step 7 (Local persistence)** | DB transaction in progress. Rolls back on crash. | **YES (Confirmed)** | **NEVER** | Same as B5: reconciler reads Meta, repairs local DB state, and marks intent `MUTATION_CONFIRMED`. | `OUTCOME_UNKNOWN` |
| **Post-Commit: Normal completion** | `ExternalExecution` is `DEPLOYED`, Intent is `CONFIRMED`. | **YES** | N/A | System in verified terminal state. | `CONFIRMED` |

---

## 3. Remote Reconciliation and Absence Verification

### 3.1 Failure of Bounded 0, 2, 5, 10s Lookup Schedules
Due to Meta's geo-distributed replication across global data center regions (TAO database caching layer):
- Write operations committed in US-West may take 15–30+ seconds to propagate to regional Graph API read replicas during high load or cross-region traffic routing.
- **Rule:** A bounded sequence of 404 responses over 10 seconds **MUST NOT** be treated as definitive proof that the resource was not created. Treating it as absent and issuing a second creation request causes duplicate ad set creation and budget overspend.

### 3.2 Criteria for a Positive Resource Match
To adopt a remote resource and transition to `CONFIRMED`, the remote entity must satisfy **ALL** four criteria:
1. **Deterministic Name Exact Match:**
   - Must match exact canonical pattern: `^VB-EXP-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-ADSET-v2$`
   - The embedded UUID must match the exact `experiment_id` of the intent. Prefix or fuzzy matching is strictly rejected.
2. **Parent Ownership Match:**
   - `campaign_id` returned by Meta must match canonical parent campaign ID `120252176243860380`.
3. **Status Invariant:**
   - Ad set `status` returned by Meta must be strictly `PAUSED`. If status is `ACTIVE`, adoption fails closed with `UNEXPECTED_RESOURCE_STATUS` to prevent unreviewed flight delivery.
4. **Budget Invariant:**
   - Lifetime budget must not exceed approved experiment allocation (₹200.00 INR).

### 3.3 Handling Repeated 404 Responses and Permanent Blocking
1. **Exponential Polling Schedule:** Reconciler polls at 0s, 5s, 15s, 30s, and 60s (5 bounded attempts over 60 seconds).
2. **Persistent 404 Outcome:**
   - If 404 persists after 60 seconds, the operation transitions to:
     `RECONCILIATION_UNRESOLVED_BLOCKED`
   - **Permanent Blocking Rule:** An unresolved operation **PERMANENTLY BLOCKS** any new mutation dispatch for that experiment and that deployment tier.
   - **Resolution Requirement:** Unblocking strictly requires an explicit, authenticated operator action:
     ```bash
     python -m venturebot.cli reconciliation resolve --experiment-id <id> --tier adset --action DECLARE_ABSENT
     ```
     This command requires interactive master passphrase entry, audit logs the human operator's decision, and marks the intent `MANUALLY_RESOLVED_ABSENT`.

---

## 4. HTTP Response Classification Matrix

| HTTP Status / Network Event | Remote Resource State | Classification | Evidence & Underlying Architecture | Required System Action |
| :--- | :--- | :---: | :--- | :--- |
| **HTTP 200 OK** | Created / modified | **CONFIRMED SUCCESS** | Response body contains valid JSON with numeric `id`. | Persist resource ID; advance state to `DEPLOYED` / `PARTIAL_*`. |
| **HTTP 400 Bad Request** *(Validation)* | Not created | **CONFIRMED REJECTION** | Error subcode indicates parameter error (e.g. invalid targeting, bad bid). | Mark intent `MUTATION_REJECTED`. Record error in audit ledger. |
| **HTTP 400 Bad Request** *(Duplicate Name)* | Created | **RECONCILIATION REQUIRED** | Error subcode indicates name collision (`"An ad set with this name already exists"`). | Resource exists on Meta! Invoke reconciler to adopt existing ID. |
| **HTTP 401 Unauthorized / 403 Forbidden** | Not created | **CONFIRMED REJECTION** | Token expired or permission lacking. Gateway halted before write. | Mark intent `MUTATION_REJECTED`. Alert operator for token refresh. |
| **HTTP 429 Too Many Requests** | Not created | **CONFIRMED REJECTION** | Meta rate-limiting proxy rejects request before execution tier. | Mark intent `MUTATION_REJECTED`. Enforce rate-limit cooldown. |
| **HTTP 500 Internal Server Error** | **Unknown** | **RECONCILIATION REQUIRED** | Meta database may have committed transaction before internal server crash. | **DO NOT RETRY.** Mark `OUTCOME_UNKNOWN`. Reconcile by deterministic name. |
| **HTTP 502 / 503 / 504 (Gateway / Proxy)** | **Unknown** | **RECONCILIATION REQUIRED** | Intermediary proxy timed out waiting for backend; backend may have succeeded. | **DO NOT RETRY.** Mark `OUTCOME_UNKNOWN`. Reconcile by deterministic name. |
| **Socket Timeout (Read Timeout)** | **Unknown** | **RECONCILIATION REQUIRED** | POST bytes transmitted; TCP socket closed while awaiting response. | **DO NOT RETRY.** Mark `OUTCOME_UNKNOWN`. Reconcile by deterministic name. |
| **Connect Timeout (TCP Handshake)** | Not created (usually) | **RECONCILIATION REQUIRED** | Handshake failed before HTTP transmission, but proxy may have buffered. | Reconcile once. If 404 confirmed, mark `MUTATION_REJECTED`. |
| **TCP Reset (RST) / Connection Aborted** | **Unknown** | **RECONCILIATION REQUIRED** | Connection dropped during transmission; server state uncertain. | Reconcile by deterministic name before declaring failure. |
| **Malformed Response (Truncated JSON)** | Created / modified | **RECONCILIATION REQUIRED** | HTTP 200 header received, but socket closed mid-payload. | Reconcile by deterministic name to retrieve created resource ID. |

---

## 5. Cryptographic Ticket Schema & Adapter Boundary

### 5.1 Exact Canonical JSON Payload Specification
The `MutationCapability` ticket binds every dimension of the remote mutation:

```json
{
  "budget_ceiling_amount": "200.00",
  "budget_ceiling_currency": "INR",
  "exact_operation": "DEPLOY_RECOVERY_ADSET_DORMANT",
  "expected_parent_id": "120252176243860380",
  "experiment_id": "49fde874-9387-5056-934c-51a9cfca164f",
  "expires_at": "2026-11-06T20:05:00Z",
  "http_method": "POST",
  "issued_at": "2026-11-06T20:00:00Z",
  "key_id": "k1",
  "nonce": "a7f3b89012cd4e5f6a7b8c9d0e1f2a3b",
  "operator_id": "c1a2b3c4-d5e6-7f8a-9b0c-1d2e3f4a5b6c",
  "parameters_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "replacement_name": "VB-EXP-49fde874-9387-5056-934c-51a9cfca164f-ADSET-v2",
  "target_endpoint": "/act_1985595022114520/adsets",
  "target_tier": "ADSET",
  "ticket_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"
}
```

### 5.2 Canonical Serialization Rules (RFC 8785)
1. Alphabetical sorting of JSON keys (`sort_keys=True`).
2. Exact separators without whitespace: `separators=(',', ':')`.
3. Floats strictly prohibited; monetary amounts formatted as fixed-point Decimal strings (`"200.00"`).
4. Timestamps formatted strictly as RFC 3339 UTC ISO strings with trailing `"Z"`.
5. Unicode normalized to Form C (`NFC`).
6. Signed using `hmac.new(master_key, canonical_bytes, hashlib.sha256).hexdigest()`.

### 5.3 Adapter Low-Level Verification (`MetaMarketingApiAdapter._execute_request`)
Before opening any network socket or invoking `urllib.request`, the adapter verifies:
1. `ticket_token` string parameter is present.
2. Token signature is verified against master key via `hmac.compare_digest`.
3. `ticket.http_method == req.get_method()`.
4. `ticket.target_endpoint in req.full_url`.
5. `ticket.parameters_digest == hashlib.sha256(req.data or b'').hexdigest()`.
6. `ticket.expires_at >= datetime.now(timezone.utc)`.
7. If any check fails, immediately raises `MetaApiSafetyError` without network I/O.

### 5.4 Explicit In-Process Security Limits of HMAC
- **What HMAC Guarantees:**
  - Guarantees message integrity and authenticity against tampering between approval and dispatch.
  - Prevents caller identity forgery and ticket replay.
  - Guarantees that parameter tampering invalidates execution.
- **What HMAC CANNOT Guarantee:**
  - **In-process memory protection:** Python is not a memory-safe sandbox. Any malicious code running in the same Python process can inspect live RAM, extract the HMAC master key, monkeypatch `MetaMarketingApiAdapter._execute_request`, or hook `urllib.request`.
  - **Non-repudiation:** Because HMAC is symmetric, the backend and signer share the secret key.
  - **Trust Root Assumption:** Operating system process isolation and Python binary integrity are assumed trust boundaries.

---

## 6. SQLite Durability, Transactions, and Concurrency

### 6.1 SQLite Durability Configuration
To guarantee persistence across power loss or process crashes:
```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 10000;
PRAGMA foreign_keys = ON;
```

### 6.2 Atomic Consumption Transaction Boundary
Ticket consumption and intent creation must execute in a single immediate transaction:
```sql
BEGIN IMMEDIATE;

-- 1. Atomic Ticket Consumption
UPDATE mutation_capabilities
SET status = 'CONSUMED',
    consumed_at = :now
WHERE id = :ticket_id
  AND status = 'ISSUED'
  AND expires_at >= :now;

-- If rows_updated == 0: ROLLBACK and raise TICKET_ALREADY_CONSUMED_OR_EXPIRED

-- 2. Insert Durable Intent
INSERT INTO mutation_intent_ledger (
    id, ticket_id, experiment_id, tier, state,
    parameters_digest, created_at, updated_at
) VALUES (
    :intent_id, :ticket_id, :experiment_id, :tier, 'MUTATION_DISPATCHING',
    :digest, :now, :now
);

COMMIT;
```

### 6.3 Handling SQLite Lock Contention
- All write operations use `BEGIN IMMEDIATE` to prevent deadlock.
- If SQLite raises `sqlite3.OperationalError: database is locked`, the service retries with exponential backoff and jitter (5 attempts, 50ms–250ms) before failing closed.
- **Zero database connections are held open during remote HTTP calls.**

### 6.4 Startup Recovery of Unresolved Intents
On application initialization, `StartupReconciliationService` scans:
```sql
SELECT * FROM mutation_intent_ledger WHERE state = 'MUTATION_DISPATCHING';
```
- If any intent is found in `MUTATION_DISPATCHING`, the bot halts automated startup, locks the tier, and launches the reconciliation protocol.

---

## 7. Emergency Stop and Operator Throttling Recovery

### 7.1 Emergency Stop Safety Invariants
1. **Unrestricted Availability:** Emergency stop is available to **ALL** authenticated operator roles (`ROLE_VIEWER`, `ROLE_OPERATOR`, `ROLE_ADMIN`).
2. **Zero Approval Friction:** Emergency stop does **NOT** require generating a `MutationCapability` ticket.
3. **Audit Provenance:** Records an append-only `ExternalExecutionEvent` with `event_type=STATUS_CHANGED, reason="EMERGENCY_STOP"`.
4. **Idempotency:** Re-executing emergency stop on an already paused/killed campaign returns success cleanly.
5. **SAFE_MODE Respect:** While `VENTUREBOT_SAFE_MODE=true` is active, automated remote mutations are blocked. The CLI instructs the operator to perform the pause via Meta Ads Manager.

### 7.2 Throttling Recovery Without Denial-of-Service
To prevent an attacker or buggy script from locking out the human operator:
- Throttling delay is computed per-operator as memory-hard delay: $\min(30.0, 2^{\text{attempts}-1})$ seconds.
- Account is **never hard-locked**.
- Emergency stop commands from an interactive console terminal bypass password throttling if authenticated via the offline recovery seed or console master key.

---

## 8. Corrected State-Transition Architecture Diagram

```
                             [ TICKET ISSUANCE ]
                                      │
                                      ▼
                             [ APPROVAL_ISSUED ]
                                      │
               ┌──────────────────────┴──────────────────────┐
               ▼                                             ▼
      (Time > 5 minutes)                       (Passed to Dispatch Service)
     [ APPROVAL_EXPIRED ]                                    │
                                                             ▼
                                                [ ATOMIC DB TRANSACTION 1 ]
                                                • UPDATE capability -> CONSUMED
                                                • INSERT intent -> DISPATCHING
                                                             │
                                                             ▼
                                                   [ MUTATION_DISPATCHING ]
                                                             │
                                    ┌────────────────────────┴────────────────────────┐
                                    ▼                                                 ▼
                         (Crash Before Socket)                             (HTTP POST Transmitted)
                         [ NOT_DISPATCHED ]                                           │
                                    │                                                 ▼
                                    ▼                                      [ DISPATCH_POSSIBLE ]
                           (Startup Reconciler)                                       │
                                    │                                 ┌───────────────┴───────────────┐
                                    ▼                                 ▼                               ▼
                         [ MUTATION_ABORTED ]                 (HTTP 200 OK)              (Timeout / 500 / RST)
                                                                      │                               │
                                                                      ▼                               ▼
                                                            [ MUTATION_CONFIRMED ]         [ OUTCOME_UNKNOWN ]
                                                                      │                               │
                                                                      ▼                               ▼
                                                          [ ATOMIC DB TRANS 2 ]             (Startup Reconciler)
                                                          • ExternalExecution DEPLOYED                │
                                                          • Events RECORDED               ┌───────────┴───────────┐
                                                          • Intent CONFIRMED              ▼                       ▼
                                                                                   (Found Match)           (Persistent 404)
                                                                                          │                       │
                                                                                          ▼                       ▼
                                                                                  [ ADOPT & CONFIRM ]   [ RECONCILIATION_BLOCKED ]
                                                                                                                  │
                                                                                                                  ▼
                                                                                                        (Manual Human Unlock)
```

---

## 9. Remaining Risks & Explicit Trust Assumptions

| Threat / Risk | Mitigated? | Architectural Reality & Boundary Definition |
| :--- | :---: | :--- |
| **Identity Forgery** | **YES** | Caller-supplied flags eliminated; operator verified via `hashlib.scrypt` session tokens. |
| **Ticket Tampering** | **YES** | HMAC-SHA256 signature verification over canonical RFC 8785 JSON representation. |
| **Ticket Replay** | **YES** | Atomic database status transition to `CONSUMED` before dispatch; unique nonce check. |
| **Network Drop Duplicate** | **YES** | `OUTCOME_UNKNOWN` classification halts dispatch; deterministic naming adoption prevents duplicate creation. |
| **In-Process Python Bypass** | **PARTIALLY** | Gated at `MetaMarketingApiAdapter._execute_request`, but process memory inspection is outside Python boundary. |
| **Host OS Admin Compromise** | **OUTSIDE** | Local root/admin can hook Python runtime, read RAM, or alter SQLite files. Host OS integrity is an assumed trust root. |
| **Meta Remote Replication Lag** | **YES** | Exponential backoff (0s to 60s) + permanent block on unresolved 404 prevents duplicate dispatch. |

---

## 10. Implementation Prerequisites for Step 90.16.6

Before writing production application code in Step 90.16.6:
1. **Database Schema Definitions:**
   - `operators` (id, username, password_hash, role, failed_attempt_count, is_active, created_at).
   - `operator_sessions` (id, operator_id, token_hash, created_at, expires_at, last_activity_at, revoked_at).
   - `mutation_capabilities` (id, operator_id, experiment_id, tier, exact_operation, parameters_digest, signature, nonce, status, issued_at, expires_at, consumed_at).
   - `mutation_intent_ledger` (id, ticket_id, experiment_id, tier, state, remote_resource_id, last_error, created_at, updated_at).
2. **Standard Library Cryptographic Engine:**
   - `backend/venturebot/security/crypto.py` utilizing `hashlib.scrypt`, `hmac`, `secrets`, and `hashlib.sha256`.
3. **Preservation of SAFE_MODE:**
   - `VENTUREBOT_SAFE_MODE=true` must remain strictly enabled throughout Step 90.16.6 implementation.

---

**SECURITY CORRECTION REVIEW COMPLETE — NO APPLICATION CODE MODIFIED, NO DATABASE MUTATIONS, NO GIT WRITES, NO META API MUTATIONS.**
