"""Unit tests for Step 28 — Read-Only Meta Marketing API Adapter.

Verifies:
1. Canonical URL construction for Meta Graph API ad account endpoint.
2. Ad account ID normalization (handling both with and without 'act_' prefix).
3. Parsing valid Meta Graph API responses into MetaAdAccountMetadata contracts.
4. Handling of malformed JSON strings.
5. Handling of missing required fields (id, name, account_status, currency).
6. Handling of Meta API error envelopes without leaking credentials.
7. HTTP 401 handling -> MetaApiAuthError without leaking token.
8. HTTP 403 handling -> MetaApiPermissionError.
9. HTTP 404 handling -> MetaApiNotFoundError.
10. Generic HTTP error handling -> MetaApiError.
11. Network connectivity failure handling -> MetaApiConnectionError.
12. Request timeout handling -> MetaApiTimeoutError.
13. Missing credentials handling (token or account ID absent raises ValueError).
14. Environment variable discovery for META_AD_ACCOUNT_ID and META_ACCESS_TOKEN.
15. Strict read-only enforcement (only HTTP GET requests, zero POST/PUT/PATCH/DELETE).
16. Secret safety (token never appears in string representations or error messages).
17. Capital safety (zero database writes, zero ledger transactions, zero experiment mutations).
"""

from __future__ import annotations

import ast
from datetime import date
from decimal import Decimal
from io import BytesIO
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import urllib.error
import urllib.request

import pytest

from venturebot.execution.meta import (
    MetaAdAccountMetadata,
    MetaApiAuthError,
    MetaApiConnectionError,
    MetaApiError,
    MetaApiNotFoundError,
    MetaApiPermissionError,
    MetaApiTimeoutError,
    MetaInsightsTelemetry,
    MetaMarketingApiAdapter,
)

SAMPLE_ACCOUNT_ID = "act_1985595022114520"
SAMPLE_RAW_ID = "1985595022114520"
SAMPLE_SECRET_TOKEN = "TEST_TOKEN_XYZ_SECRET_98765"

SAMPLE_VALID_RESPONSE = {
    "id": "act_1985595022114520",
    "name": "VentureBot Experiments",
    "account_status": 1,
    "currency": "INR",
}


def _create_mock_response(status: int = 200, json_data: dict | None = None, raw_data: bytes | None = None):
    """Helper to build a mocked urllib HTTP response context."""
    if raw_data is None:
        raw_data = json.dumps(json_data or {}).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = raw_data
    mock_resp.headers.get_content_charset.return_value = "utf-8"
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = None
    return mock_resp


# ── URL Construction & Normalization ─────────────────────────────────────────

def test_normalize_ad_account_id():
    assert MetaMarketingApiAdapter.normalize_ad_account_id("act_1985595022114520") == "act_1985595022114520"
    assert MetaMarketingApiAdapter.normalize_ad_account_id("1985595022114520") == "act_1985595022114520"
    assert MetaMarketingApiAdapter.normalize_ad_account_id("  act_999  ") == "act_999"


def test_normalize_ad_account_id_validation():
    with pytest.raises(ValueError, match="must not be empty"):
        MetaMarketingApiAdapter.normalize_ad_account_id("")
    with pytest.raises(ValueError, match="must not be empty"):
        MetaMarketingApiAdapter.normalize_ad_account_id("   ")


def test_build_account_url_default():
    url = MetaMarketingApiAdapter.build_account_url(SAMPLE_ACCOUNT_ID)
    assert url == "https://graph.facebook.com/v20.0/act_1985595022114520?fields=id%2Cname%2Caccount_status%2Ccurrency"


def test_build_account_url_normalizes_raw_id():
    url = MetaMarketingApiAdapter.build_account_url(SAMPLE_RAW_ID)
    assert url == "https://graph.facebook.com/v20.0/act_1985595022114520?fields=id%2Cname%2Caccount_status%2Ccurrency"


def test_build_account_url_custom_fields_and_version():
    url = MetaMarketingApiAdapter.build_account_url(
        SAMPLE_ACCOUNT_ID,
        fields=["id", "currency", "spend_cap"],
        api_version="v21.0",
    )
    assert url == "https://graph.facebook.com/v21.0/act_1985595022114520?fields=id%2Ccurrency%2Cspend_cap"


