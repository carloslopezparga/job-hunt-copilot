"""Normalized data model shared by connectors, scoring, tracker and generator."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

EMPLOYMENT_TYPES = ("internship", "full_time", "part_time", "contract", "temporary", "unknown")
WORKPLACE_TYPES = ("remote", "hybrid", "onsite", "unknown")


@dataclass
class Company:
    """An employer entry from companies.yaml."""

    name: str
    ats: str  # greenhouse | lever | ashby
    slug: str  # board token / company handle on the ATS
    sector: str  # one of profile.SECTORS
    region: str = ""  # lever only: "eu" for api.eu.lever.co
    notes: str = ""


@dataclass
class Job:
    """A job posting normalized across sources."""

    source: str
    external_id: str
    company: str
    company_slug: str
    title: str
    url: str
    sector: str | None = None
    location: str = ""
    locations: list[str] = field(default_factory=list)
    workplace_type: str = "unknown"  # remote | hybrid | onsite | unknown
    employment_type: str = "unknown"  # see EMPLOYMENT_TYPES
    department: str = ""
    description: str = ""  # plain text
    posted_at: str | None = None  # ISO 8601 date (YYYY-MM-DD)

    @property
    def uid(self) -> str:
        return f"{self.source}:{self.company_slug}:{self.external_id}"

    @property
    def is_remote(self) -> bool | None:
        if self.workplace_type == "remote":
            return True
        if self.workplace_type in ("onsite", "hybrid"):
            return False
        return None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["uid"] = self.uid
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})
