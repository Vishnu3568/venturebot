# VENTUREBOT ARCHITECTURE & GUARDRAILS
*Canonical Architectural Source of Truth for VentureBot*

> **CRITICAL INSTRUCTION FOR ALL AGENTS & DEVELOPERS:**
> Every future Antigravity implementation session and coding agent **MUST read this document** alongside `VENTUREBOT_PROJECT_STATE.md` before designing or implementing any changes. This document governs project identity, architectural boundaries, capital safety, agent taxonomy, and development principles.

---

## 1. Project Identity

- **Project:** VentureBot
- **Purpose:** Build a controlled autonomous opportunity-discovery and experimentation system that starts with **₹1,000 of capital** and systematically searches for legitimate, repeatable revenue opportunities.

The long-term objective is to transform small initial capital into a sustainable, compounding income-generating system through evidence-driven experimentation.

### The system must optimize for:
- Measurable revenue
- Measurable profit
- Repeatability
- Capital efficiency
- Learning from failed experiments
- Controlled scaling

### The system must NOT optimize merely for:
- Number of ideas generated
- Impressions, views, or followers
- Vanity metrics
- Number of agents spawned
- Architectural complexity
- High volume of AI-generated output

---

## 2. Core Philosophy

The central execution loop of VentureBot is:

```text
OPPORTUNITY
  ↓
RESEARCH
  ↓
HYPOTHESIS
  ↓
EXPERIMENT
  ↓
CAPITAL ALLOCATION
  ↓
EXECUTION
  ↓
MEASUREMENT
  ↓
PROFIT/LOSS ANALYSIS
  ↓
DECISION (KILL / ITERATE / SCALE / HOLD)
  ↓
LEARNING & MEMORY
  ↓
FUTURE OPPORTUNITY DISCOVERY
```

### Experimentation Mandates
Every experiment must have:
- A clear, testable hypothesis
- A defined allocated budget
- A maximum allowed spend (hard ceiling)
- Measurable success criteria
- Measurable failure criteria
- Measurable results
- An evidence-based decision

### Loss Philosophy
- Losses must produce actionable information.
- A losing experiment must **never** automatically receive more capital.
- Repeated losses without meaningful learning or progress must result in definitive termination (KILL).

---

## 3. Starting Capital

- **Starting Capital:** **₹1,000.00**
- The system must treat this starting sum as **real, constrained capital**.
- **Capital protection is mandatory.** The bot must never blindly deploy or exhaust the entire capital pool.
- Future autonomous execution must strictly operate within explicit, verified spending limits and safety guardrails.
- **Zero-Bypass Rule:** No agent, workflow, script, or component may ever bypass the capital management ledger or spending controls.

---

## 4. The System Is NOT a Niche Bot

VentureBot must **never permanently specialize** in any single category, industry, or niche.

### Hard Prohibition
Do not hard-code niche branches or static category logic:
```python
# FORBIDDEN:
if category == "horror": ...
if category == "gaming": ...
if category == "fitness": ...
```

The system must discover opportunities dynamically across a wide spectrum of potential domains, including (but not limited to):
- Entertainment
- Gaming
- Sports
- Fashion & Beauty
- Fitness & Health
- Relationships & Dating
- Travel & Local Guides
- Food & Cooking
- Movies, Anime & Fandom
- Music & Audio
- Spirituality & Mindfulness
- Hobbies & Crafts
- Memes & Pop Culture
- News & Information
- Technology & Software
- Education & Skill Building
- Personal Finance & Productivity
- Lifestyle

*Note: This list is illustrative, not exhaustive. The architecture must allow completely new and unforeseen categories to emerge organically from data.*

---

## 5. Core Architecture

The end-to-end system flow is coordinated as follows:

```text
VentureBot Orchestrator
         ↓
Opportunity Discovery
         ↓
Opportunity Analysis
         ↓
Experiment Design
         ↓
Capital Manager (Ledger & Guardrails)
         ↓
Experiment Execution
         ↓
Measurement
         ↓
Performance Analysis
         ↓
Decision Engine (KILL / ITERATE / SCALE / HOLD)
         ↓
Learning & Memory
         ↓
Future Opportunity Discovery
```

The Orchestrator coordinates the overall lifecycle. Individual agents or sub-components must **not** independently redefine project direction, budgets, or architectures.

---

## 6. Agent Architecture

The system must **not** spawn dozens of permanently running background agents. It relies on a lean set of well-defined core responsibilities:

### 1. Venture Manager / Orchestrator
- Coordinates the workflow end-to-end.
- Determines which capability or analysis is required next.
- Requests research, initiates experiments, coordinates measurement, and requests performance reviews.
- Maintains and reports overall project state.
- **Constraint:** Strictly bound by financial controls; cannot bypass the Capital Manager.

### 2. Trend Scout
- Discovers emerging attention, shifts, and audience behavior.
- Identifies growing categories and distribution channels.
- Collects verifiable evidence (search volume, social momentum, forum activity).
- **Constraint:** Must rigorously distinguish verified evidence from speculation.

### 3. Opportunity Analyst
- Evaluates whether raw attention can be transformed into a viable business.
- Analyzes competitive landscape, distribution difficulty, and production feasibility.
- Evaluates monetization models, cost structures, and operational/regulatory risks.
- Produces structured, comparable opportunity assessments.

