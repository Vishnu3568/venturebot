"""Meta Marketing API adapter and execution contract for VentureBot (Steps 28, 32A, & 36).

Provides:
- Read-only querying and validation of verified Meta Ad Account metadata.
- Read-only external Insights telemetry via Meta Graph API.
- Transient MetaExecutionSpecification contract for controlled experiment deployment.
- Paused creation payload builders and deterministic duplicate checking.
- Mockable write-operation interfaces for campaign, ad set, creative, ad, and emergency stop.

SAFETY INVARIANTS:
- Does NOT execute autonomous actions or disburse real money.
- Does NOT mutate the capital ledger, experiments, opportunities, or database.
- Live write operations (POST) are strictly disabled unless an explicit mock transport is provided.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
import json
import os
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


class MetaApiError(RuntimeError):
    """Base exception for Meta Marketing API failures."""
    pass


class MetaApiAuthError(MetaApiError):
    """Raised when authentication fails (HTTP 401 / invalid token)."""
    pass


class MetaApiPermissionError(MetaApiError):
    """Raised when permission is denied (HTTP 403 / missing ads_read/ads_management)."""
    pass


class MetaApiNotFoundError(MetaApiError):
    """Raised when the requested Meta entity is not found (HTTP 404)."""
    pass


class MetaApiConnectionError(MetaApiError):
    """Raised when network connectivity fails."""
    pass


class MetaApiTimeoutError(MetaApiError):
    """Raised when a request to Meta Graph API times out."""
    pass


class ReconciliationStatus(str, Enum):
    """Status outcomes for remote resource reconciliation lookups."""

    NOT_FOUND = "NOT_FOUND"
    FOUND_EXACT = "FOUND_EXACT"
    AMBIGUOUS = "AMBIGUOUS"
    UNKNOWN = "UNKNOWN"


class LookupResult(BaseModel):
    """Typed result of an external resource lookup/reconciliation query."""

    status: ReconciliationStatus
    resource_id: str | None = None
    details: dict[str, Any] | None = None
    error_message: str | None = None


def inr_to_paise(amount: Decimal) -> int:
    """Convert an INR amount in Decimal to an exact integer number of paise.

    Requirements:
    - Pure Decimal arithmetic (zero float conversions).
    - Rejects non-Decimal types.
    - Rejects negative values.
    - Rejects values with fractional paise (more than 2 decimal places).

    Args:
        amount: Amount in INR as a Decimal (e.g. Decimal("100.00")).

    Returns:
        int: Equivalent amount in paise (e.g. 10000).
    """
    if not isinstance(amount, Decimal):
        raise TypeError(f"amount must be a Decimal, got {type(amount).__name__}.")
    if amount < Decimal("0.00"):
        raise ValueError(f"amount cannot be negative: {amount}.")
    paise_decimal = amount * Decimal("100")
    if paise_decimal != paise_decimal.to_integral_value():
        raise ValueError(f"amount '{amount}' has fractional paise (exceeds 2 decimal places).")
    return int(paise_decimal)


def deterministic_campaign_name(experiment_id: UUID) -> str:
    """Generate deterministic Meta campaign name from canonical Experiment ID.

    Args:
        experiment_id: Canonical Experiment UUID.

    Returns:
        str: Deterministic campaign name formatted as 'VB-EXP-<experiment_id>'.
    """
    if not isinstance(experiment_id, UUID):
        raise TypeError(f"experiment_id must be a UUID, got {type(experiment_id).__name__}.")
    return f"VB-EXP-{experiment_id}"


def deterministic_adset_name(experiment_id: UUID) -> str:
    """Generate deterministic Meta Ad Set name from canonical Experiment ID.

    Args:
        experiment_id: Canonical Experiment UUID.

    Returns:
        str: Deterministic ad set name formatted as 'VB-EXP-<experiment_id>-ADSET'.
    """
    if not isinstance(experiment_id, UUID):
        raise TypeError(f"experiment_id must be a UUID, got {type(experiment_id).__name__}.")
    return f"VB-EXP-{experiment_id}-ADSET"


def deterministic_creative_name(experiment_id: UUID) -> str:
    """Generate deterministic Meta Creative name from canonical Experiment ID.

    Args:
        experiment_id: Canonical Experiment UUID.

    Returns:
        str: Deterministic creative name formatted as 'VB-EXP-<experiment_id>-CREATIVE'.
    """
    if not isinstance(experiment_id, UUID):
        raise TypeError(f"experiment_id must be a UUID, got {type(experiment_id).__name__}.")
    return f"VB-EXP-{experiment_id}-CREATIVE"


def deterministic_ad_name(experiment_id: UUID) -> str:
    """Generate deterministic Meta Ad name from canonical Experiment ID.

    Args:
        experiment_id: Canonical Experiment UUID.

    Returns:
        str: Deterministic ad name formatted as 'VB-EXP-<experiment_id>-AD'.
    """
    if not isinstance(experiment_id, UUID):
        raise TypeError(f"experiment_id must be a UUID, got {type(experiment_id).__name__}.")
    return f"VB-EXP-{experiment_id}-AD"


class MetaAdAccountMetadata(BaseModel):
    """Read-only data contract representing verified Meta Ad Account metadata."""

    id: str = Field(description="Canonical Meta Ad Account ID (e.g. 'act_1985595022114520')")
    name: str = Field(description="Ad Account display name")
    account_status: int = Field(description="Meta account status integer (1 = ACTIVE)")
    currency: str = Field(description="ISO 4217 currency code (e.g. 'INR')")

    @field_validator("id", "name", "currency")
    @classmethod
    def must_not_be_empty(cls, v: str) -> str:
        clean = v.strip() if v else ""
        if not clean:
            raise ValueError("Field must not be empty or whitespace.")
        return clean

    @property
    def is_active(self) -> bool:
        """Convenience property: account_status == 1 indicates an active account on Meta."""
        return self.account_status == 1


class MetaInsightsTelemetry(BaseModel):
    """Transient read-only data contract representing external Meta Insights telemetry (Step 32A).

    Observational FACT data from Meta Graph API.
    Does NOT write to the capital ledger or database.
    Does NOT calculate derived business conclusions or vanity scores.
    """

    account_id: str = Field(description="Canonical Meta Ad Account ID")
    campaign_id: str | None = Field(default=None, description="Meta Campaign ID if queried at campaign level")
    date_start: date = Field(description="Reporting observation window start date")
    date_stop: date = Field(description="Reporting observation window stop date")
    spend: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0.00"), description="Total spend reported by Meta in account currency")
    impressions: int = Field(default=0, ge=0, description="Total impressions delivered")
    clicks: int = Field(default=0, ge=0, description="Total link clicks recorded")
    cpc: Decimal | None = Field(default=None, ge=Decimal("0.00"), description="Cost per click if provided")
    cpm: Decimal | None = Field(default=None, ge=Decimal("0.00"), description="Cost per 1,000 impressions if provided")
    ctr: float | None = Field(default=None, ge=0.0, description="Click-through rate if provided")

    @field_validator("account_id")
    @classmethod
    def account_id_must_not_be_empty(cls, v: str) -> str:
        clean = v.strip() if v else ""
        if not clean:
            raise ValueError("account_id must not be empty or whitespace.")
        return clean

    @field_validator("campaign_id")
    @classmethod
    def clean_campaign_id(cls, v: str | None) -> str | None:
        if v is None:
            return None
        clean = v.strip()
        return clean if clean else None

    @model_validator(mode="after")
    def validate_date_range(self) -> "MetaInsightsTelemetry":
        if self.date_stop < self.date_start:
            raise ValueError(
                f"date_stop ({self.date_stop}) cannot be earlier than date_start ({self.date_start})."
            )
        return self


class MetaExecutionSpecification(BaseModel):
    """Transient data contract specifying a single controlled Meta experiment deployment (Step 36).

    Defines the parameters required to construct Meta campaign, ad set, creative, and ad.
    Does NOT mutate the capital ledger or database.
    Does NOT initiate autonomous execution or disburse real money.
    """

    experiment_id: UUID = Field(description="Canonical Experiment UUID")
    ad_account_id: str = Field(description="Target Meta Ad Account ID (e.g. 'act_1985595022114520')")
    page_id: str = Field(description="Facebook Page ID representing the ad publisher")
    destination_url: str = Field(description="Destination URL for the link ad")
    primary_text: str = Field(description="Primary text / body copy of the ad")
    headline: str = Field(description="Headline / title of the ad")
    image_asset_path: str | None = Field(default=None, description="Local path to creative image asset")
    image_hash: str | None = Field(default=None, description="Pre-uploaded Meta image hash if available")
    call_to_action: str = Field(default="LEARN_MORE", description="Call to action type enum")
    campaign_objective: str = Field(default="OUTCOME_TRAFFIC", description="ODAX campaign objective")
    countries: list[str] = Field(default_factory=lambda: ["IN"], description="List of targeted 2-letter ISO country codes")
    age_min: int = Field(default=18, ge=13, le=65, description="Targeting minimum age (13-65)")
    age_max: int = Field(default=65, ge=13, le=65, description="Targeting maximum age (13-65)")
    authorized_budget: Decimal = Field(gt=Decimal("0.00"), description="Approved budget in INR for this execution")
    start_time: datetime | None = Field(default=None, description="Scheduled start time with timezone")
    end_time: datetime | None = Field(default=None, description="Scheduled end time with timezone (required for lifetime budget)")
    special_ad_categories: list[str] = Field(default_factory=lambda: ["NONE"], description="Mandatory special ad categories array")
    status: str = Field(default="PAUSED", description="Creation status (must be PAUSED)")
    explicit_dispatch_authorized: bool = Field(default=False, description="Explicit operator authorization flag")

    @field_validator("ad_account_id")
    @classmethod
    def validate_ad_account_id(cls, v: str) -> str:
        clean = v.strip() if v else ""
        if not clean:
            raise ValueError("ad_account_id must not be empty or whitespace.")
        if clean.startswith("act_"):
            return clean
        return f"act_{clean}"

    @field_validator("page_id", "primary_text", "headline")
    @classmethod
    def validate_non_empty_strings(cls, v: str) -> str:
        clean = v.strip() if v else ""
        if not clean:
            raise ValueError("Field must not be empty or whitespace.")
        return clean

    @field_validator("destination_url")
    @classmethod
    def validate_destination_url(cls, v: str) -> str:
        clean = v.strip() if v else ""
        if not clean:
            raise ValueError("destination_url must not be empty or whitespace.")
        if not (clean.startswith("http://") or clean.startswith("https://")):
            raise ValueError("destination_url must start with 'http://' or 'https://'.")
        return clean

    @field_validator("call_to_action")
    @classmethod
    def validate_call_to_action(cls, v: str) -> str:
        clean = v.strip().upper() if v else ""
        if not clean:
            raise ValueError("call_to_action must not be empty.")
        return clean

    @field_validator("campaign_objective")
    @classmethod
    def validate_objective(cls, v: str) -> str:
        clean = v.strip().upper() if v else ""
        if not clean:
            raise ValueError("campaign_objective must not be empty.")
        return clean

    @field_validator("countries")
    @classmethod
    def validate_countries(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("countries list must contain at least one country code.")
        clean_codes: list[str] = []
        for c in v:
            code = c.strip().upper()
            if len(code) != 2 or not code.isalpha():
                raise ValueError(f"Invalid country code '{c}'. Must be 2-letter ISO code.")
            clean_codes.append(code)
        return clean_codes

    @field_validator("authorized_budget")
    @classmethod
    def validate_authorized_budget(cls, v: Decimal) -> Decimal:
        if v <= Decimal("0.00"):
            raise ValueError(f"authorized_budget must be positive: {v}")
        paise = v * Decimal("100")
        if paise != paise.to_integral_value():
            raise ValueError(f"authorized_budget '{v}' has fractional paise (exceeds 2 decimal places).")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        clean = v.strip().upper() if v else ""
        if clean != "PAUSED":
            raise ValueError("Status must strictly be 'PAUSED' for creation safety.")
        return clean

    @field_validator("special_ad_categories")
    @classmethod
    def validate_special_ad_categories(cls, v: list[str]) -> list[str]:
        if not v:
            return ["NONE"]
        clean = [cat.strip().upper() for cat in v if cat.strip()]
        if not clean:
            return ["NONE"]
        return clean

    @model_validator(mode="after")
    def validate_specification_ranges(self) -> "MetaExecutionSpecification":
        if self.age_max < self.age_min:
            raise ValueError(f"age_max ({self.age_max}) cannot be less than age_min ({self.age_min}).")
        if self.start_time and self.end_time and self.end_time <= self.start_time:
            raise ValueError(f"end_time ({self.end_time}) must be after start_time ({self.start_time}).")
        return self

    def validate_pre_dispatch(
        self,
        allocated_budget: Decimal,
        max_allowed_spend: Decimal,
        actual_spend: Decimal = Decimal("0.00"),
    ) -> None:
        """Validate that the specification satisfies pre-dispatch budget and authorization safeguards."""
        if not self.explicit_dispatch_authorized:
            raise ValueError("Explicit operator dispatch authorization is required before dispatch.")
        if self.end_time is None:
            raise ValueError("end_time is mandatory when deploying with lifetime_budget.")
        if self.image_asset_path is None and self.image_hash is None:
            raise ValueError("Either image_asset_path or image_hash must be provided.")

        remaining_allocated = allocated_budget - actual_spend
        if self.authorized_budget > remaining_allocated:
            raise ValueError(
                f"authorized_budget ({self.authorized_budget}) exceeds remaining allocated budget "
                f"({remaining_allocated} = allocated {allocated_budget} - spend {actual_spend})"
            )
        remaining_ceiling = max_allowed_spend - actual_spend
        if self.authorized_budget > remaining_ceiling:
            raise ValueError(
                f"authorized_budget ({self.authorized_budget}) exceeds remaining max allowed spend "
                f"({remaining_ceiling} = ceiling {max_allowed_spend} - spend {actual_spend})"
            )


class MetaMarketingApiAdapter:
    """Adapter for Meta Marketing API account verification, insights telemetry, and execution."""

    DEFAULT_GRAPH_API_VERSION = "v20.0"
    GRAPH_API_BASE_URL = "https://graph.facebook.com"
    DEFAULT_TIMEOUT = 10.0
    DEFAULT_FIELDS = ["id", "name", "account_status", "currency"]
    DEFAULT_INSIGHTS_FIELDS = [
        "account_id",
        "campaign_id",
        "date_start",
        "date_stop",
        "spend",
        "impressions",
        "clicks",
        "cpc",
        "cpm",
        "ctr",
    ]
    DEFAULT_USER_AGENT = "VentureBot/0.0.1 (Meta Marketing API Adapter; controlled execution foundation)"

    def __init__(
        self,
        ad_account_id: str | None = None,
        access_token: str | None = None,
        api_version: str = DEFAULT_GRAPH_API_VERSION,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Callable[[urllib.request.Request, float], bytes] | None = None,
    ):
        self.ad_account_id = ad_account_id or os.getenv("META_AD_ACCOUNT_ID")
        self._access_token = access_token or os.getenv("META_ACCESS_TOKEN")
        self.api_version = api_version.strip() if api_version else self.DEFAULT_GRAPH_API_VERSION
        self.timeout = timeout
        self._transport = transport

    @classmethod
    def normalize_ad_account_id(cls, ad_account_id: str) -> str:
        """Normalize ad account ID to ensure canonical 'act_' prefix."""
        clean = ad_account_id.strip() if ad_account_id else ""
        if not clean:
            raise ValueError("ad_account_id must not be empty or whitespace.")
        if clean.startswith("act_"):
            return clean
        return f"act_{clean}"

    @staticmethod
    def _parse_json_dict(payload: dict[str, Any] | str) -> dict[str, Any]:
        """Parse dict or JSON string into Python dict with malformed checking."""
        if isinstance(payload, str):
            clean_str = payload.strip()
            if not clean_str:
                raise ValueError("Payload string must not be empty.")
            try:
                data = json.loads(clean_str)
            except (json.JSONDecodeError, ValueError) as err:
                raise ValueError(f"Malformed JSON in Meta API response: {err}") from err
        elif isinstance(payload, dict):
            data = payload
        else:
            raise ValueError(f"Expected dict or JSON string payload, got {type(payload).__name__}.")
        return data

    @classmethod
    def _handle_meta_error_envelope(cls, err_dict: dict[str, Any]) -> None:
        """Raise appropriate Meta API error from error dict."""
        err_msg = err_dict.get("message", "Unknown Meta API error")
        err_code = err_dict.get("code")
        if err_code == 190:
            raise MetaApiAuthError(f"Meta Graph API error (code 190): {err_msg}")
        elif err_code == 200:
            raise MetaApiPermissionError(f"Meta Graph API error (code 200): {err_msg}")
        elif err_code == 404:
            raise MetaApiNotFoundError(f"Meta Graph API error (code 404): {err_msg}")
        raise MetaApiError(f"Meta Graph API error (code {err_code}): {err_msg}")

    # ── URL Builders ─────────────────────────────────────────────────────────

    @classmethod
    def build_account_url(
        cls,
        ad_account_id: str,
        fields: list[str] | None = None,
        api_version: str = DEFAULT_GRAPH_API_VERSION,
    ) -> str:
        """Construct canonical Meta Graph API endpoint URL for ad account metadata."""
        canonical_id = cls.normalize_ad_account_id(ad_account_id)
        ver = api_version.strip() if api_version else cls.DEFAULT_GRAPH_API_VERSION
        req_fields = fields if fields is not None else cls.DEFAULT_FIELDS
        clean_fields = ",".join(f.strip() for f in req_fields if f and f.strip())
        if not clean_fields:
            raise ValueError("fields list must contain at least one valid field.")
        query = urllib.parse.urlencode({"fields": clean_fields})
        return f"{cls.GRAPH_API_BASE_URL}/{ver}/{canonical_id}?{query}"

    @classmethod
    def build_insights_url(
        cls,
        object_id: str,
        fields: list[str] | None = None,
        date_preset: str | None = None,
        time_range: dict[str, str] | None = None,
        level: str | None = None,
        api_version: str = DEFAULT_GRAPH_API_VERSION,
    ) -> str:
        """Construct canonical Meta Graph API endpoint URL for object insights telemetry."""
        clean_id = object_id.strip() if object_id else ""
        if not clean_id:
            raise ValueError("object_id must not be empty or whitespace.")

        req_fields = fields if fields is not None else cls.DEFAULT_INSIGHTS_FIELDS
        clean_fields = [f.strip() for f in req_fields if f and f.strip()]
        if not clean_fields:
            raise ValueError("fields list must contain at least one valid field.")

        if date_preset and time_range:
            raise ValueError("Cannot specify both 'date_preset' and 'time_range' simultaneously.")

        params: dict[str, str] = {
            "fields": ",".join(clean_fields),
        }

        if date_preset:
            clean_preset = date_preset.strip()
            if not clean_preset:
                raise ValueError("date_preset must not be empty or whitespace when specified.")
            params["date_preset"] = clean_preset
        elif time_range:
            if not isinstance(time_range, dict) or "since" not in time_range or "until" not in time_range:
                raise ValueError("time_range must be a dict containing 'since' and 'until' keys.")
            params["time_range"] = json.dumps({
                "since": str(time_range["since"]).strip(),
                "until": str(time_range["until"]).strip(),
            })

        if level:
            clean_level = level.strip().lower()
            allowed_levels = ("account", "campaign", "adset", "ad")
            if clean_level not in allowed_levels:
                raise ValueError(f"Invalid level '{level}'. Must be one of {allowed_levels}.")
            params["level"] = clean_level

        ver = api_version.strip() if api_version else cls.DEFAULT_GRAPH_API_VERSION
        query = urllib.parse.urlencode(params)
        return f"{cls.GRAPH_API_BASE_URL}/{ver}/{clean_id}/insights?{query}"

    @classmethod
    def build_campaign_duplicate_check_url(
        cls,
        ad_account_id: str,
        campaign_name: str,
        api_version: str = DEFAULT_GRAPH_API_VERSION,
    ) -> str:
        """Construct URL for duplicate-checking an existing campaign by name."""
        canonical_id = cls.normalize_ad_account_id(ad_account_id)
        clean_name = campaign_name.strip() if campaign_name else ""
        if not clean_name:
            raise ValueError("campaign_name must not be empty.")
        ver = api_version.strip() if api_version else cls.DEFAULT_GRAPH_API_VERSION
        filtering_json = json.dumps([{"field": "name", "operator": "EQUAL", "value": clean_name}])
        query = urllib.parse.urlencode({
            "fields": "id,name,status",
            "filtering": filtering_json,
        })
        return f"{cls.GRAPH_API_BASE_URL}/{ver}/{canonical_id}/campaigns?{query}"

    @classmethod
    def build_campaign_lookup_url(
        cls,
        ad_account_id: str,
        campaign_name: str,
        api_version: str = DEFAULT_GRAPH_API_VERSION,
    ) -> str:
        """Construct URL for looking up an existing campaign by name."""
        return cls.build_campaign_duplicate_check_url(
            ad_account_id=ad_account_id,
            campaign_name=campaign_name,
            api_version=api_version,
        )

    @classmethod
    def build_adset_lookup_url(
        cls,
        campaign_id: str,
        api_version: str = DEFAULT_GRAPH_API_VERSION,
    ) -> str:
        """Construct URL for querying ad sets under a campaign."""
        clean_id = campaign_id.strip() if campaign_id else ""
        if not clean_id:
            raise ValueError("campaign_id must not be empty.")
        ver = api_version.strip() if api_version else cls.DEFAULT_GRAPH_API_VERSION
        query = urllib.parse.urlencode({
            "fields": "id,name,status,lifetime_budget,end_time,campaign_id",
        })
        return f"{cls.GRAPH_API_BASE_URL}/{ver}/{clean_id}/adsets?{query}"

    @classmethod
    def build_creative_lookup_url(
        cls,
        ad_account_id: str,
        api_version: str = DEFAULT_GRAPH_API_VERSION,
    ) -> str:
        """Construct URL for querying account-level ad creatives."""
        canonical_id = cls.normalize_ad_account_id(ad_account_id)
        ver = api_version.strip() if api_version else cls.DEFAULT_GRAPH_API_VERSION
        query = urllib.parse.urlencode({
            "fields": "id,name,object_story_spec",
        })
        return f"{cls.GRAPH_API_BASE_URL}/{ver}/{canonical_id}/adcreatives?{query}"

    @classmethod
    def build_ad_lookup_url(
        cls,
        adset_id: str,
        api_version: str = DEFAULT_GRAPH_API_VERSION,
    ) -> str:
        """Construct URL for querying ads under an ad set."""
        clean_id = adset_id.strip() if adset_id else ""
        if not clean_id:
            raise ValueError("adset_id must not be empty.")
        ver = api_version.strip() if api_version else cls.DEFAULT_GRAPH_API_VERSION
        query = urllib.parse.urlencode({
            "fields": "id,name,status,adset_id,creative",
        })
        return f"{cls.GRAPH_API_BASE_URL}/{ver}/{clean_id}/ads?{query}"

    @classmethod
    def build_campaign_create_url(
        cls,
        ad_account_id: str,
        api_version: str = DEFAULT_GRAPH_API_VERSION,
    ) -> str:
        """Construct endpoint URL for campaign creation."""
        canonical_id = cls.normalize_ad_account_id(ad_account_id)
        ver = api_version.strip() if api_version else cls.DEFAULT_GRAPH_API_VERSION
        return f"{cls.GRAPH_API_BASE_URL}/{ver}/{canonical_id}/campaigns"

    @classmethod
    def build_adset_create_url(
        cls,
        ad_account_id: str,
        api_version: str = DEFAULT_GRAPH_API_VERSION,
    ) -> str:
        """Construct endpoint URL for ad set creation."""
        canonical_id = cls.normalize_ad_account_id(ad_account_id)
        ver = api_version.strip() if api_version else cls.DEFAULT_GRAPH_API_VERSION
        return f"{cls.GRAPH_API_BASE_URL}/{ver}/{canonical_id}/adsets"

    @classmethod
    def build_image_upload_url(
        cls,
        ad_account_id: str,
        api_version: str = DEFAULT_GRAPH_API_VERSION,
    ) -> str:
        """Construct endpoint URL for ad image upload."""
        canonical_id = cls.normalize_ad_account_id(ad_account_id)
        ver = api_version.strip() if api_version else cls.DEFAULT_GRAPH_API_VERSION
        return f"{cls.GRAPH_API_BASE_URL}/{ver}/{canonical_id}/adimages"

    @classmethod
    def build_creative_create_url(
        cls,
        ad_account_id: str,
        api_version: str = DEFAULT_GRAPH_API_VERSION,
    ) -> str:
        """Construct endpoint URL for ad creative creation."""
        canonical_id = cls.normalize_ad_account_id(ad_account_id)
        ver = api_version.strip() if api_version else cls.DEFAULT_GRAPH_API_VERSION
        return f"{cls.GRAPH_API_BASE_URL}/{ver}/{canonical_id}/adcreatives"

    @classmethod
    def build_ad_create_url(
        cls,
        ad_account_id: str,
        api_version: str = DEFAULT_GRAPH_API_VERSION,
    ) -> str:
        """Construct endpoint URL for ad creation."""
        canonical_id = cls.normalize_ad_account_id(ad_account_id)
        ver = api_version.strip() if api_version else cls.DEFAULT_GRAPH_API_VERSION
        return f"{cls.GRAPH_API_BASE_URL}/{ver}/{canonical_id}/ads"

    @classmethod
    def build_pause_url(
        cls,
        object_id: str,
        api_version: str = DEFAULT_GRAPH_API_VERSION,
    ) -> str:
        """Construct endpoint URL for pausing an existing Meta object (campaign, ad set, ad)."""
        clean_id = object_id.strip() if object_id else ""
        if not clean_id:
            raise ValueError("object_id must not be empty.")
        ver = api_version.strip() if api_version else cls.DEFAULT_GRAPH_API_VERSION
        return f"{cls.GRAPH_API_BASE_URL}/{ver}/{clean_id}"

    # ── Payload Builders ─────────────────────────────────────────────────────

    @classmethod
    def build_campaign_payload(
        cls,
        name: str,
        objective: str = "OUTCOME_TRAFFIC",
        special_ad_categories: list[str] | None = None,
        status: str = "PAUSED",
    ) -> dict[str, Any]:
        """Construct deterministic creation payload for a Meta Campaign."""
        clean_name = name.strip() if name else ""
        if not clean_name:
            raise ValueError("Campaign name must not be empty.")
        if status != "PAUSED":
            raise ValueError("Creation status must strictly be 'PAUSED' for safety.")
        categories = special_ad_categories if special_ad_categories is not None else ["NONE"]
        if not categories:
            categories = ["NONE"]
        return {
            "name": clean_name,
            "objective": objective.strip(),
            "special_ad_categories": categories,
            "status": "PAUSED",
        }

    @classmethod
    def build_adset_payload(
        cls,
        campaign_id: str,
        name: str,
        lifetime_budget_paise: int,
        end_time: datetime | str,
        start_time: datetime | str | None = None,
        countries: list[str] | None = None,
        age_min: int = 18,
        age_max: int = 65,
        optimization_goal: str = "LINK_CLICKS",
        billing_event: str = "IMPRESSIONS",
        status: str = "PAUSED",
    ) -> dict[str, Any]:
        """Construct deterministic creation payload for a Meta Ad Set."""
        clean_cid = campaign_id.strip() if campaign_id else ""
        if not clean_cid:
            raise ValueError("campaign_id must not be empty.")
        clean_name = name.strip() if name else ""
        if not clean_name:
            raise ValueError("Ad Set name must not be empty.")
        if lifetime_budget_paise <= 0:
            raise ValueError("lifetime_budget_paise must be a positive integer.")
        if status != "PAUSED":
            raise ValueError("Creation status must strictly be 'PAUSED' for safety.")

        clean_countries = [c.strip().upper() for c in (countries or ["IN"]) if c.strip()]
        if not clean_countries:
            raise ValueError("countries must contain at least one ISO country code.")
        if not (13 <= age_min <= 65) or not (13 <= age_max <= 65) or age_min > age_max:
            raise ValueError(f"Invalid age range: age_min={age_min}, age_max={age_max}")

        def _format_time(t: datetime | str) -> str:
            if isinstance(t, datetime):
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                return t.isoformat()
            clean_t = t.strip()
            if not clean_t:
                raise ValueError("Time string must not be empty.")
            return clean_t

        formatted_end = _format_time(end_time)
        payload: dict[str, Any] = {
            "campaign_id": clean_cid,
            "name": clean_name,
            "optimization_goal": optimization_goal.strip(),
            "billing_event": billing_event.strip(),
            "targeting": {
                "geo_locations": {
                    "countries": clean_countries,
                },
                "age_min": age_min,
                "age_max": age_max,
            },
            "lifetime_budget": lifetime_budget_paise,
            "end_time": formatted_end,
            "status": "PAUSED",
        }
        if start_time is not None:
            payload["start_time"] = _format_time(start_time)
        return payload

    @classmethod
    def build_creative_payload(
        cls,
        name: str,
        page_id: str,
        link: str,
        message: str,
        headline: str,
        image_hash: str,
        call_to_action: str = "LEARN_MORE",
    ) -> dict[str, Any]:
        """Construct deterministic creation payload for a Meta Ad Creative."""
        clean_name = name.strip() if name else ""
        clean_page = page_id.strip() if page_id else ""
        clean_link = link.strip() if link else ""
        clean_msg = message.strip() if message else ""
        clean_headline = headline.strip() if headline else ""
        clean_hash = image_hash.strip() if image_hash else ""
        clean_cta = call_to_action.strip() if call_to_action else "LEARN_MORE"

        if not clean_name:
            raise ValueError("Creative name must not be empty.")
        if not clean_page:
            raise ValueError("page_id must not be empty.")
        if not clean_link:
            raise ValueError("link must not be empty.")
        if not clean_msg:
            raise ValueError("message must not be empty.")
        if not clean_headline:
            raise ValueError("headline must not be empty.")
        if not clean_hash:
            raise ValueError("image_hash must not be empty.")

        return {
            "name": clean_name,
            "object_story_spec": {
                "page_id": clean_page,
                "link_data": {
                    "link": clean_link,
                    "message": clean_msg,
                    "name": clean_headline,
                    "image_hash": clean_hash,
                    "call_to_action": {
                        "type": clean_cta,
                        "value": {
                            "link": clean_link,
                        },
                    },
                },
            },
        }

    @classmethod
    def build_ad_payload(
        cls,
        name: str,
        adset_id: str,
        creative_id: str,
        status: str = "PAUSED",
    ) -> dict[str, Any]:
        """Construct deterministic creation payload for a Meta Ad."""
        clean_name = name.strip() if name else ""
        clean_adset = adset_id.strip() if adset_id else ""
        clean_creative = creative_id.strip() if creative_id else ""

        if not clean_name:
            raise ValueError("Ad name must not be empty.")
        if not clean_adset:
            raise ValueError("adset_id must not be empty.")
        if not clean_creative:
            raise ValueError("creative_id must not be empty.")
        if status != "PAUSED":
            raise ValueError("Creation status must strictly be 'PAUSED' for safety.")

        return {
            "name": clean_name,
            "adset_id": clean_adset,
            "creative": {
                "creative_id": clean_creative,
            },
            "status": "PAUSED",
        }

    @classmethod
    def build_pause_payload(cls) -> dict[str, Any]:
        """Construct deterministic payload for pausing an existing Meta object."""
        return {"status": "PAUSED"}

    # ── Response Parsers ─────────────────────────────────────────────────────

    @classmethod
    def parse_account_response(cls, payload: dict[str, Any] | str) -> MetaAdAccountMetadata:
        """Parse and validate Meta Graph API account response into MetaAdAccountMetadata."""
        data = cls._parse_json_dict(payload)
        if "error" in data and isinstance(data["error"], dict):
            cls._handle_meta_error_envelope(data["error"])

        required_fields = ("id", "name", "account_status", "currency")
        missing = [f for f in required_fields if f not in data or data[f] is None]
        if missing:
            raise ValueError(f"Missing required fields in Meta API response: {missing}")

        try:
            return MetaAdAccountMetadata(
                id=str(data["id"]),
                name=str(data["name"]),
                account_status=int(data["account_status"]),
                currency=str(data["currency"]),
            )
        except Exception as err:
            raise ValueError(f"Invalid field values in Meta API response: {err}") from err

    @classmethod
    def parse_insights_response(cls, payload: dict[str, Any] | str) -> list[MetaInsightsTelemetry]:
        """Parse and validate Meta Graph API insights response into list of MetaInsightsTelemetry."""
        data = cls._parse_json_dict(payload)
        if "error" in data and isinstance(data["error"], dict):
            cls._handle_meta_error_envelope(data["error"])

        if "data" not in data or not isinstance(data["data"], list):
            raise ValueError("Invalid Meta API response format: missing or invalid 'data' list.")

        if not data["data"]:
            return []

        results: list[MetaInsightsTelemetry] = []
        for item in data["data"]:
            if not isinstance(item, dict):
                raise ValueError(f"Expected insight item to be a dict, got {type(item).__name__}.")

            account_id = item.get("account_id")
            if not account_id or not str(account_id).strip():
                raise ValueError("Missing or empty 'account_id' in insight item.")

            raw_start = item.get("date_start")
            raw_stop = item.get("date_stop")
            if not raw_start or not raw_stop:
                raise ValueError("Insight item must contain both 'date_start' and 'date_stop'.")

            try:
                parsed_start = date.fromisoformat(str(raw_start).strip())
                parsed_stop = date.fromisoformat(str(raw_stop).strip())
            except ValueError as err:
                raise ValueError(f"Invalid date format in insight item: {err}") from err

            raw_spend = item.get("spend")
            if raw_spend is None or not str(raw_spend).strip():
                raise ValueError("Missing or empty 'spend' in insight item.")
            try:
                parsed_spend = Decimal(str(raw_spend).strip())
            except Exception as err:
                raise ValueError(f"Invalid spend numeric format: {raw_spend}") from err

            raw_impressions = item.get("impressions")
            if raw_impressions is None or not str(raw_impressions).strip():
                raise ValueError("Missing or empty 'impressions' in insight item.")
            try:
                parsed_impressions = int(str(raw_impressions).strip())
            except Exception as err:
                raise ValueError(f"Invalid impressions integer format: {raw_impressions}") from err

            raw_clicks = item.get("clicks")
            if raw_clicks is None or not str(raw_clicks).strip():
                raise ValueError("Missing or empty 'clicks' in insight item.")
            try:
                parsed_clicks = int(str(raw_clicks).strip())
            except Exception as err:
                raise ValueError(f"Invalid clicks integer format: {raw_clicks}") from err

            campaign_id = item.get("campaign_id")
            clean_campaign_id = str(campaign_id).strip() if campaign_id else None

            raw_cpc = item.get("cpc")
            parsed_cpc = Decimal(str(raw_cpc).strip()) if raw_cpc is not None and str(raw_cpc).strip() else None

            raw_cpm = item.get("cpm")
            parsed_cpm = Decimal(str(raw_cpm).strip()) if raw_cpm is not None and str(raw_cpm).strip() else None

            raw_ctr = item.get("ctr")
            parsed_ctr = float(str(raw_ctr).strip()) if raw_ctr is not None and str(raw_ctr).strip() else None

            telemetry = MetaInsightsTelemetry(
                account_id=str(account_id).strip(),
                campaign_id=clean_campaign_id,
                date_start=parsed_start,
                date_stop=parsed_stop,
                spend=parsed_spend,
                impressions=parsed_impressions,
                clicks=parsed_clicks,
                cpc=parsed_cpc,
                cpm=parsed_cpm,
                ctr=parsed_ctr,
            )
            results.append(telemetry)

        return results

    @classmethod
    def parse_creation_response(cls, payload: dict[str, Any] | str) -> str:
        """Parse and validate Meta Graph API creation response returning entity ID."""
        data = cls._parse_json_dict(payload)
        if "error" in data and isinstance(data["error"], dict):
            cls._handle_meta_error_envelope(data["error"])
        entity_id = data.get("id")
        if not entity_id or not str(entity_id).strip():
            raise ValueError("Missing or empty 'id' in Meta creation response.")
        return str(entity_id).strip()

    @classmethod
    def parse_image_upload_response(cls, payload: dict[str, Any] | str, filename: str | None = None) -> str:
        """Parse and validate Meta Graph API ad image upload response returning image hash."""
        data = cls._parse_json_dict(payload)
        if "error" in data and isinstance(data["error"], dict):
            cls._handle_meta_error_envelope(data["error"])
        images = data.get("images")
        if not isinstance(images, dict) or not images:
            raise ValueError("Missing or invalid 'images' map in Meta image upload response.")

        if filename:
            clean_fn = filename.strip()
            img_info = images.get(clean_fn)
            if not img_info or not isinstance(img_info, dict):
                raise ValueError(f"Filename '{clean_fn}' not found in Meta image upload response.")
        else:
            first_key = next(iter(images))
            img_info = images[first_key]
            if not isinstance(img_info, dict):
                raise ValueError("Invalid image item structure in Meta image upload response.")

        img_hash = img_info.get("hash")
        if not img_hash or not str(img_hash).strip():
            raise ValueError("Missing or empty 'hash' in image info.")
        return str(img_hash).strip()

    @classmethod
    def parse_pause_response(cls, payload: dict[str, Any] | str) -> bool:
        """Parse and validate Meta Graph API pause mutation response."""
        data = cls._parse_json_dict(payload)
        if "error" in data and isinstance(data["error"], dict):
            cls._handle_meta_error_envelope(data["error"])
        success = data.get("success")
        if success is not True:
            raise ValueError(f"Unsuccessful pause response: expected 'success: true', got '{success}'.")
        return True

    # ── Request Execution Helper ─────────────────────────────────────────────

    def _execute_request(self, req: urllib.request.Request, is_write: bool = False) -> str:
        """Execute an HTTP request using custom transport or urllib.request.urlopen.

        SAFETY GUARD:
        - If is_write is True and self._transport is None, live write operations are strictly blocked.
        """
        if is_write and self._transport is None:
            raise MetaApiError(
                "Live Meta write operations are strictly disabled in Step 36. "
                "A mock transport must be provided to test write operations."
            )

        try:
            if self._transport is not None:
                raw = self._transport(req, self.timeout)
                return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)

            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                raw_bytes = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                return raw_bytes.decode(charset)
        except urllib.error.HTTPError as err:
            if err.code == 401:
                raise MetaApiAuthError(
                    "Meta API authentication failed (HTTP 401): invalid or expired access token."
                ) from err
            elif err.code == 403:
                raise MetaApiPermissionError(
                    "Meta API permission denied (HTTP 403): token lacks required permissions (ads_management/ads_read) or account access."
                ) from err
            elif err.code == 404:
                raise MetaApiNotFoundError(
                    f"Meta API endpoint not found (HTTP 404): {err.reason}."
                ) from err
            else:
                raise MetaApiError(
                    f"Meta API HTTP {err.code} error: {err.reason}"
                ) from err
        except urllib.error.URLError as err:
            raise MetaApiConnectionError(
                f"Meta API network connection failed: {err.reason}"
            ) from err
        except TimeoutError as err:
            raise MetaApiTimeoutError("Meta API request timed out.") from err

    # ── Read Operations ──────────────────────────────────────────────────────

    def get_account_metadata(
        self,
        ad_account_id: str | None = None,
        access_token: str | None = None,
        fields: list[str] | None = None,
    ) -> MetaAdAccountMetadata:
        """Execute read-only GET request to Meta Graph API and return verified account metadata."""
        target_account_id = ad_account_id or self.ad_account_id
        target_token = access_token or self._access_token

        if not target_account_id or not target_account_id.strip():
            raise ValueError("META_AD_ACCOUNT_ID is required (neither passed nor found in environment).")
        if not target_token or not target_token.strip():
            raise ValueError("META_ACCESS_TOKEN is required (neither passed nor found in environment).")

        canonical_id = self.normalize_ad_account_id(target_account_id)
        url = self.build_account_url(canonical_id, fields=fields, api_version=self.api_version)

        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {target_token.strip()}",
                "User-Agent": self.DEFAULT_USER_AGENT,
                "Accept": "application/json",
            },
            method="GET",
        )

        text = self._execute_request(req, is_write=False)
        return self.parse_account_response(text)

    def get_insights(
        self,
        object_id: str | None = None,
        access_token: str | None = None,
        fields: list[str] | None = None,
        date_preset: str | None = None,
        time_range: dict[str, str] | None = None,
        level: str | None = None,
    ) -> list[MetaInsightsTelemetry]:
        """Execute read-only GET request to Meta Graph API and return verified insights telemetry."""
        target_object_id = object_id or self.ad_account_id
        target_token = access_token or self._access_token

        if not target_object_id or not target_object_id.strip():
            raise ValueError("object_id is required (neither passed nor configured as ad_account_id).")
        if not target_token or not target_token.strip():
            raise ValueError("META_ACCESS_TOKEN is required (neither passed nor found in environment).")

        clean_id = target_object_id.strip()
        url = self.build_insights_url(
            clean_id,
            fields=fields,
            date_preset=date_preset,
            time_range=time_range,
            level=level,
            api_version=self.api_version,
        )

        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {target_token.strip()}",
                "User-Agent": self.DEFAULT_USER_AGENT,
                "Accept": "application/json",
            },
            method="GET",
        )

        text = self._execute_request(req, is_write=False)
        return self.parse_insights_response(text)

    def check_campaign_exists(
        self,
        campaign_name: str,
        ad_account_id: str | None = None,
        access_token: str | None = None,
    ) -> bool:
        """Check whether a campaign with the given deterministic name already exists in the ad account."""
        target_account_id = ad_account_id or self.ad_account_id
        target_token = access_token or self._access_token

        if not target_account_id or not target_account_id.strip():
            raise ValueError("META_AD_ACCOUNT_ID is required (neither passed nor found in environment).")
        if not target_token or not target_token.strip():
            raise ValueError("META_ACCESS_TOKEN is required (neither passed nor found in environment).")

        url = self.build_campaign_duplicate_check_url(
            target_account_id,
            campaign_name,
            api_version=self.api_version,
        )

        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {target_token.strip()}",
                "User-Agent": self.DEFAULT_USER_AGENT,
                "Accept": "application/json",
            },
            method="GET",
        )

        text = self._execute_request(req, is_write=False)
        data = self._parse_json_dict(text)
        if "error" in data and isinstance(data["error"], dict):
            self._handle_meta_error_envelope(data["error"])

        campaigns_data = data.get("data", [])
        return len(campaigns_data) > 0

    def lookup_campaign(
        self,
        campaign_name: str,
        ad_account_id: str | None = None,
        access_token: str | None = None,
    ) -> LookupResult:
        """Perform remote reconciliation lookup for a Campaign by deterministic name.

        Returns:
            FOUND_EXACT: Exactly 1 matching campaign found.
            NOT_FOUND: 0 matching campaigns found.
            AMBIGUOUS: >1 matching campaigns found.
            UNKNOWN: API/network error, timeout, permission failure, or malformed response.
        """
        target_account_id = ad_account_id or self.ad_account_id
        target_token = access_token or self._access_token

        if not target_account_id or not target_account_id.strip():
            return LookupResult(
                status=ReconciliationStatus.UNKNOWN,
                error_message="META_AD_ACCOUNT_ID is required for campaign lookup.",
            )
        if not target_token or not target_token.strip():
            return LookupResult(
                status=ReconciliationStatus.UNKNOWN,
                error_message="META_ACCESS_TOKEN is required for campaign lookup.",
            )

        clean_name = campaign_name.strip() if campaign_name else ""
        if not clean_name:
            return LookupResult(
                status=ReconciliationStatus.UNKNOWN,
                error_message="campaign_name must not be empty.",
            )

        try:
            url = self.build_campaign_lookup_url(
                target_account_id,
                clean_name,
                api_version=self.api_version,
            )
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {target_token.strip()}",
                    "User-Agent": self.DEFAULT_USER_AGENT,
                    "Accept": "application/json",
                },
                method="GET",
            )
            text = self._execute_request(req, is_write=False)
            data = self._parse_json_dict(text)
            if "error" in data and isinstance(data["error"], dict):
                self._handle_meta_error_envelope(data["error"])

            items = data.get("data")
            if not isinstance(items, list):
                return LookupResult(
                    status=ReconciliationStatus.UNKNOWN,
                    error_message="Invalid Meta API response format: missing or non-list 'data'.",
                )

            matches = [item for item in items if isinstance(item, dict) and item.get("name") == clean_name]
            if len(matches) == 0:
                return LookupResult(status=ReconciliationStatus.NOT_FOUND)
            elif len(matches) == 1:
                cid = str(matches[0].get("id", "")).strip()
                if not cid:
                    return LookupResult(
                        status=ReconciliationStatus.UNKNOWN,
                        error_message="Matched campaign missing 'id' attribute.",
                    )
                return LookupResult(
                    status=ReconciliationStatus.FOUND_EXACT,
                    resource_id=cid,
                    details=matches[0],
                )
            else:
                return LookupResult(
                    status=ReconciliationStatus.AMBIGUOUS,
                    details={"matches": matches},
                    error_message=f"Ambiguous: {len(matches)} campaigns found matching '{clean_name}'.",
                )
        except (MetaApiTimeoutError, TimeoutError) as err:
            return LookupResult(
                status=ReconciliationStatus.UNKNOWN,
                error_message=f"Timeout looking up campaign: {err}",
            )
        except (MetaApiError, urllib.error.URLError, ValueError) as err:
            return LookupResult(
                status=ReconciliationStatus.UNKNOWN,
                error_message=f"Error looking up campaign: {err}",
            )
        except Exception as err:
            return LookupResult(
                status=ReconciliationStatus.UNKNOWN,
                error_message=f"Unexpected error looking up campaign: {err}",
            )

    def lookup_adset(
        self,
        campaign_id: str,
        adset_name: str,
        expected_lifetime_budget_paise: int | None = None,
        access_token: str | None = None,
    ) -> LookupResult:
        """Perform remote reconciliation lookup for an Ad Set under a verified parent campaign.

        Returns:
            FOUND_EXACT: Exactly 1 matching ad set found verifying name, parent campaign, and budget.
            NOT_FOUND: 0 matching ad sets found.
            AMBIGUOUS: >1 matching ad sets found.
            UNKNOWN: API/network error, timeout, permission failure, or malformed response.
        """
        clean_cid = campaign_id.strip() if campaign_id else ""
        if not clean_cid:
            return LookupResult(
                status=ReconciliationStatus.UNKNOWN,
                error_message="campaign_id is required for ad set lookup.",
            )
        clean_name = adset_name.strip() if adset_name else ""
        if not clean_name:
            return LookupResult(
                status=ReconciliationStatus.UNKNOWN,
                error_message="adset_name must not be empty.",
            )
        target_token = access_token or self._access_token
        if not target_token or not target_token.strip():
            return LookupResult(
                status=ReconciliationStatus.UNKNOWN,
                error_message="META_ACCESS_TOKEN is required for ad set lookup.",
            )

        try:
            url = self.build_adset_lookup_url(clean_cid, api_version=self.api_version)
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {target_token.strip()}",
                    "User-Agent": self.DEFAULT_USER_AGENT,
                    "Accept": "application/json",
                },
                method="GET",
            )
            text = self._execute_request(req, is_write=False)
            data = self._parse_json_dict(text)
            if "error" in data and isinstance(data["error"], dict):
                self._handle_meta_error_envelope(data["error"])

            items = data.get("data")
            if not isinstance(items, list):
                return LookupResult(
                    status=ReconciliationStatus.UNKNOWN,
                    error_message="Invalid Meta API response format: missing or non-list 'data'.",
                )

            matches: list[dict[str, Any]] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("name") != clean_name:
                    continue
                raw_parent_id = str(item.get("campaign_id", "")).strip()
                if raw_parent_id and raw_parent_id != clean_cid:
                    continue
                if expected_lifetime_budget_paise is not None:
                    raw_budget = item.get("lifetime_budget")
                    if raw_budget is not None and str(raw_budget).strip() != str(expected_lifetime_budget_paise):
                        continue
                matches.append(item)

            if len(matches) == 0:
                return LookupResult(status=ReconciliationStatus.NOT_FOUND)
            elif len(matches) == 1:
                as_id = str(matches[0].get("id", "")).strip()
                if not as_id:
                    return LookupResult(
                        status=ReconciliationStatus.UNKNOWN,
                        error_message="Matched ad set missing 'id' attribute.",
                    )
                return LookupResult(
                    status=ReconciliationStatus.FOUND_EXACT,
                    resource_id=as_id,
                    details=matches[0],
                )
            else:
                return LookupResult(
                    status=ReconciliationStatus.AMBIGUOUS,
                    details={"matches": matches},
                    error_message=f"Ambiguous: {len(matches)} ad sets found matching '{clean_name}'.",
                )
        except (MetaApiTimeoutError, TimeoutError) as err:
            return LookupResult(
                status=ReconciliationStatus.UNKNOWN,
                error_message=f"Timeout looking up ad set: {err}",
            )
        except (MetaApiError, urllib.error.URLError, ValueError) as err:
            return LookupResult(
                status=ReconciliationStatus.UNKNOWN,
                error_message=f"Error looking up ad set: {err}",
            )
        except Exception as err:
            return LookupResult(
                status=ReconciliationStatus.UNKNOWN,
                error_message=f"Unexpected error looking up ad set: {err}",
            )

    def lookup_creative(
        self,
        creative_name: str,
        page_id: str,
        image_hash: str,
        destination_url: str,
        ad_account_id: str | None = None,
        access_token: str | None = None,
    ) -> LookupResult:
        """Perform remote reconciliation lookup for an Ad Creative using client-side matching.

        Criteria:
        - exact name
        - expected page_id
        - expected image_hash
        - expected destination_url

        Returns:
            FOUND_EXACT: Exactly 1 matching creative found satisfying all criteria.
            NOT_FOUND: 0 matching creatives found.
            AMBIGUOUS: >1 matching creatives found.
            UNKNOWN: API/network error, timeout, permission failure, or malformed response.
        """
        target_account_id = ad_account_id or self.ad_account_id
        target_token = access_token or self._access_token

        if not target_account_id or not target_account_id.strip():
            return LookupResult(
                status=ReconciliationStatus.UNKNOWN,
                error_message="META_AD_ACCOUNT_ID is required for creative lookup.",
            )
        if not target_token or not target_token.strip():
            return LookupResult(
                status=ReconciliationStatus.UNKNOWN,
                error_message="META_ACCESS_TOKEN is required for creative lookup.",
            )

        clean_name = creative_name.strip() if creative_name else ""
        clean_page = page_id.strip() if page_id else ""
        clean_hash = image_hash.strip() if image_hash else ""
        clean_url = destination_url.strip() if destination_url else ""

        if not clean_name or not clean_page or not clean_hash or not clean_url:
            return LookupResult(
                status=ReconciliationStatus.UNKNOWN,
                error_message="creative_name, page_id, image_hash, and destination_url are all required for creative lookup.",
            )

        try:
            url = self.build_creative_lookup_url(target_account_id, api_version=self.api_version)
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {target_token.strip()}",
                    "User-Agent": self.DEFAULT_USER_AGENT,
                    "Accept": "application/json",
                },
                method="GET",
            )
            text = self._execute_request(req, is_write=False)
            data = self._parse_json_dict(text)
            if "error" in data and isinstance(data["error"], dict):
                self._handle_meta_error_envelope(data["error"])

            items = data.get("data")
            if not isinstance(items, list):
                return LookupResult(
                    status=ReconciliationStatus.UNKNOWN,
                    error_message="Invalid Meta API response format: missing or non-list 'data'.",
                )

            matches: list[dict[str, Any]] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("name") != clean_name:
                    continue
                spec = item.get("object_story_spec")
                if not isinstance(spec, dict):
                    continue
                if str(spec.get("page_id", "")).strip() != clean_page:
                    continue
                link_data = spec.get("link_data")
                if not isinstance(link_data, dict):
                    continue
                if str(link_data.get("image_hash", "")).strip() != clean_hash:
                    continue
                if str(link_data.get("link", "")).strip() != clean_url:
                    continue
                matches.append(item)

            if len(matches) == 0:
                return LookupResult(status=ReconciliationStatus.NOT_FOUND)
            elif len(matches) == 1:
                cr_id = str(matches[0].get("id", "")).strip()
                if not cr_id:
                    return LookupResult(
                        status=ReconciliationStatus.UNKNOWN,
                        error_message="Matched creative missing 'id' attribute.",
                    )
                return LookupResult(
                    status=ReconciliationStatus.FOUND_EXACT,
                    resource_id=cr_id,
                    details=matches[0],
                )
            else:
                return LookupResult(
                    status=ReconciliationStatus.AMBIGUOUS,
                    details={"matches": matches},
                    error_message=f"Ambiguous: {len(matches)} creatives found matching '{clean_name}'.",
                )
        except (MetaApiTimeoutError, TimeoutError) as err:
            return LookupResult(
                status=ReconciliationStatus.UNKNOWN,
                error_message=f"Timeout looking up creative: {err}",
            )
        except (MetaApiError, urllib.error.URLError, ValueError) as err:
            return LookupResult(
                status=ReconciliationStatus.UNKNOWN,
                error_message=f"Error looking up creative: {err}",
            )
        except Exception as err:
            return LookupResult(
                status=ReconciliationStatus.UNKNOWN,
                error_message=f"Unexpected error looking up creative: {err}",
            )

    def lookup_ad(
        self,
        adset_id: str,
        ad_name: str,
        creative_id: str,
        access_token: str | None = None,
    ) -> LookupResult:
        """Perform remote reconciliation lookup for an Ad under a verified parent ad set and creative.

        Criteria:
        - exact name
        - verified parent adset_id
        - matching creative_id

        Returns:
            FOUND_EXACT: Exactly 1 matching ad found satisfying all criteria.
            NOT_FOUND: 0 matching ads found.
            AMBIGUOUS: >1 matching ads found.
            UNKNOWN: API/network error, timeout, permission failure, or malformed response.
        """
        clean_adset = adset_id.strip() if adset_id else ""
        clean_name = ad_name.strip() if ad_name else ""
        clean_creative = creative_id.strip() if creative_id else ""

        if not clean_adset or not clean_name or not clean_creative:
            return LookupResult(
                status=ReconciliationStatus.UNKNOWN,
                error_message="adset_id, ad_name, and creative_id are all required for ad lookup.",
            )
        target_token = access_token or self._access_token
        if not target_token or not target_token.strip():
            return LookupResult(
                status=ReconciliationStatus.UNKNOWN,
                error_message="META_ACCESS_TOKEN is required for ad lookup.",
            )

        try:
            url = self.build_ad_lookup_url(clean_adset, api_version=self.api_version)
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {target_token.strip()}",
                    "User-Agent": self.DEFAULT_USER_AGENT,
                    "Accept": "application/json",
                },
                method="GET",
            )
            text = self._execute_request(req, is_write=False)
            data = self._parse_json_dict(text)
            if "error" in data and isinstance(data["error"], dict):
                self._handle_meta_error_envelope(data["error"])

            items = data.get("data")
            if not isinstance(items, list):
                return LookupResult(
                    status=ReconciliationStatus.UNKNOWN,
                    error_message="Invalid Meta API response format: missing or non-list 'data'.",
                )

            matches: list[dict[str, Any]] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("name") != clean_name:
                    continue
                raw_adset_id = str(item.get("adset_id", "")).strip()
                if raw_adset_id and raw_adset_id != clean_adset:
                    continue
                raw_creative = item.get("creative")
                item_creative_id: str | None = None
                if isinstance(raw_creative, dict):
                    item_creative_id = str(raw_creative.get("id") or raw_creative.get("creative_id") or "").strip()
                elif raw_creative:
                    item_creative_id = str(raw_creative).strip()
                elif item.get("creative_id"):
                    item_creative_id = str(item.get("creative_id")).strip()

                if item_creative_id != clean_creative:
                    continue

                matches.append(item)

            if len(matches) == 0:
                return LookupResult(status=ReconciliationStatus.NOT_FOUND)
            elif len(matches) == 1:
                ad_id = str(matches[0].get("id", "")).strip()
                if not ad_id:
                    return LookupResult(
                        status=ReconciliationStatus.UNKNOWN,
                        error_message="Matched ad missing 'id' attribute.",
                    )
                return LookupResult(
                    status=ReconciliationStatus.FOUND_EXACT,
                    resource_id=ad_id,
                    details=matches[0],
                )
            else:
                return LookupResult(
                    status=ReconciliationStatus.AMBIGUOUS,
                    details={"matches": matches},
                    error_message=f"Ambiguous: {len(matches)} ads found matching '{clean_name}'.",
                )
        except (MetaApiTimeoutError, TimeoutError) as err:
            return LookupResult(
                status=ReconciliationStatus.UNKNOWN,
                error_message=f"Timeout looking up ad: {err}",
            )
        except (MetaApiError, urllib.error.URLError, ValueError) as err:
            return LookupResult(
                status=ReconciliationStatus.UNKNOWN,
                error_message=f"Error looking up ad: {err}",
            )
        except Exception as err:
            return LookupResult(
                status=ReconciliationStatus.UNKNOWN,
                error_message=f"Unexpected error looking up ad: {err}",
            )

    # ── Mockable Write Operations ────────────────────────────────────────────

    def create_campaign(
        self,
        name: str,
        objective: str = "OUTCOME_TRAFFIC",
        special_ad_categories: list[str] | None = None,
        status: str = "PAUSED",
        ad_account_id: str | None = None,
        access_token: str | None = None,
    ) -> str:
        """Create a Meta campaign in PAUSED status. Returns newly assigned campaign ID."""
        target_account_id = ad_account_id or self.ad_account_id
        target_token = access_token or self._access_token

        if not target_account_id or not target_account_id.strip():
            raise ValueError("META_AD_ACCOUNT_ID is required (neither passed nor found in environment).")
        if not target_token or not target_token.strip():
            raise ValueError("META_ACCESS_TOKEN is required (neither passed nor found in environment).")

        url = self.build_campaign_create_url(target_account_id, api_version=self.api_version)
        payload = self.build_campaign_payload(
            name=name,
            objective=objective,
            special_ad_categories=special_ad_categories,
            status=status,
        )
        body = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {target_token.strip()}",
                "User-Agent": self.DEFAULT_USER_AGENT,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        response_text = self._execute_request(req, is_write=True)
        return self.parse_creation_response(response_text)

    def create_adset(
        self,
        campaign_id: str,
        name: str,
        lifetime_budget_paise: int,
        end_time: datetime | str,
        start_time: datetime | str | None = None,
        countries: list[str] | None = None,
        age_min: int = 18,
        age_max: int = 65,
        optimization_goal: str = "LINK_CLICKS",
        billing_event: str = "IMPRESSIONS",
        status: str = "PAUSED",
        ad_account_id: str | None = None,
        access_token: str | None = None,
    ) -> str:
        """Create a Meta ad set in PAUSED status with lifetime budget. Returns newly assigned ad set ID."""
        target_account_id = ad_account_id or self.ad_account_id
        target_token = access_token or self._access_token

        if not target_account_id or not target_account_id.strip():
            raise ValueError("META_AD_ACCOUNT_ID is required (neither passed nor found in environment).")
        if not target_token or not target_token.strip():
            raise ValueError("META_ACCESS_TOKEN is required (neither passed nor found in environment).")

        url = self.build_adset_create_url(target_account_id, api_version=self.api_version)
        payload = self.build_adset_payload(
            campaign_id=campaign_id,
            name=name,
            lifetime_budget_paise=lifetime_budget_paise,
            end_time=end_time,
            start_time=start_time,
            countries=countries,
            age_min=age_min,
            age_max=age_max,
            optimization_goal=optimization_goal,
            billing_event=billing_event,
            status=status,
        )
        body = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {target_token.strip()}",
                "User-Agent": self.DEFAULT_USER_AGENT,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        response_text = self._execute_request(req, is_write=True)
        return self.parse_creation_response(response_text)

    def upload_image(
        self,
        file_bytes: bytes,
        filename: str,
        ad_account_id: str | None = None,
        access_token: str | None = None,
    ) -> str:
        """Upload an ad image asset. Returns newly assigned image hash."""
        target_account_id = ad_account_id or self.ad_account_id
        target_token = access_token or self._access_token

        if not target_account_id or not target_account_id.strip():
            raise ValueError("META_AD_ACCOUNT_ID is required (neither passed nor found in environment).")
        if not target_token or not target_token.strip():
            raise ValueError("META_ACCESS_TOKEN is required (neither passed nor found in environment).")
        if not file_bytes:
            raise ValueError("file_bytes must not be empty.")
        clean_fn = filename.strip() if filename else ""
        if not clean_fn:
            raise ValueError("filename must not be empty.")

        url = self.build_image_upload_url(target_account_id, api_version=self.api_version)
        boundary = f"----VentureBotBoundary{uuid4().hex}"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="filename"; filename="{clean_fn}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8") + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {target_token.strip()}",
                "User-Agent": self.DEFAULT_USER_AGENT,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
            },
        )

        response_text = self._execute_request(req, is_write=True)
        return self.parse_image_upload_response(response_text, filename=clean_fn)

    def create_creative(
        self,
        name: str,
        page_id: str,
        link: str,
        message: str,
        headline: str,
        image_hash: str,
        call_to_action: str = "LEARN_MORE",
        ad_account_id: str | None = None,
        access_token: str | None = None,
    ) -> str:
        """Create an ad creative. Returns newly assigned creative ID."""
        target_account_id = ad_account_id or self.ad_account_id
        target_token = access_token or self._access_token

        if not target_account_id or not target_account_id.strip():
            raise ValueError("META_AD_ACCOUNT_ID is required (neither passed nor found in environment).")
        if not target_token or not target_token.strip():
            raise ValueError("META_ACCESS_TOKEN is required (neither passed nor found in environment).")

        url = self.build_creative_create_url(target_account_id, api_version=self.api_version)
        payload = self.build_creative_payload(
            name=name,
            page_id=page_id,
            link=link,
            message=message,
            headline=headline,
            image_hash=image_hash,
            call_to_action=call_to_action,
        )
        body = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {target_token.strip()}",
                "User-Agent": self.DEFAULT_USER_AGENT,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        response_text = self._execute_request(req, is_write=True)
        return self.parse_creation_response(response_text)

    def create_ad(
        self,
        name: str,
        adset_id: str,
        creative_id: str,
        status: str = "PAUSED",
        ad_account_id: str | None = None,
        access_token: str | None = None,
    ) -> str:
        """Create a Meta ad in PAUSED status. Returns newly assigned ad ID."""
        target_account_id = ad_account_id or self.ad_account_id
        target_token = access_token or self._access_token

        if not target_account_id or not target_account_id.strip():
            raise ValueError("META_AD_ACCOUNT_ID is required (neither passed nor found in environment).")
        if not target_token or not target_token.strip():
            raise ValueError("META_ACCESS_TOKEN is required (neither passed nor found in environment).")

        url = self.build_ad_create_url(target_account_id, api_version=self.api_version)
        payload = self.build_ad_payload(
            name=name,
            adset_id=adset_id,
            creative_id=creative_id,
            status=status,
        )
        body = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {target_token.strip()}",
                "User-Agent": self.DEFAULT_USER_AGENT,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        response_text = self._execute_request(req, is_write=True)
        return self.parse_creation_response(response_text)

    def pause_campaign(
        self,
        campaign_id: str,
        access_token: str | None = None,
    ) -> bool:
        """Emergency pause mutation for a campaign. Stops all delivery for constituent ad sets/ads."""
        clean_cid = campaign_id.strip() if campaign_id else ""
        if not clean_cid:
            raise ValueError("campaign_id must not be empty.")
        target_token = access_token or self._access_token
        if not target_token or not target_token.strip():
            raise ValueError("META_ACCESS_TOKEN is required (neither passed nor found in environment).")

        url = self.build_pause_url(clean_cid, api_version=self.api_version)
        payload = self.build_pause_payload()
        body = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {target_token.strip()}",
                "User-Agent": self.DEFAULT_USER_AGENT,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        response_text = self._execute_request(req, is_write=True)
        return self.parse_pause_response(response_text)
