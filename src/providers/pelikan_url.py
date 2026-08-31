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
page reports "1 Dospělý" / "2 osoby" / "3 osoby" for n = 1/2/3.

Card prices are **per person**, confirmed from the site's own label, which
switches with the passenger count:

    1 passenger  -> "Celková cena pro všechny osoby" (total for all persons)
    2 passengers -> "Průměrná cena na osobu"         (average price per person)

For a single passenger the two are the same number, which is why prices look
identical across passenger counts. Multiply by the party size for a trip total
(`Itinerary.total_for_party`).
"""
from __future__ import annotations

from datetime import date

from ..sources import DEFAULTS, Source

TRIP_ROUND = "1"
TRIP_ONE_WAY = "2"


def _fmt_date(value: date) -> str:
    return f"{value.year}_{value.month}_{value.day}"


# The placeholders `build_search_url` fills in. Named here rather than left
# implicit in the `.format` call below, so a template typed into the Sources tab
# can be checked before it is saved instead of after it has broken a sweep.
TEMPLATE_FIELDS = ("trip_type", "adults", "origin", "destination", "depart")


def check_template(template: str) -> None:
    """Raise ValueError naming what is wrong with a search-URL template.

    A template is edited from the Sources tab, which exists so that a site
    renaming a class can be repaired without touching code. That only holds if a
    bad edit is recoverable: an unknown placeholder used to sail through the
    save and then raise `KeyError` from inside `.format` on every search - a 500
    from the test button with nothing to read, and the next sweep dying the same
    way with no sweep to fall back on.
    """
    try:
        template.format(**dict.fromkeys(TEMPLATE_FIELDS, "X"))
    except KeyError as exc:
        raise ValueError(
            f"the template uses {{{exc.args[0]}}}, which is not one of the values this "
            f"app can fill in ({', '.join('{' + f + '}' for f in TEMPLATE_FIELDS)})"
        ) from exc
    except (IndexError, ValueError) as exc:
        raise ValueError(
            f"the template is not a valid format string ({exc}). A literal brace has "
            f"to be written twice, as {{{{ or }}}}."
        ) from exc


def build_search_url(
    origin: str,
    destination: str,
    depart: date,
    ret: date | None = None,
    adults: int = 1,
    source: Source | None = None,
) -> str:
    """Build a directly navigable pelikan.cz search URL.

    Passing `ret` produces a round-trip search; omitting it produces a one-way.

    `source` supplies the base URL and template, so a site that moves its path
    or renames a parameter can be repaired by editing `data/sources.json`
    instead of this file. Omitting it uses the built-in defaults, which is what
    every caller that predates the file does.
    """
    if ret is not None and ret < depart:
        raise ValueError(f"return date {ret} precedes departure date {depart}")
    if adults < 1:
        raise ValueError(f"adults must be at least 1, got {adults}")

    source = source or DEFAULTS["PELIKAN"]
    path = source.url_template.format(
        trip_type=TRIP_ROUND if ret is not None else TRIP_ONE_WAY,
        adults=adults,
        origin=origin,
        destination=destination,
        depart=_fmt_date(depart),
    )
    if ret is not None:
        path += f",DR:{_fmt_date(ret)}"
    return source.base_url + path + "/"
