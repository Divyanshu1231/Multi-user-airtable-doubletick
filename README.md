# Space Consultant Multi-User WhatsApp Recruitment System

FastAPI application with a bundled frontend (`static/index.html`), Airtable candidate sources, per-user HR access, admin settings, DoubleTick template management and message history.

## Included features
- Candidate filters correctly send the selected specialization, state and city to the API; dropdown options remain available after filtering.
- HR candidate list and HR history do not expose candidate phone numbers.
- Admin can configure multiple Airtable view URLs (one URL per line).
- Admin can configure the daily WhatsApp message-attempt cap per HR user (default: 50); the backend enforces it. Failed attempts count toward the cap. Admin is exempt.
- Admin can create/manage HR users.
- DoubleTick approved-template workflow and send integration.
- `/health` endpoint for Render health checks.
- Single Render web service serves both API and frontend.

## Deploy to Render
1. Push the contents of this folder to a GitHub repository (the files `main.py`, `matcher.py`, `requirements.txt`, `render.yaml`, `runtime.txt`, `Procfile`, and `static/` must be at the repository root).
2. In Render, choose **New + → Blueprint** and connect that repository. Render reads `render.yaml`.
3. Set the secret values in the Render service Environment page:
   - `ADMIN_PASSWORD`: strong admin password (required).
   - `AIRTABLE_TOKEN`: Airtable personal access token with read access to the relevant bases (needed for private Airtable views).
   - `DOUBLETICK_API_KEY` and `DOUBLETICK_WABA_NUMBER`: needed for DoubleTick template submit/status functions.
   - `DOUBLETICK_PROXY_URL`, `DOUBLETICK_PROXY_TOKEN`, `DOUBLETICK_SENDER_NUMBER`: needed by the existing DoubleTick sending proxy integration.
   - `DATABASE_URL` is linked automatically by the included Render Blueprint; do not replace it with the SQLite `DATABASE_PATH`.
4. Deploy. Open the Render service URL. Check `https://YOUR-SERVICE.onrender.com/health`; expected response is `{"ok":true}`.
5. Wait until both the web service and PostgreSQL database are available, then sign in with the configured admin username/password. Open Airtable Settings and save one Airtable URL per line, then create HR users and configure the daily limit.

## PostgreSQL database connection
The project now supports PostgreSQL. The Render Blueprint creates a PostgreSQL database and automatically injects its connection string into `DATABASE_URL`. When `DATABASE_URL` is set, the app uses PostgreSQL; when it is empty, local development uses SQLite. Tables are created automatically on startup/first database access.

**Important Free-plan limitation:** Render Free Postgres expires 30 days after creation. It then becomes inaccessible; there is a 14-day grace period to upgrade before Render deletes the database and its data. Free Postgres is limited to 1 GB and has no backups. Upgrade the database to a paid plan before expiry if you need to keep the data. See https://render.com/docs/free.

If you already created a separate Render Postgres database, remove the `databases:` block from `render.yaml` and set the web service `DATABASE_URL` environment variable to that database's **Internal Database URL** (web service and database should be in the same region).

## Local run (Windows)
1. Install Python 3.12.
2. Open CMD in this folder.
3. Run:
   ```cmd
   py -3.12 -m venv .venv
   .venv\Scripts\activate
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   copy .env.example .env
   ```
4. Edit `.env` and add the credentials above.
5. Start:
   ```cmd
   uvicorn main:app --host 127.0.0.1 --port 8000 --reload
   ```
6. Open `http://127.0.0.1:8000`.

## Security and messaging notes
- Never commit `.env`, API keys, access tokens or passwords to GitHub.
- Keep Airtable and DoubleTick credentials only in Render Environment Variables.
- Use approved WhatsApp templates for business-initiated messages and message only candidates with the required consent/opt-in.
- Custom free-text sending is not implemented by the supplied proxy integration.

## Latest candidate-filter fix
The dashboard now sends `specialization` (not the old `spec` parameter) and the backend also accepts `spec` as a backward-compatible alias. Dropdown options are generated from the full Airtable candidate set, while the displayed candidate rows are filtered on the server.


## Candidate loading performance
- Airtable candidate records are cached in each running backend worker for 5 minutes by default.
- Specialization, state and city filters run in the browser against the loaded candidate list, so changing dropdowns does not download Airtable records again.
- The Refresh button forces a fresh Airtable read.
- Set `AIRTABLE_CACHE_TTL_SECONDS` to change the cache lifetime (minimum 30 seconds).
- Saving Admin Airtable settings clears the in-process candidate cache.


## DoubleTick status synchronization (important)

The backend now supports DoubleTick status callbacks at `POST /api/webhooks/doubletick`. It handles `TEMPLATE_UPDATE` / `TEMPLATE_STATUS_UPDATE`, `MESSAGE_STATUS_UPDATE`, and incoming `MESSAGE_RECEIVED` events. Incoming messages are stored in the WhatsApp Chats inbox and linked to the HR user who most recently messaged that phone number. Add `DOUBLETICK_WEBHOOK_SECRET` as a backend environment variable (use a long random value, 32+ characters), then register a webhook in DoubleTick Settings → Webhooks:

- URL: `https://YOUR-PUBLIC-DOMAIN/api/webhooks/doubletick`
- Method: `POST`
- Body format: `JSON`
- Header: `X-Webhook-Secret: <same value as DOUBLETICK_WEBHOOK_SECRET>`
- Events: `TEMPLATE_UPDATE`, `MESSAGE_STATUS_UPDATE`, `MESSAGE_RECEIVED`
- Enable retry on timeout.

The template page also refreshes approval status from `GET https://public.doubletick.io/v2/templates` when opened. Message sends are saved per candidate immediately. A successful proxy acceptance is recorded as `ENQUEUED` until DoubleTick reports `SENT`, `DELIVERED`, `READ`, or `FAILED`. Old abandoned `PROCESSING` records become `UNKNOWN` after 15 minutes; check DoubleTick before retrying those to avoid duplicate messages.

DoubleTick webhook documentation: https://docs.doubletick.io/reference/template-webhooks and https://docs.doubletick.io/reference/messaging-webhooks-1


### Test the webhook safely

After the service redeploys, you can test the endpoint from PowerShell (replace the URL and secret; do not share the secret):

```powershell
$secret = $env:DOUBLETICK_WEBHOOK_SECRET
Invoke-RestMethod -Method Post -Uri "https://YOUR-PUBLIC-DOMAIN/api/webhooks/doubletick" -Headers @{"X-Webhook-Secret"=$secret} -ContentType "application/json" -Body '{"event":"TEMPLATE_STATUS_UPDATE","templateName":"your_template_name","templateLanguage":"en","newStatus":"APPROVED"}'
```

This test changes the matching saved template status to APPROVED. For a real delivery test, use a real DoubleTick message status event; do not manually mark real candidate messages as delivered.


### WhatsApp Chats inbox
After deployment, open **WhatsApp Chats** in the dashboard. In DoubleTick Settings → Webhooks, make sure `MESSAGE_RECEIVED` is enabled along with the existing template and delivery-status events. Incoming text and media messages are saved by the backend; media URLs are shown as links. Replies are assigned to the HR user associated with the most recent outgoing message to that phone number. Admin can view all chats. The inbox is read-only: send approved templates from Candidate Search.

DoubleTick documents the incoming event and payload at https://docs.doubletick.io/reference/messaging-webhooks-1.
