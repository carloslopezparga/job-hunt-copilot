"""Greenhouse Job Board API.

Docs: https://developers.greenhouse.io/job-board.html
Endpoint: GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true
"""

from __future__ import annotations

from typing import Any

from jobhunt.models import Company, Job
from jobhunt.sources.base import (
    JobSource,
    iso_date,
    normalize_employment,
    normalize_workplace,
    unique,
)
from jobhunt.textutils import html_to_text

_EMPLOYMENT_FIELDS = ("employment type", "employment_type", "job type", "contract type")
_WORKPLACE_FIELDS = ("workplace type", "location type", "remote", "work model")


def _metadata_value(metadata: list[dict[str, Any]] | None, names: tuple[str, ...]) -> str:
    for item in metadata or []:
        if str(item.get("name", "")).strip().lower() in names:
            value = item.get("value")
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value)
            return str(value or "")
    return ""


class GreenhouseSource(JobSource):
    name = "greenhouse"

    def board_url(self, company: Company) -> str:
        return f"https://boards-api.greenhouse.io/v1/boards/{company.slug}/jobs?content=true"

    def parse(self, payload: Any, company: Company) -> list[Job]:
        jobs: list[Job] = []
        for item in payload["jobs"]:
            title = str(item["title"]).strip()
            description = html_to_text(item.get("content"), escaped=True)
            location = str((item.get("location") or {}).get("name") or "").strip()
            offices = [str(o.get("name") or "") for o in item.get("offices") or []]
            metadata = item.get("metadata")
            departments = [str(d.get("name") or "") for d in item.get("departments") or []]
            jobs.append(
                Job(
                    source=self.name,
                    external_id=str(item["id"]),
                    company=company.name,
                    company_slug=company.slug,
                    sector=company.sector,
                    title=title,
                    url=str(item.get("absolute_url") or ""),
                    location=location,
                    locations=unique([location, *offices]),
                    workplace_type=normalize_workplace(
                        _metadata_value(metadata, _WORKPLACE_FIELDS), title, location
                    ),
                    employment_type=normalize_employment(
                        _metadata_value(metadata, _EMPLOYMENT_FIELDS), title
                    ),
                    department=", ".join(unique(departments)),
                    description=description,
                    posted_at=iso_date(item.get("first_published") or item.get("updated_at")),
                )
            )
        return jobs
