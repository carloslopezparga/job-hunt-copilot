"""Ashby public Job Posting API.

Docs: https://developers.ashbyhq.com/docs/public-job-posting-api
Endpoint: GET https://api.ashbyhq.com/posting-api/job-board/{job_board_name}?includeCompensation=true
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


class AshbySource(JobSource):
    name = "ashby"

    def board_url(self, company: Company) -> str:
        return (
            f"https://api.ashbyhq.com/posting-api/job-board/{company.slug}?includeCompensation=true"
        )

    def parse(self, payload: Any, company: Company) -> list[Job]:
        jobs: list[Job] = []
        for item in payload["jobs"]:
            if item.get("isListed") is False:
                continue
            title = str(item["title"]).strip()
            location = str(item.get("location") or "").strip()
            secondary = [str(s.get("location") or "") for s in item.get("secondaryLocations") or []]
            description = item.get("descriptionPlain") or html_to_text(item.get("descriptionHtml"))
            workplace = item.get("workplaceType")
            if not workplace and item.get("isRemote") is True:
                workplace = "remote"
            jobs.append(
                Job(
                    source=self.name,
                    external_id=str(item["id"]),
                    company=company.name,
                    company_slug=company.slug,
                    sector=company.sector,
                    title=title,
                    url=str(item.get("jobUrl") or item.get("applyUrl") or ""),
                    location=location,
                    locations=unique([location, *secondary]),
                    workplace_type=normalize_workplace(workplace, title, location),
                    employment_type=normalize_employment(item.get("employmentType"), title),
                    department=" / ".join(
                        unique([str(item.get("department") or ""), str(item.get("team") or "")])
                    ),
                    description=description.strip(),
                    posted_at=iso_date(item.get("publishedAt")),
                )
            )
        return jobs
