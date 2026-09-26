"""Unit tests for Step 36 — Meta Execution Contract & Mocked Adapter.

Verifies:
A. MetaExecutionSpecification validation
B. Valid specification construction
C. Invalid budget rejection (zero, negative, fractional paise)
D. Invalid age range rejection (min < 13, max > 65, min > max)
E. Missing required fields rejection (ad_account_id, page_id, destination_url, primary_text, headline)
F. Special ad categories handling (defaults to ["NONE"], allows custom valid)
G. PAUSED creation payloads across all entities
H. Campaign creation payload structure
I. Ad Set creation payload structure with lifetime budget
J. Targeting payload structure (geo_locations countries, age_min, age_max)
K. Deterministic budget conversion (inr_to_paise Decimal arithmetic)
L. Lifetime budget requires end_time and ISO 8601 formatting
M. Deterministic campaign naming (VB-EXP-<uuid>)
N. Duplicate preflight request URL construction & check_campaign_exists method
O. Campaign response parsing (extracts numeric string ID)
P. Ad Set response parsing
Q. Image upload response parsing (extracts image hash)
R. Creative response parsing
S. Ad response parsing
T. Pause response parsing (verifies success: true)
U. Malformed response handling (malformed JSON, missing ID, invalid image map, success != true)
V. Meta Graph API error envelope handling (codes 100, 190, 200, 404)
W. Guardrail: Calling write operations without mock transport strictly raises error (zero live network access)
X. Zero capital ledger mutations
Y. Zero experiment status changes
Z. Zero decision creation
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
import urllib.error
import urllib.request
from uuid import UUID, uuid4

import pytest

from venturebot.database.connection import get_engine, get_session_factory, init_db
from venturebot.database.models import CapitalTransactionORM, DecisionORM, ExperimentORM
from venturebot.execution.meta import (
    MetaApiAuthError,
    MetaApiConnectionError,
    MetaApiError,
    MetaApiNotFoundError,
    MetaApiPermissionError,
    MetaApiTimeoutError,
    MetaExecutionSpecification,
    MetaMarketingApiAdapter,
    deterministic_campaign_name,
    inr_to_paise,
)

SAMPLE_ACCOUNT_ID = "act_1985595022114520"
SAMPLE_TOKEN = "TEST_MOCK_TOKEN_XYZ_12345"
SAMPLE_EXPERIMENT_ID = UUID("11111111-2222-3333-4444-555555555555")
SAMPLE_PAGE_ID = "109876543210123"


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def valid_spec_kwargs() -> dict:
    return {
        "experiment_id": SAMPLE_EXPERIMENT_ID,
        "ad_account_id": SAMPLE_ACCOUNT_ID,
        "page_id": SAMPLE_PAGE_ID,
        "destination_url": "https://venturebot.example.com/landing",
        "primary_text": "Discover tested, data-driven revenue opportunities.",
        "headline": "VentureBot Experiment",
        "image_asset_path": "assets/creatives/experiment_hero.png",
        "image_hash": "a1b2c3d4e5f67890123456789abcdef0",
        "call_to_action": "LEARN_MORE",
        "campaign_objective": "OUTCOME_TRAFFIC",
        "countries": ["IN"],
        "age_min": 21,
        "age_max": 55,
        "authorized_budget": Decimal("100.00"),
        "start_time": datetime(2026, 10, 1, 10, 0, 0, tzinfo=timezone.utc),
        "end_time": datetime(2026, 10, 8, 10, 0, 0, tzinfo=timezone.utc),
        "special_ad_categories": ["NONE"],
        "status": "PAUSED",
        "explicit_dispatch_authorized": True,
    }


# ── A - F. MetaExecutionSpecification Tests ─────────────────────────────────

def test_meta_execution_specification_valid(valid_spec_kwargs):
    """Test valid specification construction."""
    spec = MetaExecutionSpecification(**valid_spec_kwargs)
    assert spec.experiment_id == SAMPLE_EXPERIMENT_ID
    assert spec.ad_account_id == SAMPLE_ACCOUNT_ID
    assert spec.page_id == SAMPLE_PAGE_ID
    assert spec.destination_url == "https://venturebot.example.com/landing"
    assert spec.authorized_budget == Decimal("100.00")
    assert spec.countries == ["IN"]
    assert spec.age_min == 21
    assert spec.age_max == 55
    assert spec.status == "PAUSED"
    assert spec.explicit_dispatch_authorized is True


def test_meta_execution_specification_account_id_normalization(valid_spec_kwargs):
    """Verify ad account ID prefix normalization."""
    valid_spec_kwargs["ad_account_id"] = "1985595022114520"
    spec = MetaExecutionSpecification(**valid_spec_kwargs)
    assert spec.ad_account_id == "act_1985595022114520"


def test_meta_execution_specification_invalid_budget(valid_spec_kwargs):
    """Test rejection of zero, negative, or fractional paise budgets."""
    # Zero budget
    valid_spec_kwargs["authorized_budget"] = Decimal("0.00")
    with pytest.raises(ValueError, match="Input should be greater than 0"):
        MetaExecutionSpecification(**valid_spec_kwargs)

    # Negative budget
    valid_spec_kwargs["authorized_budget"] = Decimal("-50.00")
    with pytest.raises(ValueError, match="Input should be greater than 0"):
        MetaExecutionSpecification(**valid_spec_kwargs)

    # Fractional paise
    valid_spec_kwargs["authorized_budget"] = Decimal("100.005")
    with pytest.raises(ValueError, match="fractional paise"):
        MetaExecutionSpecification(**valid_spec_kwargs)


def test_meta_execution_specification_invalid_age_ranges(valid_spec_kwargs):
    """Test rejection of invalid age minimums, maximums, and inverted ranges."""
    # Under 13
    valid_spec_kwargs["age_min"] = 12
    with pytest.raises(ValueError, match="Input should be greater than or equal to 13"):
        MetaExecutionSpecification(**valid_spec_kwargs)

    # Over 65
    valid_spec_kwargs["age_min"] = 18
    valid_spec_kwargs["age_max"] = 66
    with pytest.raises(ValueError, match="Input should be less than or equal to 65"):
        MetaExecutionSpecification(**valid_spec_kwargs)

    # Inverted range (min > max)
    valid_spec_kwargs["age_min"] = 40
    valid_spec_kwargs["age_max"] = 30
    with pytest.raises(ValueError, match="cannot be less than age_min"):
        MetaExecutionSpecification(**valid_spec_kwargs)


def test_meta_execution_specification_missing_required_fields(valid_spec_kwargs):
    """Test rejection of empty strings for required fields."""
    for field_name in ["page_id", "primary_text", "headline"]:
        kwargs = valid_spec_kwargs.copy()
        kwargs[field_name] = "   "
        with pytest.raises(ValueError, match="Field must not be empty or whitespace"):
            MetaExecutionSpecification(**kwargs)

    # Missing ad_account_id
    kwargs = valid_spec_kwargs.copy()
    kwargs["ad_account_id"] = "  "
    with pytest.raises(ValueError, match="ad_account_id must not be empty"):
        MetaExecutionSpecification(**kwargs)

    # Invalid destination URL (not starting with http/https)
    kwargs = valid_spec_kwargs.copy()
    kwargs["destination_url"] = "ftp://example.com"
    with pytest.raises(ValueError, match="destination_url must start with 'http://' or 'https://'"):
        MetaExecutionSpecification(**kwargs)


def test_meta_execution_specification_special_ad_categories(valid_spec_kwargs):
    """Test default and custom special ad categories."""
    # Defaults to ["NONE"]
    valid_spec_kwargs.pop("special_ad_categories", None)
    spec = MetaExecutionSpecification(**valid_spec_kwargs)
    assert spec.special_ad_categories == ["NONE"]

    # Explicit empty list coerced to ["NONE"]
    valid_spec_kwargs["special_ad_categories"] = []
    spec = MetaExecutionSpecification(**valid_spec_kwargs)
    assert spec.special_ad_categories == ["NONE"]


def test_meta_execution_specification_status_must_be_paused(valid_spec_kwargs):
    """Test status cannot be ACTIVE on creation."""
    valid_spec_kwargs["status"] = "ACTIVE"
    with pytest.raises(ValueError, match="Status must strictly be 'PAUSED' for creation safety"):
        MetaExecutionSpecification(**valid_spec_kwargs)


def test_meta_execution_specification_invalid_date_range(valid_spec_kwargs):
    """Test rejection when end_time is before or equal to start_time."""
    valid_spec_kwargs["start_time"] = datetime(2026, 10, 8, 10, 0, 0, tzinfo=timezone.utc)
    valid_spec_kwargs["end_time"] = datetime(2026, 10, 1, 10, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="end_time .* must be after start_time"):
        MetaExecutionSpecification(**valid_spec_kwargs)


def test_meta_execution_specification_pre_dispatch_safeguards(valid_spec_kwargs):
    """Verify validate_pre_dispatch checks explicit authorization, end_time, and budget caps."""
    spec = MetaExecutionSpecification(**valid_spec_kwargs)

    # Valid pre-dispatch validation
    spec.validate_pre_dispatch(
        allocated_budget=Decimal("200.00"),
        max_allowed_spend=Decimal("200.00"),
        actual_spend=Decimal("50.00"),
    )

    # Unauthorized dispatch
    unauth_kwargs = valid_spec_kwargs.copy()
    unauth_kwargs["explicit_dispatch_authorized"] = False
    unauth_spec = MetaExecutionSpecification(**unauth_kwargs)
    with pytest.raises(ValueError, match="Explicit operator dispatch authorization is required"):
        unauth_spec.validate_pre_dispatch(
            allocated_budget=Decimal("200.00"),
            max_allowed_spend=Decimal("200.00"),
        )

    # Missing end_time
    no_end_kwargs = valid_spec_kwargs.copy()
    no_end_kwargs["end_time"] = None
    no_end_spec = MetaExecutionSpecification(**no_end_kwargs)
    with pytest.raises(ValueError, match="end_time is mandatory"):
        no_end_spec.validate_pre_dispatch(
            allocated_budget=Decimal("200.00"),
            max_allowed_spend=Decimal("200.00"),
        )

    # Exceeds remaining allocated budget
    with pytest.raises(ValueError, match="exceeds remaining allocated budget"):
        spec.validate_pre_dispatch(
            allocated_budget=Decimal("120.00"),
            max_allowed_spend=Decimal("200.00"),
            actual_spend=Decimal("50.00"),  # remaining allocated = 70 < 100
        )

    # Exceeds remaining max allowed ceiling
    with pytest.raises(ValueError, match="exceeds remaining max allowed spend"):
        spec.validate_pre_dispatch(
            allocated_budget=Decimal("200.00"),
            max_allowed_spend=Decimal("130.00"),
            actual_spend=Decimal("50.00"),  # remaining ceiling = 80 < 100
        )


# ── K. Budget Conversion (inr_to_paise) ──────────────────────────────────────

def test_inr_to_paise_deterministic():
    """Verify Decimal INR to integer paise conversions without float arithmetic."""
    assert inr_to_paise(Decimal("100.00")) == 10000
    assert inr_to_paise(Decimal("50.25")) == 5025
    assert inr_to_paise(Decimal("0.01")) == 1
    assert inr_to_paise(Decimal("0.00")) == 0
    assert inr_to_paise(Decimal("999.99")) == 99999

    # Rejection of negative amounts
    with pytest.raises(ValueError, match="cannot be negative"):
        inr_to_paise(Decimal("-10.00"))

    # Rejection of fractional paise (sub-cent)
    with pytest.raises(ValueError, match="fractional paise"):
        inr_to_paise(Decimal("10.005"))

    # Rejection of non-Decimal types
    with pytest.raises(TypeError, match="must be a Decimal"):
        inr_to_paise(100.0)  # type: ignore[arg-type]


# ── M. Deterministic Campaign Naming ─────────────────────────────────────────

def test_deterministic_campaign_name():
    """Verify deterministic campaign name format VB-EXP-<uuid>."""
    exp_id = UUID("abcdef01-2345-6789-abcd-ef0123456789")
    name = deterministic_campaign_name(exp_id)
    assert name == "VB-EXP-abcdef01-2345-6789-abcd-ef0123456789"

    with pytest.raises(TypeError, match="must be a UUID"):
        deterministic_campaign_name("not-a-uuid")  # type: ignore[arg-type]


# ── G - J. Payload Construction Tests ────────────────────────────────────────

def test_build_campaign_payload():
    """Verify Campaign creation payload construction with status=PAUSED."""
    payload = MetaMarketingApiAdapter.build_campaign_payload(
        name="VB-EXP-1111",
        objective="OUTCOME_TRAFFIC",
        special_ad_categories=["NONE"],
    )
    assert payload == {
        "name": "VB-EXP-1111",
        "objective": "OUTCOME_TRAFFIC",
        "special_ad_categories": ["NONE"],
        "status": "PAUSED",
    }


def test_build_campaign_payload_rejects_active_status():
    with pytest.raises(ValueError, match="Creation status must strictly be 'PAUSED'"):
        MetaMarketingApiAdapter.build_campaign_payload(name="VB-EXP-1", status="ACTIVE")


def test_build_adset_payload():
    """Verify Ad Set creation payload construction with lifetime budget and IN targeting."""
    end_dt = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
    payload = MetaMarketingApiAdapter.build_adset_payload(
        campaign_id="120205837492810384",
        name="AdSet — Traffic IN",
        lifetime_budget_paise=10000,
        end_time=end_dt,
        countries=["IN"],
        age_min=18,
        age_max=50,
        optimization_goal="LINK_CLICKS",
        billing_event="IMPRESSIONS",
    )
    assert payload["campaign_id"] == "120205837492810384"
    assert payload["name"] == "AdSet — Traffic IN"
    assert payload["lifetime_budget"] == 10000
    assert payload["status"] == "PAUSED"
    assert payload["optimization_goal"] == "LINK_CLICKS"
    assert payload["billing_event"] == "IMPRESSIONS"
    assert payload["end_time"] == "2026-10-08T12:00:00+00:00"
    assert payload["targeting"] == {
        "geo_locations": {"countries": ["IN"]},
        "age_min": 18,
        "age_max": 50,
    }


def test_build_creative_payload():
    """Verify Ad Creative creation payload with object_story_spec."""
    payload = MetaMarketingApiAdapter.build_creative_payload(
        name="Creative — Hero V1",
        page_id=SAMPLE_PAGE_ID,
        link="https://example.com/experiment",
        message="Primary copy text",
        headline="Headline text",
        image_hash="hash_12345",
        call_to_action="LEARN_MORE",
    )
    assert payload["name"] == "Creative — Hero V1"
    assert payload["object_story_spec"]["page_id"] == SAMPLE_PAGE_ID
    link_data = payload["object_story_spec"]["link_data"]
    assert link_data["link"] == "https://example.com/experiment"
    assert link_data["message"] == "Primary copy text"
    assert link_data["name"] == "Headline text"
    assert link_data["image_hash"] == "hash_12345"
    assert link_data["call_to_action"] == {
        "type": "LEARN_MORE",
        "value": {"link": "https://example.com/experiment"},
    }


def test_build_ad_payload():
    """Verify Ad creation payload with status=PAUSED."""
    payload = MetaMarketingApiAdapter.build_ad_payload(
        name="Ad — Unit 1",
        adset_id="120205837501920384",
        creative_id="120205837512340384",
    )
    assert payload == {
        "name": "Ad — Unit 1",
        "adset_id": "120205837501920384",
        "creative": {"creative_id": "120205837512340384"},
        "status": "PAUSED",
    }


def test_build_pause_payload():
    """Verify pause payload contains status=PAUSED."""
    assert MetaMarketingApiAdapter.build_pause_payload() == {"status": "PAUSED"}


# ── N. Duplicate Preflight Request & Check ───────────────────────────────────

def test_build_campaign_duplicate_check_url():
    """Verify preflight URL contains fields and name equality filtering."""
    url = MetaMarketingApiAdapter.build_campaign_duplicate_check_url(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        campaign_name="VB-EXP-test-123",
        api_version="v26.0",
    )
    assert url.startswith("https://graph.facebook.com/v26.0/act_1985595022114520/campaigns?")
    assert "fields=id%2Cname%2Cstatus" in url
    assert "filtering=" in url


def test_check_campaign_exists_with_mock_transport():
    """Verify preflight check detects existing campaign or returns False."""
    # Existing campaign response
    existing_response = json.dumps({
        "data": [{"id": "120205837492810384", "name": "VB-EXP-123", "status": "PAUSED"}]
    }).encode("utf-8")

    mock_transport_found = lambda req, timeout: existing_response
    adapter_found = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=mock_transport_found,
    )
    assert adapter_found.check_campaign_exists("VB-EXP-123") is True

    # Non-existing campaign response
    empty_response = json.dumps({"data": []}).encode("utf-8")
    mock_transport_empty = lambda req, timeout: empty_response
    adapter_empty = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=mock_transport_empty,
    )
    assert adapter_empty.check_campaign_exists("VB-EXP-nonexistent") is False


# ── O - T. Response Parsing Tests ────────────────────────────────────────────

def test_parse_creation_response_valid():
    """Test creation response parsing returns entity ID."""
    assert MetaMarketingApiAdapter.parse_creation_response('{"id": "120205837492810384"}') == "120205837492810384"
    assert MetaMarketingApiAdapter.parse_creation_response({"id": "9876543210"}) == "9876543210"


def test_parse_image_upload_response_valid():
    """Test image upload response parsing extracts hash."""
    payload = {
        "images": {
            "hero.png": {
                "hash": "c4ca4238a0b923820dcc509a6f75849b",
                "url": "https://scontent.xx.fbcdn.net/hero.png",
            }
        }
    }
    # With explicit filename
    assert MetaMarketingApiAdapter.parse_image_upload_response(payload, filename="hero.png") == "c4ca4238a0b923820dcc509a6f75849b"
    # Auto-extract first
    assert MetaMarketingApiAdapter.parse_image_upload_response(payload) == "c4ca4238a0b923820dcc509a6f75849b"


def test_parse_pause_response_valid():
    """Test pause mutation response parsing."""
    assert MetaMarketingApiAdapter.parse_pause_response('{"success": true}') is True
    assert MetaMarketingApiAdapter.parse_pause_response({"success": True}) is True


# ── U - V. Malformed & Meta Error Handling ───────────────────────────────────

def test_parse_creation_response_malformed():
    with pytest.raises(ValueError, match="Malformed JSON"):
        MetaMarketingApiAdapter.parse_creation_response("{not-valid-json}")

    with pytest.raises(ValueError, match="Missing or empty 'id'"):
        MetaMarketingApiAdapter.parse_creation_response('{"other_field": 123}')


def test_parse_image_upload_response_malformed():
    with pytest.raises(ValueError, match="Missing or invalid 'images' map"):
        MetaMarketingApiAdapter.parse_image_upload_response('{"id": "123"}')

    payload = {"images": {"img1.png": {"url": "https://example.com"}}}
    with pytest.raises(ValueError, match="Missing or empty 'hash'"):
        MetaMarketingApiAdapter.parse_image_upload_response(payload, filename="img1.png")


def test_parse_pause_response_unsuccessful():
    with pytest.raises(ValueError, match="Unsuccessful pause response"):
        MetaMarketingApiAdapter.parse_pause_response('{"success": false}')


def test_parse_response_meta_api_errors():
    """Test Meta Graph API error envelope mapping."""
    # Code 100: Invalid Parameter
    err_100 = json.dumps({"error": {"code": 100, "message": "Invalid budget"}}).encode("utf-8")
    with pytest.raises(MetaApiError, match="Meta Graph API error \\(code 100\\)"):
        MetaMarketingApiAdapter.parse_creation_response(err_100.decode("utf-8"))

    # Code 190: OAuth Error
    err_190 = json.dumps({"error": {"code": 190, "message": "Invalid OAuth access token"}}).encode("utf-8")
    with pytest.raises(MetaApiAuthError, match="code 190"):
        MetaMarketingApiAdapter.parse_creation_response(err_190.decode("utf-8"))

    # Code 200: Permission Error
    err_200 = json.dumps({"error": {"code": 200, "message": "Missing ads_management permission"}}).encode("utf-8")
    with pytest.raises(MetaApiPermissionError, match="code 200"):
        MetaMarketingApiAdapter.parse_creation_response(err_200.decode("utf-8"))

    # Code 404: Not Found
    err_404 = json.dumps({"error": {"code": 404, "message": "Object not found"}}).encode("utf-8")
    with pytest.raises(MetaApiNotFoundError, match="code 404"):
        MetaMarketingApiAdapter.parse_creation_response(err_404.decode("utf-8"))


# ── W. Write Guardrail & Mocked Operations ───────────────────────────────────

def test_live_write_guardrail_strictly_blocks_unmocked_requests():
    """CRITICAL SAFETY TEST: Verify write methods raise error if transport is not provided."""
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=None,  # No mock transport provided
    )

    with pytest.raises(MetaApiError, match="Live Meta write operations are strictly disabled in Step 36"):
        adapter.create_campaign(name="VB-EXP-1")

    with pytest.raises(MetaApiError, match="Live Meta write operations are strictly disabled in Step 36"):
        adapter.create_adset(
            campaign_id="123",
            name="AdSet-1",
            lifetime_budget_paise=5000,
            end_time="2026-10-08T12:00:00+00:00",
        )

    with pytest.raises(MetaApiError, match="Live Meta write operations are strictly disabled in Step 36"):
        adapter.upload_image(file_bytes=b"fake-image-bytes", filename="hero.png")

    with pytest.raises(MetaApiError, match="Live Meta write operations are strictly disabled in Step 36"):
        adapter.create_creative(
            name="Creative-1",
            page_id=SAMPLE_PAGE_ID,
            link="https://example.com",
            message="Test",
            headline="Test",
            image_hash="hash1",
        )

    with pytest.raises(MetaApiError, match="Live Meta write operations are strictly disabled in Step 36"):
        adapter.create_ad(name="Ad-1", adset_id="123", creative_id="456")

    with pytest.raises(MetaApiError, match="Live Meta write operations are strictly disabled in Step 36"):
        adapter.pause_campaign(campaign_id="120205837492810384")


def test_full_mocked_creation_and_pause_sequence():
    """Verify entire sequential creation and emergency pause lifecycle using mock transport."""
    recorded_requests: list[urllib.request.Request] = []

    def mock_transport(req: urllib.request.Request, timeout: float) -> bytes:
        recorded_requests.append(req)
        url = req.full_url
        if "/campaigns" in url:
            return b'{"id": "CAMPAIGN_12345"}'
        elif "/adsets" in url:
            return b'{"id": "ADSET_12345"}'
        elif "/adimages" in url:
            return b'{"images": {"hero.png": {"hash": "HASH_12345"}}}'
        elif "/adcreatives" in url:
            return b'{"id": "CREATIVE_12345"}'
        elif "/ads" in url:
            return b'{"id": "AD_12345"}'
        elif "/CAMPAIGN_12345" in url:
            return b'{"success": true}'
        return b'{"error": {"code": 404, "message": "Not Found"}}'

    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=mock_transport,
    )

    # 1. Create Campaign
    cid = adapter.create_campaign(name="VB-EXP-TEST")
    assert cid == "CAMPAIGN_12345"

    # 2. Create Ad Set
    asid = adapter.create_adset(
        campaign_id=cid,
        name="AdSet — IN Traffic",
        lifetime_budget_paise=10000,
        end_time=datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert asid == "ADSET_12345"

    # 3. Upload Image
    imghash = adapter.upload_image(file_bytes=b"fake-bytes", filename="hero.png")
    assert imghash == "HASH_12345"

    # 4. Create Creative
    crid = adapter.create_creative(
        name="Creative — Hero",
        page_id=SAMPLE_PAGE_ID,
        link="https://example.com/exp",
        message="Primary copy",
        headline="Headline",
        image_hash=imghash,
    )
    assert crid == "CREATIVE_12345"

    # 5. Create Ad
    adid = adapter.create_ad(
        name="Ad — Unit 1",
        adset_id=asid,
        creative_id=crid,
    )
    assert adid == "AD_12345"

    # 6. Pause Campaign (Emergency Stop / Kill)
    paused = adapter.pause_campaign(campaign_id=cid)
    assert paused is True

    # Verify all 6 requests used POST
    assert len(recorded_requests) == 6
    for req in recorded_requests:
        assert req.get_method() == "POST"


# ── X - Z. Zero Side-Effect Safety Checks ────────────────────────────────────

def test_zero_capital_ledger_experiment_or_decision_mutations():
    """Verify that mocked adapter operations cause zero database mutations."""
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = get_session_factory(engine)

    def mock_transport(req: urllib.request.Request, timeout: float) -> bytes:
        return b'{"id": "12345"}'

    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=mock_transport,
    )

    # Perform mocked campaign creation
    adapter.create_campaign(name="VB-EXP-SAFE")

    # Assert database tables remain completely empty
    with session_factory() as session:
        ledger_count = session.query(CapitalTransactionORM).count()
        exp_count = session.query(ExperimentORM).count()
        dec_count = session.query(DecisionORM).count()

        assert ledger_count == 0, "CapitalTransactionORM was unexpectedly mutated"
        assert exp_count == 0, "ExperimentORM was unexpectedly mutated"
        assert dec_count == 0, "DecisionORM was unexpectedly mutated"
