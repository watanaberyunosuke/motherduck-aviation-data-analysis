"""Parser for ICAO-format NOTAMs (ICAO Annex 15 / Doc 8126 layout).

    (A2241/26 NOTAMN
    Q) VHHK/QMRLC/IV/NBO/A /000/999/2219N11355E005
    A) VHHH B) 2609281800 C) 2609282359
    D) ...            (optional schedule)
    E) free text
    F) SFC G) 2000FT AMSL)   (optional vertical limits)

Items are read in their fixed order rather than by searching for every "X)" in the
text, because free text such as "TWY C)" would otherwise be mistaken for item C.
Anything that does not match (for example FAA domestic-format NOTAMs) returns a
ParsedNotam with the unparsed fields left as None instead of raising.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

_HEADER = re.compile(r"\(?\s*(?P<number>[A-Z]\d{4}/\d{2})\s+NOTAM(?P<type>[NRC])(?:\s+(?P<replaces>[A-Z]\d{4}/\d{2}))?")
_Q = re.compile(
    # The FAA writes some US FIRs with 3 letters (ZAN for Anchorage).
    r"Q\)\s*(?P<fir>[A-Z]{3,4})/(?P<code>Q[A-Z]{4})/(?P<traffic>[A-Z ]*)/(?P<purpose>[A-Z ]*)/"
    r"(?P<scope>[A-Z ]*)/(?P<lower>\d{3})/(?P<upper>\d{3})/"
    r"(?P<lat>\d{4}[NS])(?P<lon>\d{5}[EW])(?P<radius>\d{3})"
)
_ITEM_A = re.compile(r"(?<![A-Z0-9])A\)\s*")
_ITEM_E = re.compile(r"(?<![A-Z0-9])E\)\s*")
_A_TO_D = re.compile(
    r"^(?P<A>.*?)\s*B\)\s*(?P<B>\d{10})"
    r"(?:\s*C\)\s*(?P<C>\d{10}(?:\s*EST)?|PERM|UFN))?"
    r"(?:\s*D\)\s*(?P<D>.*?))?\s*$",
    re.S,
)
# F) and G) only count when they look like vertical limits, and only at the end.
_LIMIT = r"(?:SFC|GND|UNL|FL\s?\d{2,3}|\d+\s?(?:FT|M)(?:\s?(?:AMSL|AGL|MSL))?)"
_F_G = re.compile(rf"\s+F\)\s*(?P<F>{_LIMIT})\s+G\)\s*(?P<G>{_LIMIT})\)?\s*$", re.S)

# Q-code letters 2-3 (subject). Deliberately partial: unknown codes fall through to
# "other" and keep their raw q_code, so nothing is silently mislabelled.
SUBJECT_CATEGORY = {
    "MR": "runway", "MX": "taxiway", "MA": "movement_area", "MN": "apron",
    "FA": "aerodrome", "IC": "ils", "IG": "ils", "IL": "ils",
    "NV": "navaid", "ND": "navaid", "NB": "navaid",
    "LR": "lighting", "LX": "lighting",
    "PF": "flow_control", "PD": "sid", "PA": "star", "PI": "approach",
    "RT": "airspace_restriction", "RR": "airspace_restriction",
    "RD": "airspace_restriction", "RP": "airspace_restriction",
    "WU": "uas", "WM": "firing", "WL": "obstacle_or_light_display",
    "OB": "obstacle", "OA": "ais",
}
# Q-code letters 4-5 (condition).
CONDITION = {
    "LC": "closed", "AS": "unserviceable", "CA": "activated", "LW": "will_take_place",
    "CH": "changed", "AH": "hours_changed", "XX": "see_text", "TT": "trigger",
}


@dataclass
class ParsedNotam:
    number: str | None = None
    notam_type: str | None = None
    replaces: str | None = None
    fir: str | None = None
    q_code: str | None = None
    traffic: str | None = None
    purpose: str | None = None
    scope: str | None = None
    lower_fl: int | None = None
    upper_fl: int | None = None
    centre_lat: float | None = None
    centre_lon: float | None = None
    radius_nm: int | None = None
    location: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    is_permanent: bool | None = None
    is_estimated: bool | None = None
    schedule: str | None = None
    body: str | None = None
    lower_limit: str | None = None
    upper_limit: str | None = None

    @property
    def category(self) -> str | None:
        if not self.q_code:
            return None
        return SUBJECT_CATEGORY.get(self.q_code[1:3], "other")

    @property
    def condition(self) -> str | None:
        if not self.q_code:
            return None
        return CONDITION.get(self.q_code[3:5], "other")

    def as_dict(self) -> dict:
        return asdict(self)


def parse_icao_time(value: str) -> datetime:
    """yymmddhhmm, always UTC."""
    return datetime.strptime(value, "%y%m%d%H%M").replace(tzinfo=timezone.utc)


def _dms(value: str) -> float:
    """'2219N' -> 22.3167, '11355E' -> 113.9167 (degrees + minutes)."""
    hemi = value[-1]
    digits = value[:-1]
    deg, minutes = int(digits[:-2]), int(digits[-2:])
    result = deg + minutes / 60
    return -result if hemi in "SW" else result


def parse(text: str) -> ParsedNotam:
    text = text.replace("\r\n", "\n")
    out = ParsedNotam()

    if m := _HEADER.search(text):
        out.number, out.notam_type, out.replaces = m["number"], m["type"], m["replaces"]

    if m := _Q.search(text):
        out.fir, out.q_code = m["fir"], m["code"]
        out.traffic = m["traffic"].strip() or None
        out.purpose = m["purpose"].strip() or None
        out.scope = m["scope"].strip() or None
        out.lower_fl, out.upper_fl = int(m["lower"]), int(m["upper"])
        out.centre_lat, out.centre_lon = round(_dms(m["lat"]), 4), round(_dms(m["lon"]), 4)
        out.radius_nm = int(m["radius"])

    a = _ITEM_A.search(text)
    if not a:
        return out
    e = _ITEM_E.search(text, a.end())
    if not e:
        return out

    if m := _A_TO_D.match(text[a.end():e.start()].strip()):
        out.location = " ".join(m["A"].split()) or None
        out.starts_at = parse_icao_time(m["B"])
        c = m["C"]
        if c in ("PERM", "UFN"):
            out.is_permanent, out.is_estimated = True, False
        elif c:
            out.is_permanent = False
            out.is_estimated = c.endswith("EST")
            out.ends_at = parse_icao_time(c[:10])
        out.schedule = " ".join(m["D"].split()) if m["D"] else None

    tail = text[e.end():]
    if fg := _F_G.search(tail):
        out.lower_limit, out.upper_limit = fg["F"].strip(), fg["G"].strip()
        tail = tail[: fg.start()]
    body = tail.strip()
    if body.endswith(")"):
        body = body[:-1].rstrip()
    out.body = body or None
    return out
