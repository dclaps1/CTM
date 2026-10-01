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
