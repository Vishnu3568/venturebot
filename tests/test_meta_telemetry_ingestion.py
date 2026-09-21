"""Unit and integration tests for On-Demand Meta Telemetry Ingestion (Step 44).

Verifies:
A. Valid campaign telemetry ingestion.
B. Experiment without ExternalExecution -> rejected, no Meta request.
C. ExternalExecution without campaign_id -> rejected, no Meta request.
D. Valid Meta telemetry mapping: spend -> cost, impressions -> impressions, clicks -> clicks.
E. Raw telemetry marked FACT.
F. source_reference uses locked campaign identity.
G. recorded_at is generated as observation-ingestion timestamp.
H. Exact duplicate: same logical window, same observed values -> DUPLICATE_NO_OP, no second metric row.
I. Restatement: same logical window, changed observed values -> RESTATEMENT_APPENDED, new immutable metric row.
J. Previous observation remains unchanged upon restatement.
K. Missing optional metrics remain unset (visitors, conversions, conversion_rate, roas).
L. Missing != zero.
M. Zero observed value is preserved (spend=0.00, impressions=0, clicks=0).
N. Malformed Meta response -> no metric created.
O. Meta timeout/network failure -> no metric created.
P. Meta permission/auth failure -> no metric created.
Q. Zero CapitalTransaction created.
R. Zero ExperimentDecision created.
S. Zero experiment lifecycle mutation.
T. Repeated exact ingestion is idempotent.
U. Restatement creates a new observation rather than updating the previous one.
V. No Meta write methods are invoked (GET only).
W. Full test path works with mocked Meta transport.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable
from urllib.request import Request
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from venturebot.database.connection import get_engine, get_session_factory, init_db
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.database.repositories.decision import DecisionRepository
from venturebot.database.repositories.experiment import ExperimentRepository
from venturebot.database.repositories.external_execution import ExternalExecutionRepository
from venturebot.database.repositories.metrics import MetricsRepository
from venturebot.database.repositories.opportunity import OpportunityRepository
from venturebot.execution.meta import (
    MetaApiAuthError,
    MetaApiPermissionError,
    MetaApiTimeoutError,
    MetaMarketingApiAdapter,
)
from venturebot.measurement.telemetry import (
    MetaTelemetryIngestionResult,
    MetaTelemetryIngestionService,
    TelemetryIngestionStatus,
)
from venturebot.models.evidence import EvidenceCategory
from venturebot.models.experiment import Channel, Experiment, ExperimentStatus, MonetizationMethod
from venturebot.models.external_execution import ExternalExecution, ExternalExecutionStatus
from venturebot.models.opportunity import Opportunity, OpportunityCategory, OpportunityStatus

SAMPLE_ACCOUNT_ID = "act_1985595022114520"
SAMPLE_CAMPAIGN_ID = "12021000000000001"
SAMPLE_TOKEN = "EAABwzL12345fakeToken"


@pytest.fixture
def session() -> Session:
    """Isolated in-memory SQLite database session with starting capital initialized."""
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = get_session_factory(engine)
    with session_factory() as sess:
        cap_repo = CapitalRepository(sess)
        cap_repo.initialize_starting_capital()
        return sess


@pytest.fixture
def test_experiment(session: Session) -> Experiment:
    """Persisted experiment in DRAFT status."""
    opp_repo = OpportunityRepository(session)
    opp = opp_repo.create(
        Opportunity(
            title="AI Invoicing Assistant",
            description="Tool generating automated invoices for freelancers.",
            category=OpportunityCategory.PRODUCT,
            status=OpportunityStatus.DISCOVERED,
            source="Manual survey",
            audience="Freelancers in India",
            monetization_notes="₹499/mo direct subscription",
            estimated_revenue_min=Decimal("500.00"),
            estimated_revenue_max=Decimal("2000.00"),
            estimated_cost_min=Decimal("50.00"),
            estimated_cost_max=Decimal("100.00"),
        )
    )

    exp_repo = ExperimentRepository(session)
    return exp_repo.create(
        Experiment(
            opportunity_id=opp.id,
            hypothesis="Freelancers will subscribe to automated invoice generator.",
            objective="Acquire 3 paying subscribers.",
            channel=Channel.FACEBOOK,
            monetization_method=MonetizationMethod.SUBSCRIPTION,
            allocated_budget=Decimal("100.00"),
            max_allowed_spend=Decimal("100.00"),
            actual_spend=Decimal("0.00"),
            success_criteria="3 paying users.",
            failure_criteria="0 users from ₹100 spend.",
            status=ExperimentStatus.DRAFT,
        )
    )


@pytest.fixture
def deployed_external_execution(session: Session, test_experiment: Experiment) -> ExternalExecution:
    """Persisted ExternalExecution record linked to test_experiment with valid campaign_id."""
    ext_repo = ExternalExecutionRepository(session)
    return ext_repo.create(
        ExternalExecution(
            experiment_id=test_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id=SAMPLE_CAMPAIGN_ID,
            adset_id="12021000000000002",
            creative_id="12021000000000003",
            ad_id="12021000000000004",
            status=ExternalExecutionStatus.DEPLOYED,
        )
    )


def make_mock_adapter(
    response_payload: dict[str, Any] | None = None,
    error_to_raise: Exception | None = None,
) -> MetaMarketingApiAdapter:
    """Construct MetaMarketingApiAdapter with a mock transport returning JSON or raising."""

    def mock_transport(req: Request, timeout: float) -> bytes:
        # Enforce read-only constraint: only GET allowed
        assert req.get_method() == "GET", f"Expected GET, got {req.get_method()}"
        if error_to_raise:
            raise error_to_raise
        return json.dumps(response_payload or {"data": []}).encode("utf-8")

    return MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=mock_transport,
    )


# ── A, D, E, F, G, K, Q, R, S, V, W: Valid Ingestion & Guardrails ─────────────

def test_valid_campaign_telemetry_ingestion(
    session: Session,
    test_experiment: Experiment,
    deployed_external_execution: ExternalExecution,
):
    """Verifies valid telemetry ingestion, correct field mapping, evidence FACT, and zero mutations."""
    date_start = date(2026, 9, 19)
    date_stop = date(2026, 9, 19)

    payload = {
        "data": [
            {
                "account_id": SAMPLE_ACCOUNT_ID,
                "campaign_id": SAMPLE_CAMPAIGN_ID,
                "date_start": "2026-09-19",
                "date_stop": "2026-09-19",
                "spend": "45.50",
                "impressions": "1200",
                "clicks": "35",
                "cpc": "1.30",
                "cpm": "37.92",
                "ctr": "2.92",
            }
        ]
    }
    adapter = make_mock_adapter(response_payload=payload)

    # Initial state
    cap_repo = CapitalRepository(session)
    dec_repo = DecisionRepository(session)
    initial_liquid = cap_repo.get_financial_summary().current_balance
    initial_exp_status = test_experiment.status

    # Ingest
    result = MetaTelemetryIngestionService.ingest_campaign_metrics(
        session=session,
        experiment_id=test_experiment.id,
        date_start=date_start,
        date_stop=date_stop,
        adapter=adapter,
    )

    # Status & Result checks
    assert result.status == TelemetryIngestionStatus.INGESTED
    assert result.experiment_id == test_experiment.id
    assert result.campaign_id == SAMPLE_CAMPAIGN_ID
    assert result.date_start == date_start
    assert result.date_stop == date_stop
    expected_ref = f"meta:insights:campaign:{SAMPLE_CAMPAIGN_ID}:2026-09-19:2026-09-19"
    assert result.source_reference == expected_ref

    # Metric mapping checks
    metrics = result.metrics
    assert metrics.cost == Decimal("45.50")
    assert metrics.impressions == 1200
    assert metrics.clicks == 35
    assert metrics.evidence_type == EvidenceCategory.FACT
    assert metrics.source_reference == expected_ref
    assert isinstance(metrics.recorded_at, datetime)
    assert metrics.recorded_at.tzinfo is not None

    # Unprovided fields remain unset / None
    assert metrics.visitors is None
    assert metrics.conversions is None
    assert metrics.revenue is None
    assert metrics.profit_loss is None
    assert metrics.roi is None
    assert metrics.roas is None

    # Financial ledger isolation
    assert cap_repo.get_financial_summary().current_balance == initial_liquid
    assert cap_repo.get_financial_summary().total_cost == Decimal("0.00")
    assert len(cap_repo.get_transaction_history()) == 1  # only INITIAL_DEPOSIT

    # Decision isolation
    assert dec_repo.list(experiment_id=test_experiment.id) == []

    # Lifecycle isolation
    reloaded_exp = ExperimentRepository(session).get(test_experiment.id)
    assert reloaded_exp is not None
    assert reloaded_exp.status == initial_exp_status


# ── B: Experiment Without ExternalExecution ───────────────────────────────────

def test_ingestion_rejects_missing_external_execution(session: Session, test_experiment: Experiment):
    """Experiment without an ExternalExecution mapping raises ValueError without calling Meta."""
    adapter = make_mock_adapter(error_to_raise=RuntimeError("Should never be called"))

    with pytest.raises(ValueError, match="No ExternalExecution record found"):
        MetaTelemetryIngestionService.ingest_campaign_metrics(
            session=session,
            experiment_id=test_experiment.id,
            date_start=date(2026, 9, 19),
            date_stop=date(2026, 9, 19),
            adapter=adapter,
        )


# ── C: ExternalExecution Without campaign_id ──────────────────────────────────

def test_ingestion_rejects_missing_campaign_id(session: Session, test_experiment: Experiment):
    """ExternalExecution without campaign_id raises ValueError without calling Meta."""
    ext_repo = ExternalExecutionRepository(session)
    ext_repo.create(
        ExternalExecution(
            experiment_id=test_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id=None,  # missing
            status=ExternalExecutionStatus.PENDING,
        )
    )

    adapter = make_mock_adapter(error_to_raise=RuntimeError("Should never be called"))

    with pytest.raises(ValueError, match="missing campaign_id"):
        MetaTelemetryIngestionService.ingest_campaign_metrics(
            session=session,
            experiment_id=test_experiment.id,
            date_start=date(2026, 9, 19),
            date_stop=date(2026, 9, 19),
            adapter=adapter,
        )


# ── Non-Existent Experiment ───────────────────────────────────────────────────

def test_ingestion_rejects_non_existent_experiment(session: Session):
    """Attempting ingestion for a non-existent experiment ID raises ValueError."""
    fake_id = uuid4()
    with pytest.raises(ValueError, match=f"Experiment '{fake_id}' does not exist"):
        MetaTelemetryIngestionService.ingest_campaign_metrics(
            session=session,
            experiment_id=fake_id,
            date_start=date(2026, 9, 19),
            date_stop=date(2026, 9, 19),
        )


# ── Invalid Date Ranges ───────────────────────────────────────────────────────

def test_ingestion_rejects_inverted_dates(
    session: Session,
    test_experiment: Experiment,
    deployed_external_execution: ExternalExecution,
):
    """date_stop earlier than date_start raises ValueError."""
    with pytest.raises(ValueError, match="cannot be earlier than date_start"):
        MetaTelemetryIngestionService.ingest_campaign_metrics(
            session=session,
            experiment_id=test_experiment.id,
            date_start=date(2026, 9, 20),
            date_stop=date(2026, 9, 19),
        )


# ── H, T: Exact Duplicate -> DUPLICATE_NO_OP (Idempotency) ───────────────────

def test_exact_duplicate_is_idempotent_no_op(
    session: Session,
    test_experiment: Experiment,
    deployed_external_execution: ExternalExecution,
):
    """Repeated ingestion of identical observations returns DUPLICATE_NO_OP with zero new rows."""
    payload = {
        "data": [
            {
                "account_id": SAMPLE_ACCOUNT_ID,
                "campaign_id": SAMPLE_CAMPAIGN_ID,
                "date_start": "2026-09-19",
                "date_stop": "2026-09-19",
                "spend": "50.00",
                "impressions": "1000",
                "clicks": "25",
            }
        ]
    }
    adapter = make_mock_adapter(response_payload=payload)

    # First ingestion
    res1 = MetaTelemetryIngestionService.ingest_campaign_metrics(
        session=session,
        experiment_id=test_experiment.id,
        date_start="2026-09-19",
        date_stop="2026-09-19",
        adapter=adapter,
    )
    assert res1.status == TelemetryIngestionStatus.INGESTED
    initial_metric_id = res1.metrics.id

    metrics_repo = MetricsRepository(session)
    assert len(metrics_repo.list_for_experiment(test_experiment.id)) == 1

    # Second ingestion with exact same data
    res2 = MetaTelemetryIngestionService.ingest_campaign_metrics(
        session=session,
        experiment_id=test_experiment.id,
        date_start="2026-09-19",
        date_stop="2026-09-19",
        adapter=adapter,
    )
    assert res2.status == TelemetryIngestionStatus.DUPLICATE_NO_OP
    assert res2.metrics.id == initial_metric_id

    # Database count remains strictly 1 (no duplicate row)
    assert len(metrics_repo.list_for_experiment(test_experiment.id)) == 1


# ── I, J, U: Restatement -> RESTATEMENT_APPENDED ─────────────────────────────

def test_restatement_appends_new_immutable_observation(
    session: Session,
    test_experiment: Experiment,
    deployed_external_execution: ExternalExecution,
):
    """When Meta returns revised values for the same window, a new observation is appended."""
    payload1 = {
        "data": [
            {
                "account_id": SAMPLE_ACCOUNT_ID,
                "campaign_id": SAMPLE_CAMPAIGN_ID,
                "date_start": "2026-09-19",
                "date_stop": "2026-09-19",
                "spend": "100.00",
                "impressions": "1000",
                "clicks": "20",
            }
        ]
    }
    adapter1 = make_mock_adapter(response_payload=payload1)

    # Initial ingestion
    res1 = MetaTelemetryIngestionService.ingest_campaign_metrics(
        session=session,
        experiment_id=test_experiment.id,
        date_start="2026-09-19",
        date_stop="2026-09-19",
        adapter=adapter1,
    )
    assert res1.status == TelemetryIngestionStatus.INGESTED
    first_metric_id = res1.metrics.id

    # Restatement payload with updated spend and clicks
    payload2 = {
        "data": [
            {
                "account_id": SAMPLE_ACCOUNT_ID,
                "campaign_id": SAMPLE_CAMPAIGN_ID,
                "date_start": "2026-09-19",
                "date_stop": "2026-09-19",
                "spend": "105.00",
                "impressions": "1050",
                "clicks": "22",
            }
        ]
    }
    adapter2 = make_mock_adapter(response_payload=payload2)

    # Second ingestion (restatement)
    res2 = MetaTelemetryIngestionService.ingest_campaign_metrics(
        session=session,
        experiment_id=test_experiment.id,
        date_start="2026-09-19",
        date_stop="2026-09-19",
        adapter=adapter2,
    )
    assert res2.status == TelemetryIngestionStatus.RESTATEMENT_APPENDED
    assert res2.metrics.id != first_metric_id
    assert res2.metrics.cost == Decimal("105.00")
    assert res2.metrics.impressions == 1050
    assert res2.metrics.clicks == 22
    assert res2.metrics.revenue is None
    assert res2.metrics.profit_loss is None
    assert res2.metrics.roi is None
    assert res2.metrics.roas is None
    assert res2.source_reference == res1.source_reference

    # Verify both records exist in chronological sequence
    metrics_repo = MetricsRepository(session)
    all_records = metrics_repo.list_for_experiment(test_experiment.id)
    assert len(all_records) == 2

    # Verify the previous observation is completely unchanged
    first_record = metrics_repo.get(first_metric_id)
    assert first_record is not None
    assert first_record.cost == Decimal("100.00")
    assert first_record.impressions == 1000
    assert first_record.clicks == 20
    assert first_record.revenue is None
    assert first_record.profit_loss is None


# ── M: Zero Observed Values Preserved ─────────────────────────────────────────

def test_zero_observed_values_preserved(
    session: Session,
    test_experiment: Experiment,
    deployed_external_execution: ExternalExecution,
):
    """Meta returning 0 clicks, 0 impressions, 0.00 spend is preserved accurately."""
    payload = {
        "data": [
            {
                "account_id": SAMPLE_ACCOUNT_ID,
                "campaign_id": SAMPLE_CAMPAIGN_ID,
                "date_start": "2026-09-20",
                "date_stop": "2026-09-20",
                "spend": "0.00",
                "impressions": "0",
                "clicks": "0",
            }
        ]
    }
    adapter = make_mock_adapter(response_payload=payload)

    result = MetaTelemetryIngestionService.ingest_campaign_metrics(
        session=session,
        experiment_id=test_experiment.id,
        date_start="2026-09-20",
        date_stop="2026-09-20",
        adapter=adapter,
    )
    assert result.status == TelemetryIngestionStatus.INGESTED
    assert result.metrics.cost == Decimal("0.00")
    assert result.metrics.impressions == 0
    assert result.metrics.clicks == 0
    assert result.metrics.roi is None  # no roi when cost is zero


# ── N: Malformed Meta Response ────────────────────────────────────────────────

def test_malformed_meta_response_creates_no_metrics(
    session: Session,
    test_experiment: Experiment,
    deployed_external_execution: ExternalExecution,
):
    """Malformed response payload halts with ValueError and creates no metric row."""
    # Invalid missing 'data' key
    adapter = make_mock_adapter(response_payload={"unexpected_key": 123})

    with pytest.raises(ValueError, match="missing or invalid 'data' list"):
        MetaTelemetryIngestionService.ingest_campaign_metrics(
            session=session,
            experiment_id=test_experiment.id,
            date_start="2026-09-19",
            date_stop="2026-09-19",
            adapter=adapter,
        )

    metrics_repo = MetricsRepository(session)
    assert len(metrics_repo.list_for_experiment(test_experiment.id)) == 0


# ── O: Meta Timeout Failure ───────────────────────────────────────────────────

def test_meta_timeout_creates_no_metrics(
    session: Session,
    test_experiment: Experiment,
    deployed_external_execution: ExternalExecution,
):
    """Meta API timeout propagates MetaApiTimeoutError and creates no metrics."""
    adapter = make_mock_adapter(error_to_raise=MetaApiTimeoutError("Request timed out"))

    with pytest.raises(MetaApiTimeoutError):
        MetaTelemetryIngestionService.ingest_campaign_metrics(
            session=session,
            experiment_id=test_experiment.id,
            date_start="2026-09-19",
            date_stop="2026-09-19",
            adapter=adapter,
        )

    metrics_repo = MetricsRepository(session)
    assert len(metrics_repo.list_for_experiment(test_experiment.id)) == 0


# ── P: Meta Auth Failure ──────────────────────────────────────────────────────

def test_meta_auth_failure_creates_no_metrics(
    session: Session,
    test_experiment: Experiment,
    deployed_external_execution: ExternalExecution,
):
    """Meta API auth failure propagates MetaApiAuthError and creates no metrics."""
    adapter = make_mock_adapter(error_to_raise=MetaApiAuthError("HTTP 401: Invalid token"))

    with pytest.raises(MetaApiAuthError):
        MetaTelemetryIngestionService.ingest_campaign_metrics(
            session=session,
            experiment_id=test_experiment.id,
            date_start="2026-09-19",
            date_stop="2026-09-19",
            adapter=adapter,
        )

    metrics_repo = MetricsRepository(session)
    assert len(metrics_repo.list_for_experiment(test_experiment.id)) == 0


# ── Empty Response Handling ───────────────────────────────────────────────────

def test_empty_meta_response_raises_and_creates_no_metrics(
    session: Session,
    test_experiment: Experiment,
    deployed_external_execution: ExternalExecution,
):
    """Empty data list from Meta raises ValueError indicating no delivery."""
    adapter = make_mock_adapter(response_payload={"data": []})

    with pytest.raises(ValueError, match="No Meta Insights telemetry data returned"):
        MetaTelemetryIngestionService.ingest_campaign_metrics(
            session=session,
            experiment_id=test_experiment.id,
            date_start="2026-09-19",
            date_stop="2026-09-19",
            adapter=adapter,
        )

    metrics_repo = MetricsRepository(session)
    assert len(metrics_repo.list_for_experiment(test_experiment.id)) == 0


# ── Multiple Response Rows Rejection (Section 12) ─────────────────────────────

def test_multiple_meta_rows_rejected(
    session: Session,
    test_experiment: Experiment,
    deployed_external_execution: ExternalExecution,
):
    """Unexpected multiple rows in campaign-level query raises ValueError without merging."""
    payload = {
        "data": [
            {
                "account_id": SAMPLE_ACCOUNT_ID,
                "campaign_id": SAMPLE_CAMPAIGN_ID,
                "date_start": "2026-09-19",
                "date_stop": "2026-09-19",
                "spend": "10.00",
                "impressions": "100",
                "clicks": "5",
            },
            {
                "account_id": SAMPLE_ACCOUNT_ID,
                "campaign_id": SAMPLE_CAMPAIGN_ID,
                "date_start": "2026-09-20",
                "date_stop": "2026-09-20",
                "spend": "20.00",
                "impressions": "200",
                "clicks": "10",
            },
        ]
    }
    adapter = make_mock_adapter(response_payload=payload)

    with pytest.raises(ValueError, match="Unexpected multiple Insights rows"):
        MetaTelemetryIngestionService.ingest_campaign_metrics(
            session=session,
            experiment_id=test_experiment.id,
            date_start="2026-09-19",
            date_stop="2026-09-20",
            adapter=adapter,
        )

    metrics_repo = MetricsRepository(session)
    assert len(metrics_repo.list_for_experiment(test_experiment.id)) == 0


# ── Mismatched Campaign ID Rejection ──────────────────────────────────────────

def test_mismatched_campaign_id_rejected(
    session: Session,
    test_experiment: Experiment,
    deployed_external_execution: ExternalExecution,
):
    """Telemetry returning a different campaign_id than expected raises ValueError."""
    payload = {
        "data": [
            {
                "account_id": SAMPLE_ACCOUNT_ID,
                "campaign_id": "99999999999999999",  # wrong campaign
                "date_start": "2026-09-19",
                "date_stop": "2026-09-19",
                "spend": "10.00",
                "impressions": "100",
                "clicks": "5",
            }
        ]
    }
    adapter = make_mock_adapter(response_payload=payload)

    with pytest.raises(ValueError, match="does not match expected campaign"):
        MetaTelemetryIngestionService.ingest_campaign_metrics(
            session=session,
            experiment_id=test_experiment.id,
            date_start="2026-09-19",
            date_stop="2026-09-19",
            adapter=adapter,
        )

    metrics_repo = MetricsRepository(session)
    assert len(metrics_repo.list_for_experiment(test_experiment.id)) == 0
