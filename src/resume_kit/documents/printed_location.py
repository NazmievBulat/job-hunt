"""Display explicit US city/state/country locations without changing saved facts.

The browser follows this rule in printed-location.ts. Shared cases and a state-table parity
check keep the preview, copied text, PDF and Word output together.
"""

import re
from unicodedata import category

_US_STATES: dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    "district of columbia": "DC",
}
_STATE_CODES = frozenset(_US_STATES.values())
_US_COUNTRIES = frozenset(
    ("united states", "united states of america", "us", "usa", "u.s.", "u.s.a.")
)
# White space as the browser's String.trim and \s see it. Python's own strip and split also
# count U+001C-U+001F and U+0085, and not U+FEFF, so the preview and the files would disagree.
_SPACE = (
    "\t\n\v\f\r \u00a0\u1680"
    + "".join(chr(code) for code in range(0x2000, 0x200B))
    + "\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_SPACES = re.compile(f"[{re.escape(_SPACE)}]+")
_QUALIFIER = re.compile(
    r"\b(remote|hybrid|relocation|relocating|onsite|on-site|greater|area|metropolitan)\b"
)


def printed_location(location: str | None) -> str | None:
    """Shorten only a complete, unqualified US location; return everything else as saved."""
    if not location:
        return location
    parts = [part.strip(_SPACE) for part in location.split(",")]
    if len(parts) != 3:
        return location
    city, state, country = parts
    state = _SPACES.sub(" ", state)
    country = _SPACES.sub(" ", country)
    if country.lower() not in _US_COUNTRIES:
        return location
    code = _US_STATES.get(state.lower())
    if code is None and state.upper() in _STATE_CODES:
        code = state.upper()
    if code is None or not city or _QUALIFIER.search(city.lower()):
        return location
    # Parentheses, slashes, digits and other qualifiers are deliberately not interpreted.
    if not any(char.isalpha() for char in city) or any(
        not (char.isalpha() or category(char).startswith("M") or char in " .'-\u2019")
        for char in city
    ):
        return location
    return f"{city}, {code}"
