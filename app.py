import io
import os
import json
import time
import smtplib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import pandas as pd
import streamlit as st
from pydantic import BaseModel, Field
from google import genai
from google.genai import types


# ---------------------------------------------------------------------
# Configuration
# Set these as environment variables or in Streamlit Community Cloud
# -> App -> Settings -> Secrets.
# ---------------------------------------------------------------------
CHALO_PROFILE = """
Chalo is India's leading transport technology company.

Core Products & Services:
- Electric Bus (e-Bus)/ City Bus Deployment, Operation & Maintenance (O&M) / Gross Cost Contract (GCC) services.
- Electronic Ticket Issuing Machine (ETIM/ETM/ETMS).
- Automated Fare Collection System (AFCS).
- Automated Vehicle Location System (AVLS) and Vehicle Tracking Units (VTU).
- Cloud Hosting and data SIMs for ETIMs, VTUs and cameras.
- Digital Audit, Contract Management System (CMS), Bus Management System (BMS).
- Dashboards and reports.
- Mobile app with mobile ticketing and live tracking.
- National Common Mobility Card (NCMC) / NFC-based smart card.
- Tap In Tap Out / driver-based fare collection.
- Passenger Information System (PIS) software and hardware.
- Programme Management Unit (PMU): operations, contract, depot, ticketing and revenue management.
- Operations, training and support.

Irrelevant tenders:
General civil construction, road works, building construction, catering, security guards,
office furniture, standard non-transit software/hardware procurement, and unrelated
IT projects involving mobile application or website development.
"""

INCLUDE_KEYWORDS = [
    "bus", "transit", "transport", "ticketing", "afcs", "ncmc", "etim", "etm",
    "etms", "itms", "operation", "maintenance", "integrated transport management system",
    "gps", "fleet", "passenger information", "its", "intelligent transport",
    "electric bus", "electric buses", "e-bus", "fare collection", "mobility", "depot",
    "vehicle tracking", "avls", "vtu", "bms", "bus management system", "cms",
    "contract management system", "digital audit", "pis", "national common mobility card",
    "driver based fare collection system", "tap in tap out", "pmu",
    "contract management", "smart card"
]

EXCLUDE_KEYWORDS = [
    "civil", "road", "building construction", "furniture", "security guard",
    "catering", "drainage", "sewerage", "bridge", "paint", "cleaning",
    "stationery", "janitorial", "canteen", "sanitary"
]


class TenderEvaluation(BaseModel):
    relevance_score: int = Field(description="Score between 0 and 100 based on fit for Chalo.")
    is_relevant: bool = Field(description="True if score >= 60, False otherwise.")
    matching_core_domain: str = Field(description="Specific domain matched, e.g., AFCS/NCMC, GPS/Fleet Tracking, or N/A.")
    brief_reason: str = Field(description="1-2 sentence justification for the relevance score.")


def get_setting(name: str, default=None):
    """Read Streamlit secrets first, then environment variables."""
    try:
        value = st.secrets.get(name, None)
        if value not in (None, ""):
            return value
    except Exception:
        pass
    return os.getenv(name, default)


def is_potentially_relevant(row_text: str) -> bool:
    text = row_text.lower()
    if any(keyword in text for keyword in EXCLUDE_KEYWORDS):
        return False
    return any(keyword in text for keyword in INCLUDE_KEYWORDS)


def evaluate_tender_row(client: genai.Client, tender_text: str) -> TenderEvaluation:
    prompt = f"""
You are an expert bid-qualification evaluator for Chalo.

Company Profile:
{CHALO_PROFILE}

Evaluate the following Tender Details:
---
{tender_text}
---

Return a relevance score from 0 to 100, whether it is relevant (score >= 60),
the matching core domain, and a brief reason.
"""
    max_retries = 4
    base_delay = 5

    # Retains the model choices from the supplied automation.
    for attempt in range(max_retries):
        try:
            model_to_use = "gemini-3.5-flash" if attempt < 2 else "gemini-3.5-flash-lite"
            response = client.models.generate_content(
                model=model_to_use,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=TenderEvaluation,
                    temperature=0.1,
                ),
            )
            result_json = json.loads(response.text)
            return TenderEvaluation(**result_json)
        except Exception as exc:
            error_message = str(exc)
            transient = any(token in error_message.upper() for token in ("503", "UNAVAILABLE", "429"))
            if transient and attempt < max_retries - 1:
                wait_time = base_delay * (2 ** attempt)
                time.sleep(wait_time)
            else:
                raise RuntimeError(f"Gemini evaluation failed: {exc}") from exc

    raise RuntimeError("Gemini evaluation failed after retries.")


