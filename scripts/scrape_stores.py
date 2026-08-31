#!/usr/bin/env python3
"""Scrape Australia Post store/facility locations into stores.json.

Queries AusPost's public "workcentres" locations API (the same one the
store locator widget on auspost.com.au uses) with a grid of lat/lon points
covering Australia and its territories, paginating each point until all
results are collected, then dedupes by location id and writes a flat
stores.json.

The AUTH-KEY header is a key shipped in auspost.com.au's frontend JS
(visible to anyone who opens devtools on the store locator page), not a
private credential in the usual sense - but it's still supplied via the
AUSPOST_AUTH_KEY env var rather than hardcoded here, so it isn't baked into
source control. AusPost does rotate it occasionally; if the scrape starts
getting 401s, grab a fresh value from a browser network trace of
https://auspost.com.au/find-us and update the AUSPOST_AUTH_KEY secret/env
var. See README.md for details.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from typing import Any

import requests

API_URL = "https://digitalapi.auspost.com.au/locations-private/v3/workcentres"
AUTH_KEY = os.environ.get("AUSPOST_AUTH_KEY")
if not AUTH_KEY:
    sys.exit(
        "AUSPOST_AUTH_KEY env var is not set. Grab the AUTH-KEY header value "
        "from a browser network trace of https://auspost.com.au/find-us "
        "(see README.md) and set it, e.g.:\n"
        "  AUSPOST_AUTH_KEY=... python scripts/scrape_stores.py"
    )

# Location types to keep. Excludes R_SPB/C_SPB (street posting boxes -
# literal street mailboxes, not stores/facilities).
#   PO  = Post Office / LPO
#   UPL = Unattended Parcel Locker
#   OS  = Other site (e.g. PO Box annexe/suite)
#   DC  = Delivery/distribution centre
TYPES = "PO,UPL,OS,DC"

PAGE_SIZE = 100
RADIUS_KM = 250          # per-query search radius (API max is 499)
GRID_STEP_KM = 300       # grid point spacing (< RADIUS_KM * sqrt(2) for full coverage)

# Bounding box covering mainland Australia, Tasmania, and outlying
# territories serviced by Australia Post (Norfolk Island, Lord Howe Island,
# Christmas Island, Cocos (Keeling) Islands).
LAT_MIN, LAT_MAX = -45.0, -9.0
LON_MIN, LON_MAX = 96.0, 170.0

MAX_RETRIES = 5
REQUEST_DELAY_SEC = 0.15
MAX_FAILED_POINTS_RATIO = 0.05

HEADERS = {
    "AUTH-KEY": AUTH_KEY,
    "Accept": "*/*",
    "Content-Type": "application/json",
    "Origin": "https://auspost.com.au",
    "Referer": "https://auspost.com.au/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
    ),
}


def generate_grid() -> list[tuple[float, float]]:
    """Grid points spaced so every point in the bbox is within RADIUS_KM of one."""
    points: list[tuple[float, float]] = []
    lat_step_deg = GRID_STEP_KM / 111.0
    lat = LAT_MIN
    while lat <= LAT_MAX:
        lon_step_deg = GRID_STEP_KM / (111.32 * math.cos(math.radians(lat)))
        lon = LON_MIN
        while lon <= LON_MAX:
            points.append((round(lat, 4), round(lon, 4)))
            lon += lon_step_deg
        lat += lat_step_deg
    return points


def fetch_page(session: requests.Session, lat: float, lon: float, offset: int) -> dict[str, Any]:
    params = {
        "offset": offset,
        "size": PAGE_SIZE,
        "lat": lat,
        "lon": lon,
        "radius": RADIUS_KM,
        "type": TYPES,
    }
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(API_URL, headers=HEADERS, params=params, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(2 ** attempt, 30))
                continue
            resp.raise_for_status()
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"failed to fetch lat={lat} lon={lon} offset={offset}") from last_exc


def parse_hours(capability: dict[str, Any]) -> dict[str, Any] | None:
    periods = (
        capability.get("attributes", {})
        .get("standard_hours", {})
        .get("periods", [])
    )
    if not periods:
        return None
    hours: dict[str, Any] = {}
    for period in periods:
        day = period.get("day")
        open_t = period.get("open")
        close_t = period.get("close")
        if not day:
            continue
        if open_t is None or close_t is None:
            hours[day] = None  # closed
        else:
            hours[day] = {
                "open": f"{open_t['hour']:02d}:{open_t['minute']:02d}",
                "close": f"{close_t['hour']:02d}:{close_t['minute']:02d}",
            }
    return hours or None


def transform(record: dict[str, Any]) -> dict[str, Any]:
    attrs = record.get("attributes", {})
    location = record.get("location", {})
    loc_attrs = location.get("attributes", {})
    addresses = location.get("addresses", [])
    address = next((a for a in addresses if a.get("type") == "STREET"), None) or (
        addresses[0] if addresses else {}
    )
    addr_attrs = address.get("attributes", {})

    trading_hours = None
    services = []
    for cap in attrs.get("operational_capabilities", []):
        cap_id = cap.get("id")
        if cap_id == "ACCESS_HOURS":
            trading_hours = parse_hours(cap)
        elif cap.get("attributes", {}).get("service_offering"):
            services.append(cap_id)

    return {
        "id": record.get("id"),
        "type": record.get("type"),
        "name": attrs.get("business_long_name") or attrs.get("business_name"),
        "phone": attrs.get("phone_number"),
        "fax": attrs.get("fax_number"),
        "address": {
            "line1": addr_attrs.get("address_line_1"),
            "line2": addr_attrs.get("address_line_2"),
            "suburb": addr_attrs.get("suburb"),
            "state": addr_attrs.get("state"),
            "postcode": addr_attrs.get("postcode"),
            "country": addr_attrs.get("country_name"),
        },
        "lat": loc_attrs.get("lat"),
        "lon": loc_attrs.get("lon"),
        "trading_hours": trading_hours,
        "services": sorted(services),
    }


def scrape() -> list[dict[str, Any]]:
    grid = generate_grid()
    session = requests.Session()
    results: dict[str, dict[str, Any]] = {}
    failed_points = 0

    print(f"Scanning {len(grid)} grid points (radius={RADIUS_KM}km, step={GRID_STEP_KM}km)...")

    for i, (lat, lon) in enumerate(grid, 1):
        try:
            offset = 0
            total = None
            while total is None or offset < total:
                data = fetch_page(session, lat, lon, offset)
                total = data.get("pagination", {}).get("total", 0)
                for record in data.get("workcentres", []):
                    rid = record.get("id")
                    if rid and rid not in results:
                        results[rid] = transform(record)
                offset += PAGE_SIZE
                time.sleep(REQUEST_DELAY_SEC)
        except RuntimeError as exc:
            failed_points += 1
            print(f"  WARN: {exc}", file=sys.stderr)

        if i % 25 == 0 or i == len(grid):
            print(f"  [{i}/{len(grid)}] grid points scanned, {len(results)} unique locations so far")

    if grid and failed_points / len(grid) > MAX_FAILED_POINTS_RATIO:
        raise RuntimeError(
            f"too many failed grid points ({failed_points}/{len(grid)}); aborting without writing output"
        )

    return sorted(
        results.values(),
        key=lambda r: (r["address"]["state"] or "", r["address"]["postcode"] or "", r["name"] or ""),
    )


def main() -> None:
    stores = scrape()

    by_type: dict[str, int] = {}
    for s in stores:
        by_type[s["type"]] = by_type.get(s["type"], 0) + 1

    output = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": API_URL,
        "count": len(stores),
        "count_by_type": by_type,
        "stores": stores,
    }

    out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "stores.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Wrote {len(stores)} locations to {out_path}")
    print("By type:", by_type)


if __name__ == "__main__":
    main()
