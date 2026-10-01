# Notes for Claude

## Locations

- Only 10 Voda locations use CTM (the call center) today. They are the 10 markets in `MARKETS` in
  `app/brief.py`. West Boston is one of them. It is probably the market this code calls "Greater Boston"
  (keyword "boston"); not yet confirmed.
- Every location is being added to Workiz (`WORKIZ_TOKEN_<LOCATION>`), including ones not on CTM.
  Workiz-only locations get the Jobs & Revenue numbers but no CTM matching (marked "not on CTM").
- Dan (CEO) will confirm the names of the other 9 CTM locations; update `MARKETS` and
  `MARKET_ENV` in `app/workiz_client.py` to match.

## Conventions

- Never print Workiz or CTM tokens. Workiz puts the token in the URL path, so never log request URLs.
- Run `pytest` and `ruff check app tests` before committing.

## Open questions

- Greensboro (Workiz-only, not on CTM; token added 2026-10-01, connection checked and working). Its Workiz has a
  "done pending approval" status (11 jobs in 2026 so far). `app/jobs.py` counts any status starting with "done"
  as sold once it has revenue, so these count as closed today. Ask Dan whether they should wait until approved.
- Kansas City (token added as `WORKIZ_TOKEN_KANSAS_CITY`; connection checked 2026-10-01 and working, 219 jobs in the
  last 12 months). The code expects `SOUTH_KANSAS_CITY` for the CTM market "South Kansas City", so today it shows up
  as a separate Workiz-only "Kansas City" with no CTM matching. Its jobs are mostly Kansas City MO, Independence,
  Blue Springs, Liberty, Kearney and Leavenworth KS (north/east, not south). Ask Dan whether it is the South Kansas
  City franchise: if yes, map `KANSAS_CITY` to "South Kansas City" in `MARKET_ENV` (or rename the token); if no, keep
  it separate and South Kansas City still needs its own token. It also has 1 "done pending approval" job (same
  question as Greensboro).
- NW Arkansas (Workiz-only, not on CTM; token `WORKIZ_TOKEN_NW_ARKANSAS`, connection checked 2026-10-01 and working,
  225 jobs in the last 12 months, 13 team members, 46 leads). Jobs are Fayetteville, Bentonville, Bella Vista, Rogers,
  Springdale and Fort Smith. It has 18 "done pending approval / Payment Pending" jobs in 2026 (same question as
  Greensboro).
- Houston Sugar Land (token `WORKIZ_TOKEN_HOUSTON_SUGAR_LAND`, plus a secret). Checked 2026-10-01: Workiz rejects
  the token with HTTP 401, while other markets connect fine with the same code. Dan thinks this location may be
  closed. Come back to it: confirm with Dan whether it is closed; if so, remove its token/secret so it stops
  appearing (it is skipped with a warning today); if not, get a fresh API token from its Workiz admin.
