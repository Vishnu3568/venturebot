"""Tests for Step 19 — Wikimedia Research Source Adapter.

Verifies:
1. Canonical URL construction for Wikimedia Analytics Pageviews API.
2. Canonical Wikipedia article URL construction with encoding.
3. Filtering of clearly identifiable non-content/system pages (Main_Page, Special:...).
4. Parsing valid Wikimedia responses into canonical ResearchEvidenceItem objects.
5. All observations are strictly classified as EvidenceCategory.FACT.
6. Statements include empirical pageview count, rank, and ISO date.
7. Source references are non-empty, traceable to the canonical URL and API endpoint.
8. Limit parameter controls maximum observations returned.
9. Deterministic handling of malformed responses, missing fields, and bad JSON.
10. Deterministic handling of HTTP, network, and timeout failures.
11. Enforcement of required non-empty User-Agent header per Wikimedia policy.
12. Zero database writes, zero financial side effects, and zero automatic Opportunity creation.
13. Clean integration through OpportunityIngestionPayload and OpportunityIngestionService.
"""

from collections.abc import Generator
from datetime import date
from decimal import Decimal
import json
from unittest.mock import MagicMock, patch
import urllib.error
import urllib.request

import pytest
from sqlalchemy.orm import Session

from venturebot.database.connection import get_engine, get_session_factory, init_db
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.database.repositories.opportunity import OpportunityRepository
from venturebot.models.evidence import EvidenceCategory
from venturebot.models.opportunity import Opportunity, OpportunityCategory, OpportunityStatus
from venturebot.opportunity.ingestion import OpportunityIngestionService
from venturebot.opportunity.models import OpportunityIngestionPayload, ResearchEvidenceItem
from venturebot.opportunity.wikimedia import WikimediaPageviewsAdapter


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """Isolated SQLite in-memory database session."""
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = get_session_factory(engine)
    with session_factory() as sess:
        yield sess


SAMPLE_WIKIMEDIA_RESPONSE = {
    "items": [
        {
            "project": "en.wikipedia",
            "access": "all-access",
            "year": "2026",
            "month": "09",
            "day": "16",
            "articles": [
                {
                    "article": "Main_Page",
                    "views": 15000000,
                    "rank": 1,
                },
                {
                    "article": "Special:Search",
                    "views": 2500000,
                    "rank": 2,
                },
                {
                    "article": "Artificial_intelligence",
                    "views": 450123,
                    "rank": 3,
                },
                {
                    "article": "Formula_One",
                    "views": 312450,
                    "rank": 4,
                },
                {
                    "article": "Dune_(franchise)",
                    "views": 189700,
                    "rank": 5,
                },
            ],
        }
    ]
}


def make_mock_http_response(payload: dict | str, charset: str = "utf-8") -> MagicMock:
    """Helper to generate a mock urllib HTTP response."""
    mock_resp = MagicMock()
    if isinstance(payload, dict):
        raw_bytes = json.dumps(payload).encode(charset)
    else:
        raw_bytes = payload.encode(charset)
    mock_resp.read.return_value = raw_bytes
    mock_resp.headers.get_content_charset.return_value = charset
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False
    return mock_resp


# ── URL Construction & Filtering Tests ──────────────────────────────────────────

def test_build_url_constructs_valid_endpoint():
    """Builds the canonical Wikimedia endpoint with zero-padded date components."""
    target = date(2026, 9, 5)
    url = WikimediaPageviewsAdapter.build_url(target)
    assert url == "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/2026/09/05"


def test_build_url_validates_inputs():
    """Rejects dates before July 2015 and empty project/access strings."""
    with pytest.raises(ValueError, match="target_date must be a datetime.date"):
        WikimediaPageviewsAdapter.build_url("2026-09-05")  # type: ignore

    with pytest.raises(ValueError, match="only available from 2015-07-01"):
        WikimediaPageviewsAdapter.build_url(date(2014, 12, 31))

    with pytest.raises(ValueError, match="project must not be empty"):
        WikimediaPageviewsAdapter.build_url(date(2026, 9, 5), project="  ")

    with pytest.raises(ValueError, match="access must not be empty"):
        WikimediaPageviewsAdapter.build_url(date(2026, 9, 5), access="")


def test_build_article_url():
    """Builds canonical Wikipedia article URL with proper encoding."""
    url1 = WikimediaPageviewsAdapter.build_article_url("Formula_One")
    assert url1 == "https://en.wikipedia.org/wiki/Formula_One"

    url2 = WikimediaPageviewsAdapter.build_article_url("Dune (franchise)")
    assert url2 == "https://en.wikipedia.org/wiki/Dune_(franchise)"

    with pytest.raises(ValueError, match="article title must not be empty"):
        WikimediaPageviewsAdapter.build_article_url("   ")


