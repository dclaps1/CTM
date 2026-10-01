# CTM Call Center Hub

A small web app that pulls call data and recordings out of **CallTrackingMetrics (CTM)** and gives
call center **managers** and **agents** reporting and coaching tools in one place.

## What it does

| For managers | For agents |
|---|---|
| **Team dashboard:** total calls, inbound answer rate, missed calls, voicemail, average talk time, average time to answer, new callers, conversions and revenue, average QA score | **My dashboard:** the same KPIs for their own calls only |
| Calls by day (answered / missed / outbound) and **inbound calls by hour** for staffing | Feedback waiting for them, with an **Acknowledge** button |
| **Agent leaderboard:** calls, in/out, answered, average talk, time to answer, conversions, QA average | **My feedback:** every QA review of their calls |
| **Call log** with filters (date, agent, direction, outcome, source, search by name, number or notes), **CSV export** | Call log and recordings for **their own calls only** |
| **Recording playback** in the browser, plus transcript and notes when CTM has them | |
| **QA scorecard** with 6 items scored 0–5 or N/A, plus written coaching feedback. The score can optionally be written back to CTM | |
| **Missed-call callback queue:** missed inbound calls with no answered return call or outbound callback yet, oldest open first | Same queue, so agents can work callbacks |
| Admin page: sync status, manual re-sync, team accounts, linking each login to a CTM agent | |

### How data gets in

1. **Scheduled sync (default):** a background worker calls the CTM REST API every
   `SYNC_INTERVAL_MINUTES` minutes. The first run backfills `INITIAL_SYNC_DAYS` days of calls.
   Later runs are incremental and re-pull the last 2 days, so late changes in CTM
   (notes, tags, scores, transcripts) also come through. Agents are read from CTM users and from the calls themselves.
2. **Webhook (optional, near real-time):** set `WEBHOOK_TOKEN` and point a CTM trigger or webhook at
   `https://<your-host>/webhooks/ctm?token=<WEBHOOK_TOKEN>`. The app uses the webhook only as a signal:
   it takes the call id and fetches the full call record from the API.

Recordings are **streamed through the app** and never linked to directly. So:
- CTM API keys never reach the browser.
- Agents can only play recordings of their own calls.
- The app only fetches recordings from `*.calltrackingmetrics.com` over HTTPS, so a bad URL in the call data can't make the server send your CTM credentials to another host.

