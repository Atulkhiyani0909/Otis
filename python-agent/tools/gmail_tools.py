import base64
import json
import re
from email.header import Header
from email.mime.text import MIMEText

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from langchain_core.tools import tool

from tools.auth_helper import build_google_credentials

GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.readonly",
]


def get_gmail_service(creds_data: dict):
    creds = build_google_credentials(creds_data)
    # Ensure token is refreshed if expired
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as err:
            print(f"[AUTH REFRESH WARNING] {err}")
    return build("gmail", "v1", credentials=creds, static_discovery=False)


@tool
def fetch_unread_emails(max_results: int = 5) -> str:
    """Fetches the latest unread emails with sender, subject, date, and snippet."""
    from agent import get_current_google_tokens
    tokens = get_current_google_tokens()
    if not tokens:
        return "Error: No Google authentication tokens available."

    try:
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
    except Exception as e:
        return f"Error fetching unread emails: {str(e)}"


@tool
def search_email_threads(query: str, max_results: int = 3) -> str:
    """Searches emails using standard Gmail query syntax (e.g. 'from:hrishabh', 'important meeting')."""
    from agent import get_current_google_tokens
    tokens = get_current_google_tokens()
    if not tokens:
        return "Error: No Google authentication tokens available."

    try:
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
    except Exception as e:
        return f"Error searching emails: {str(e)}"


@tool
def send_email(recipient: str, subject: str, body: str) -> str:
    """
    Stages an email draft for user confirmation before dispatching.
    Always use this when sending an email to a recipient.
    The recipient MUST be a full email address (use search_contact first if you only have a name).

    Args:
        recipient: Email address of the recipient.
        subject: Subject line of the email.
        body: Plain text content or message body of the email.
    """
    recipient = (recipient or "").strip()
    if not EMAIL_RE.match(recipient):
        return (
            f"Error: '{recipient}' is not a valid email address. "
            "Call search_contact to find the exact address, or ask the user for it. Do not stage the draft yet."
        )

    payload = {
        "action": "send_email",
        "recipient": recipient,
        "subject": subject,
        "body": body,
    }

    return (
        f"✉️ Email staged for approval\n\n"
        f"To: {recipient}\n"
        f"Subject: {subject}\n\n"
        f"Body:\n{body}\n\n"
        f"[APPROVAL_REQUIRED:{json.dumps(payload)}]"
    )


@tool
def execute_send_email_direct(tokens: dict, recipient: str, subject: str, body: str) -> str:
    """Directly sends the email via Gmail API once confirmed by the user."""
    try:
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            return "❌ Failed to dispatch email: Google login is missing a refresh token. Please re-authorize."

        recipient = (recipient or "").strip()
        if not EMAIL_RE.match(recipient):
            return f"❌ Failed to dispatch email: '{recipient}' is not a valid email address."

        # Build credentials with auto-refresh capability
        creds = Credentials(
            token=tokens.get("token") or tokens.get("access_token"),
            refresh_token=refresh_token,
            token_uri=tokens.get("token_uri") or GOOGLE_TOKEN_URI,
            client_id=tokens.get("client_id"),
            client_secret=tokens.get("client_secret"),
            scopes=GMAIL_SCOPES,
        )

        # Explicitly refresh if expired or about to expire
        if (not creds.valid or creds.expired) and creds.refresh_token:
            creds.refresh(Request())

        service = build("gmail", "v1", credentials=creds, static_discovery=False)

        # UTF-8 encoding support for emojis and special characters
        message = MIMEText(body or "", "plain", "utf-8")
        message["To"] = recipient
        message["Subject"] = Header(subject or "", "utf-8")
        raw_msg = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

        sent = service.users().messages().send(userId="me", body={"raw": raw_msg}).execute()
        print(f"[GMAIL] Successfully sent message id={sent.get('id')} to {recipient}")

        return f"✅ Email successfully dispatched to {recipient}."

    except HttpError as e:
        status = getattr(e.resp, "status", "?")
        reason = getattr(e, "reason", None) or str(e)
        print(f"[GMAIL ERROR] HTTP {status}: {e}")
        if status == 403:
            reason += " (Account lacks the 'gmail.send' scope or Gmail API is not enabled in Google Console)"
        return f"❌ Failed to dispatch email: HTTP {status}: {reason}"
    except Exception as e:
        print(f"[GMAIL ERROR] {type(e).__name__}: {e}")
        return f"❌ Failed to dispatch email: {str(e)}"