### 4. Experiment Designer
- Translates approved opportunities into small, controlled tests.
- Formulates hypotheses, proposed channels, and monetization methods.
- Defines strict budgets, hard spending caps, success criteria, and failure triggers.
- Outlines clear, measurable milestones.

### 5. Performance Analyst
- Analyzes experiment metrics (impressions, clicks, conversions, revenue, costs).
- Computes accurate financial performance and identifies conversion bottlenecks.
- Dissects profit/loss and documents root causes.
- Recommends structured decisions (`KILL`, `ITERATE`, `SCALE`, or `HOLD`) backed by data.

### 6. Capital Manager
- Maintains and enforces the authoritative financial ledger.
- Enforces experiment budgets and prevents unauthorized spending.
- Records all capital movements (inflows, outflows, revenues, costs, refunds).
- Allocates capital only under explicit authorization rules.
- **Constraint:** Authoritative control over money; no component or agent may bypass it.

---

## 7. Dynamic Specialist Agents

When a task requires deep domain knowledge, **ephemeral/dynamic specialist agents** may be instantiated on demand.

Examples include:
- Gaming Research Specialist
- Horror Content Specialist
- Affiliate Monetization Specialist
- Ad Creative Specialist
- SEO Specialist
- Landing Page Specialist

### Rules for Specialists:
- They are temporary, task-scoped capabilities, not permanent daemons.
- Do **not** create permanent classes, modules, or services for every conceivable niche.
- The Orchestrator alone determines when domain specialization is warranted and dissolves the specialist once the task concludes.

---

## 8. Opportunity Scoring

Opportunities may eventually be scored across structured dimensions:
- Trend strength & momentum
- Target audience size & accessibility
- Category growth indicators
- Competitive intensity
- Monetization viability & diversification
- Production difficulty & initial costs
- Distribution difficulty & channel costs
- Automation potential
- Platform dependency & lock-in risks
- Regulatory, legal, and policy risks
- Expected margins & unit economics
- Confidence rating (0.0 to 1.0)

### Scoring Rules:
- Scores are **decision-support heuristics**, never guarantees of profitability.
- Do not invent speculative scoring algorithms or weighted formulas unless explicitly defined and approved in a project step.

---

## 9. Experiment System

Every experiment record must maintain:
- Unique experiment ID
- Linked opportunity ID
- Clear hypothesis & measurable objective
- Proposed distribution channel & monetization method
- Explicitly separated:
  - `allocated_budget` (funds reserved)
  - `max_allowed_spend` (absolute ceiling)
  - `actual_spend` (real money disbursed)
- Pre-defined success criteria & failure criteria
- Recorded metrics, revenue, costs, and net profit/loss
- Final recorded decision

### Experiment Execution Rules:
- Initial experiments must be small and low-risk.
- Capital allocation increases only when empirical evidence demonstrates positive ROI.

---

## 10. Financial System & Ledger

The financial ledger (`CapitalTransaction` and `CapitalRepository`) is the **authoritative record** of all internal capital movements.

### Immutable Financial Distinctions:
- **Revenue ≠ Profit:** Revenue is total money earned; Profit is revenue minus costs.
- **Allocated Budget ≠ Actual Spend:** Budget is reserved; spend is disbursed cash.
- **Capital Balance ≠ Profit:** Balance reflects net cash pool; profit reflects experiment performance.
- **ROI ≠ ROAS:** ROI measures net return on investment (`net_profit / cost`); ROAS measures ad revenue over ad spend (`revenue / ad_spend`).

### Accounting Standards:
- Exact monetary representations (`Decimal`) must be used at all times. Floating-point arithmetic for currency is strictly prohibited.
- The ledger is **append-only**. Historical transactions are immutable and must never be edited or deleted to "correct" balances.
- Any legitimate correction must be recorded as an explicit compensating transaction.

---

## 11. Dashboard Vision

The future dashboard provides immediate, transparent insight into VentureBot's financial and experimental health.

### Core Metrics Dashboard Must Present:
- Starting capital (₹1,000.00)
- Current available balance (liquid capital)
- Deployed / allocated capital
- Total capital inflows and outflows
- Total revenue and total costs
- Net profit/loss and overall ROI
- Experiment status breakdown (Active, Completed, Paused, Killed)
- Performance comparisons (winning vs. losing experiments)
- Documented reasons for losses
- Recommended next actions
- Complete, auditable transaction history

### The Questions the Dashboard Must Answer:
1. *"Where did every rupee go?"*
2. *"How much money have we made?"*
3. *"How much money have we lost, and why?"*
4. *"What concrete changes must be made for the next test?"*
5. *"Which experiments have earned additional capital allocation?"*
6. *"Which experiments must be immediately terminated?"*
7. *"Is our revenue becoming predictable and repeatable?"*

*The dashboard is an operational decision cockpit, not a display of superficial visual vanity.*

---

## 12. Loss Recovery Philosophy

When an experiment experiences a loss, VentureBot must conduct a structured bottleneck analysis.

### Potential Bottlenecks to Diagnose:
- Insufficient traffic / reach
- Low click-through rate (poor creative or messaging)
- Weak offer or value proposition
- Poor landing page user experience or friction
- Low conversion rate
- Misaligned pricing structure
- Flawed monetization mechanic
- Unsustainably high customer acquisition cost (CAC)
- Poor retention or zero repeat interest
- Platform distribution penalty or policy shift

