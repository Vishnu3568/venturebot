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
*END OF VENTUREBOT ARCHITECTURE DOCUMENT*
