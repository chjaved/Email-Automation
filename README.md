# Atlas Professional Bookkeeping Campaign Engine

Email outreach automation for Atlas Professional Bookkeeping in Ireland.

## Quick start

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Configure the environment, then authenticate the Atlas Gmail mailbox:

```bash
python main.py auth-mailboxes
python main.py test-send you@example.com
```

The active mailbox is `admin@atlasprobookkeeping.ie`, configured in `config.py` with `credentials_atlas.json` and `token_atlas.json`.

## Leads and campaign

Import the Irish company lead CSV for the campaign owner:

```bash
python main.py import leads.csv
```

Supported columns include:

- `Company Name`
- `Email`
- `Website`
- `Partial Address`
- `Contacts`
- `Industry` or `Category`, when available

Enrich websites, preview messages, and run the campaign:

```bash
python main.py enrich
python main.py preview --n 10
python main.py run
```

The campaign sends the Atlas bookkeeping initial message followed by two follow-ups after 3 and 7 days. Automatic campaign sending is limited to Monday-Friday, 09:00-17:00 Europe/Dublin. Per-user limits and spacing are configured in the dashboard settings; the Atlas mailbox cap is 80 messages per day.

## Dashboard

```bash
python main.py dashboard
```

Open `http://127.0.0.1:8000`.

- **Overview** — campaign statistics, mailbox use, trends, and recent activity.
- **Companies** — import, search, review, and manage Irish company leads.
- **Follow-ups** — upcoming and queued follow-ups.
- **Replies** — reply and unsubscribe monitoring.
- **Settings** — Atlas sender identity, signature, campaign limits, and spacing.

## Configuration

Important environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | — | Website enrichment and personalization |
| `DATABASE_URL` | local SQLite | Shared production database connection |
| `SECRET_KEY` | random local value | Session signing and encrypted settings |
| `FOLLOWUP_SCHEDULE` | `3,7` | Follow-up delays in days |
| `TIMEZONE` | `Europe/Dublin` | Application timezone |

Gmail sending uses OAuth. Do not commit OAuth tokens, API keys, passwords, or production lead data.

## Deployment

The web dashboard and campaign worker must use the same persistent database. For Railway, set the same `DATABASE_URL`, `SECRET_KEY`, Gmail OAuth configuration, and campaign environment variables on both services.

The included `Procfile` defines:

- `web` — Atlas dashboard
- `worker` — campaign sender loop

After deploying code changes, import the qualified leads into the production database while authenticated as the production campaign owner. A local `campaign.db` is not automatically copied into Railway or another hosted database.