def test_is_non_content_page_filters_system_entries():
    """Identifies Main_Page and Special namespace entries while preserving content."""
    assert WikimediaPageviewsAdapter.is_non_content_page("Main_Page") is True
    assert WikimediaPageviewsAdapter.is_non_content_page("main_page") is True
    assert WikimediaPageviewsAdapter.is_non_content_page("Main Page") is True
    assert WikimediaPageviewsAdapter.is_non_content_page("Special:Search") is True
    assert WikimediaPageviewsAdapter.is_non_content_page("special:recentchanges") is True
    assert WikimediaPageviewsAdapter.is_non_content_page("  ") is True

    assert WikimediaPageviewsAdapter.is_non_content_page("Artificial_intelligence") is False
    assert WikimediaPageviewsAdapter.is_non_content_page("Formula_One") is False
    assert WikimediaPageviewsAdapter.is_non_content_page("Main_Street") is False


# ── Parsing & Evidence Contract Tests ──────────────────────────────────────────

def test_parse_response_creates_fact_evidence_items():
    """Parses response into ResearchEvidenceItem objects with FACT category and non-content filtering."""
    target = date(2026, 9, 16)
    api_url = WikimediaPageviewsAdapter.build_url(target)

    items = WikimediaPageviewsAdapter.parse_response(
        payload=SAMPLE_WIKIMEDIA_RESPONSE,
        api_url=api_url,
        target_date=target,
    )

    # 5 articles in sample minus Main_Page and Special:Search = 3 items
    assert len(items) == 3

    for item in items:
        assert isinstance(item, ResearchEvidenceItem)
        assert item.category == EvidenceCategory.FACT
        assert item.statement != ""
        assert item.source_reference != ""
        assert api_url in item.source_reference

    # Check first content item: Artificial_intelligence
    ai_item = items[0]
    assert "Artificial intelligence" in ai_item.statement
    assert "450123 pageviews" in ai_item.statement
    assert "rank #3" in ai_item.statement
    assert "2026-09-16" in ai_item.statement
    assert "https://en.wikipedia.org/wiki/Artificial_intelligence" in ai_item.source_reference


def test_parse_response_respects_limit():
    """Returns only up to limit valid content observations."""
    target = date(2026, 9, 16)
    api_url = WikimediaPageviewsAdapter.build_url(target)

    items = WikimediaPageviewsAdapter.parse_response(
        payload=SAMPLE_WIKIMEDIA_RESPONSE,
        api_url=api_url,
        target_date=target,
        limit=2,
    )
    assert len(items) == 2
    assert "Artificial intelligence" in items[0].statement
    assert "Formula One" in items[1].statement

    with pytest.raises(ValueError, match="limit must be greater than zero"):
        WikimediaPageviewsAdapter.parse_response(
            payload=SAMPLE_WIKIMEDIA_RESPONSE,
            api_url=api_url,
            target_date=target,
            limit=0,
        )


def test_parse_response_deterministic_error_handling():
    """Deterministic validation on missing or malformed fields."""
    target = date(2026, 9, 16)
    api_url = "https://example.com"

    # Empty payload string
    with pytest.raises(ValueError, match="Malformed Wikimedia JSON"):
        WikimediaPageviewsAdapter.parse_response("{not json}", api_url, target)

    # Missing items
    with pytest.raises(ValueError, match="missing or non-list 'items'"):
        WikimediaPageviewsAdapter.parse_response({}, api_url, target)

    # Empty items returns empty list
    assert WikimediaPageviewsAdapter.parse_response({"items": []}, api_url, target) == []

    # Missing articles in items[0]
    with pytest.raises(ValueError, match="missing or non-list 'articles'"):
        WikimediaPageviewsAdapter.parse_response({"items": [{}]}, api_url, target)

    # Invalid observation missing article
    bad_payload = {
        "items": [
            {
                "articles": [
                    {"views": 100, "rank": 1}
                ]
            }
        ]
    }
    with pytest.raises(ValueError, match="missing or empty 'article'"):
        WikimediaPageviewsAdapter.parse_response(bad_payload, api_url, target)

    # Invalid observation negative views
    bad_payload2 = {
        "items": [
            {
                "articles": [
                    {"article": "Test", "views": -5, "rank": 1}
                ]
            }
        ]
    }
    with pytest.raises(ValueError, match="views' must be a non-negative integer"):
        WikimediaPageviewsAdapter.parse_response(bad_payload2, api_url, target)


# ── HTTP Boundary & Network Error Handling Tests ───────────────────────────────

def test_fetch_top_pageviews_success_via_mock():
    """Fetches and parses top pageviews using standard library urllib mock."""
    target = date(2026, 9, 16)
    mock_resp = make_mock_http_response(SAMPLE_WIKIMEDIA_RESPONSE)

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        items = WikimediaPageviewsAdapter.fetch_top_pageviews(target_date=target, limit=2)
        assert len(items) == 2
        assert mock_urlopen.called

        # Verify request parameters
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert isinstance(req, urllib.request.Request)
        assert req.get_header("User-agent") == WikimediaPageviewsAdapter.DEFAULT_USER_AGENT
        assert req.get_header("Accept") == "application/json"


def test_fetch_top_pageviews_validates_user_agent():
    """Rejects empty or whitespace User-Agent strings."""
    with pytest.raises(ValueError, match="User-Agent header must not be empty"):
        WikimediaPageviewsAdapter.fetch_top_pageviews(date(2026, 9, 16), user_agent="   ")