### Rule:
The system proposes a targeted, testable iteration **only when empirical evidence points to a specific bottleneck**. Retrying a failed experiment blindly or hoping for different results is forbidden.

---

## 13. Scaling Philosophy

**A profitable experiment is NOT automatically a scalable business.**

Before allocating larger capital pools, the system must verify:
- Repeatability across different audience cohorts
- Sustainable unit economics at higher volume
- Customer acquisition cost stability (checking for diminishing returns)
- Revenue consistency over time
- Gross and net profit margins
- Retention and churn rates
- Platform dependency risks
- Operational and delivery complexity
- Cash-flow cycle and capital requirements

Scaling must be incremental, controlled, and reversible.

---

## 14. V0 → V4 Roadmap

```text
V0 (Current): Research & Decision Foundation
    └── Data contracts, append-only SQLite financial ledger, ₹1,000 capital tracking,
        architecture lock, documentation. No autonomous spending.

V1: Experiment Generation
    └── Opportunity-to-experiment pipeline, structured experiment specifications,
        controlled asset/creative generation, approval workflows. Still controlled.

V2: Controlled Execution
    └── Distribution channel integrations, supervised campaign runs, revenue tracking,
        strict per-experiment spending limits.

V3: Feedback & Optimization
    └── Automated loss diagnosis, bottleneck detection, experiment iteration loops,
        historical learning repository, refined capital allocation models.

V4: Portfolio & Scale Management
    └── Multi-venture concurrent portfolio, dynamic specialist agents, portfolio-level
        treasury management, automated scaling within human-defined bounds.
```

*Never skip ahead to future phases without completing and verifying prerequisite stages.*

---

## 15. Technology Principles

- **Backend:** Python (≥ 3.11)
- **Data Validation:** Pydantic (v2) contracts
- **Database / ORM:** SQLAlchemy 2.x with SQLite for V0 (zero infrastructure, local file/in-memory). Clear migration path to PostgreSQL when scale requires it.
- **Frontend / Dashboard (Future):** React + modern lightweight tooling (e.g. Vite).
- **Simplicity Over Cleverness:** Boring, reliable, testable code wins. Avoid complex metaprogramming, obscure ORM hooks, or unneeded abstractions.
- **Minimal Dependencies:** Use Python standard library (`pathlib`, `json`, `uuid`, `datetime`, `decimal`, `sqlite3`) before reaching for third-party packages.
- **Every dependency added must solve a concrete, immediate requirement.**

---

## 16. Security and Financial Safety

To protect capital and integrity, VentureBot enforces:
- **Global Emergency STOP:** Ability to immediately halt all outbound operations and spending.
- **Hard Spend Caps:** Absolute per-experiment limit, daily spending limit, and aggregate deployment limits.
- **Approval Thresholds:** High-value actions require explicit human sign-off.
- **Auditable Ledger:** Every money movement is permanently logged with timestamps and descriptions.
- **API Credential Isolation:** Secrets stored in environment variables, never checked into version control.
- **No Unrestricted Autonomous Spending:** Autonomous money movement is strictly disabled in early versions and permanently capped in later versions.

---

## 17. Evidence Rules

VentureBot explicitly distinguishes between cognitive categories:

| Category | Definition | Standard |
|---|---|---|
| **FACT** | Verified, measurable historical data | Must have a source reference or ledger record |
| **INFERENCE** | Logical deduction derived from facts | Must document underlying facts and deduction path |
| **HYPOTHESIS** | Testable premise for an experiment | Must specify explicit success and failure criteria |
| **PREDICTION** | Expected outcome or forward estimate | Must carry confidence rating and explicit uncertainty |

### Prohibition on Fabrication:
Agents must **never** hallucinate or fabricate:
- Market size or growth figures
- User counts, views, or engagement metrics
- Revenue, profit, or conversion rates
- Competitor activity or pricing
- API capabilities or experiment results

*If empirical evidence is unavailable, explicitly label the field as `UNKNOWN` or `UNVERIFIED`.*

---

## 18. Anti-Hallucination & Execution Rules

Before making changes, every future coding agent must:
1. Read `VENTUREBOT_ARCHITECTURE.md`.
2. Read `VENTUREBOT_PROJECT_STATE.md`.
3. Inspect the repository to verify current reality.
4. Identify existing code before adding new utilities (ladder of abstraction).
5. Adhere strictly to the requested scope of the current step.
6. Refrain from creating speculative code, empty placeholders, or fake implementations.
7. Run all tests before and after making changes.
8. Document all assumptions and explicit omissions.
9. Stop and report when the requested step is complete.

> **Conflict Resolution:** If a prompt instruction conflicts with this architecture document, **STOP and report the conflict** to the user rather than silently modifying architectural guardrails.

---

## 19. Scope Control

VentureBot is built **one verified step at a time**.

### Explicitly Forbidden:
- Building multiple phases simultaneously
- Creating abstractions for single-use scenarios
- Spawning agents before their domain model and tools exist
- Building API endpoints before service logic is verified
- Building UI dashboards before backend contracts and persistent data exist
- Hooking up external payment/advertising APIs prematurely
- Adding infrastructure (e.g. Redis, Kafka, Celery, Docker) without direct necessity

---

## 20. Definition of Success

Success for VentureBot is **not** defined as:
> *"Building an elaborate AI agent platform with complex interactions."*

