import os
import io
import time
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
- Electric Bus (e-Bus)/ City Bus Deployment, Operation & Maintenance (O&M) / Gross Cost Contract (GCC) services.
- Electronic Ticket Issuing Machine (‘ETIM’ or 'ETM' or 'ETMS').
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

INCLUDE_KEYWORDS = [
    "bus", "transit", "transport", "ticketing", "afcs", "ncmc", "etim", "etm", "etms", "itms", "operation", "maintenance", 
    "integrated transport management system", "gps", "fleet", "passenger information", "its", "intelligent transport", 
    "electric bus", "electric buses", "e-bus", "fare collection", "mobility", "depot", "vehicle tracking", "avls",
    "vtu", "bms", "bus management system", "cms", "contract management system", "digital audit", "pis", "ncmc", 
    "national common mobility card", "driver based fare collection system", "tap in tap out", "pmu", "contract management", "smart card"
]

EXCLUDE_KEYWORDS = [
    "civil", "road", "building construction", "furniture", "security guard", "catering", "drainage", "sewerage", 
    "bridge", "paint", "cleaning", "stationery", "janitorial", "canteen", "sanitary"
]

def is_potentially_relevant(row_text: str) -> bool:
    """Pre-filters tenders using basic keyword logic."""
    text = row_text.lower()
    
    # Check if any excluded keyword is present
    if any(ex_kw in text for ex_kw in EXCLUDE_KEYWORDS):
        return False
        
    # Check if at least one included domain keyword is present
    return any(inc_kw in text for inc_kw in INCLUDE_KEYWORDS)

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
    """Evaluates individual tender details with automatic retries for 503/429 errors."""
    prompt = f"""
    You are an expert bid-qualification evaluator for Chalo.
    
    Company Profile:
    {CHALO_PROFILE}
    
    Evaluate the following Tender Details:
    ---
    {tender_text}
    ---
    """
    
    # Retry configuration for temporary 503 (High Demand) or 429 (Rate Limit) errors
    max_retries = 4
    base_delay = 5  # Start with 5 seconds delay
    
    for attempt in range(max_retries):
        try:
            # Try primary model first, fallback to flash-lite if demand is high
            model_to_use = 'gemini-3.5-flash' if attempt < 2 else 'gemini-3.5-flash-lite'
            
            response = client.models.generate_content(
                model=model_to_use,
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
            err_msg = str(e)
            is_transient_error = "503" in err_msg or "UNAVAILABLE" in err_msg or "429" in err_msg
            
            if is_transient_error and attempt < max_retries - 1:
                wait_time = base_delay * (2 ** attempt)  # Exponential wait: 5s, 10s, 20s...
                print(f"⚠️ Temporary Server/Rate Error ({'503 High Demand' if '503' in err_msg else '429 Rate Limit'}). Retrying in {wait_time}s (Attempt {attempt + 1}/{max_retries})...")
                time.sleep(wait_time)
            else:
                print(f"❌ Gemini API Error: {type(e).__name__} - {e}")
                break

    return TenderEvaluation(
        relevance_score=0,
        is_relevant=False,
        matching_core_domain="Error",
        brief_reason="AI evaluation failed due to server high demand after retries."
    )


def process_and_filter_tenders(file_bytes: bytes, output_dir: str, min_score: int = 60) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("Missing GEMINI_API_KEY environment variable.")

    client = genai.Client(api_key=api_key)

    # 1. Parse TenderDetail file
    try:
        df = pd.read_excel(io.BytesIO(file_bytes))
    except Exception:
        try:
            dfs = pd.read_html(io.BytesIO(file_bytes))
            df = dfs[0] if dfs else pd.DataFrame()
        except Exception:
            df = pd.read_csv(io.BytesIO(file_bytes))

    if df.empty:
        empty_path = os.path.join(output_dir, f"Empty_Export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
        pd.DataFrame({"Status": ["No data found in source file"]}).to_excel(empty_path, index=False)
        return empty_path

    print(f"📊 Total Raw Tenders Downloaded: {len(df)}")

    # 2. Apply Local Python Keyword Pre-Filtering
    df["Full_Text"] = df.apply(lambda r: " ".join([str(val) for val in r.values if pd.notna(val)]), axis=1)
    
    # Filter rows based on keywords
    pre_filtered_df = df[df["Full_Text"].apply(is_potentially_relevant)].copy()
    
    print(f"🔍 Pre-filtered: {len(pre_filtered_df)} / {len(df)} tenders sent to Gemini AI.")

    # Drop temporary column before evaluation
    df.drop(columns=["Full_Text"], inplace=True)
    pre_filtered_df.drop(columns=["Full_Text"], inplace=True)

    # If no tenders survived pre-filtering, return raw list marked with 0 scores
    if pre_filtered_df.empty:
        print("⚠️ No tenders matched keyword pre-filtering. Returning unprocessed sheet.")
        df["AI_Relevance_Score"] = 0
        df["AI_Is_Relevant"] = False
        df["AI_Matched_Domain"] = "N/A"
        df["AI_Reasoning"] = "Filtered out during pre-evaluation keyword check."
        
        filtered_file_path = os.path.join(output_dir, f"Chalo_Qualified_Tenders_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
        df.to_excel(filtered_file_path, index=False)
        return filtered_file_path

    # 3. Evaluate ONLY the pre-filtered tenders via Gemini AI
    eval_map = {}

    for idx, row in pre_filtered_df.iterrows():
        tdr = str(row.get("TDR", idx))
        tender_summary = "\n".join([f"{col}: {val}" for col, val in row.items() if pd.notna(val)])
        
        print(f"   🤖 AI Evaluating TDR {tdr} ({len(eval_map) + 1}/{len(pre_filtered_df)})...")
        
        # 13-second sleep ensures we strictly respect 5 RPM (free tier limit)
        time.sleep(13)
        eval_res = evaluate_tender_row(client, tender_summary)
        eval_map[tdr] = eval_res

    # 4. Assign evaluation results to DataFrame
    scores, is_relevants, domains, reasons = [], [], [], []

    for idx, row in df.iterrows():
        tdr = str(row.get("TDR", idx))
        if tdr in eval_map:
            res = eval_map[tdr]
            scores.append(res.relevance_score)
            is_relevants.append(res.is_relevant)
            domains.append(res.matching_core_domain)
            reasons.append(res.brief_reason)
        else:
            scores.append(0)
            is_relevants.append(False)
            domains.append("N/A")
            reasons.append("Filtered out locally by keyword matching.")

    df["AI_Relevance_Score"] = scores
    df["AI_Is_Relevant"] = is_relevants
    df["AI_Matched_Domain"] = domains
    df["AI_Reasoning"] = reasons

    # Sort and filter by score
    df = df.sort_values(by="AI_Relevance_Score", ascending=False)
    filtered_df = df[df["AI_Relevance_Score"] >= min_score]

    if filtered_df.empty:
        print(f"⚠️ No tenders scored >= {min_score}%. Returning pre-filtered tenders.")
        filtered_df = df[df["AI_Reasoning"] != "Filtered out locally by keyword matching."]

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
    # Verify environment variables exist before running Playwright
    if not USERNAME or not PASSWORD:
        raise ValueError("❌ Missing TENDER_USERNAME or TENDER_PASSWORD environment variables. Check GitHub Secrets.")

    download_dir = get_dynamic_download_dir(prefix="tender_export")
    profile_dir = os.path.abspath("./tender_chrome_profile")
    os.makedirs(profile_dir, exist_ok=True)

    async with async_playwright() as p:
        browser_context = await p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=True,
            args=[
                "--no-sandbox", 
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled"
            ]
        )

        page = browser_context.pages[0] if browser_context.pages else await browser_context.new_page()

        # Set desktop viewport size
        await page.set_viewport_size({"width": 1280, "height": 800})

        print("🌐 Navigating to TenderDetail login page...")
        await page.goto("https://www.tenderdetail.com/Account/Logon", wait_until="domcontentloaded", timeout=45000)

        print("🔑 Interacting with login form...")
        
        # Click the tab explicitly to activate the username/password fields
        pill_tab = page.locator("#pill-username")
        if await pill_tab.is_visible(timeout=5000):
            await pill_tab.click(force=True)
            await page.wait_for_timeout(500)

        # Wait specifically for the text box to become attached & visible
        txt_login = page.locator("#txtLogin")
        await txt_login.wait_for(state="visible", timeout=30000)
        
        await txt_login.fill(USERNAME)
        await page.fill("#txtPassword", PASSWORD)
        await page.click("#btnLogin")

        print("⏳ Waiting for dashboard navigation...")
        await page.wait_for_url("https://www.tenderdetail.com/RegisterUser/Dashboard*", timeout=45000)

        # Navigate to inbox and trigger export
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
