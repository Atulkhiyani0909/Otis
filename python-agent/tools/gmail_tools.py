import base64
from typing import Optional
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from langchain_core.tools import tool
from tools.auth_helper import build_google_credentials
from email.mime.text import MIMEText


def get_gmail_service(creds_data: dict):
    creds = build_google_credentials(creds_data)
    return build("gmail", "v1", credentials=creds, static_discovery=False)

@tool
def fetch_unread_emails(max_results: int = 5) -> str:
    """Fetches the latest unread emails with sender, subject, date, and snippet."""
    from agent import get_current_google_tokens
    tokens = get_current_google_tokens()
    if not tokens:
        return "Error: No Google authentication tokens available."

    service = get_gmail_service(tokens)
    res = service.users().messages().list(userId="me", q="is:unread", maxResults=max_results).execute()
    messages = res.get("messages", [])

    if not messages:
        return "No unread emails found."

    summary = []
    for msg in messages:
        detail = service.users().messages().get(userId="me", id=msg["id"], format="metadata").execute()
        headers = detail.get("payload", {}).get("headers", [])
        
        subject = next((h["value"] for h in headers if h["name"] == "Subject"), "(No Subject)")
        sender = next((h["value"] for h in headers if h["name"] == "From"), "Unknown Sender")
        date = next((h["value"] for h in headers if h["name"] == "Date"), "Unknown Date")
        snippet = detail.get("snippet", "")

        summary.append(f"• ID: {msg['id']}\n  From: {sender}\n  Date: {date}\n  Subject: {subject}\n  Snippet: {snippet}\n")

    return "\n".join(summary)

@tool
def search_email_threads(query: str, max_results: int = 3) -> str:
    """Searches emails using standard Gmail query syntax (e.g. 'from:hrishabh', 'important meeting')."""
    from agent import get_current_google_tokens
    tokens = get_current_google_tokens()
    if not tokens:
        return "Error: No Google authentication tokens available."

    service = get_gmail_service(tokens)
    res = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    messages = res.get("messages", [])

    if not messages:
        return f"No emails matched query: '{query}'."

    summary = []
    for msg in messages:
        detail = service.users().messages().get(userId="me", id=msg["id"], format="metadata").execute()
        headers = detail.get("payload", {}).get("headers", [])
        subject = next((h["value"] for h in headers if h["name"] == "Subject"), "No Subject")
        sender = next((h["value"] for h in headers if h["name"] == "From"), "Unknown")
        snippet = detail.get("snippet", "")
        summary.append(f"• From: {sender} | Subject: {subject}\n  Snippet: {snippet}")

    return "\n\n".join(summary)


@tool
def send_email(to_email: str, subject: str, body: str) -> str:
    """
    Sends an email to a recipient using the user's connected Gmail account.
    Args:
        to_email: Recipient's valid email address (e.g. 'colleague@example.com').
        subject: The subject line of the email.
        body: Plain text body content of the email.
    """
    from agent import get_current_google_tokens
    tokens = get_current_google_tokens()
    if not tokens:
        return "Error: No Google authentication tokens available."

    try:
        service = get_gmail_service(tokens)

        # 1. Build standard MIME text container
        message = MIMEText(body)
        message["to"] = to_email
        message["subject"] = subject

        # 2. Encode to base64url format required by Gmail API
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        body_payload = {"raw": raw_message}

        # 3. Dispatch to Gmail
        sent_message = service.users().messages().send(userId="me", body=body_payload).execute()
        message_id = sent_message.get("id")

        return f"Email sent successfully to {to_email}. (Message ID: {message_id})"
    except Exception as e:
        return f"Failed to send email to {to_email}: {str(e)}"
    