Success is defined as:
> *"Building a reliable, disciplined system capable of discovering, testing, measuring, and progressively scaling legitimate revenue opportunities while uncompromisingly protecting its ₹1,000 capital base."*

---

## 21. Mandatory Agent Instruction

Every future Antigravity implementation prompt for VentureBot must instruct the coding agent to read:
1. `VENTUREBOT_ARCHITECTURE.md`
2. `VENTUREBOT_PROJECT_STATE.md`

The agent must treat these files as the immutable source of truth. After completing each step, the agent must report:
- What was planned
- What was implemented
- What was intentionally omitted / not implemented
- Test execution results
- Assumptions made
- Any architectural deviations (or explicit statement that zero deviations occurred)
- Current repository state (`git status --short`)

---

## 22. External Execution Identity, Reconciliation & Retry Invariants (Step 39.3 Contract Lock)

To guarantee idempotency and eliminate duplicate resource creation during network interruptions, timeouts, and process crashes, VentureBot locks the following external execution rules:

### 1. The Global Reconciliation Invariant
An external write (POST) is allowed **ONLY** after the system has established:
`"The intended external resource does not currently exist."`
- Lookup failures, socket timeouts, HTTP 403, 429, 500, or network errors must **NEVER** be interpreted as `NOT_FOUND`.
- `UNKNOWN` status strictly mandates: **NO POST**.

### 2. Lookup Result Taxonomy
Every reconciliation lookup must evaluate to exactly one of:
- **`NOT_FOUND`**: Verified remote lookup returned zero matching candidates. $\rightarrow$ **POST allowed**.
- **`FOUND_EXACT`**: Verified remote lookup returned exactly one candidate matching all required identity, parent, and status (`PAUSED`) constraints. $\rightarrow$ **ADOPT external ID**.
- **`AMBIGUOUS`**: Lookup returned multiple possible matches or insufficiently distinguishable candidates. $\rightarrow$ **STOP dispatch** (surface error; never guess).
- **`UNKNOWN`**: Lookup failed due to timeout, socket drop, HTTP error, or malformed response. $\rightarrow$ **STOP dispatch** (never POST).

### 3. Hierarchical Identity & Scoping Chain
1. **Experiment:** Canonical UUID (`experiment_id`).
2. **Campaign:** Deterministic name `VB-EXP-<experiment_id>`, scoped to Ad Account.
3. **Ad Set:** Deterministic name `VB-EXP-<experiment_id>-ADSET`, strictly scoped to parent `campaign_id` (supporting fields: `lifetime_budget` in minor currency units/paise, required `end_time`, `billing_event='IMPRESSIONS'`, `optimization_goal='LINK_CLICKS'`).
4. **Creative:** Deterministic name `VB-EXP-<experiment_id>-CREATIVE`, scoped to Ad Account and verified against `object_story_spec`.
5. **Ad:** Deterministic name `VB-EXP-<experiment_id>-AD`, strictly scoped to parent `adset_id`.
6. **Image:** Content-addressed MD5 hash; inherently idempotent by payload checksum.

### 4. Adoption & Retry Semantics
- If an external ID is already known locally in `ExternalExecution`, it is reused without recreation.
- If an external ID is missing locally, remote lookup is attempted before POST. If an exact matching resource exists remotely in `PAUSED` status, its ID is adopted and persisted.
- Retrying an interrupted dispatch always resumes from the first missing/unverified tier; it never replays from scratch.
- Zero financial side effects: reconciliation and adoption never mutate the capital ledger.

---

## 23. Execution Dispatch Architecture & Write Gateway Boundaries (Step 40.1 Consolidation)

To ensure exactly **ONE** unambiguous, auditable authorization and execution path for all external write operations, VentureBot locks the following structural boundaries:

### 1. Component Responsibilities
- **`ExecutionDispatchService` (Tier 1 — Governance & Safety Gate):**
  - The authoritative, channel-agnostic write gateway.
  - Validates experiment existence, domain status (`APPROVED`), allocated capital presence (`allocated_budget > 0`), and budget ceilings (`proposed_budget <= remaining_budget`).
  - Enforces the global `SAFE_MODE` guardrail (`VENTUREBOT_SAFE_MODE`, default: `True`), unconditionally blocking write actions when active.
- **`MetaExperimentDispatchService` (Tier 2 — Channel Deployment Coordinator):**
  - Channel-specific execution coordinator for Meta Marketing API deployments.
  - Enforces Meta pre-dispatch safeguards (`spec.explicit_dispatch_authorized`, account currency `INR`, active account status, lifetime budget and `end_time` bounds).
  - Coordinates sequential creation and reconciliation across the 5 tiers (Campaign $\rightarrow$ Image $\rightarrow$ Ad Set $\rightarrow$ Creative $\rightarrow$ Ad) in controlled `PAUSED` status.
  - Updates `ExternalExecution` persistence after each tier and initiates handoff to `ExperimentExecutionService.start()` upon full deployment.
- **`MetaMarketingApiAdapter` (Low-Level Transport):**
  - Stateless HTTP client communicating directly with Meta Graph API endpoints.
  - Strictly limited to payload construction, URL assembly, network transport, response parsing, and credential sanitization. Contains no lifecycle or business logic.
- **`ExternalExecutionRepository` (State Persistence):**
  - Atomic data access layer for `ExternalExecutionORM` records maintaining 1-to-1 linkage with domain experiments.

