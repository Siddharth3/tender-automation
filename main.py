import os
import io
import json
import asyncio
import smtplib
import pandas as pd
from datetime import datetime
from email.message import EmailMessage
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright
from google import genai
from google.genai import types

# Environment Variables
USERNAME = os.getenv("TENDER_USERNAME")
PASSWORD = os.getenv("TENDER_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

# --- CHALO COMPANY PROFILE FOR AI EVALUATION ---
CHALO_PROFILE = """
Chalo is India’s leading transport technology company.

Core Products & Services:
- Electronic Ticket Issuing Machine (‘ETIM’ or 'ETM').
- Automated Fare Collection System (‘AFCS’).
- Automated Vehicle Location System (‘AVLS’) and Vehicle Tracking Units (‘VTU’).
- Cloud Hosting.
- Data SIMs for ETIMs, VTUs and Cameras.
- Digital Audit.
- Contract Management System (‘CMS’).
- Bus Management System (‘BMS’).
- Dashboards and Reports.
- Mobile App with Mobile Ticketing and Live Tracking.
- National Common Mobility Card (‘NCMC’) / NFC-based Smart Card.
- Digital Bus (Tap In Tap Out) Solution/Driver Based Fare Collection System.
- PIS Software & Hardware.
- Programme Management Unit (‘PMU’) it consist: Operations Management & Monitoring, Contract Management, Depot Management Services, and Ticketing & Revenue Management.
- Operations, Training and Support Team.

Irrelevant Tenders:
- General civil construction, road works, building construction, catering, security guards, office furniture, standard non-transit software/hardware procurement, any other IT projects with mobile application or website development.
"""

# --- STRUCTURED OUTPUT MODEL ---
class TenderEvaluation(BaseModel):
    relevance_score: int = Field(description="Score between 0 and 100 based on fit for Chalo.")
    is_relevant: bool = Field(description="True if score >= 60, False otherwise.")
    matching_core_domain: str = Field(description="Specific domain matched, e.g., 'AFCS/NCMC', 'GPS/Fleet Tracking', or 'N/A'.")
    brief_reason: str = Field(description="1-2 sentence justification for the relevance score.")


def get_dynamic_download_dir(prefix="tenders"):
    timestamp = datetime.now().strftime("%Y_%m_%d_%H%M%S")
    folder_name = f"{prefix}_{timestamp}"
    download_dir = os.path.join(os.getcwd(), "downloads", folder_name)
    os.makedirs(download_dir, exist_ok=True)
    return download_dir


def evaluate_tender_row(client: genai.Client, tender_text: str) -> TenderEvaluation:
    """Evaluates individual tender details using Gemini API."""
    prompt = f"""
    You are an expert bid-qualification evaluator for Chalo.
    
    Company Profile:
    {CHALO_PROFILE}
    
    Evaluate the following Tender Details:
    ---
    {tender_text}
    ---
    """
    try:
        # Small delay to respect Gemini API rate limits (15 RPM on free tier)
        time.sleep(1.0)
        
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=TenderEvaluation,
                temperature=0.1
            ),
        )
        result_json = json.loads(response.text)
        return TenderEvaluation(**result_json)
    except Exception as e:
        # Print actual error message to GitHub Actions console for debugging
        print(f"❌ Gemini API Error: {type(e).__name__} - {e}")
        return TenderEvaluation(
            relevance_score=0,
            is_relevant=False,
            matching_core_domain="Error",
            brief_reason=f"AI Error: {str(e)[:100]}"
        )