def read_uploaded_file(file_bytes: bytes, filename: str) -> pd.DataFrame:
    suffix = Path(filename).suffix.lower()
    if suffix in (".xlsx", ".xls"):
        return pd.read_excel(io.BytesIO(file_bytes))
    if suffix == ".csv":
        return pd.read_csv(io.BytesIO(file_bytes))
    # Some tender portals export HTML tables with an Excel-like extension.
    try:
        tables = pd.read_html(io.BytesIO(file_bytes))
        if tables:
            return tables[0]
    except Exception:
        pass
    try:
        return pd.read_excel(io.BytesIO(file_bytes))
    except Exception:
        return pd.read_csv(io.BytesIO(file_bytes))


def process_and_filter_tenders(file_bytes: bytes, filename: str, min_score: int = 60):
    api_key = get_setting("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not configured in Streamlit Secrets or environment variables.")

    client = genai.Client(api_key=api_key)
    df = read_uploaded_file(file_bytes, filename)

    if df.empty:
        raise ValueError("The uploaded file contains no tender rows.")

    # Make a working copy and create text for keyword pre-filtering.
    df = df.copy()
    df["Full_Text"] = df.apply(
        lambda row: " ".join(str(value) for value in row.values if pd.notna(value)),
        axis=1,
    )
    pre_filtered_df = df[df["Full_Text"].apply(is_potentially_relevant)].copy()
    df.drop(columns=["Full_Text"], inplace=True)
    pre_filtered_df.drop(columns=["Full_Text"], inplace=True)

    st.write(f"**Raw tenders found:** {len(df)}")
    st.write(f"**Tenders sent to AI evaluation:** {len(pre_filtered_df)}")

    if pre_filtered_df.empty:
        df["AI_Relevance_Score"] = 0
        df["AI_Is_Relevant"] = False
        df["AI_Matched_Domain"] = "N/A"
        df["AI_Reasoning"] = "Filtered out during pre-evaluation keyword check."
        output_df = df
    else:
        eval_map = {}
        progress = st.progress(0, text="Evaluating tenders with Gemini AI...")
        total = len(pre_filtered_df)

        for count, (idx, row) in enumerate(pre_filtered_df.iterrows(), start=1):
            tender_id = str(row.get("TDR", idx))
            tender_summary = "\n".join(
                f"{column}: {value}" for column, value in row.items() if pd.notna(value)
            )
            progress.progress(
                int((count - 1) / total * 100),
                text=f"Evaluating tender {count} of {total} (TDR: {tender_id})...",
            )
            # Preserves the original conservative request pacing.
            time.sleep(13)
            eval_map[tender_id] = evaluate_tender_row(client, tender_summary)

        progress.progress(100, text="Tender evaluation complete.")

        scores, relevant_flags, domains, reasons = [], [], [], []
        for idx, row in df.iterrows():
            tender_id = str(row.get("TDR", idx))
            if tender_id in eval_map:
                result = eval_map[tender_id]
                scores.append(result.relevance_score)
                relevant_flags.append(result.is_relevant)
                domains.append(result.matching_core_domain)
                reasons.append(result.brief_reason)
            else:
                scores.append(0)
                relevant_flags.append(False)
                domains.append("N/A")
                reasons.append("Filtered out locally by keyword matching.")

        df["AI_Relevance_Score"] = scores
        df["AI_Is_Relevant"] = relevant_flags
        df["AI_Matched_Domain"] = domains
        df["AI_Reasoning"] = reasons
        df = df.sort_values(by="AI_Relevance_Score", ascending=False)
        output_df = df[df["AI_Relevance_Score"] >= min_score].copy()

        # Match original behavior: if no rows meet threshold, return AI-evaluated rows.
        if output_df.empty:
            output_df = df[df["AI_Reasoning"] != "Filtered out locally by keyword matching."].copy()

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        output_df.to_excel(writer, index=False, sheet_name="Qualified Tenders")
    output.seek(0)
    return output_df, output.getvalue(), len(df), len(pre_filtered_df)


