"""Wikimedia Analytics Pageviews research source adapter (Step 19).

Provides deterministic fetching and parsing of daily top pageview observations
from the Wikimedia Analytics Pageviews API into canonical ResearchEvidenceItem contracts.
Does NOT create opportunities, write to databases, or perform financial actions.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from venturebot.models.evidence import EvidenceCategory
from venturebot.opportunity.models import ResearchEvidenceItem


class WikimediaPageviewsAdapter:
    """Adapter for fetching and parsing daily top pageviews from Wikimedia Analytics API."""

    BASE_URL = "https://wikimedia.org/api/rest_v1/metrics/pageviews/top"
    DEFAULT_PROJECT = "en.wikipedia"
    DEFAULT_ACCESS = "all-access"
    DEFAULT_USER_AGENT = (
        "VentureBot/0.0.1 (https://github.com/Vishnu3568/venturebot; research@venturebot.local)"
    )
    EARLIEST_AVAILABLE_DATE = date(2015, 7, 1)

    @classmethod
    def build_url(
        cls,
        target_date: date,
        project: str = DEFAULT_PROJECT,
        access: str = DEFAULT_ACCESS,
    ) -> str:
        """Construct canonical Wikimedia pageviews top endpoint URL for a given date."""
        if not isinstance(target_date, date):
            raise ValueError("target_date must be a datetime.date instance.")
        if target_date < cls.EARLIEST_AVAILABLE_DATE:
            raise ValueError(
                f"Wikimedia pageviews data is only available from {cls.EARLIEST_AVAILABLE_DATE.isoformat()} onwards."
            )

        clean_project = project.strip()
        if not clean_project:
            raise ValueError("project must not be empty or whitespace.")

        clean_access = access.strip()
        if not clean_access:
            raise ValueError("access must not be empty or whitespace.")

        year_str = f"{target_date.year:04d}"
        month_str = f"{target_date.month:02d}"
        day_str = f"{target_date.day:02d}"

        return f"{cls.BASE_URL}/{clean_project}/{clean_access}/{year_str}/{month_str}/{day_str}"

    @classmethod
    def build_article_url(cls, article: str, project: str = DEFAULT_PROJECT) -> str:
        """Construct canonical Wikipedia article URL for an article title."""
        clean_article = article.strip()
        if not clean_article:
            raise ValueError("article title must not be empty or whitespace.")

        clean_project = project.strip()
        if "." in clean_project:
            domain = clean_project if clean_project.endswith(".org") else f"{clean_project}.org"
        else:
            domain = f"{clean_project}.wikipedia.org"

        formatted_title = clean_article.replace(" ", "_")
        encoded_title = urllib.parse.quote(formatted_title, safe=":/@()")
        return f"https://{domain}/wiki/{encoded_title}"

    @classmethod
    def is_non_content_page(cls, article: str) -> bool:
        """Deterministically identify clearly non-content or system pages."""
        clean = article.strip()
        if not clean:
            return True
        lower = clean.lower().replace(" ", "_")
        if lower == "main_page":
            return True
        if lower.startswith("special:"):
            return True
        return False

    @classmethod
    def parse_response(
        cls,
        payload: dict[str, Any] | str,
        api_url: str,
        target_date: date,
        project: str = DEFAULT_PROJECT,
        limit: int | None = None,
    ) -> list[ResearchEvidenceItem]:
        """Parse Wikimedia API JSON response into canonical ResearchEvidenceItem objects."""
        if not isinstance(target_date, date):
            raise ValueError("target_date must be a datetime.date instance.")
        if limit is not None and limit <= 0:
            raise ValueError("limit must be greater than zero when specified.")

        clean_api_url = api_url.strip() if api_url else ""
        if not clean_api_url:
            raise ValueError("api_url must not be empty or whitespace.")

        if isinstance(payload, str):
            try:
                data = json.loads(payload)
            except json.JSONDecodeError as err:
                raise ValueError(f"Malformed Wikimedia JSON payload: {err}") from err
        elif isinstance(payload, dict):
            data = payload
        else:
            raise ValueError("payload must be a JSON dictionary or JSON string.")

        items = data.get("items")
        if not isinstance(items, list):
            raise ValueError("Invalid Wikimedia response: missing or non-list 'items' field.")
        if not items:
            return []

        first_item = items[0]
        if not isinstance(first_item, dict):
            raise ValueError("Invalid Wikimedia response: 'items' element must be an object.")

        articles = first_item.get("articles")
        if not isinstance(articles, list):
            raise ValueError("Invalid Wikimedia response: missing or non-list 'articles' field in items[0].")

        evidence_items: list[ResearchEvidenceItem] = []

        for entry in articles:
            if not isinstance(entry, dict):
                raise ValueError(f"Invalid article observation: expected object, got {type(entry).__name__}.")

            raw_article = entry.get("article")
            raw_views = entry.get("views")
            raw_rank = entry.get("rank")

            if not isinstance(raw_article, str) or not raw_article.strip():
                raise ValueError(f"Invalid observation fields: missing or empty 'article' in {entry}.")
            if not isinstance(raw_views, int) or raw_views < 0:
                raise ValueError(f"Invalid observation fields: 'views' must be a non-negative integer in {entry}.")
            if not isinstance(raw_rank, int) or raw_rank <= 0:
                raise ValueError(f"Invalid observation fields: 'rank' must be a positive integer in {entry}.")

            article_title = raw_article.strip()
            if cls.is_non_content_page(article_title):
                continue

            display_title = article_title.replace("_", " ")
            article_url = cls.build_article_url(article_title, project=project)

            statement = (
                f"Wikipedia article '{display_title}' recorded {raw_views} pageviews "
                f"(rank #{raw_rank}) on {target_date.isoformat()}."
            )
            source_ref = f"{article_url} (via {clean_api_url})"

            evidence_items.append(
                ResearchEvidenceItem(
                    statement=statement,
                    category=EvidenceCategory.FACT,
                    source_reference=source_ref,
                    observation_date=target_date,
                    metric_value=Decimal(raw_views),
                )
            )

            if limit is not None and len(evidence_items) >= limit:
                break

        return evidence_items

    @classmethod
    def fetch_top_pageviews(
        cls,
        target_date: date,
        project: str = DEFAULT_PROJECT,
        access: str = DEFAULT_ACCESS,
        limit: int | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 10.0,
    ) -> list[ResearchEvidenceItem]:
        """Fetch daily top pageviews over HTTP and return parsed ResearchEvidenceItem objects."""
        clean_ua = user_agent.strip() if user_agent else ""
        if not clean_ua:
            raise ValueError("User-Agent header must not be empty per Wikimedia policy.")

        url = cls.build_url(target_date, project=project, access=access)

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": clean_ua,
                "Accept": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw_data = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                text = raw_data.decode(charset)
        except urllib.error.HTTPError as err:
            raise RuntimeError(f"Wikimedia API HTTP {err.code} error: {err.reason} for {url}") from err
        except urllib.error.URLError as err:
            raise RuntimeError(f"Wikimedia API network connection failed: {err.reason} for {url}") from err
        except TimeoutError as err:
            raise RuntimeError(f"Wikimedia API request timed out for {url}") from err

        return cls.parse_response(
            payload=text,
            api_url=url,
            target_date=target_date,
            project=project,
            limit=limit,
        )
