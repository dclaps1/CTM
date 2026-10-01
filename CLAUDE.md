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
- South Kansas City: Dan confirmed (2026-10-01) that `WORKIZ_TOKEN_KANSAS_CITY` is the South Kansas City franchise,
  so `MARKET_ENV` maps `KANSAS_CITY` to it. It has 1 "done pending approval" job (same question as Greensboro).
- South Atlanta (CTM market) has no Workiz token. Neither Atlanta token is it (checked 2026-10-01, last 12 months):
  `ATLANTA_MARIETTA_WOODSTOCK` is the NW suburbs (Alpharetta, Marietta, Powder Springs, Canton, Woodstock, Smyrna;
  only 6 of 605 jobs south of the city) and `NORTH_ATLANTA` is the NE suburbs (Alpharetta, Cumming, Gainesville,
  Lawrenceville, Duluth; none south). Do not map either to South Atlanta. Ask Dan whether South Atlanta has its own
  Workiz account (McDonough, Stockbridge, Fayetteville, Peachtree City, Newnan) and get its token.
- NW Arkansas (Workiz-only, not on CTM; token `WORKIZ_TOKEN_NW_ARKANSAS`, connection checked 2026-10-01 and working,
  225 jobs in the last 12 months, 13 team members, 46 leads). Jobs are Fayetteville, Bentonville, Bella Vista, Rogers,
  Springdale and Fort Smith. It has 18 "done pending approval / Payment Pending" jobs in 2026 (same question as
  Greensboro).
- Fort Worth Arlington (Workiz-only, not on CTM; token `WORKIZ_TOKEN_FORT_WORTH_ARLINGTON`, plus a secret). Checked
  2026-10-01: connects and works (24 team members), but the account has gone quiet. 284 jobs all time, Oct 2024 to
  Dec 12 2025; the newest job was created Dec 10 2025 and the newest lead is from Nov 7 2025. Nothing in 2026. Last
  12 months: 48 jobs (32 Oct, 14 Nov, 2 Dec 2025), about $27.8k closed revenue, 6 "done pending approval". Jobs are
  Fort Worth, Arlington, Crowley, North Richland Hills, Benbrook and Weatherford TX. Ask Dan whether this location
  closed, or moved to a different Workiz account. If it moved, it needs a new token.
- Houston Sugar Land (token `WORKIZ_TOKEN_HOUSTON_SUGAR_LAND`, plus a secret). Checked 2026-10-01: Workiz rejects
  the token with HTTP 401, while other markets connect fine with the same code. Dan thinks this location may be
  closed. Come back to it: confirm with Dan whether it is closed; if so, remove its token/secret so it stops
  appearing (it is skipped with a warning today); if not, get a fresh API token from its Workiz admin.
- Charlotte Matthews (Workiz-only, not on CTM; token `WORKIZ_TOKEN_CHARLOTTE_MATTHEWS`, plus a secret). Checked
  2026-10-01: Workiz rejects the token with HTTP 401 on both `team/all` and `job/all`, while Greensboro connects
  fine with the same code at the same time. The token looks well formed (36 chars, `api_` prefix, no stray
  whitespace or quotes), so it is most likely revoked, regenerated, or copied from the wrong account. Get a fresh
  API token from its Workiz admin (Settings → Developer/API). It is skipped with a warning until then.
- `WORKIZ_SECRET_PITSSBURGH` is misspelled (token is `WORKIZ_TOKEN_PITTSBURGH`). Harmless today because the code
  only reads tokens, but rename it to `WORKIZ_SECRET_PITTSBURGH` if secrets are ever used.
- SW Georgia (Workiz-only, not on CTM; token `WORKIZ_TOKEN_SW_GEORGIA`, no secret). Checked 2026-10-01: connects
  and works (12 team members). Last 12 months: 429 jobs (373 Done, 33 Canceled, 16 In progress), about $651k closed
  revenue, newest job created 2026-09-30. Jobs are Leesburg, Albany, Thomasville, Cairo, Bainbridge, Valdosta and
  Americus GA. No "done pending approval" jobs.
