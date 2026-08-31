# stores-auspost

Scrapes Australia Post's public location-finder API into a single
[`stores.json`](./stores.json), kept up to date by a daily GitHub Actions
run.

## What's in stores.json

Every Post Office/LPO, Parcel Locker, PO Box annexe, and Delivery Centre
in Australia (street posting boxes are deliberately excluded — they're
mailboxes, not stores). Each entry looks like:

```json
{
  "id": "243184_PO",
  "type": "PO",
  "name": "QVB Market Street Post Office",
  "phone": "13 13 18",
  "fax": "(02)8303 6773",
  "address": {
    "line1": "Lg",
    "line2": "44 Market Street",
    "suburb": "SYDNEY",
    "state": "NSW",
    "postcode": "2000",
    "country": "Australia"
  },
  "lat": -33.87073396,
  "lon": 151.20587674,
  "trading_hours": { "MON": { "open": "09:00", "close": "17:00" }, "...": "..." },
  "services": ["BANK_AT_POST", "PARCEL_COLLECT", "..."]
}
```

`type` is one of:

- `PO` — Post Office / LPO
- `UPL` — Unattended Parcel Locker
- `OS` — Other site (e.g. PO Box annexe/suite)
- `DC` — Delivery/distribution centre

## How the scrape works

AusPost's store locator (`auspost.com.au/find-us`) calls a "workcentres"
locations API that only supports **radius search around a lat/lon** — there's
no "list everything" endpoint. `scripts/scrape_stores.py` works around that
by:

1. Laying a grid of points (~300km apart) over Australia and its outlying
   territories (Norfolk Island, Lord Howe Island, Christmas Island, Cocos
   Islands).
2. Querying each grid point with a 250km radius (paginating with
   `offset`/`size` until all results for that point are collected — the API
   caps `size` at 100 per page and `radius` at under 500km).
3. Deduping results by location id across overlapping grid points.
4. Flattening each record into the schema above and writing `stores.json`.

A full run is ~340 grid points / ~5,700 unique locations and takes about
7 minutes.

## The API key

The `AUTH-KEY` header is a key embedded in auspost.com.au's frontend
JavaScript — visible to anyone with devtools open on the store locator page,
not a private credential. It's hardcoded as a fallback default in the
script. AusPost does rotate it occasionally; if the scrape starts getting
401s:

1. Open `https://auspost.com.au/find-us` in a browser with devtools open,
   search for a location, and find the `AUTH-KEY` header on the
   `workcentres` request.
2. Either update `DEFAULT_AUTH_KEY` in `scripts/scrape_stores.py`, or set it
   as the `AUSPOST_AUTH_KEY` repo secret (used by the workflow, takes
   priority over the hardcoded default) so no code change is needed.

## Running locally

```bash
pip install -r requirements.txt
python scripts/scrape_stores.py
```

## GitHub Actions

[`.github/workflows/scrape.yml`](./.github/workflows/scrape.yml) runs the
scraper daily (03:17 UTC) and on manual dispatch, committing `stores.json`
back to the repo only when it changed. It needs no secrets to run, but you
can set `AUSPOST_AUTH_KEY` if AusPost rotates the key and you'd rather not
edit the script.
