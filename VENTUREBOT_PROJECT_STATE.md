# VENTUREBOT PROJECT STATE

---

## Project Name
**VentureBot**

---

## Project Objective
VentureBot is a controlled autonomous experimentation system designed to discover, test, measure, improve, and scale **legitimate, repeatable revenue opportunities** — starting from a defined capital base.

---

## Starting Capital
**₹1,000**

---

## Long-Term Objective
Systematically discover, test, measure, improve, and scale legitimate revenue opportunities using a disciplined, evidence-driven approach. Every capital allocation must be justified by measurable data.

---

## Core Principles

- **Evidence-first allocation**: Capital must only be deployed based on measurable, reproducible evidence — never on speculation.
- **Controlled experimentation**: Each opportunity is treated as an experiment with a hypothesis, controls, and exit criteria.
- **Incremental trust**: The system earns the right to spend more capital by demonstrating positive ROI at smaller scales first.
- **Transparency**: Every decision, allocation, and outcome must be logged and auditable.

---

## Important Constraints

- The system must never blindly spend the entire capital. A defined reserve must always be maintained.
- Autonomous spending does NOT exist in the current version. All financial actions require explicit human approval.
- No guessing. If evidence is insufficient, the default action is to gather more data, not to spend.

---

## Current Version
**V0.1 — Data Contracts**

---

## Current Status
**Step 2 — Canonical Data Contracts Defined**

Five Pydantic v2 data models have been defined and tested. No database, no agents, no APIs, no dashboard.

---

## What Is Intentionally NOT Implemented Yet

- AI / Agent logic (LLM agents, sub-agents, orchestration)
- Advertising APIs (Meta Ads, Google Ads, TikTok Ads)
- Payment / financial systems (Razorpay, Stripe, UPI integrations)
- Autonomous spending (any code that moves real money)
- Content publishing (social media posting, blog automation)
- Web scraping (any data harvesting pipelines)
- Dashboard / UI (React frontend, charts, analytics views)
- **Persistent database** (ORM, migrations, SQLite/Postgres — Step 3)
- Revenue tracking / ledger logic (Step 3+)
- Automated decision making (any non-human-triggered action)

---

## Future High-Level Phases

- **V0**   Foundation — Clean project structure, documentation, configuration ✅
- **V0.1** Data Contracts — Pydantic models for all canonical entities ✅
- **V0.2** Research/Decision Foundation — Tools for structured opportunity research and human-led decision logging
- **V1**   Experiment Generation — Framework to define, scope, and document experiments before running them
- **V2**   Controlled Execution — Supervised execution of approved experiments with hard capital limits
- **V3**   Feedback & Optimization — Measurement, analysis, and iteration loops on completed experiments
- **V4**   Portfolio & Scale Management — Multi-experiment management, compounding, and scale decisions

---

## Assumptions Made in Step 1

- Python is the chosen backend language.
- React will be used for the future frontend dashboard (not yet initialized).
- Git is used for version control from the beginning.
- No external dependencies are installed at this stage.

---

## Step 1 Readiness Checklist

- [x] Git repository initialized
- [x] VENTUREBOT_PROJECT_STATE.md created (this file)
- [x] README.md created
- [x] Directory structure scaffolded (backend/, frontend/, docs/, tests/)
- [x] Minimum Python package structure created in backend/
- [x] .gitignore created (Python + Node/React)
- [x] No functional code written
- [x] No dependencies installed
- [x] No placeholder logic invented

---

## Step 2 — Data Contracts

### Canonical Entities Defined

| Model | File | Key design decisions |
|---|---|---|
| Opportunity | venturebot/models/opportunity.py | OpportunityCategory + OpportunityStatus enums; Decimal money estimates; confidence 0–1 |
| Experiment | venturebot/models/experiment.py | allocated_budget and actual_spend kept explicitly separate; max_allowed_spend enforced by model_validator |
| ExperimentMetrics | venturebot/models/metrics.py | All money in Decimal; rates/ROI optional and caller-set (no universal formula imposed) |
| CapitalTransaction | venturebot/models/capital.py | TransactionType enum; amounts always positive (direction implied by type) for clean audit trail |
| Decision | venturebot/models/decision.py | DecisionOutcome enum: KILL / ITERATE / SCALE / HOLD; reason field required on every decision |

### Dependencies Added

- pydantic>=2.7 (runtime)
- pytest>=8 (dev)

### Step 2 Checklist

- [x] Opportunity model — typed, tested
- [x] Experiment model — budget/spend distinction enforced
- [x] ExperimentMetrics model — no universal formulas imposed
- [x] CapitalTransaction model — auditable, enum-constrained types
- [x] Decision model — KILL/ITERATE/SCALE/HOLD outcomes
- [x] 39 unit tests — all passing
- [x] No database implemented
- [x] No agents implemented
- [x] No APIs implemented
- [x] No dashboard implemented

Project is ready for Step 3 (persistent database and financial ledger).
