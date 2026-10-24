# Privacy Policy — Email-to-Calendar Scheduler Bot

_Last updated: October 24, 2026_

This is a personal automation tool run by its owner on their own Google account. It is not offered to the public.

**What it accesses**
- **Gmail** (`gmail.modify`): reads unread messages (subject, sender, plain-text body) and marks them read / applies a `Scheduled` label.
- **Google Calendar** (`calendar.events`): creates events on the owner's primary calendar.

**How data is used**
- Email subject, sender and body are sent to the Google Gemini API solely to detect and extract event details (title, time, location, summary).
- Extracted events are written to the owner's Google Calendar. Nothing else is done with the data.

**Storage and sharing**
- The bot stores no email content. OAuth tokens are kept only in the owner's local `token.json` and in encrypted GitHub Actions secrets.
- No data is sold, shared with third parties, or used for advertising. The only external service involved is the Gemini API.
- Use of information received from Google APIs adheres to the [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy), including the Limited Use requirements.

**Revoking access**
Remove the app at https://myaccount.google.com/permissions at any time.

**Contact**
Open an issue on this repository.