Texts, form fills and chats in the CTM activity feed are skipped. Only voice calls are stored.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env         # then fill in CTM_ACCESS_KEY, CTM_SECRET_KEY, CTM_ACCOUNT_ID, SECRET_KEY
uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8000
```

Then:

1. Open http://localhost:8000. The first visit asks you to create the **admin** account.
2. Open **Admin**. The first sync starts automatically in the background, or click **Sync now**.
3. After the sync, CTM agents show up in the Admin page. Add a login for each manager and agent, and
   **link each agent login to their CTM agent**. That link is how the app shows agents their own calls.

### Getting your CTM API keys

In CTM, go to **Settings → Account Settings → API Integration** (you need account admin access). Create an API
key, which comes as an *access key* and a *secret key*. Your **account id** is the number in the CTM URL and on the API page.

### Command line

```bash
python -m app.cli sync               # incremental sync
python -m app.cli sync --days 90     # re-pull the last 90 days
python -m app.cli agents             # list CTM agent ids
python -m app.cli create-user pat@example.com --name "Pat" --role manager
python -m app.cli create-user sam@example.com --name "Sam" --role agent --agent-id 12345
python -m app.cli brief                                  # yesterday's Daily Call Center Brief (Markdown)
python -m app.cli brief --date 2026-09-30 --html brief.html --json brief.json
```

### Daily Call Center Brief

`python -m app.cli brief` reads CTM directly (no database) and builds a management brief for one day:

- **Scorecard** against targets, with the prior day and the 7-day average: answered live, web leads called in
  5 minutes, opportunity conversion, jobs booked, open quotes followed up within 24 hours.
- **Missed calls called back within 60 seconds**, and **calls handled** (answered live + called back within 60 sec).
- **Action list** with an owner on each item: missed callers never reached, open quotes with no follow-up,
  and the prior day's quotes that still have none.
- **Follow-through** on the prior day's list, **alerts** (weak hours, slow web leads, markets with no bookings,
  dialer calling spam), and **agent** and **market** tables.

All definitions live in `app/brief.py`, so the numbers are the same every run. It pulls 90 days of CTM activity
(calls, texts, forms and chats) to tell new leads from repeat contacts. Targets are in `Targets` in the same file.
Use `--notes file.txt` to add commentary under the title.

## Configuration

All settings are environment variables. `.env` is read automatically. See [`.env.example`](.env.example).

| Variable | Default | Notes |
|---|---|---|
| `CTM_ACCESS_KEY`, `CTM_SECRET_KEY`, `CTM_ACCOUNT_ID` | — | Required for sync and recordings |
| `DATABASE_URL` | `sqlite:///./ctm.db` | Use Postgres for larger teams or multi-instance hosting |
| `SECRET_KEY` | `change-me` | **Set this.** It signs login cookies |
| `SESSION_HTTPS_ONLY` | `false` | Set `true` behind HTTPS |
| `TIMEZONE` | `America/New_York` | Date filters, charts and times are shown in this zone |
| `SYNC_INTERVAL_MINUTES` | `10` | `0` turns off the built-in worker (then run `python -m app.cli sync` from cron) |
| `INITIAL_SYNC_DAYS` | `30` | Backfill window for the first sync |
| `WEBHOOK_TOKEN` | empty | Turns on `POST /webhooks/ctm` |
| `PUSH_SCORES_TO_CTM` | `false` | If `true`, a QA review can also set the call's 1–5 score in CTM (sale record). The call's existing conversion and value are kept |

## Roles

- **admin:** everything, plus the Admin page (users, sync)
- **manager:** all calls, all agents, dashboards, QA reviews
- **agent:** only calls handled by their linked CTM agent, their own stats and feedback, and the shared callback queue

## Deploying

- Run a **single app process** when the built-in sync worker is on. With several workers or instances,
  set `SYNC_INTERVAL_MINUTES=0` and run `python -m app.cli sync` on a schedule instead.
- Put it behind HTTPS and set `SESSION_HTTPS_ONLY=true` and a strong `SECRET_KEY`.
- Call recordings can contain sensitive customer information. Keep the app on an access-controlled host.

## Project layout

```
app/
  ctm_client.py   CTM REST API client (Basic auth, pagination, retry/backoff, recordings, sale scores)
  sync.py         CTM → database mapping, incremental sync, background worker
  metrics.py      filters, KPIs, leaderboard, missed-call callback matching
  scorecard.py    QA scorecard items and scoring
  brief.py        Daily Call Center Brief: definitions, KPIs, action lists, alerts (raw CTM activity, no DB)
  brief_render.py brief → email-safe HTML or Markdown
  main.py         web routes (dashboard, calls, recordings, reviews, callbacks, admin, webhook)
  auth.py         password hashing and role checks
  cli.py          sync / create-user / agents / brief commands
  templates/      Jinja pages
  static/         CSS and charts
tests/            API client, sync, metrics and web tests (CTM is mocked)
```

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

To change the QA scorecard items, edit `CRITERIA` in `app/scorecard.py`.

## Notes and next steps

- The CTM field mapping in `sync.normalize_call` follows the v1 `calls.json` response (`dial_status`,
  `talk_time`, `ring_time`, `agent`, `tag_list`, `sale`, `audio`, `unix_time`, …). Each call's raw JSON is also
  stored in `calls.raw`, so new reports can use any other CTM field.
- Possible next steps: AI call summaries and auto-scoring from transcripts, scheduled email reports for managers, SLA alerts
  when the missed-call queue gets too old.