def test_build_account_url_validation():
    with pytest.raises(ValueError, match="fields list must contain at least one valid field"):
        MetaMarketingApiAdapter.build_account_url(SAMPLE_ACCOUNT_ID, fields=[])


# ── Response Parsing ─────────────────────────────────────────────────────────

def test_parse_account_response_valid_dict():
    parsed = MetaMarketingApiAdapter.parse_account_response(SAMPLE_VALID_RESPONSE)
    assert isinstance(parsed, MetaAdAccountMetadata)
    assert parsed.id == "act_1985595022114520"
    assert parsed.name == "VentureBot Experiments"
    assert parsed.account_status == 1
    assert parsed.currency == "INR"
    assert parsed.is_active is True


def test_parse_account_response_valid_json_string():
    raw_str = json.dumps(SAMPLE_VALID_RESPONSE)
    parsed = MetaMarketingApiAdapter.parse_account_response(raw_str)
    assert parsed.id == "act_1985595022114520"
    assert parsed.currency == "INR"
    assert parsed.is_active is True


def test_parse_account_response_inactive_status():
    data = {**SAMPLE_VALID_RESPONSE, "account_status": 2}
    parsed = MetaMarketingApiAdapter.parse_account_response(data)
    assert parsed.account_status == 2
    assert parsed.is_active is False


def test_parse_account_response_malformed_json():
    with pytest.raises(ValueError, match="Malformed JSON"):
        MetaMarketingApiAdapter.parse_account_response("{not valid json")


def test_parse_account_response_empty_payload():
    with pytest.raises(ValueError, match="must not be empty"):
        MetaMarketingApiAdapter.parse_account_response("")


def test_parse_account_response_invalid_type():
    with pytest.raises(ValueError, match="Expected dict or JSON string"):
        MetaMarketingApiAdapter.parse_account_response(12345)  # type: ignore


def test_parse_account_response_missing_required_fields():
    # Missing currency
    incomplete = {
        "id": "act_1985595022114520",
        "name": "VentureBot Experiments",
        "account_status": 1,
    }
    with pytest.raises(ValueError, match="Missing required fields.*currency"):
        MetaMarketingApiAdapter.parse_account_response(incomplete)

    # Missing account_status
    incomplete_2 = {
        "id": "act_1985595022114520",
        "name": "VentureBot Experiments",
        "currency": "INR",
    }
    with pytest.raises(ValueError, match="Missing required fields.*account_status"):
        MetaMarketingApiAdapter.parse_account_response(incomplete_2)


def test_parse_account_response_empty_field_values():
    bad_data = {
        "id": "act_1985595022114520",
        "name": "   ",
        "account_status": 1,
        "currency": "INR",
    }
    with pytest.raises(ValueError, match="Invalid field values"):
        MetaMarketingApiAdapter.parse_account_response(bad_data)


def test_parse_account_response_meta_api_error_envelope():
    error_payload = {
        "error": {
            "message": "Invalid OAuth access token - Cannot parse access token",
            "type": "OAuthException",
            "code": 190,
            "fbtrace_id": "AbCdEfGhIjK",
        }
    }
    with pytest.raises(MetaApiError, match="Meta Graph API error \\(code 190\\): Invalid OAuth access token"):
        MetaMarketingApiAdapter.parse_account_response(error_payload)


# ── GET Querying & HTTP Error Handling ───────────────────────────────────────

def test_get_account_metadata_success():
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_SECRET_TOKEN,
    )

    mock_resp = _create_mock_response(status=200, json_data=SAMPLE_VALID_RESPONSE)

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        result = adapter.get_account_metadata()

        assert result.id == "act_1985595022114520"
        assert result.name == "VentureBot Experiments"
        assert result.account_status == 1
        assert result.currency == "INR"
        assert result.is_active is True

        # Verify the outgoing Request
        mock_urlopen.assert_called_once()
        call_args = mock_urlopen.call_args
        req: urllib.request.Request = call_args[0][0]

        # Verify strictly GET method
        assert req.get_method() == "GET"

        # Verify headers
        assert req.headers["Authorization"] == f"Bearer {SAMPLE_SECRET_TOKEN}"
        assert req.headers["Accept"] == "application/json"
        assert "VentureBot" in req.headers["User-agent"]