def send_email_with_attachment(file_bytes: bytes, filename: str, recipient_email: str):
    sender_email = get_setting("SENDER_EMAIL")
    sender_password = get_setting("SENDER_PASSWORD")
    if not sender_email or not sender_password:
        raise ValueError("SENDER_EMAIL or SENDER_PASSWORD is not configured.")
    if not recipient_email:
        raise ValueError("Recipient email is missing.")

    message = EmailMessage()
    message["Subject"] = f"Chalo Qualified Tenders Report - {filename}"
    message["From"] = sender_email
    message["To"] = recipient_email
    message.set_content(
        "Hello Team,\n\n"
        "Please find attached the AI-filtered tender export list tailored for "
        "Chalo's core offerings.\n\n"
        "Best regards,\nTender Automation Portal"
    )
    message.add_attachment(
        file_bytes,
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(sender_email, sender_password)
        server.send_message(message)


# ---------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------
st.set_page_config(page_title="Chalo Tender Qualification Portal", page_icon="🚌", layout="wide")
st.title("🚌 Chalo Tender Qualification Portal")
st.caption("Upload a TenderDetail export, evaluate relevance, view results, download Excel, and email the report.")

with st.expander("Setup status", expanded=False):
    for key in ("GEMINI_API_KEY", "SENDER_EMAIL", "SENDER_PASSWORD"):
        st.write(f"{'✅ Configured' if get_setting(key) else '❌ Missing'}: {key}")

uploaded_file = st.file_uploader(
    "Upload your manually downloaded TenderDetail file",
    type=["xlsx", "xls", "csv", "html", "htm"],
    help="Upload the Excel/CSV export downloaded after signing in to TenderDetail in your browser.",
)

col1, col2 = st.columns([1, 2])
with col1:
    min_score = st.slider("Minimum relevance score", min_value=0, max_value=100, value=60, step=5)
with col2:
    recipient_default = get_setting("RECIPIENT_EMAIL", "")
    recipient_email = st.text_input("Email report to", value=recipient_default or "")

if uploaded_file:
    st.info(f"Selected file: {uploaded_file.name} ({uploaded_file.size:,} bytes)")

process_clicked = st.button(
    "Process tenders, show results, and email report",
    type="primary",
    disabled=uploaded_file is None,
    use_container_width=True,
)

if process_clicked and uploaded_file is not None:
    try:
        with st.spinner("Reading file and evaluating tenders. This may take a while for large exports..."):
            result_df, excel_bytes, raw_count, ai_count = process_and_filter_tenders(
                uploaded_file.getvalue(),
                uploaded_file.name,
                min_score=min_score,
            )

        st.session_state["result_df"] = result_df
        st.session_state["excel_bytes"] = excel_bytes
        st.session_state["report_name"] = f"Chalo_Qualified_Tenders_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        st.session_state["raw_count"] = raw_count
        st.session_state["ai_count"] = ai_count

        if recipient_email.strip():
            with st.spinner(f"Emailing report to {recipient_email.strip()}..."):
                send_email_with_attachment(
                    excel_bytes,
                    st.session_state["report_name"],
                    recipient_email.strip(),
                )
            st.success(f"Report processed and emailed to {recipient_email.strip()}.")
        else:
            st.warning("Report processed, but no recipient email was provided. Enter an email address and click the email button below.")
    except Exception as exc:
        st.error(f"Processing failed: {exc}")
        st.exception(exc)

if "result_df" in st.session_state:
    st.divider()
    st.subheader("Qualified tender results")
    result_df = st.session_state["result_df"]
    metric1, metric2, metric3 = st.columns(3)
    metric1.metric("Source tenders", st.session_state.get("raw_count", len(result_df)))
    metric2.metric("Sent to AI", st.session_state.get("ai_count", 0))
    metric3.metric("Rows in report", len(result_df))

    st.dataframe(result_df, use_container_width=True, hide_index=True)

    st.download_button(
        "Download qualified tenders Excel",
        data=st.session_state["excel_bytes"],
        file_name=st.session_state["report_name"],
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

    with st.expander("Email the report again"):
        resend_to = st.text_input("Recipient email for resend", value=recipient_default or "", key="resend_to")
        if st.button("Send report email", key="resend_button"):
            try:
                send_email_with_attachment(
                    st.session_state["excel_bytes"],
                    st.session_state["report_name"],
                    resend_to.strip(),
                )
                st.success(f"Email sent to {resend_to.strip()}.")
            except Exception as exc:
                st.error(f"Email could not be sent: {exc}")