def test_fetch_top_pageviews_handles_http_error():
    """Translates urllib.error.HTTPError into descriptive RuntimeError."""
    target = date(2026, 9, 16)
    err = urllib.error.HTTPError(
        url="https://wikimedia.org", code=404, msg="Not Found", hdrs={}, fp=None  # type: ignore
    )

    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(RuntimeError, match="Wikimedia API HTTP 404 error: Not Found"):
            WikimediaPageviewsAdapter.fetch_top_pageviews(target)


def test_fetch_top_pageviews_handles_network_error():
    """Translates urllib.error.URLError into descriptive RuntimeError."""
    target = date(2026, 9, 16)
    err = urllib.error.URLError("Connection refused")

    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(RuntimeError, match="Wikimedia API network connection failed: Connection refused"):
            WikimediaPageviewsAdapter.fetch_top_pageviews(target)


def test_fetch_top_pageviews_handles_timeout():
    """Translates TimeoutError into descriptive RuntimeError."""
    target = date(2026, 9, 16)

    with patch("urllib.request.urlopen", side_effect=TimeoutError("Connection timed out")):
        with pytest.raises(RuntimeError, match="Wikimedia API request timed out"):
            WikimediaPageviewsAdapter.fetch_top_pageviews(target)


# ── Side-Effect Isolation & Contract Boundary Tests ────────────────────────────

def test_adapter_has_zero_database_and_financial_side_effects(session: Session):
    """Adapter operations do not touch the database, ledger, or capital balance."""
    target = date(2026, 9, 16)
    mock_resp = make_mock_http_response(SAMPLE_WIKIMEDIA_RESPONSE)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        items = WikimediaPageviewsAdapter.fetch_top_pageviews(target)
        assert len(items) == 3

    # Verify database isolation
    opp_repo = OpportunityRepository(session)
    assert opp_repo.list() == []

    cap_repo = CapitalRepository(session)
    assert cap_repo.get_transaction_history() == []
    assert cap_repo.get_current_balance() == Decimal("0.00")


def test_adapter_does_not_create_opportunity():
    """Adapter returns strictly list[ResearchEvidenceItem], never an Opportunity."""
    target = date(2026, 9, 16)
    mock_resp = make_mock_http_response(SAMPLE_WIKIMEDIA_RESPONSE)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = WikimediaPageviewsAdapter.fetch_top_pageviews(target)

    assert isinstance(result, list)
    for item in result:
        assert isinstance(item, ResearchEvidenceItem)
        assert not isinstance(item, Opportunity)


def test_integration_flow_with_opportunity_ingestion_service(session: Session):
    """End-to-end integration: Wikimedia adapter output ingested into canonical Opportunity."""
    target = date(2026, 9, 16)
    mock_resp = make_mock_http_response(SAMPLE_WIKIMEDIA_RESPONSE)

    # 1. Fetch/parse Wikimedia evidence observations
    with patch("urllib.request.urlopen", return_value=mock_resp):
        evidence_items = WikimediaPageviewsAdapter.fetch_top_pageviews(target, limit=1)

    assert len(evidence_items) == 1
    evidence_item = evidence_items[0]
    assert evidence_item.category == EvidenceCategory.FACT

    # 2. Caller constructs OpportunityIngestionPayload
    payload = OpportunityIngestionPayload(
        opportunity=Opportunity(
            title="AI Tools Aggregator Newsletter",
            description="Curated daily briefing on top surging AI developments.",
            category=OpportunityCategory.CONTENT,
            status=OpportunityStatus.UNDER_REVIEW,  # Should be overridden to DISCOVERED
            audience="AI practitioners and researchers",
            monetization_notes="Sponsorships and premium subscriptions",
            estimated_revenue_min=Decimal("500.00"),
            estimated_revenue_max=Decimal("2000.00"),
            estimated_cost_min=Decimal("50.00"),
            estimated_cost_max=Decimal("200.00"),
        ),
        evidence_items=[evidence_item],
    )

    # 3. Existing OpportunityIngestionService performs the domain write
    persisted_opp = OpportunityIngestionService.ingest(session, payload)

    # 4. Assert canonical Opportunity properties
    assert persisted_opp.status == OpportunityStatus.DISCOVERED
    assert "[FACT]" in persisted_opp.evidence_notes
    assert "Artificial intelligence" in persisted_opp.evidence_notes
    assert "450123 pageviews" in persisted_opp.evidence_notes
    assert "https://en.wikipedia.org/wiki/Artificial_intelligence" in persisted_opp.source
    assert "https://wikimedia.org/api/rest_v1/metrics/pageviews/top" in persisted_opp.source

    # 5. Verify exactly one Opportunity created in database
    opp_repo = OpportunityRepository(session)
    all_opps = opp_repo.list()
    assert len(all_opps) == 1
    assert all_opps[0].id == persisted_opp.id

    # 6. Verify zero financial side effects
    cap_repo = CapitalRepository(session)
    assert cap_repo.get_transaction_history() == []
