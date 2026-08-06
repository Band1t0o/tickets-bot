"""Airport catalogue with measured viability.

Every entry here was checked live against pelikan.cz on a sample January 2027
date: a one-way to NRT and a one-way back from MNL. Airports that returned no
inventory are kept in the list rather than deleted, so the finding is visible in
the UI and nobody re-litigates Brno in six months.

Combined figures are leg A minimum + leg C minimum, 1 adult, CZK.
"""
from __future__ import annotations

EUROPE = [
    {"iata": "PRG", "city": "Prague", "available": True, "default": True,
     "note": "30,735 combined — home base"},
    {"iata": "VIE", "city": "Vienna", "available": True, "default": True,
     "note": "23,624 combined — cheapest measured"},
    {"iata": "FRA", "city": "Frankfurt", "available": True, "default": True,
     "note": "27,154 combined — 45% under Prague on a live run"},
    {"iata": "BER", "city": "Berlin", "available": True, "default": False,
     "note": "31,054 combined — 319 worse than Prague"},
    {"iata": "MUC", "city": "Munich", "available": True, "default": False,
     "note": "31,795 combined — 1,060 worse than Prague"},
    {"iata": "KRK", "city": "Krakow", "available": True, "default": False,
     "note": "33,828 combined — 3,093 worse than Prague"},
    {"iata": "KTW", "city": "Katowice", "available": True, "default": False,
     "note": "38,173 combined — 7,438 worse than Prague"},
    {"iata": "BTS", "city": "Bratislava", "available": False, "default": False,
     "note": "No return inventory, and 78% over Prague outbound"},
    {"iata": "BRQ", "city": "Brno", "available": False, "default": False,
     "note": "No long-haul inventory in either direction"},
]

JAPAN = [
    {"iata": "NRT", "city": "Tokyo Narita", "available": True, "default": True, "note": ""},
    {"iata": "HND", "city": "Tokyo Haneda", "available": True, "default": True, "note": ""},
    {"iata": "KIX", "city": "Osaka Kansai", "available": True, "default": True, "note": ""},
]

PHILIPPINES = [
    {"iata": "MNL", "city": "Manila", "available": True, "default": True,
     "note": "NRT→MNL from 3,863"},
    {"iata": "CEB", "city": "Cebu", "available": True, "default": True,
     "note": "KIX→CEB from 4,669"},
]


def catalogue() -> dict:
    return {"europe": EUROPE, "japan": JAPAN, "philippines": PHILIPPINES}
