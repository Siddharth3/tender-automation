# Chalo Tender Qualification Portal

This small Streamlit web portal replaces the TenderDetail browser-login step with a manual upload workflow.

## What it does

1. Upload an Excel/CSV export downloaded manually from TenderDetail.
2. Apply the existing keyword pre-filter.
3. Evaluate matching tenders using Gemini AI.
4. Show the qualified tender results in the browser.
5. Download the qualified results as Excel.
6. Email the Excel report to the configured recipient (or an address entered in the portal).

## Run locally

Requires Python 3.10 or newer.

```bash
python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows PowerShell:
# .venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

Set environment variables before running.

macOS/Linux:
```bash
export GEMINI_API_KEY="your-gemini-api-key"
export SENDER_EMAIL="your-gmail-address"
export SENDER_PASSWORD="your-gmail-app-password"
export RECIPIENT_EMAIL="team@example.com"
streamlit run app.py
```

Windows PowerShell:
```powershell
$env:GEMINI_API_KEY="your-gemini-api-key"
$env:SENDER_EMAIL="your-gmail-address"
$env:SENDER_PASSWORD="your-gmail-app-password"
$env:RECIPIENT_EMAIL="team@example.com"
streamlit run app.py
```

Open the local URL Streamlit prints, normally http://localhost:8501.

## Deploy as a hosted web portal

You can deploy this app on Streamlit Community Cloud:

1. Create a GitHub repository and upload `app.py` and `requirements.txt`.
2. Go to https://share.streamlit.io/ and deploy the repository, choosing `app.py` as the main file.
3. In the app's settings, open **Secrets** and add:

```toml
GEMINI_API_KEY = "your-gemini-api-key"
SENDER_EMAIL = "your-gmail-address"
SENDER_PASSWORD = "your-gmail-app-password"
RECIPIENT_EMAIL = "team@example.com"
```

Use a Gmail **App Password** if your Google account supports it; do not use your normal account password. Never commit API keys, email passwords, or the secrets file to GitHub.

## Important notes

- This app no longer logs in to TenderDetail, so it avoids the Playwright/Cloudflare login failure.
- The user must still download the tender export manually and upload it.
- Gemini API usage may incur charges or be subject to quotas.
- The code retains the model names from the supplied script. If the Gemini API reports that a model is unavailable, change `model_to_use` in `evaluate_tender_row()` to a model enabled for your API key.
- Evaluation is performed sequentially and waits 13 seconds per tender, following the original script's pacing. Large files may take a while.
- The report is emailed when the processing button is clicked and an email recipient is configured.
- This is a basic internal-use app. If deployed publicly, add authentication/access restrictions before using it with confidential tender data.