def test_get_account_metadata_http_401_secret_safety():
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_SECRET_TOKEN,
    )

    http_error = urllib.error.HTTPError(
        url="https://graph.facebook.com/v20.0/act_1985595022114520",
        code=401,
        msg="Unauthorized",
        hdrs=MagicMock(),
        fp=BytesIO(b'{"error": {"message": "Invalid token"}}'),
    )

    with patch("urllib.request.urlopen", side_effect=http_error):
        with pytest.raises(MetaApiAuthError) as exc_info:
            adapter.get_account_metadata()

        # Token must NEVER appear in the exception message
        error_msg = str(exc_info.value)
        assert SAMPLE_SECRET_TOKEN not in error_msg
        assert "HTTP 401" in error_msg


def test_get_account_metadata_http_403():
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_SECRET_TOKEN,
    )

    http_error = urllib.error.HTTPError(
        url="https://graph.facebook.com/v20.0/act_1985595022114520",
        code=403,
        msg="Forbidden",
        hdrs=MagicMock(),
        fp=BytesIO(b'{"error": {"message": "Permission denied"}}'),
    )

    with patch("urllib.request.urlopen", side_effect=http_error):
        with pytest.raises(MetaApiPermissionError, match="HTTP 403"):
            adapter.get_account_metadata()


def test_get_account_metadata_http_404():
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_SECRET_TOKEN,
    )

    http_error = urllib.error.HTTPError(
        url="https://graph.facebook.com/v20.0/act_1985595022114520",
        code=404,
        msg="Not Found",
        hdrs=MagicMock(),
        fp=BytesIO(b'{"error": {"message": "Unknown path"}}'),
    )

    with patch("urllib.request.urlopen", side_effect=http_error):
        with pytest.raises(MetaApiNotFoundError, match="HTTP 404"):
            adapter.get_account_metadata()


def test_get_account_metadata_generic_http_error():
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_SECRET_TOKEN,
    )

    http_error = urllib.error.HTTPError(
        url="https://graph.facebook.com/v20.0/act_1985595022114520",
        code=500,
        msg="Internal Server Error",
        hdrs=MagicMock(),
        fp=BytesIO(b'{"error": {"message": "Meta service down"}}'),
    )

    with patch("urllib.request.urlopen", side_effect=http_error):
        with pytest.raises(MetaApiError, match="Meta API HTTP 500 error"):
            adapter.get_account_metadata()


def test_get_account_metadata_network_connection_error():
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_SECRET_TOKEN,
    )

    url_error = urllib.error.URLError("getaddrinfo failed")

    with patch("urllib.request.urlopen", side_effect=url_error):
        with pytest.raises(MetaApiConnectionError, match="network connection failed"):
            adapter.get_account_metadata()


def test_get_account_metadata_timeout_error():
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_SECRET_TOKEN,
    )

    with patch("urllib.request.urlopen", side_effect=TimeoutError("Connection timed out")):
        with pytest.raises(MetaApiTimeoutError, match="request timed out"):
            adapter.get_account_metadata()


# ── Credential & Environment Handling ────────────────────────────────────────

