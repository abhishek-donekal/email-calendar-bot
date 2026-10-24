# Email-to-Calendar Scheduler Bot

Reads your unread Gmail, uses Gemini (`gemini-3.8-flash` by default; set `GEMINI_MODEL` to change it) to pull out any meeting or event,
and adds it to your primary Google Calendar. Runs locally or headless on GitHub Actions once a day.

> **Heads up:** every email the bot processes is **marked as read**, whether or not it contained
> an event, so it isn't processed twice. Try `--dry-run` first to see what it would do.

## How it works

1. Search Gmail for `is:unread -label:Scheduled` (up to 25 messages per run; change with `--max` or `MAX_EMAILS`).
2. For each one, read Subject, From, and the `text/plain` body (or the snippet if there isn't one).
3. Send them to Gemini with the current date and time, so "tomorrow" or "next Tuesday" resolve correctly,
   and get back structured JSON (`CalendarEventExtraction`).
4. If there's an event with a valid start time, insert it into the `primary` calendar and log the link.
   If no end time is given, the event lasts 1 hour.
5. Remove `UNREAD`, and add the `Scheduled` label if you've created one in Gmail.
   If Gemini or the API fails on a message, the message is **left unread** and retried on the next run.

## Configuration

| Variable | Required | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | yes | From [Google AI Studio](https://aistudio.google.com/apikey) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REFRESH_TOKEN` | CI only | Headless OAuth (see step 3) |
| `TIMEZONE` | recommended | IANA name, e.g. `America/New_York`. Defaults to `UTC`, so "3pm" means 3pm UTC on GitHub runners. |
| `MAX_EMAILS` | no | Cap per run (default 25) |
| `GEMINI_MODEL` | no | Gemini model name (default `gemini-3.8-flash`) |

Locally, put these in a `.env` file (it's gitignored):

```
GEMINI_API_KEY=your-key
TIMEZONE=America/New_York
```

---

## 1. Get `credentials.json` from Google Cloud Console

1. Go to <https://console.cloud.google.com/> and create (or select) a project.
2. **APIs & Services → Library**: enable the **Gmail API** and the **Google Calendar API**.
3. **APIs & Services → OAuth consent screen** (Google Auth Platform → Branding / Audience):
   - User type **External** (or Internal if you're on Workspace), and fill in the app name and your email.
   - Under **Audience → Test users**, add the Gmail address the bot will read.
   - Recommended: **Publish app** (set it to *In production*). While the app is in *Testing*, Google
     expires refresh tokens after **7 days** and the GitHub run will start failing with `invalid_grant`.
     A personal, unverified app works in production; you'll just click through an "unverified app" warning once.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**, type **Desktop app**.
5. Download the JSON and save it as `credentials.json` in this folder.

## 2. Run once locally to generate `token.json`

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python scheduler_bot.py --dry-run  # opens a browser for Google sign-in
```

Approve both scopes (Gmail modify, Calendar events). That writes `token.json`, and `--dry-run` logs
what would be scheduled without changing anything. When the output looks right, run it for real:

```bash
python scheduler_bot.py
```

Optional: create a Gmail label named exactly **`Scheduled`** to have processed mail tagged.

## 3. Copy secrets from `token.json` into GitHub

`token.json` looks like this:

```json
{"token": "...", "refresh_token": "1//0g...", "client_id": "123-abc.apps.googleusercontent.com",
 "client_secret": "GOCSPX-...", "token_uri": "...", "scopes": [...], "expiry": "..."}
```

In your repo go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret name | Value from |
|---|---|
| `GOOGLE_REFRESH_TOKEN` | `refresh_token` in `token.json` |
| `GOOGLE_CLIENT_ID` | `client_id` in `token.json` |
| `GOOGLE_CLIENT_SECRET` | `client_secret` in `token.json` |
| `GEMINI_API_KEY` | your Gemini API key |

You don't need `token`, because it's a short-lived access token that the bot refreshes itself.
Then, on the **Variables** tab, add a repository variable `TIMEZONE` (e.g. `America/New_York`).

Never commit `credentials.json`, `token.json`, or `.env`. They're already in `.gitignore`.

## 4. Test the workflow manually on GitHub Actions

1. Push this repo to GitHub.
2. Open the **Actions** tab → **Daily email-to-calendar** → **Run workflow**.
3. Tick **dry_run** for a safe first run, then click **Run workflow**.
4. Open the run → `run` job → **Run scheduler bot** step to see the log (events found, calendar links).

After that it runs on its own every day at **11:00 UTC** (`.github/workflows/daily_schedule.yml`).
Note that GitHub turns off scheduled workflows in repos with no activity for 60 days, and you can
turn them back on from the Actions tab.

## Troubleshooting

- **`invalid_grant`**: the refresh token expired or was revoked (often because of the 7-day *Testing* limit).
  Delete `token.json`, run locally again, and update `GOOGLE_REFRESH_TOKEN`.
- **Events at the wrong hour**: set `TIMEZONE`.
- **`GEMINI_API_KEY is not set`**: add it to `.env` locally, or as a repo secret for Actions.