### 2. Mandatory Write Call Graph
All future real-world external execution actions must strictly follow this single linear hierarchy:
```
Human Operator / Approved Domain Action
             │
             ▼
ExecutionDispatchService (Tier 1: Generic Safety & SAFE_MODE Gateway)
             │ (validates approval, budget ceiling, checks SAFE_MODE)
             ▼
Channel Dispatcher [MetaExperimentDispatchService] (Tier 2: Deployment Coordinator)
             │ (validates spec, verifies account, reconciles external tiers)
             ▼
Channel Adapter [MetaMarketingApiAdapter] (Low-Level Transport)
             │ (HTTP client)
             ▼
External Platform (Meta Graph API)
```

### 3. Direct Channel Dispatcher Bypass Invariant (Blocking Gap)
- Direct invocation of `MetaExperimentDispatchService` bypassing `ExecutionDispatchService` and `SAFE_MODE` is strictly classified as a **BLOCKING ARCHITECTURE GAP**.
- In the consolidated architecture, `MetaExperimentDispatchService` must never serve as an un-gated entry point. It must either:
  1. Be invoked exclusively by `ExecutionDispatchService`, OR
  2. Embed an explicit `is_safe_mode()` check before initiating any remote write or reconciliation operation.

### 4. SAFE_MODE Semantics & Fail-Closed Adapter Mutation Guard
- **Authoritative Source:** The environment variable `VENTUREBOT_SAFE_MODE` parsed via `is_safe_mode()` in `backend/venturebot/env.py`. It defaults to `True` unless explicitly set to `'false'`, `'0'`, or `'no'`. Any parsing failure or environment error fails closed to `True`.
- **Non-Bypassable Mutation Guard:** Implemented in `is_safe_mode_enforced(caller_safe_mode)` in `backend/venturebot/execution/dispatch.py`. In production runtime, passing `safe_mode=False` to any dispatch or mutation method (`deploy_recovery_dormant`, `activate_recovery_flight`, `lock_schedule`, `dispatch`, `_dispatch_from_gateway`, `emergency_pause_adset`) is unconditionally rejected and overridden by `VENTUREBOT_SAFE_MODE=true`.
- **Elimination of Production Test Bypasses:** In Step 90.12.14, `_TestHarnessContext` was completely removed from `dispatch.py` and `tests/conftest.py` was deleted. Production code contains zero test bypass backdoors. Automated unit test suites testing mocked execution components explicitly use standard pytest `monkeypatch.setenv("VENTUREBOT_SAFE_MODE", "false")` fixtures scoped strictly to their respective test modules.
- **Fail-Closed Adapter Boundary Guard:** In `MetaMarketingApiAdapter._execute_request(req, is_write=True)`, all write requests (POST, PUT, DELETE, PATCH, or `is_write=True`) are checked against `is_safe_mode()` **before transport execution**. If `is_safe_mode()` is active, it raises `MetaApiSafetyError` unconditionally, regardless of whether `self._transport` is None, a real HTTP transport, or a configured/injected transport. If safe mode is disabled, an unmocked adapter (`_transport is None`) is independently blocked by the Step 36 guard (`MetaApiError`). Read-only GET requests continue to function.
- **Conservative Emergency-Stop Policy (Closure):** Because VentureBot currently lacks a cryptographic or session-bound operator identity/authentication boundary (as documented in Step 90.12.10), emergency pause is **NOT** exempted from SAFE_MODE. Under `VENTUREBOT_SAFE_MODE=true`, `ExecutionDispatchService.emergency_pause_adset()` and `MetaExperimentDispatchService._emergency_stop()` fail closed with `SAFE_MODE_ENABLED`. Emergency stop mutations are strictly disabled until a trustworthy, separate authorization boundary exists.
- **Setting `VENTUREBOT_SAFE_MODE=false` Never Authorizes Mutations:** Disabling SAFE_MODE does not bypass domain prerequisites: approved status, budget ceilings, explicit operator authorization, account preflight, and deterministic reconciliation remain mandatory.
- **Reconciliation and Financial Invariant:** Reconciliation and dispatch operations have strictly **zero financial side effects** on the authoritative capital ledger.
- **Remaining RBAC Limitations:** VentureBot currently lacks cryptographic/session-bound operator authentication. Operator IDs remain audited metadata parameters. Higher recovery versions (> 2) remain blocked. Emergency stop remains unavailable in safe mode.

---

## 24. Meta Telemetry Ingestion, Idempotency & Restatement Contract (Step 43.1 / 43.1.1 Contract Lock)

To guarantee measurement accuracy, preserve complete audit provenance, and handle upstream Meta reporting revisions without corrupting data or the financial ledger, VentureBot locks the following telemetry ingestion rules:

### 1. Conceptual Model: Option C (Immutable Observations + Logical Reporting Window Identity)
- VentureBot rejects in-place record overwrites (**Option B**) to preserve historical evidence provenance as required by Section 17 (`EvidenceCategory.FACT`).
- VentureBot rejects indiscriminate snapshot appending (**Option A**) to prevent database bloat and catastrophic distortion of progressive metric analysis.
- VentureBot adopts **Option C**: Every ingested observation is an immutable record, mapped to a deterministic **Logical Reporting Window**. Repeated identical observations are deduplicated (no-op). Legitimate Meta restatements append a new immutable observation with the same logical identity, where the latest record by `recorded_at` represents the authoritative measurement for that window.