def process_and_filter_tenders(file_bytes: bytes, output_dir: str, min_score: int = 60) -> str:
    """Parses raw downloaded bytes, applies AI scoring, and saves filtered Excel."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("Missing GEMINI_API_KEY environment variable.")

    client = genai.Client(api_key=api_key)

    # 1. Parse TenderDetail file (handles HTML tables, CSVs, and Excel files)
    try:
        df = pd.read_excel(io.BytesIO(file_bytes))
    except Exception:
        try:
            # Handle ASP.NET HTML tables disguised as Excel
            dfs = pd.read_html(io.BytesIO(file_bytes))
            df = dfs[0] if dfs else pd.DataFrame()
        except Exception:
            df = pd.read_csv(io.BytesIO(file_bytes))

    if df.empty:
        print("⚠️ Raw downloaded file was empty or could not be parsed.")
        empty_path = os.path.join(output_dir, f"Empty_Export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
        pd.DataFrame({"Status": ["No data found in source file"]}).to_excel(empty_path, index=False)
        return empty_path

    print(f"📊 Successfully loaded {len(df)} raw tenders from export.")

    # 2. Score tenders using Gemini
    scores, is_relevants, domains, reasons = [], [], [], []

    for idx, row in df.iterrows():
        tender_summary = "\n".join([f"{col}: {val}" for col, val in row.items() if pd.notna(val)])
        print(f"   Evaluating tender {idx + 1}/{len(df)}...")
        
        eval_res = evaluate_tender_row(client, tender_summary)
        scores.append(eval_res.relevance_score)
        is_relevants.append(eval_res.is_relevant)
        domains.append(eval_res.matching_core_domain)
        reasons.append(eval_res.brief_reason)

    # Append AI results
    df["AI_Relevance_Score"] = scores
    df["AI_Is_Relevant"] = is_relevants
    df["AI_Matched_Domain"] = domains
    df["AI_Reasoning"] = reasons

    # Sort all tenders by score descending
    df = df.sort_values(by="AI_Relevance_Score", ascending=False)

    # 3. Filter by threshold
    filtered_df = df[df["AI_Relevance_Score"] >= min_score]

    print(f"🎯 Tenders matching score >= {min_score}%: {len(filtered_df)} / {len(df)}")

    # SAFEGUARD: If no tenders cross min_score, return ALL scored tenders so you see what AI evaluated
    if filtered_df.empty:
        print(f"⚠️ No tenders met score >= {min_score}%. Returning all scored tenders so email is not empty.")
        filtered_df = df

    filtered_file_path = os.path.join(
        output_dir, 
        f"Chalo_Qualified_Tenders_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    )
    filtered_df.to_excel(filtered_file_path, index=False)

    return filtered_file_path

def send_email_with_attachment(file_path, recipient_email):
    sender_email = os.getenv("SENDER_EMAIL")
    sender_password = os.getenv("SENDER_PASSWORD")
    
    msg = EmailMessage()
    msg["Subject"] = f"Chalo Qualified Tenders Report - {os.path.basename(file_path)}"
    msg["From"] = sender_email
    msg["To"] = recipient_email
    msg.set_content("Hello Team,\n\nPlease find attached the AI-filtered, high-relevance tender export list tailored for Chalo's core offerings.\n\nBest regards,\nTender Automation Bot")

    with open(file_path, "rb") as f:
        msg.add_attachment(
            f.read(), 
            maintype="application", 
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet", 
            filename=os.path.basename(file_path)
        )

    print(f"📧 Sending filtered report to {recipient_email}...")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender_email, sender_password)
        server.send_message(msg)
    print("✅ Email sent successfully!")


async def run_automation():
    download_dir = get_dynamic_download_dir(prefix="tender_export")
    profile_dir = os.path.abspath("./tender_chrome_profile")
    os.makedirs(profile_dir, exist_ok=True)

    async with async_playwright() as p:
        browser_context = await p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )

        page = browser_context.pages[0] if browser_context.pages else await browser_context.new_page()

        await page.goto("https://www.tenderdetail.com/Account/Logon", wait_until="domcontentloaded")
        await page.click("#pill-username")
        await page.wait_for_timeout(300)

        await page.fill("#txtLogin", USERNAME)
        await page.fill("#txtPassword", PASSWORD)
        await page.click("#btnLogin")

        await page.wait_for_url("https://www.tenderdetail.com/RegisterUser/Dashboard*", timeout=30000)

        await page.locator("a[href*='/registeruser/inbox']").first.click()
        await page.wait_for_load_state("domcontentloaded")

        download_element = page.locator("a[href*='/registeruser/exportxslttoexcel']").first
        relative_url = await download_element.get_attribute("href")
        full_download_url = f"https://www.tenderdetail.com{relative_url}" if relative_url.startswith("/") else relative_url

        response = await page.request.get(full_download_url)
        file_bytes = await response.body()

        await browser_context.close()

        # Pass downloaded bytes to AI processing engine
        final_file_path = process_and_filter_tenders(file_bytes, download_dir, min_score=60)
        return final_file_path


if __name__ == "__main__":
    file_path = asyncio.run(run_automation())
    send_email_with_attachment(file_path, RECIPIENT_EMAIL)
