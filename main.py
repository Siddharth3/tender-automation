import os
import asyncio
import smtplib
from email.message import EmailMessage
from datetime import datetime
from playwright.async_api import async_playwright

USERNAME = os.getenv("TENDER_USERNAME")
PASSWORD = os.getenv("TENDER_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

def get_dynamic_download_dir(prefix="tenders"):
    timestamp = datetime.now().strftime("%Y_%m_%d_%H%M%S")
    folder_name = f"{prefix}_{timestamp}"
    download_dir = os.path.join(os.getcwd(), "downloads", folder_name)
    os.makedirs(download_dir, exist_ok=True)
    return download_dir

def send_email_with_attachment(file_path, recipient_email):
    sender_email = os.getenv("SENDER_EMAIL")
    sender_password = os.getenv("SENDER_PASSWORD")
    
    msg = EmailMessage()
    msg["Subject"] = f"Automated Tender Export - {os.path.basename(file_path)}"
    msg["From"] = sender_email
    msg["To"] = recipient_email
    msg.set_content("Please find attached the latest tender export file.")

    with open(file_path, "rb") as f:
        msg.add_attachment(
            f.read(), 
            maintype="application", 
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet", 
            filename=os.path.basename(file_path)
        )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender_email, sender_password)
        server.send_message(msg)

async def run_automation():
    download_dir = get_dynamic_download_dir(prefix="tender_export")
    profile_dir = os.path.abspath("./tender_chrome_profile")
    os.makedirs(profile_dir, exist_ok=True)

    async with async_playwright() as p:
        # Headless=True for cloud execution
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
        
        final_file_path = os.path.join(download_dir, "tender_export.xlsx")
        with open(final_file_path, "wb") as f:
            f.write(file_bytes)

        await browser_context.close()
        return final_file_path

if __name__ == "__main__":
    file_path = asyncio.run(run_automation())
    send_email_with_attachment(file_path, RECIPIENT_EMAIL)
