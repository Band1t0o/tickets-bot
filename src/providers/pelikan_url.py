"""Deep-link URL construction for pelikan.cz.

Navigating straight to a search URL skips the entire form-driving sequence
(cookie banner, autocomplete typing, walking the Czech-month calendar). A search
that took ~150s through the form returns results in 10-15s this way.

Grammar, derived by observing the URL the site produces after a form search and
confirmed by sweeping variants:

    /cs/letenky/T:{type},P:{adults}000E_0_0,CDF:{FROM}{FROM},CDT:A{TO},DD:{y}_{m}_{d}[,DR:{y}_{m}_{d}]/

    T:1  round trip (requires DR)
    T:2  one-way
    T:0  returns no results - never generate
    R:0  returns no results - never generate

Dates are bare integers, not zero-padded. The origin code is repeated in CDF
while the destination is prefixed with "A" in CDT; the asymmetry is the site's,
not ours.

Passenger count: verified live that `P:{n}000E_0_0` is honoured - the results
page reports "1 Dospělý" / "2 osoby" / "3 osoby" for n = 1/2/3. Prices in the
result cards were byte-identical across all three, so **the displayed price is
per person, not a trip total**. Anything summing legs must multiply by the
passenger count.
"""
from __future__ import annotations

from datetime import date

BASE = "https://www.pelikan.cz/cs/letenky/"

TRIP_ROUND = "1"
TRIP_ONE_WAY = "2"


def _fmt_date(value: date) -> str:
    return f"{value.year}_{value.month}_{value.day}"


def build_search_url(
    origin: str,
    destination: str,
    depart: date,
    ret: date | None = None,
    adults: int = 1,
) -> str:
    """Build a directly navigable pelikan.cz search URL.

    Passing `ret` produces a round-trip search; omitting it produces a one-way.
    """
    if ret is not None and ret < depart:
        raise ValueError(f"return date {ret} precedes departure date {depart}")
    if adults < 1:
        raise ValueError(f"adults must be at least 1, got {adults}")

    trip_type = TRIP_ROUND if ret is not None else TRIP_ONE_WAY
    parts = [
        f"T:{trip_type}",
        f"P:{adults}000E_0_0",
        f"CDF:{origin}{origin}",
        f"CDT:A{destination}",
        f"DD:{_fmt_date(depart)}",
    ]
    if ret is not None:
        parts.append(f"DR:{_fmt_date(ret)}")
    return BASE + ",".join(parts) + "/"
