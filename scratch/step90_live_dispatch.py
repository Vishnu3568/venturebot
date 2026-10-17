"""Step 90: Controlled Live Dispatch for Pilot Experiment 49fde874-9387-5056-934c-51a9cfca164f."""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root))
sys.path.insert(0, str(repo_root / "backend"))

from venturebot.database.connection import get_engine, get_session_factory, init_db
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.database.repositories.experiment import ExperimentRepository
from venturebot.database.repositories.external_execution import ExternalExecutionRepository
from venturebot.env import is_safe_mode, load_env_file
from venturebot.execution.dispatch import (
    ExecutionAction,
    ExecutionDispatchService,
    ExecutionRequest,
)
from venturebot.execution.meta import (
    MetaExecutionSpecification,
    MetaMarketingApiAdapter,
)
from tests.test_pilot_persistence import (
    PILOT_CREATIVE_PATH,
    PILOT_DESTINATION_URL,
    PILOT_EXPERIMENT_ID,
)

load_env_file()

def live_http_transport(req: urllib.request.Request, timeout: float) -> bytes:
    """Execute live HTTP request directly using standard urllib with error response logging."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as err:
        error_body = err.read().decode("utf-8", errors="replace")
        print(f"\n[HTTP ERROR {err.code}] {err.reason}: {error_body}")
        # Re-raise so adapter exception handling works normally
        raise

def execute_live_dispatch():
    print("=" * 80)
    print("STEP 90 — AUTHORIZED LIVE DISPATCH: REAL-MONEY META EXECUTION")
    print("=" * 80)

    # 1. Establish session & repositories
    engine = get_engine()
    init_db(engine)
    session = get_session_factory(engine)()

    exp_repo = ExperimentRepository(session)
    cap_repo = CapitalRepository(session)
    ext_repo = ExternalExecutionRepository(session)

    # 2. Re-verify database state
    exp = exp_repo.get(PILOT_EXPERIMENT_ID)
    assert exp is not None, "Experiment not found"
    assert exp.status.value == "approved", f"Experiment not in approved status: {exp.status}"
    assert exp.allocated_budget == Decimal("200.00"), f"Allocated budget not 200: {exp.allocated_budget}"
    ext = ext_repo.get_by_experiment_id(PILOT_EXPERIMENT_ID)
    if ext is not None:
        from venturebot.models.external_execution import ExternalExecutionStatus
        assert ext.status == ExternalExecutionStatus.FAILED, f"Unexpected existing execution: {ext}"

    # 3. Construct Canonical MetaExecutionSpecification
    token = os.environ.get("META_ACCESS_TOKEN", "").strip()
    ad_account_id = os.environ.get("META_AD_ACCOUNT_ID", "").strip()
    page_id = os.environ.get("META_PAGE_ID", "").strip()

    start_utc = datetime(2026, 10, 6, 8, 30, tzinfo=timezone.utc)
    end_utc = datetime(2026, 10, 8, 8, 30, tzinfo=timezone.utc)
    approved_headline = "Stop Chasing Invoices — Solopreneur Financial Workflow Guide"
    approved_primary = (
        "Chasing late invoices costs freelancers 4+ hours every week. "
        "Get the battle-tested, 5-step financial workflow guide to automate follow-ups and stabilize cash flow."
    )

    spec = MetaExecutionSpecification(
        experiment_id=PILOT_EXPERIMENT_ID,
        ad_account_id=ad_account_id,
        page_id=page_id,
        destination_url=PILOT_DESTINATION_URL,
        primary_text=approved_primary,
        headline=approved_headline,
        image_asset_path=str(PILOT_CREATIVE_PATH),
        call_to_action="LEARN_MORE",
        campaign_objective="OUTCOME_TRAFFIC",
        countries=["IN"],
        age_min=22,
        age_max=55,
        authorized_budget=Decimal("200.00"),
        start_time=start_utc,
        end_time=end_utc,
        special_ad_categories=["NONE"],
        status="PAUSED",
        explicit_dispatch_authorized=True,
    )

    spec.validate_pre_dispatch(
        allocated_budget=exp.allocated_budget,
        max_allowed_spend=exp.max_allowed_spend,
        actual_spend=exp.actual_spend,
    )
    print("Canonical MetaExecutionSpecification validated successfully.")

    # 4. Construct live adapter with custom transport
    adapter = MetaMarketingApiAdapter(
        ad_account_id=ad_account_id,
        access_token=token,
        transport=live_http_transport,
    )

    # 5. Build ExecutionRequest
    req = ExecutionRequest(
        experiment_id=PILOT_EXPERIMENT_ID,
        requested_action=ExecutionAction.DEPLOY_EXPERIMENT.value,
        proposed_budget=Decimal("200.00"),
    )

    print("\nExecuting live dispatch through ExecutionDispatchService...")
    print(f"Target Ad Account: {ad_account_id}")
    print(f"Target Page ID: {page_id}")
    print(f"Budget: ₹200.00 lifetime (20,000 paise)")
    print(f"Schedule: {start_utc.isoformat()} to {end_utc.isoformat()} (48h)")
    print(f"Status: PAUSED")

    # 6. Execute dispatch with safe_mode=False for this explicitly authorized invocation
    dispatch_res = ExecutionDispatchService.dispatch(
        request=req,
        session=session,
        safe_mode=False,
        spec=spec,
        adapter=adapter,
    )

    print("\nDispatch Result:")
    print(f"  Success: {dispatch_res.success}")
    print(f"  Blocked: {dispatch_res.blocked}")
    print(f"  Reason: {dispatch_res.reason}")

    if not dispatch_res.success:
        print("\nDISPATCH FAILED OR BLOCKED!")
        return False, dispatch_res

    # 7. Post-dispatch database verification
    session.expire_all()
    exp_updated = exp_repo.get(PILOT_EXPERIMENT_ID)
    execution = ext_repo.get_by_experiment_id(PILOT_EXPERIMENT_ID)

    print("\nDatabase Record State:")
    print(f"  Experiment Status: {exp_updated.status if exp_updated else 'None'}")
    print(f"  Actual Start: {exp_updated.actual_start if exp_updated else 'None'}")
    print(f"  ExternalExecution ID: {execution.id if execution else 'None'}")
    print(f"  ExternalExecution Status: {execution.status.value if execution else 'None'}")
    print(f"  Campaign ID: {execution.campaign_id if execution else 'None'}")
    print(f"  Ad Set ID: {execution.adset_id if execution else 'None'}")
    print(f"  Creative ID: {execution.creative_id if execution else 'None'}")
    print(f"  Ad ID: {execution.ad_id if execution else 'None'}")
    print(f"  Image Hash: {execution.image_hash if execution else 'None'}")

    return True, dispatch_res

if __name__ == "__main__":
    execute_live_dispatch()
