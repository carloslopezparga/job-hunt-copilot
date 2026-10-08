"""Understand job locations: split multi-location strings, detect remote/hybrid,
resolve cities/countries/regions, decide whether a visa is needed, and rank a job
into the candidate's location tiers.

Tiers, best first:
    preferred   matches ``preferences.locations.preferred`` (e.g. Madrid)
    acceptable  matches ``preferences.locations.acceptable`` (e.g. Spain, Remote - EU, EU)
    other       somewhere else the candidate may legally work (or unknown places)
    avoided     only places listed in ``preferences.locations.avoid``
    visa        every listed place needs a work permit the candidate does not hold
    unknown     the posting gives no location at all

A job that lists several places is ranked by the best place it offers, except that a
posting with more than ``MANY_PLACES`` places is not "Madrid" just because Madrid is one of
them: its preferred hit is demoted to the next tier.

On top of the tiers, every job gets a *level* that orders the candidate's priorities:
    home      the preferred city (Madrid)
    country   the rest of the home country (Spain), on site
    remote    remote from the home country or anywhere the candidate can work
    abroad    on site elsewhere in the EU/EEA/CH (no visa)
    other     anywhere else the candidate may work
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jobhunt.models import Job
from jobhunt.textutils import fold

# --------------------------------------------------------------------------- data

EU_COUNTRIES = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
    "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
}  # fmt: skip
# EU citizens can also work without a visa in the EEA and Switzerland.
FREE_MOVEMENT = EU_COUNTRIES | {"NO", "IS", "LI", "CH"}
EUROPE = FREE_MOVEMENT | {"GB", "UA", "RS", "BA", "AL", "MK", "ME", "MD", "TR"}

# name (accent-folded, lowercase) -> ISO country code
COUNTRIES: dict[str, str] = {
    "spain": "ES", "espana": "ES", "españa": "ES", "espanya": "ES",
    "portugal": "PT", "france": "FR", "francia": "FR", "germany": "DE", "alemania": "DE",
    "deutschland": "DE", "italy": "IT", "italia": "IT", "netherlands": "NL",
    "the netherlands": "NL", "paises bajos": "NL", "holland": "NL", "belgium": "BE",
    "belgica": "BE", "ireland": "IE", "irlanda": "IE", "austria": "AT", "switzerland": "CH",
    "suiza": "CH", "schweiz": "CH", "poland": "PL", "polonia": "PL", "czech republic": "CZ",
    "czechia": "CZ", "denmark": "DK", "dinamarca": "DK", "sweden": "SE", "suecia": "SE",
    "norway": "NO", "noruega": "NO", "finland": "FI", "finlandia": "FI", "estonia": "EE",
    "latvia": "LV", "lithuania": "LT", "luxembourg": "LU", "luxemburgo": "LU", "malta": "MT",
    "greece": "GR", "grecia": "GR", "cyprus": "CY", "hungary": "HU", "hungria": "HU",
    "romania": "RO", "rumania": "RO", "bulgaria": "BG", "croatia": "HR", "slovakia": "SK",
    "slovenia": "SI", "iceland": "IS", "liechtenstein": "LI",
    "united kingdom": "GB", "reino unido": "GB", "great britain": "GB", "england": "GB",
    "scotland": "GB", "wales": "GB", "northern ireland": "GB",
    "united states": "US", "united states of america": "US", "estados unidos": "US",
    "usa": "US", "canada": "CA", "mexico": "MX", "brazil": "BR", "brasil": "BR",
    "argentina": "AR", "chile": "CL", "colombia": "CO", "peru": "PE", "uruguay": "UY",
    "singapore": "SG", "singapur": "SG", "hong kong": "HK", "japan": "JP", "japon": "JP",
    "china": "CN", "india": "IN", "australia": "AU", "new zealand": "NZ", "israel": "IL",
    "united arab emirates": "AE", "uae": "AE", "south korea": "KR", "korea": "KR",
    "taiwan": "TW", "turkey": "TR", "turkiye": "TR", "ukraine": "UA", "serbia": "RS",
    "south africa": "ZA", "nigeria": "NG", "egypt": "EG", "philippines": "PH",
}  # fmt: skip

# city (accent-folded, lowercase) -> ISO country code
CITIES: dict[str, str] = {
    # Spain
    "madrid": "ES", "barcelona": "ES", "valencia": "ES", "sevilla": "ES", "seville": "ES",
    "malaga": "ES", "bilbao": "ES", "zaragoza": "ES", "alicante": "ES", "palma": "ES",
    "palma de mallorca": "ES", "las palmas": "ES", "granada": "ES", "murcia": "ES",
    "valladolid": "ES", "vigo": "ES", "a coruna": "ES", "la coruna": "ES",
    "san sebastian": "ES", "donostia": "ES", "pamplona": "ES", "santander": "ES",
    "oviedo": "ES", "gijon": "ES", "cordoba": "ES", "cadiz": "ES", "tarragona": "ES",
    "girona": "ES", "sant cugat": "ES", "tres cantos": "ES", "alcobendas": "ES",
    "pozuelo de alarcon": "ES", "las rozas": "ES", "getafe": "ES", "leganes": "ES",
    "boadilla del monte": "ES", "san sebastian de los reyes": "ES", "santiago de compostela": "ES",
    # rest of the EU / EEA / CH
    "lisbon": "PT", "lisboa": "PT", "porto": "PT", "paris": "FR", "lyon": "FR",
    "marseille": "FR", "toulouse": "FR", "nice": "FR", "bordeaux": "FR", "lille": "FR",
    "nantes": "FR", "sophia antipolis": "FR", "berlin": "DE", "munich": "DE", "munchen": "DE",
    "hamburg": "DE", "frankfurt": "DE", "cologne": "DE", "koln": "DE", "stuttgart": "DE",
    "dusseldorf": "DE", "amsterdam": "NL", "rotterdam": "NL", "the hague": "NL",
    "utrecht": "NL", "eindhoven": "NL", "brussels": "BE", "bruxelles": "BE", "antwerp": "BE",
    "ghent": "BE", "dublin": "IE", "cork": "IE", "galway": "IE", "milan": "IT", "milano": "IT",
    "rome": "IT", "roma": "IT", "turin": "IT", "torino": "IT", "vienna": "AT", "wien": "AT",
    "zurich": "CH", "geneva": "CH", "geneve": "CH", "basel": "CH", "lausanne": "CH",
    "bern": "CH", "zug": "CH", "warsaw": "PL", "warszawa": "PL", "krakow": "PL",
    "wroclaw": "PL", "gdansk": "PL", "prague": "CZ", "praha": "CZ", "brno": "CZ",
    "copenhagen": "DK", "kobenhavn": "DK", "aarhus": "DK", "stockholm": "SE",
    "gothenburg": "SE", "goteborg": "SE", "malmo": "SE", "oslo": "NO", "helsinki": "FI",
    "espoo": "FI", "tallinn": "EE", "riga": "LV", "vilnius": "LT", "athens": "GR",
    "budapest": "HU", "bucharest": "RO", "cluj-napoca": "RO", "sofia": "BG", "zagreb": "HR",
    "bratislava": "SK", "ljubljana": "SI", "valletta": "MT", "limassol": "CY",
    "nicosia": "CY", "luxembourg city": "LU", "reykjavik": "IS",
    # UK
    "london": "GB", "manchester": "GB", "edinburgh": "GB", "cambridge": "GB", "oxford": "GB",
    "bristol": "GB", "glasgow": "GB", "belfast": "GB", "birmingham": "GB", "leeds": "GB",
    # US
    "new york": "US", "new york city": "US", "nyc": "US", "san francisco": "US",
    "bay area": "US", "chicago": "US", "boston": "US", "seattle": "US", "austin": "US",
    "los angeles": "US", "miami": "US", "palo alto": "US", "mountain view": "US",
    "menlo park": "US", "sunnyvale": "US", "san jose": "US", "denver": "US", "atlanta": "US",
    "dallas": "US", "houston": "US", "washington": "US", "philadelphia": "US",
    "pittsburgh": "US", "princeton": "US", "stamford": "US", "greenwich": "US",
    "jersey city": "US", "san diego": "US", "salt lake city": "US", "phoenix": "US",
    # elsewhere
    "toronto": "CA", "montreal": "CA", "vancouver": "CA", "waterloo": "CA",
    "mexico city": "MX", "ciudad de mexico": "MX", "sao paulo": "BR", "buenos aires": "AR",
    "bogota": "CO", "santiago": "CL", "lima": "PE", "singapore": "SG", "tokyo": "JP",
    "sydney": "AU", "melbourne": "AU", "bangalore": "IN", "bengaluru": "IN", "mumbai": "IN",
    "hyderabad": "IN", "pune": "IN", "delhi": "IN", "gurgaon": "IN", "gurugram": "IN",
    "dubai": "AE", "abu dhabi": "AE", "tel aviv": "IL", "shanghai": "CN", "beijing": "CN",
    "shenzhen": "CN", "seoul": "KR", "taipei": "TW", "istanbul": "TR", "kyiv": "UA",
    "cape town": "ZA", "lagos": "NG", "cairo": "EG", "manila": "PH",
}  # fmt: skip

# region -> set of countries it can be satisfied in ("*" = anywhere)
REGIONS: dict[str, set[str]] = {
    "eu": EU_COUNTRIES, "ue": EU_COUNTRIES, "european union": EU_COUNTRIES,
    "union europea": EU_COUNTRIES, "europe": EUROPE, "europa": EUROPE, "emea": EUROPE,
    "eea": FREE_MOVEMENT, "dach": {"DE", "AT", "CH"}, "benelux": {"BE", "NL", "LU"},
    "nordics": {"DK", "SE", "NO", "FI", "IS"}, "iberia": {"ES", "PT"}, "cet": EUROPE,
    "cest": EUROPE, "latam": {"MX", "BR", "AR", "CL", "CO", "PE", "UY"},
    "latin america": {"MX", "BR", "AR", "CL", "CO", "PE", "UY"},
    "north america": {"US", "CA", "MX"}, "americas": {"US", "CA", "MX", "BR", "AR"},
    "apac": {"SG", "HK", "JP", "CN", "IN", "AU", "NZ", "KR", "TW", "PH"},
    "asia": {"SG", "HK", "JP", "CN", "IN", "KR", "TW", "PH"},
    "worldwide": {"*"}, "anywhere": {"*"}, "global": {"*"}, "international": {"*"},
}  # fmt: skip

# Case-sensitive abbreviations (only matched in UPPER CASE to avoid "us", "in", "or"...)
ABBREVIATIONS: dict[str, str] = {
    "US": "US", "USA": "US", "U.S.": "US", "U.S.A.": "US", "UK": "GB", "U.K.": "GB",
    "NY": "US", "CA": "US", "MA": "US", "IL": "US", "TX": "US", "WA": "US", "NJ": "US",
    "CO": "US", "GA": "US", "PA": "US", "FL": "US", "DC": "US", "CT": "US", "VA": "US",
    "NC": "US", "MN": "US", "OH": "US", "MI": "US", "UT": "US", "AZ": "US", "OR": "US",
    "MD": "US", "UAE": "AE",
    # states whose code is not an everyday word (IN, ME, OR, ID, LA, AR, DE are left out)
    "HI": "US", "KS": "US", "KY": "US", "NM": "US", "NV": "US", "OK": "US", "TN": "US",
    "WI": "US", "WV": "US", "SC": "US", "SD": "US", "ND": "US", "MT": "US", "MO": "US",
    "IA": "US", "AL": "US", "AK": "US", "MS": "US", "NH": "US", "VT": "US", "WY": "US",
    "RI": "US", "NE": "US",
}  # fmt: skip
US_STATES = {
    "california", "texas", "massachusetts", "illinois", "washington state", "new jersey",
    "colorado", "georgia", "pennsylvania", "florida", "connecticut", "virginia",
    "north carolina", "minnesota", "ohio", "michigan", "utah", "arizona", "oregon",
    "maryland", "new york state",
}  # fmt: skip

REMOTE_TERMS = [
    "remote", "remoto", "remota", "teletrabajo", "work from home", "wfh",
    "fully distributed", "distributed", "anywhere", "home based", "home-based",
]  # fmt: skip
HYBRID_TERMS = ["hybrid", "hibrido", "hibrida", "flexible"]

_SEGMENT_SPLIT = re.compile(r"\s*(?:;|\||\n|\s/\s|\s+or\s+|\s+o\s+)\s*")


def _alternation(keys) -> re.Pattern[str]:  # noqa: ANN001
    ordered = sorted(keys, key=len, reverse=True)
    return re.compile(r"(?<![\w])(" + "|".join(re.escape(k) for k in ordered) + r")(?![\w])")


_COUNTRY_RE = _alternation(COUNTRIES)
_CITY_RE = _alternation(CITIES)
_REGION_RE = _alternation(REGIONS)
_STATE_RE = _alternation(US_STATES)
_ABBR_RE = re.compile(
    r"(?<![\w.])(" + "|".join(re.escape(k) for k in sorted(ABBREVIATIONS, key=len, reverse=True))
    + r")(?![\w])"
)  # fmt: skip
_REMOTE_RE = _alternation(REMOTE_TERMS)
_HYBRID_RE = _alternation(HYBRID_TERMS)


# ------------------------------------------------------------------------- places


@dataclass(frozen=True)
class Place:
    raw: str
    country: str | None = None  # ISO code
    region: str | None = None  # key of REGIONS
    cities: frozenset[str] = field(default_factory=frozenset)
    remote: bool = False
    hybrid: bool = False

    @property
    def is_known(self) -> bool:
        return bool(self.country or self.region or self.cities)

    def countries(self) -> set[str]:
        if self.country:
            return {self.country}
        if self.region:
            return set(REGIONS[self.region])
        return set()

    def needs_visa(self, authorized: set[str]) -> bool | None:
        """True if the candidate cannot work here, False if they can, None if unknown."""
        where = self.countries()
        if not where:
            return None
        if "*" in where:
            return False
        return not (where & authorized)


def authorized_countries(work_authorization: list[str]) -> set[str]:
    allowed: set[str] = set()
    for item in work_authorization:
        code = item.strip().upper()
        if code == "EU":
            allowed |= FREE_MOVEMENT
        elif code == "UK":
            allowed.add("GB")
        elif code:
            allowed.add(code)
    return allowed


def _analyze(segment: str) -> list[Place]:
    text = fold(segment)
    remote = bool(_REMOTE_RE.search(text))
    hybrid = not remote and bool(_HYBRID_RE.search(text))
    cities = [m.group(1) for m in _CITY_RE.finditer(text)]
    countries = [COUNTRIES[m.group(1)] for m in _COUNTRY_RE.finditer(text)]
    # "Washington" as a state name should not hide "Washington DC" etc.; states imply US.
    if _STATE_RE.search(text):
        countries.append("US")
    city_countries = {CITIES[c] for c in cities}
    for m in _ABBR_RE.finditer(segment):
        abbr = m.group(1)
        # "Toronto, ON, CA" / "Tel Aviv, IL": a 2-letter code that is the ISO code of the
        # city's own country is that country, not a US state.
        if len(abbr) == 2 and abbr in city_countries:
            continue
        countries.append(ABBREVIATIONS[abbr])
    region = next((m.group(1) for m in _REGION_RE.finditer(text)), None)

    distinct = list(dict.fromkeys(countries))
    if len(distinct) > 1:  # "Spain, Portugal" or "Madrid, Spain, London, UK"
        return [
            Place(segment, code, None, frozenset(c for c in cities if CITIES[c] == code),
                  remote, hybrid)
            for code in distinct
        ]  # fmt: skip
    if distinct:  # the qualifier wins over the city ("Cambridge, MA" -> US)
        return [Place(segment, distinct[0], None, frozenset(cities), remote, hybrid)]
    if len(city_countries) > 1:  # "Madrid, London" -> two places
        return [
            Place(c.title(), CITIES[c], None, frozenset({c}), remote, hybrid)
            for c in dict.fromkeys(cities)
        ]
    if cities:
        return [Place(segment, CITIES[cities[0]], None, frozenset(cities), remote, hybrid)]
    if region:
        return [Place(segment, None, region, frozenset(), remote, hybrid)]
    return [Place(segment, None, None, frozenset(), remote, hybrid)]


def parse_places(texts: list[str]) -> list[Place]:
    """Split and resolve location strings such as 'Madrid; Remote - EU' or 'Hybrid - Madrid'."""
    places: list[Place] = []
    seen: set[str] = set()
    for text in texts:
        for segment in _SEGMENT_SPLIT.split(text or ""):
            segment = segment.strip(" ,-–—")
            key = fold(segment)
            if not segment or key in seen:
                continue
            seen.add(key)
            places.extend(_analyze(segment))
    # A generic "Remote" segment next to a qualified one ("Remote; Spain") is redundant.
    unique: dict[tuple, Place] = {}
    for p in places:
        unique.setdefault((p.country, p.region, p.cities, p.remote, p.hybrid), p)
    return list(unique.values())


def job_places(job: Job) -> list[Place]:
    places = parse_places([job.location, *job.locations])
    if job.workplace_type == "remote" and places and not any(p.remote for p in places):
        places = [
            Place(p.raw, p.country, p.region, p.cities, True, False) for p in places
        ]  # e.g. Ashby isRemote with location "Madrid": remote from Madrid
    if not places and job.workplace_type == "remote":
        places = [Place("Remote", remote=True)]
    return places


# -------------------------------------------------------------------- preferences


def matches(place: Place, pattern: str) -> bool:
    """Does ``place`` satisfy a preference entry such as 'Madrid', 'Spain', 'EU',
    'Remote', 'Remote - Spain' or 'Hybrid - Madrid'?"""
    wanted = _analyze(pattern)[0]
    if wanted.remote and not place.remote:
        return False
    if wanted.hybrid and not (place.hybrid or place.remote):
        return False
    if wanted.cities:
        return bool(place.cities & wanted.cities)
    if wanted.country:
        return place.country == wanted.country or (
            place.country is None and place.region is not None
            and REGIONS[place.region] == {wanted.country}
        )  # fmt: skip
    if wanted.region:
        allowed = REGIONS[wanted.region]
        if "*" in allowed:
            return True
        if place.country:
            return place.country in allowed
        if place.region:
            return REGIONS[place.region] <= allowed
        return False
    if wanted.remote:  # plain "Remote": remote jobs with no stated geography
        return place.remote and not (place.country or place.region)
    return fold(pattern).strip() in fold(place.raw)  # unknown names: plain text match


TIER_ORDER = ("preferred", "acceptable", "other", "unknown", "avoided", "visa")
LEVELS = ("home", "country", "remote", "abroad", "other")
MANY_PLACES = 5  # a posting listing more places than this is not specific to any of them


@dataclass
class LocationAssessment:
    tier: str
    place: Place | None
    places: list[Place]
    remote: bool  # the best place can be worked remotely
    visa_places: list[Place]
    level: str | None = None  # one of LEVELS (None for visa/avoided/unknown)
    many: bool = False  # lists more than MANY_PLACES places

    @property
    def needs_visa(self) -> bool:
        return self.tier == "visa"

    def label(self) -> str:
        where = self.place.raw if self.place else "location unknown"
        if self.tier == "visa":
            raw = self.visa_places[0].raw if self.visa_places else where
            codes = sorted({c for p in self.visa_places for c in p.countries()} - {"*"})
            return f"{raw} (visa needed: {'/'.join(codes) or '?'}) ✗"
        extra = f", +{len(self.places) - 1} more" if len(self.places) > 1 else ""
        if self.many:
            extra += f" - {len(self.places)} places, not specific to any"
        return f"{where} ({self.tier}{extra})"


def home_countries(preferred: list[str]) -> set[str]:
    """Countries of the preferred places (Madrid -> {'ES'})."""
    return {c for pattern in preferred for place in _analyze(pattern) for c in place.countries()}


def place_level(
    place: Place, preferred: list[str], home: set[str], authorized: set[str], many: bool = False
) -> str:
    """Priority level of one workable place (see module docstring). With ``many`` places
    listed, a preferred-city hit is not specific enough to count as home."""
    if not many and any(matches(place, p) for p in preferred):
        return "home"
    countries = place.countries()
    if place.remote:
        reachable = not countries or "*" in countries or bool(countries & (home | authorized))
        return "remote" if reachable else "other"
    if place.country in home:
        return "country"
    if place.country in FREE_MOVEMENT & authorized:
        return "abroad"
    return "other"


def assess(
    job: Job,
    preferred: list[str],
    acceptable: list[str],
    avoid: list[str],
    work_authorization: list[str],
    many_places: int = MANY_PLACES,
) -> LocationAssessment:
    places = job_places(job)
    if not places:
        return LocationAssessment("unknown", None, [], job.workplace_type == "remote", [])
    authorized = authorized_countries(work_authorization)
    visa = [p for p in places if p.needs_visa(authorized)]
    workable = [p for p in places if p not in visa]
    if not workable:
        return LocationAssessment("visa", None, places, False, visa)
    many = len(places) > many_places
    allowed = [p for p in workable if not any(matches(p, a) for a in avoid)]
    if not allowed:
        return LocationAssessment("avoided", workable[0], places, workable[0].remote, visa,
                                  many=many)  # fmt: skip
    home = home_countries(preferred)
    levels = {id(p): place_level(p, preferred, home, authorized, many) for p in allowed}
    best = min(allowed, key=lambda p: LEVELS.index(levels[id(p)]))
    level = levels[id(best)]
    stages = [("preferred", preferred), ("acceptable", acceptable)]
    if many:  # a long list of places is not "Madrid" just because Madrid is one of them
        stages = [("acceptable", [*preferred, *acceptable])]
    for tier, patterns in stages:
        hits = [p for p in allowed if any(matches(p, pat) for pat in patterns)]
        if hits:
            any_remote = any(p.remote for p in hits)
            return LocationAssessment(tier, hits[0], places, any_remote, visa, level, many)
    known = [p for p in allowed if p.is_known or p.remote]
    shown = known[0] if known else allowed[0]
    tier = "other" if known else "unknown"
    any_remote = any(p.remote for p in allowed)
    return LocationAssessment(tier, shown, places, any_remote, visa, level if known else None, many)
