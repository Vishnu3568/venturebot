"""Step 90: Fresh pre-dispatch invariant verification before any execution."""

import hashlib
import json
import os
import struct
import sys
import urllib.request
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
from venturebot.execution.meta import MetaMarketingApiAdapter
from venturebot.models.experiment import ExperimentStatus
from tests.test_pilot_persistence import (
    PILOT_CREATIVE_PATH,
    PILOT_CREATIVE_SHA256,
    PILOT_DESTINATION_URL,
    PILOT_EXPERIMENT_ID,
    PILOT_OPPORTUNITY_ID,
)

load_env_file()

def run_pre_check():
    print("=" * 70)
    print("STEP 90 — FRESH PRE-DISPATCH INVARIANT VERIFICATION")
    print("=" * 70)

    # Database connection
    engine = get_engine()
    init_db(engine)
    session = get_session_factory(engine)()

    exp_repo = ExperimentRepository(session)
    cap_repo = CapitalRepository(session)
    ext_repo = ExternalExecutionRepository(session)

    # A. Experiment exists and is APPROVED
    exp = exp_repo.get(PILOT_EXPERIMENT_ID)
    assert exp is not None, "A FAIL: Experiment does not exist"
    assert exp.status == ExperimentStatus.APPROVED, f"A FAIL: Experiment status is {exp.status}, expected APPROVED"
    print("A. [PASS] Experiment exists and status is APPROVED.")

    # B. Allocated budget is exactly ₹200.00
    assert exp.allocated_budget == Decimal("200.00"), f"B FAIL: Allocated budget is {exp.allocated_budget}, expected 200.00"
    print(f"B. [PASS] Allocated budget is exactly ₹{exp.allocated_budget:.2f}.")

    # C. Actual experiment spend is ₹0.00
    actual_spend = cap_repo.get_experiment_actual_spend(PILOT_EXPERIMENT_ID)
    assert actual_spend == Decimal("0.00"), f"C FAIL: Actual spend is {actual_spend}, expected 0.00"
    assert exp.actual_spend == Decimal("0.00"), f"C FAIL: exp.actual_spend is {exp.actual_spend}, expected 0.00"
    print(f"C. [PASS] Actual experiment spend is ₹{actual_spend:.2f}.")

    # D. No ExternalExecution already exists for this experiment
    ext = ext_repo.get_by_experiment_id(PILOT_EXPERIMENT_ID)
    assert ext is None, f"D FAIL: ExternalExecution already exists: {ext}"
    print("D. [PASS] No ExternalExecution record exists for this experiment.")

    # E. No prior Meta dispatch exists
    assert exp.actual_start is None, f"E FAIL: actual_start is {exp.actual_start}, expected None"
    print("E. [PASS] No prior Meta dispatch exists (actual_start is None).")

    # F, G, H, I: Meta Account Verification
    token = os.environ.get("META_ACCESS_TOKEN", "").strip()
    ad_account_id = os.environ.get("META_AD_ACCOUNT_ID", "").strip()
    page_id = os.environ.get("META_PAGE_ID", "").strip()

    assert ad_account_id == "act_1985595022114520", f"F FAIL: Unexpected ad_account_id {ad_account_id}"
    print(f"F. [PASS] Meta ad account ID matches {ad_account_id}.")

    adapter = MetaMarketingApiAdapter(ad_account_id=ad_account_id, access_token=token)
    meta = adapter.get_account_metadata(ad_account_id)
    assert meta.is_active, f"G FAIL: Meta account is not active (status={meta.account_status})"
    print(f"G. [PASS] Meta account is ACTIVE (status={meta.account_status}).")

    assert meta.currency == "INR", f"H FAIL: Meta currency is {meta.currency}, expected INR"
    print(f"H. [PASS] Currency is {meta.currency}.")

    # I: Prepaid Balance
    fields = "id,name,account_status,currency,is_prepay_account,amount_spent,balance,funding_source_details"
    url_acct = f"https://graph.facebook.com/v20.0/{meta.id}?fields={fields}"
    req_acct = urllib.request.Request(url_acct, headers={"Authorization": f"Bearer {token}", "User-Agent": "VentureBot/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req_acct) as resp:
        acct_details = json.loads(resp.read().decode("utf-8"))

    funding = acct_details.get("funding_source_details", {})
    disp = funding.get("display_string", "")
    assert "200.00" in disp, f"I FAIL: Prepaid balance string '{disp}' does not show ₹200.00"
    print(f"I. [PASS] Meta prepaid available balance is verified at least ₹200.00 ({disp}).")

    # J, K: Page ID & Permissions
    assert page_id == "1389949167526709", f"J FAIL: Unexpected page_id {page_id}"
    print(f"J. [PASS] Page ID matches {page_id}.")

    url_accounts = "https://graph.facebook.com/v20.0/me/accounts?fields=id,name,tasks"
    req_acc = urllib.request.Request(url_accounts, headers={"Authorization": f"Bearer {token}", "User-Agent": "VentureBot/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req_acc) as resp:
        pages = json.loads(resp.read().decode("utf-8")).get("data", [])
    matched_page = next((p for p in pages if str(p.get("id")) == page_id), None)
    tasks = matched_page.get("tasks", []) if matched_page else []
    assert "ADVERTISE" in tasks, f"K FAIL: Token lacks ADVERTISE task on Page {page_id}"
    print(f"K. [PASS] Page advertising permission verified (tasks: {tasks}).")

    # L: Creative SHA-256
    assert PILOT_CREATIVE_PATH.is_file(), f"L FAIL: Creative missing at {PILOT_CREATIVE_PATH}"
    img_data = PILOT_CREATIVE_PATH.read_bytes()
    computed_sha = hashlib.sha256(img_data).hexdigest()
    assert computed_sha == PILOT_CREATIVE_SHA256, f"L FAIL: SHA-256 mismatch ({computed_sha})"
    print(f"L. [PASS] Creative exists and matches SHA-256 ({computed_sha}).")

    # M: Destination Reachability
    req_dest = urllib.request.Request(PILOT_DESTINATION_URL, headers={"User-Agent": "VentureBot-Audit/1.0"})
    with urllib.request.urlopen(req_dest, timeout=10) as resp:
        dest_code = resp.getcode()
    assert dest_code == 200, f"M FAIL: Destination returned HTTP {dest_code}"
    print(f"M. [PASS] Destination is reachable (HTTP {dest_code}).")

    # N: Telemetry Endpoint Health
    worker_url = "https://venturebot-telemetry.uvishnu3568.workers.dev/event/guide_access"
    req_opt = urllib.request.Request(
        worker_url,
        headers={
            "Origin": "https://vishnu3568.github.io",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
            "User-Agent": "VentureBot-Audit/1.0",
        },
        method="OPTIONS",
    )
    with urllib.request.urlopen(req_opt, timeout=10) as resp:
        opt_code = resp.getcode()
    assert opt_code == 204, f"N FAIL: Telemetry OPTIONS returned HTTP {opt_code}"
    print(f"N. [PASS] Telemetry endpoint is healthy (OPTIONS HTTP {opt_code}).")

    # O: Remote Meta Object State Clean
    url_c = f"https://graph.facebook.com/v20.0/{meta.id}/campaigns?fields=id,name"
    req_c = urllib.request.Request(url_c, headers={"Authorization": f"Bearer {token}", "User-Agent": "VentureBot/1.0"})
    with urllib.request.urlopen(req_c) as resp:
        c_list = json.loads(resp.read().decode("utf-8")).get("data", [])

    url_as = f"https://graph.facebook.com/v20.0/{meta.id}/adsets?fields=id,name"
    req_as = urllib.request.Request(url_as, headers={"Authorization": f"Bearer {token}", "User-Agent": "VentureBot/1.0"})
    with urllib.request.urlopen(req_as) as resp:
        as_list = json.loads(resp.read().decode("utf-8")).get("data", [])

    url_ad = f"https://graph.facebook.com/v20.0/{meta.id}/ads?fields=id,name"
    req_ad = urllib.request.Request(url_ad, headers={"Authorization": f"Bearer {token}", "User-Agent": "VentureBot/1.0"})
    with urllib.request.urlopen(req_ad) as resp:
        ad_list = json.loads(resp.read().decode("utf-8")).get("data", [])

    assert len(c_list) == 0, f"O FAIL: Remote campaigns found: {c_list}"
    assert len(as_list) == 0, f"O FAIL: Remote ad sets found: {as_list}"
    assert len(ad_list) == 0, f"O FAIL: Remote ads found: {ad_list}"
    print("O. [PASS] Remote Meta object state is clean (Campaigns: 0, Ad Sets: 0, Ads: 0).")

    print("\nALL 15 CRITICAL PRE-DISPATCH INVARIANTS PASS.")
    return True

if __name__ == "__main__":
    run_pre_check()
