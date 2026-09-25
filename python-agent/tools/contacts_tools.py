import os
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from langchain_core.tools import tool
from tools.auth_helper import build_google_credentials


def get_people_service(creds_data: dict):
    creds = build_google_credentials(creds_data)
    return build("people", "v1", credentials=creds, static_discovery=False)

@tool
def search_contact(name: str) -> str:
    """Searches Google Contacts for a person by name to get their email addresses and phone numbers."""
    from agent import get_current_google_tokens
    tokens = get_current_google_tokens()
    if not tokens:
        return "Error: No Google authentication tokens."

    service = get_people_service(tokens)
    try:
        response = service.people().searchContacts(
            query=name,
            readMask="names,emailAddresses,phoneNumbers"
        ).execute()

        results = response.get("results", [])
        if not results:
            return f"No contact found matching '{name}'."

        first_match = results[0].get("person", {})
        display_name = first_match.get("names", [{}])[0].get("displayName", name)
        emails = [e.get("value") for e in first_match.get("emailAddresses", [])]
        phones = [p.get("value") for p in first_match.get("phoneNumbers", [])]

        return f"Contact Found: {display_name}\n• Emails: {', '.join(emails) if emails else 'None'}\n• Phones: {', '.join(phones) if phones else 'None'}"
    except Exception as e:
        return f"Could not search contacts: {str(e)}"