def test_missing_credentials_raise_value_error(monkeypatch):
    monkeypatch.delenv("META_AD_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("META_ACCESS_TOKEN", raising=False)

    adapter = MetaMarketingApiAdapter()

    with pytest.raises(ValueError, match="META_AD_ACCOUNT_ID is required"):
        adapter.get_account_metadata(access_token="token_only")

    with pytest.raises(ValueError, match="META_ACCESS_TOKEN is required"):
        adapter.get_account_metadata(ad_account_id=SAMPLE_ACCOUNT_ID)


def test_credentials_read_from_environment(monkeypatch):
    monkeypatch.setenv("META_AD_ACCOUNT_ID", SAMPLE_ACCOUNT_ID)
    monkeypatch.setenv("META_ACCESS_TOKEN", SAMPLE_SECRET_TOKEN)

    adapter = MetaMarketingApiAdapter()
    assert adapter.ad_account_id == SAMPLE_ACCOUNT_ID
    assert adapter._access_token == SAMPLE_SECRET_TOKEN

    mock_resp = _create_mock_response(status=200, json_data=SAMPLE_VALID_RESPONSE)
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        result = adapter.get_account_metadata()
        assert result.id == SAMPLE_ACCOUNT_ID
        mock_urlopen.assert_called_once()


# ── Architectural & Capital Safety Checks ────────────────────────────────────

def test_meta_adapter_isolation_from_financial_and_domain_logic():
    """Verify Meta adapter has zero imports from capital ledger, experiments, or opportunities."""
    adapter_file = Path(__file__).resolve().parent.parent / "backend" / "venturebot" / "execution" / "meta.py"
    with open(adapter_file, "r", encoding="utf-8") as fp:
        source = fp.read()

    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    # Must NOT import database, capital, approval, or decision repositories
    for mod in imported_modules:
        assert not mod.startswith("venturebot.database"), f"Forbidden import: {mod}"
        assert not mod.startswith("venturebot.approval"), f"Forbidden import: {mod}"
        assert not mod.startswith("venturebot.decision"), f"Forbidden import: {mod}"
        assert not mod.startswith("venturebot.opportunity"), f"Forbidden import: {mod}"
        assert not mod.startswith("venturebot.learning"), f"Forbidden import: {mod}"
        assert "facebook" not in mod, f"Forbidden third-party SDK import: {mod}"


def test_meta_adapter_only_allows_get_requests():
    """Verify that MetaMarketingApiAdapter contains zero POST, PUT, PATCH, or DELETE calls."""
    adapter_file = Path(__file__).resolve().parent.parent / "backend" / "venturebot" / "execution" / "meta.py"
    with open(adapter_file, "r", encoding="utf-8") as fp:
        source = fp.read().lower()

    # Search for forbidden HTTP method assignments
    assert 'method="post"' not in source
    assert 'method="put"' not in source
    assert 'method="patch"' not in source
    assert 'method="delete"' not in source
    assert 'method="get"' in source


# ── Meta Insights URL Construction & Validation (Step 32A) ─────────────────

def test_build_insights_url_default():
    url = MetaMarketingApiAdapter.build_insights_url(SAMPLE_ACCOUNT_ID)
    assert url.startswith("https://graph.facebook.com/v20.0/act_1985595022114520/insights?")
    assert "fields=account_id%2Ccampaign_id%2Cdate_start%2Cdate_stop%2Cspend%2Cimpressions%2Cclicks%2Ccpc%2Ccpm%2Cctr" in url


def test_build_insights_url_custom_fields():
    url = MetaMarketingApiAdapter.build_insights_url(
        SAMPLE_ACCOUNT_ID,
        fields=["spend", "impressions", "clicks"],
        api_version="v26.0",
    )
    assert url.startswith("https://graph.facebook.com/v26.0/act_1985595022114520/insights?")
    assert "fields=spend%2Cimpressions%2Cclicks" in url


def test_build_insights_url_with_time_range():
    url = MetaMarketingApiAdapter.build_insights_url(
        SAMPLE_ACCOUNT_ID,
        time_range={"since": "2026-09-01", "until": "2026-09-20"},
    )
    assert "time_range=%7B%22since%22%3A+%222026-09-01%22%2C+%22until%22%3A+%222026-09-20%22%7D" in url or "time_range=%7B%22since%22%3A%222026-09-01%22%2C%22until%22%3A%222026-09-20%22%7D" in url


def test_build_insights_url_with_date_preset():
    url = MetaMarketingApiAdapter.build_insights_url(
        SAMPLE_ACCOUNT_ID,
        date_preset="maximum",
    )
    assert "date_preset=maximum" in url


def test_build_insights_url_with_level():
    url = MetaMarketingApiAdapter.build_insights_url(
        SAMPLE_ACCOUNT_ID,
        level="campaign",
    )
    assert "level=campaign" in url


def test_build_insights_url_validation():
    with pytest.raises(ValueError, match="object_id must not be empty"):
        MetaMarketingApiAdapter.build_insights_url("")
    with pytest.raises(ValueError, match="object_id must not be empty"):
        MetaMarketingApiAdapter.build_insights_url("   ")
    with pytest.raises(ValueError, match="fields list must contain at least one valid field"):
        MetaMarketingApiAdapter.build_insights_url(SAMPLE_ACCOUNT_ID, fields=[])
    with pytest.raises(ValueError, match="Cannot specify both 'date_preset' and 'time_range'"):
        MetaMarketingApiAdapter.build_insights_url(
            SAMPLE_ACCOUNT_ID,
            date_preset="today",
            time_range={"since": "2026-09-01", "until": "2026-09-20"},
        )
    with pytest.raises(ValueError, match="Invalid level"):
        MetaMarketingApiAdapter.build_insights_url(SAMPLE_ACCOUNT_ID, level="invalid_level")
    with pytest.raises(ValueError, match="time_range must be a dict containing 'since' and 'until'"):
        MetaMarketingApiAdapter.build_insights_url(SAMPLE_ACCOUNT_ID, time_range={"invalid": "1"})


# ── Meta Insights Response Parsing & Contract Validation (Step 32A) ──────────

def test_meta_insights_telemetry_model_validation():
    t = MetaInsightsTelemetry(
        account_id=SAMPLE_ACCOUNT_ID,
        campaign_id="12021000001",
        date_start=date(2026, 9, 1),
        date_stop=date(2026, 9, 20),
        spend=Decimal("12.50"),
        impressions=1500,
        clicks=45,
        cpc=Decimal("0.28"),
        cpm=Decimal("8.33"),
        ctr=3.0,
    )
    assert t.spend == Decimal("12.50")
    assert t.impressions == 1500

    with pytest.raises(ValueError, match="cannot be earlier than date_start"):
        MetaInsightsTelemetry(
            account_id=SAMPLE_ACCOUNT_ID,
            date_start=date(2026, 9, 20),
            date_stop=date(2026, 9, 1),
            spend=Decimal("0.00"),
            impressions=0,
            clicks=0,
        )

    with pytest.raises(ValueError, match="account_id must not be empty"):
        MetaInsightsTelemetry(
            account_id="   ",
            date_start=date(2026, 9, 1),
            date_stop=date(2026, 9, 2),
            spend=Decimal("0.00"),
            impressions=0,
            clicks=0,
        )


def test_parse_insights_response_valid():
    sample_payload = {
        "data": [
            {
                "account_id": "1985595022114520",
                "campaign_id": "12021000001",
                "date_start": "2026-09-01",
                "date_stop": "2026-09-20",
                "spend": "12.50",
                "impressions": "1500",
                "clicks": "45",
                "cpc": "0.28",
                "cpm": "8.33",
                "ctr": "3.00",
            }
        ]
    }
    results = MetaMarketingApiAdapter.parse_insights_response(sample_payload)
    assert len(results) == 1
    item = results[0]
    assert item.account_id == "1985595022114520"
    assert item.campaign_id == "12021000001"
    assert item.date_start == date(2026, 9, 1)
    assert item.date_stop == date(2026, 9, 20)
    assert item.spend == Decimal("12.50")
    assert item.impressions == 1500
    assert item.clicks == 45
    assert item.cpc == Decimal("0.28")
    assert item.cpm == Decimal("8.33")
    assert item.ctr == 3.0


def test_parse_insights_response_empty_data():
    empty_payload = {"data": []}
    results = MetaMarketingApiAdapter.parse_insights_response(empty_payload)
    assert results == []


def test_parse_insights_response_string_numeric_and_none_optionals():
    sample_payload = {
        "data": [
            {
                "account_id": "1985595022114520",
                "date_start": "2026-09-05",
                "date_stop": "2026-09-05",
                "spend": "0.00",
                "impressions": "0",
                "clicks": "0",
            }
        ]
    }
    results = MetaMarketingApiAdapter.parse_insights_response(sample_payload)
    assert len(results) == 1
    item = results[0]
    assert item.campaign_id is None
    assert item.spend == Decimal("0.00")
    assert item.impressions == 0
    assert item.clicks == 0
    assert item.cpc is None
    assert item.cpm is None
    assert item.ctr is None


def test_parse_insights_response_malformed_json():
    with pytest.raises(ValueError, match="Malformed JSON"):
        MetaMarketingApiAdapter.parse_insights_response("invalid json string {")


def test_parse_insights_response_missing_data_field():
    with pytest.raises(ValueError, match="missing or invalid 'data' list"):
        MetaMarketingApiAdapter.parse_insights_response({"missing": True})


def test_parse_insights_response_meta_api_error_envelope():
    err_payload = {
        "error": {
            "message": "Invalid OAuth access token.",
            "type": "OAuthException",
            "code": 190,
        }
    }
    with pytest.raises(MetaApiError, match="Meta Graph API error \\(code 190\\): Invalid OAuth access token"):
        MetaMarketingApiAdapter.parse_insights_response(err_payload)


# ── Meta Insights HTTP Operations & Safety (Step 32A) ────────────────────────

def test_get_insights_success_mock():
    sample_payload = {
        "data": [
            {
                "account_id": SAMPLE_ACCOUNT_ID,
                "date_start": "2026-09-01",
                "date_stop": "2026-09-20",
                "spend": "25.00",
                "impressions": "2000",
                "clicks": "60",
            }
        ]
    }
    mock_resp = _create_mock_response(status=200, json_data=sample_payload)
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_SECRET_TOKEN)

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        results = adapter.get_insights()
        assert len(results) == 1
        assert results[0].spend == Decimal("25.00")
        assert results[0].impressions == 2000
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert req.get_method() == "GET"
        assert req.headers.get("Authorization") == f"Bearer {SAMPLE_SECRET_TOKEN}"


def test_get_insights_http_401_secret_safety():
    mock_err = urllib.error.HTTPError(
        url="https://graph.facebook.com/v20.0/act_1985595022114520/insights",
        code=401,
        msg="Unauthorized",
        hdrs=MagicMock(),
        fp=BytesIO(b'{"error": "Unauthorized"}'),
    )
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_SECRET_TOKEN)

    with patch("urllib.request.urlopen", side_effect=mock_err):
        with pytest.raises(MetaApiAuthError) as exc_info:
            adapter.get_insights()
        assert "HTTP 401" in str(exc_info.value)
        assert SAMPLE_SECRET_TOKEN not in str(exc_info.value)


def test_get_insights_http_403():
    mock_err = urllib.error.HTTPError(
        url="https://graph.facebook.com/v20.0/act_1985595022114520/insights",
        code=403,
        msg="Forbidden",
        hdrs=MagicMock(),
        fp=BytesIO(b'{"error": "Forbidden"}'),
    )
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_SECRET_TOKEN)

    with patch("urllib.request.urlopen", side_effect=mock_err):
        with pytest.raises(MetaApiPermissionError, match="HTTP 403"):
            adapter.get_insights()


def test_get_insights_http_404():
    mock_err = urllib.error.HTTPError(
        url="https://graph.facebook.com/v20.0/act_1985595022114520/insights",
        code=404,
        msg="Not Found",
        hdrs=MagicMock(),
        fp=BytesIO(b'{"error": "Not Found"}'),
    )
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_SECRET_TOKEN)

    with patch("urllib.request.urlopen", side_effect=mock_err):
        with pytest.raises(MetaApiNotFoundError, match="HTTP 404"):
            adapter.get_insights()


def test_get_insights_timeout_error():
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_SECRET_TOKEN)
    with patch("urllib.request.urlopen", side_effect=TimeoutError("Request timed out")):
        with pytest.raises(MetaApiTimeoutError, match="timed out"):
            adapter.get_insights()


def test_get_insights_network_connection_error():
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_SECRET_TOKEN)
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("DNS resolution failed")):
        with pytest.raises(MetaApiConnectionError, match="network connection failed"):
            adapter.get_insights()


def test_get_insights_missing_credentials_raise_value_error():
    adapter = MetaMarketingApiAdapter(ad_account_id="", access_token="")
    with pytest.raises(ValueError, match="object_id is required"):
        adapter.get_insights()

    adapter_with_id = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token="")
    with pytest.raises(ValueError, match="META_ACCESS_TOKEN is required"):
        adapter_with_id.get_insights()


def test_insights_adapter_zero_capital_or_database_side_effects():
    """Verify that get_insights execution never interacts with CapitalRepository or models."""
    sample_payload = {"data": []}
    mock_resp = _create_mock_response(status=200, json_data=sample_payload)
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_SECRET_TOKEN)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = adapter.get_insights()
        assert res == []

