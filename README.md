# Campaign Engine

A complete Python email campaign automation system for AP ONLINE JOBS.

## Quick start

1. Install dependencies:

```bash
python -m venv venv
venv\Scripts\activate  # Windows
# source venv/bin/activate  # macOS/Linux
pip install -r requirements.txt
```

2. Configure environment:

```bash
cp .env.example .env
```

Edit `.env` and add your `OPENAI_API_KEY`. Multi-mailbox OAuth sending is configured in `config.py`.

3. Set up Gmail OAuth for the 3 mailboxes:

- Create a Google Cloud project and enable the **Gmail API**.
- Download one OAuth Desktop app `credentials.json` per mailbox:
  - `credentials_info.json` for `info@iprosedutech.com.my`
  - `credentials_contact.json` for `contact@iprosedutech.com.my`
  - `credentials_ipros.json` for `ipros@iprosedutech.com.my`
- Run the OAuth flow once per mailbox to generate token files:

```bash
python main.py auth-mailboxes
```

This creates `token_info.json`, `token_contact.json`, and `token_ipros.json`.

- On Railway/deployment, upload the `token_*.json` files alongside the code.
- Test the setup: `python main.py test-send you@example.com`

4. Import leads:

```bash
python main.py import leads.csv
```

5. Enrich leads (website scraping + OpenAI classification):

```bash
python main.py enrich
```

6. Preview generated emails:

```bash
python main.py preview --n 10
```

7. Run the scheduler + sender daemon:

```bash
python main.py run
```

Press `Ctrl+C` to stop.

8. View stats:

```bash
python main.py stats
```

If the campaign auto-pauses due to bounce rate, fix the issue and then:

```bash
python main.py reset-pause
```

## CSV format

The CSV can use flexible column names. These are recognised automatically:

- `company_name` / `Company` / `Name of Company`
- `email` / `E-mail` / `Email Address`
- `website` / `Website` / `URL`
- `facebook`, `instagram`, `linkedin`, `phone`
- `industry`, `location`

## Configuration

Edit `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | — | Required for enrichment and email generation |
| `FROM_ALIAS` | primary Gmail address | Send-as alias to use |
| `DAILY_CAP` | 20 | Max sends per day |
| `MIN_GAP_SECONDS` | 90 | Minimum gap between any two sends |

## Notes

- Sending now uses the Gmail API with OAuth credentials. The active mailbox pool, warmup ramp, and daily caps are defined in `config.py` and `mailboxes.py`.
- The sender rotates round-robin to the active mailbox with the fewest sends today that is under its warmup-adjusted daily cap.
- `do_not_email.csv` is created automatically and stores unsubscribed/bounced email addresses.
- All runtime data is stored in `campaign.db` and logs are written to `campaign.log`.

## Dashboard

Run locally:

```bash
python main.py dashboard
```

Open `http://127.0.0.1:8000`. Tabs:

- **Overview** - stats, trend chart, funnel, follow-up sequence counts, recent activity.
- **Companies** - searchable/filterable list of every lead with a **Send now** button per row (sends the next due email - initial or follow-up - immediately).
- **Follow-ups** - leads awaiting their next follow-up with computed due dates, plus anything already queued by the scheduler.
- **Replies** - inbox of replies/unsubscribes, with a "Mark customer" action.

### One-off batch sends

```bash
python send_batch.py prospect_research\enriched_100.csv --limit 10
```

This records every send in `campaign.db` (so it shows up on the dashboard and the normal 3/7/14-day follow-up schedule applies automatically). Already-contacted emails are skipped on subsequent runs unless `--force` is passed.

## Deployment (Railway recommended)

This app needs a **persistent process + writable disk** (SQLite file, log file, IMAP polling) - that fits **Railway** well. **Vercel is not recommended**: its serverless functions are stateless/ephemeral, so `campaign.db` and any background sender loop would not persist between requests.

Railway setup:

1. Push this folder to a GitHub repo and create a new Railway project from it (or `railway init` + `railway up`).
2. Set all `.env` variables as Railway environment variables (`OPENAI_API_KEY`, `SMTP_USER`, `SMTP_PASSWORD`, `FROM_ALIAS`, `FROM_DISPLAY_NAME`, etc.). Railway automatically injects `PORT`, which `config.py` already respects.
3. Add a **volume** mounted at the project directory (or at least covering `campaign.db`, `campaign.log`, `do_not_email.csv`) so data survives redeploys.
4. This repo includes a `Procfile` with two process types:
   - `web` - runs the dashboard (`dashboard.py`) on Railway's assigned `$PORT`.
   - `worker` - runs the always-on scheduler/sender loop (`python main.py run`).
   Enable both as separate Railway services pointing at the same repo/image.
5. Upload `AP_ONLINE_JOBS_COMPANY_PROFILE.pdf` alongside the code (or via the volume) so it's attached to outbound emails.
