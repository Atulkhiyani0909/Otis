from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/tasks",  
    "https://www.googleapis.com/auth/drive.readonly",      
  "https://www.googleapis.com/auth/spreadsheets"
]

def build_google_credentials(creds_data: dict) -> Credentials:
    """Builds a verified, refreshable Google Credentials instance."""
    return Credentials(
        token=creds_data.get("access_token"),
        refresh_token=creds_data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",  
        client_id=creds_data.get("client_id"),
        client_secret=creds_data.get("client_secret"),
        scopes=SCOPES
    )


def get_google_credentials(token_dict: dict) -> Credentials:
    creds = Credentials(
        token=token_dict.get("access_token"),
        refresh_token=token_dict.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=token_dict.get("client_id"),
        client_secret=token_dict.get("client_secret"),
        scopes=token_dict.get("scopes")
    )
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds