"""Lever Postings API.

Docs: https://github.com/lever/postings-api
Endpoint: GET https://api.lever.co/v0/postings/{company}?mode=json
(EU-hosted accounts: https://api.eu.lever.co/v0/postings/{company}?mode=json)
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


class LeverSource(JobSource):
    name = "lever"

    def board_url(self, company: Company) -> str:
        host = "api.eu.lever.co" if company.region.lower() == "eu" else "api.lever.co"
        return f"https://{host}/v0/postings/{company.slug}?mode=json"

    def parse(self, payload: Any, company: Company) -> list[Job]:
        if not isinstance(payload, list):
            raise TypeError("Lever payload must be a JSON list of postings")
        jobs: list[Job] = []
        for item in payload:
            title = str(item["text"]).strip()
            categories = item.get("categories") or {}
            location = str(categories.get("location") or "").strip()
            all_locations = [str(x) for x in categories.get("allLocations") or []]

            sections = [item.get("descriptionPlain") or html_to_text(item.get("description"))]
            for block in item.get("lists") or []:
                heading = str(block.get("text") or "").strip()
                body = html_to_text(f"<ul>{block.get('content') or ''}</ul>")
                sections.append(f"{heading}\n{body}".strip())
            sections.append(item.get("additionalPlain") or html_to_text(item.get("additional")))
            description = "\n\n".join(s.strip() for s in sections if s and s.strip())

            jobs.append(
                Job(
                    source=self.name,
                    external_id=str(item["id"]),
                    company=company.name,
                    company_slug=company.slug,
                    sector=company.sector,
                    title=title,
                    url=str(item.get("hostedUrl") or item.get("applyUrl") or ""),
                    location=location,
                    locations=unique([location, *all_locations]),
                    workplace_type=normalize_workplace(item.get("workplaceType"), title, location),
                    employment_type=normalize_employment(categories.get("commitment"), title),
                    department=" / ".join(
                        unique([str(categories.get(k) or "") for k in ("department", "team")])
                    ),
                    description=description,
                    posted_at=iso_date(item.get("createdAt")),
                )
            )
        return jobs