### 2. Disambiguation of Key Concepts
1. **Logical Reporting Window:**
   The external reporting scope being observed:
   `experiment_id` + `channel` ('meta') + `account_id` + `object_id` (campaign ID) + `level` ('campaign') + `date_start` + `date_stop`.
2. **Observation:**
   An immutable `EvidenceCategory.FACT` snapshot received from Meta for that logical reporting window at a particular `recorded_at` timestamp, yielding specific numeric measurements: `(spend, impressions, clicks)`.
3. **Performance Checkpoint:**
   A logically distinct reporting window or observation period that can represent chronological progression for performance analysis over time.
   
> [!IMPORTANT]
> **Core Disambiguation Invariant:**
> A newer observation (such as a RESTATEMENT) of the SAME logical reporting window is **NOT** automatically a new performance checkpoint! A restatement represents revised historical accuracy for an existing window, not business progression into a new period.

### 3. Campaign-Level Scope of Canonical Identity (`source_reference`)
- The canonical identifier for a Meta reporting observation window is:
  `meta:insights:campaign:<campaign_id>:<date_start>:<date_stop>`
- **Step 44 Current Scope:**
  This identifier is scoped strictly to campaign-level Meta telemetry for the single campaign deployed per experiment. It is not assumed to be universally sufficient for all future Meta aggregation levels (such as ad set, ad, or breakdown levels); any future multi-level telemetry contracts must be explicitly locked before implementation.
- **API Version Exclusion Invariant:**
  The Meta Graph API version (e.g. `v20.0`) must **NOT** be included in the logical identity string. API versions reflect transport protocols, not the identity of the underlying advertising event. Upgrading the API version must never cause identical historical windows to be re-ingested as new data. API version information belongs exclusively in transport audit metadata.

### 4. Idempotency Rule (Exact Duplicate $\rightarrow$ NO-OP)
- When a fetched observation matches an existing record's `experiment_id` and `source_reference`, and all observed numeric values (`cost`/`spend`, `impressions`, `clicks`) are identical:
  - Action: **IDEMPOTENT NO-OP**.
  - No new row is inserted into `experiment_metrics`.
  - The existing measurement record is returned.

### 5. Restatement Rule (Changed Values $\rightarrow$ Append with Provenance)
- When a fetched observation matches an existing record's `experiment_id` and `source_reference`, but any observed numeric value (`cost`/`spend`, `impressions`, `clicks`) has changed:
  - Action: **APPEND RESTATEMENT**.
  - A new immutable `ExperimentMetrics` row is persisted with `evidence_type = EvidenceCategory.FACT`, `recorded_at = datetime.now(timezone.utc)`, and identical `source_reference`.
  - The restatement preserves the same logical reporting-window identity (`source_reference`), receives a new `recorded_at`, remains immutable, remains `EvidenceCategory.FACT`, does NOT mutate the previous observation, does NOT create a financial ledger transaction, does NOT create an `ExperimentDecision`, and does **NOT** automatically represent a new performance checkpoint.
  - **No Invented Fields / No Overloading:** `retention_notes` is strictly reserved for customer retention and repeat-user signals; it must **never** be used or overloaded to store restatement commentary. The immutable record's persistence timestamp (`recorded_at`) and identical `source_reference` provide the complete audit trail.

### 6. Time and Date Semantics
- `ExperimentMetrics.recorded_at`: Authoritative UTC timestamp of when VentureBot received and persisted that particular observation snapshot.
  It does **NOT** mean:
  - Start of the reporting window
  - End of the reporting window
  - Performance checkpoint date
  - Evidence that a new business period began
- `date_start` and `date_stop`: Calendar dates (`YYYY-MM-DD`) evaluated strictly in the Meta Ad Account's reporting timezone. They represent the delivery period, not the database ingestion time.

### 7. Analysis Contract & Resolution Rule
- When calculating sequential performance deltas, the Analysis layer MUST NOT blindly treat every `ExperimentMetrics` row as a separate performance checkpoint.
- For a logical reporting window with multiple observations (`Observation A`, `Observation B (restatement)`, `Observation C (restatement)`):
  - The Analysis layer must resolve the **CURRENT/LATEST observation** for that logical reporting window (by `recorded_at.desc()`) before using it as a performance checkpoint.
  - Therefore: **`RESTATEMENT ≠ NEW PERFORMANCE PERIOD`**.
- *Scope Note:* This analysis behavior is a locked requirement for future implementation; it is NOT implemented in Step 43.1.1.

### 8. Financial Ledger & Governance Isolation Invariants
- **Telemetry $\neq$ Financial Ledger:** Observational ad spend (`cost`) recorded from Meta Insights never touches `CapitalTransaction` or `liquid_capital`.
- **Restatement Ledger Protection:** A restatement from ₹100 to ₹105 MUST NOT create a ₹5 `EXPERIMENT_SPEND` transaction. Restatements never mutate ledger balances, spend ceilings, or allocations.
- **Zero Autonomous Decisions:** Neither raw telemetry nor restatements can trigger automated lifecycle decisions (`SCALE`, `ITERATE`, `KILL`, `HOLD`, `APPROVE`). All decisions remain explicitly human-authorized via `ExperimentDecisionService`.

---

## 25. Recovery Architecture (Option B) & Emergency-Stop Security Boundaries (Step 90.12 Consolidation)

To ensure auditable recovery from interrupted external deployments without mutating expired resources, VentureBot defines the following canonical recovery architecture:

### 1. Two-Phase Readiness-Gated Recovery (Option B) & Granular Deployment Architecture
- **Phase 1: Granular Dormant Deployment:** Option B dormant deployment is decoupled into three independently callable, auditable, and idempotent tiers:
  1. `deploy_recovery_adset_dormant()`: Validates experiment preconditions, token validity, zero spend, and budget ceilings. Performs replacement ad set idempotency lookup (`VB-EXP-{id}-ADSET-v2`). Adopts or creates the replacement ad set verifying explicit `PAUSED` status with a 24-hour buffer schedule under the existing campaign. Records `RESOURCE_SUPERSEDED` and `RESOURCE_CREATED`/`RESOURCE_ADOPTED` audit events. Advances execution status to `PARTIAL_ADSET` and persists `adset_id`. Never invokes creative or ad endpoints.
  2. `deploy_recovery_creative_dormant()`: Fails closed if replacement `adset_id` is missing (`MISSING_ADSET_PREREQUISITE`) or if execution is not in a valid Tier 1 prerequisite state (`PARTIAL_ADSET`). Enforces zero spend (`₹0.00`) and budget ceilings. Performs creative lookup and creates or adopts the creative with deterministic naming and approved image hash. Records `RESOURCE_CREATED`/`RESOURCE_ADOPTED` audit events. Advances status to `PARTIAL_CREATIVE` and persists `creative_id`. Never invokes ad endpoints.
  3. `deploy_recovery_ad_dormant()`: Fails closed if replacement `adset_id` or `creative_id` is missing (`MISSING_ADSET_PREREQUISITE` / `MISSING_CREATIVE_PREREQUISITE`) or if execution is not in a valid Tier 2 prerequisite state (`PARTIAL_CREATIVE`). Enforces zero spend (`₹0.00`) and budget ceilings. Performs ad lookup and adopts (verifying `PAUSED` status) or creates the ad in explicit `PAUSED` status for policy review. Records `RESOURCE_CREATED`/`RESOURCE_ADOPTED` audit events. Advances status to `DEPLOYED_DORMANT` and persists `ad_id`.
  4. Composite `deploy_recovery_dormant()`: Preserves backward compatibility by composing Tier 1 $\rightarrow$ Tier 2 $\rightarrow$ Tier 3 sequentially.
- **Granular State Machine:**
  `APPROVED` $\rightarrow$ `PARTIAL_ADSET` $\rightarrow$ `PARTIAL_CREATIVE` $\rightarrow$ `DEPLOYED_DORMANT`
  Each tier independently verifies prerequisites, spend, and status, and can be independently executed, verified, and audited by the operator before progressing to downstream tiers. Original expired ad sets are preserved completely intact and never modified, deleted, or unpaused. Executions in live/flighting states (`ACTIVATED`, `DEPLOYED`) are strictly prohibited from re-entering dormant deployment.
- **Replay Prevention Invariant:** Once a versioned recovery deployment has executed and generated `RESOURCE_CREATED` or `RESOURCE_ADOPTED` events, repeated creation under the same version is strictly blocked (`RECOVERY_AUTHORIZATION_REPLAY_REJECTED`). Replay checks inspect `new_resource_id`, event `reason`, and `api_request_summary` to account for purely numeric Meta resource IDs.
- **Phase 2: Readiness-Gated Activation:** Activation strictly requires Meta ad review approval, explicit human authorization, SAFE_MODE un-gated strictly for that single action, verified `₹0.00` actual ledger spend, and a schedule locked to an exact 48.0-hour duration in the future.

### 2. Multi-Tier Emergency Stop & Exclusive Campaign Ownership
- Emergency stopping coordinates pause across all four tiers: Campaign, Ad, Replacement Ad Set, and Original Ad Set.
- Campaign pausing strictly requires exclusive ownership verification: exact campaign ID matching the target experiment, verified ad account ID matching, single execution record in `ExternalExecutionRepository.get_by_campaign_id()`, and zero conflicting historical audit events.

### 3. Lineage Classification & Legacy Fallthrough Prohibition
- Every emergency stop evaluates `classify_execution_lineage()`:
  - `STANDARD`: Standard execution without recovery markers $\rightarrow$ legacy campaign-only shutdown.
  - `OPTION_B`: Valid recovery execution with verified lineage $\rightarrow$ 4-tier emergency stop. Evaluates both execution `adset_id` and audit event metadata (`reason`, `api_request_summary`) so versioned replacement ad sets are reliably attributed even when `adset_id` is a purely numeric Meta ID.
  - `AMBIGUOUS_RECOVERY`: Incomplete, malformed, circular, or conflicting lineage $\rightarrow$ **fails closed immediately with zero external mutations**. An ambiguous recovery execution must NEVER fall through to legacy campaign-only shutdown.

### 4. Token Inspector Preflight, Account ID Validation & Secret Redaction
- `/debug_token` inspection requires an authorized App Access Token (`META_APP_ACCESS_TOKEN`) or developer credential. System user self-inspection is unverified and disabled by default.
- Preflight verifies App ID (`1063651013045060`), compatible type (`SYSTEM_USER`/`USER`), required scopes (`ads_management`, `ads_read`), granular account coverage (normalizing whitespace and `act_` prefixes), and $\ge 72\text{h}$ validity on both `data_access_expires_at` and `expires_at`.
- **Strict Account ID Validation:** `validate_meta_account_id()` strictly requires ASCII digits only (optional single `act_` prefix) and rejects repeated prefixes (`act_act_`), whitespace, punctuation, letters, or Unicode digits across adapter normalization, execution specifications, and token inspection.
- **Centralized Credential Sanitization:** `sanitize_error()` centrally redacts sensitive tokens, headers, and proofs (`access_token`, `input_token`, `appsecret_proof`, Bearer tokens) across error messages, URLs, and JSON payloads.

### 5. Multi-Layer SAFE_MODE Enforcement
- `is_safe_mode_enforced(safe_mode)` evaluates the environment setting `VENTUREBOT_SAFE_MODE` and guarantees that caller-supplied `safe_mode=False` cannot override an active environment guard, while caller-supplied `safe_mode=True` is always honored even if environment SAFE_MODE is false.
- `MetaMarketingApiAdapter._execute_request()` raises `MetaApiSafetyError` before calling any transport on all mutation methods (POST, PUT, DELETE, PATCH, `is_write=True`). Transport injection cannot bypass SAFE_MODE.

### 6. Concurrency Control & Double-Checked Locking (Step 90.15.4)
- **Meta Graph API Duplicate Creation Vulnerability:** The Meta Marketing API does not support distributed idempotency keys or unique name constraints for ad sets, creatives, or ads. Concurrent POST requests with identical parameters produce distinct duplicate remote resources.
- **Two-Layer Reservation & Mutual Exclusion (`claim_deployment_tier`):**
  1. *In-Process Thread Mutex:* Process-wide dictionary of non-reentrant locks `_TIER_THREAD_LOCKS[(experiment_id, tier)]` managed under `_TIER_LOCKS_MUTEX`.
  2. *Cross-Process DB Reservation:* `DeploymentClaimORM` table with unique constraint `uq_deployment_claims_exp_tier` on `(experiment_id, tier)`. Attempts to acquire an existing active claim result in database-level mutual exclusion and poll with exponential backoff until released or timeout (default 10.0s).
  3. *TTL Abandoned Claim Recovery:* Claims hold a 60-second TTL (`expires_at`). If a process crashes while holding a claim, subsequent operations automatically detect the expired claim, purge it safely, and re-acquire.
- **Double-Checked Locking Pattern:**
  - Fast-path: Perform read-only lookup on Meta. If resource exists exact, adopt immediately.
  - Claim acquisition: If resource does not exist, acquire `claim_deployment_tier`.
  - Double-check: Refresh execution and re-query Meta within claim lock. If another thread/process completed creation during lock acquisition, adopt the newly created resource without invoking Meta's create endpoint.
  - Remote mutation & immediate persistence: If still absent, create the resource on Meta and persist resource ID and state to the local DB immediately *within* the claim lock before releasing.

### 7. Recovery Version Trust Boundary & Operator Identity (Step 90.15.4)
- **Absence of Identity Provider:** VentureBot currently possesses NO cryptographic signature verification, JWT validator, OAuth identity provider, or role-based access control (RBAC) subsystem.
- **Caller-Supplied Flags vs. Authentication:** The parameter `explicit_auth: bool` and audit fields such as `operator_id: str` are caller-supplied arguments and cannot establish verified human identity across an untrusted boundary.
- **Operational Reality of Version 2:** Recovery version 2 (`-ADSET-v2`) was hardcoded as a narrow operational assumption following the human operator instruction to adopt Option B. However, an arbitrary local caller can invoke version 2.
- **Fail-Closed Higher Versions:** Recovery versions above 2 fail closed under Rules A.7 & A.8 (`UNTRUSTED_OPERATOR_AUTHENTICATION`).
- **Mandatory Safety Blocking:** Because no authenticated operator identity provider exists, live Meta mutations MUST remain strictly blocked under `VENTUREBOT_SAFE_MODE=true`.

### 8. Operational & Financial Safeguards
- Audit events in `external_execution_events` provide an **application-level append-only** guarantee, not engine-level immutability.
- **Spend Protection Semantics & Thresholds:**
  - **Approved Allocation:** ₹200.00 ceiling committed for the experiment.
  - **Warning Threshold:** ₹180.00 (`is_warning=True`) triggers operator alerts.
  - **Circuit-Breaker Threshold:** ₹190.00 (`is_circuit_breaker_triggered=True`) initiates a best-effort automated emergency pause.
  - **Reconciliation & Reporting Latency:** Actual spend is reconciled separately via Meta Graph API reporting, which typically experiences 15–60 minute reporting latency. Because Meta ad delivery continues during this window, overspend beyond ₹190 or ₹200 may occur before a polling-based circuit breaker halts delivery.
  - **Prepaid Wallet Safeguard:** The prepaid Meta ad wallet balance (₹200.00) serves as an additional downstream account safeguard against runaway post-paid billing, but is not an exact real-time per-flight billing cutoff.
  - **No Guaranteed ₹200 Maximum Cap:** The system does NOT guarantee that spend will never exceed ₹200.00; thresholds are best-effort polling circuit breakers.
- `GUIDE_ACCESS` telemetry records server-observed edge beacon requests, not verified human visitors, reading, conversions, or revenue.

---
*END OF VENTUREBOT ARCHITECTURE DOCUMENT*

