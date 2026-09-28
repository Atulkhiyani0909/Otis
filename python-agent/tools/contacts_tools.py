from googleapiclient.discovery import build
from langchain_core.tools import tool
from tools.auth_helper import build_google_credentials


def get_people_service(creds_data: dict):
    creds = build_google_credentials(creds_data)
    return build("people", "v1", credentials=creds, static_discovery=False)


@tool
def search_contact(query: str) -> str:
    """
    Searches Google Contacts for a person by name, nickname, or email prefix.
    Returns matched names, primary email addresses, and phone numbers.
    Args:
        query: Name or search query (e.g. 'Hrishabh', 'Rahul', 'Accountant').
    """
    from agent import get_current_google_tokens

    tokens = get_current_google_tokens()
    if not tokens:
        return "Error: No Google authentication tokens available."

    try:
        service = get_people_service(tokens)
        results = (
            service.people()
            .searchContacts(
                query=query,
                readMask="names,emailAddresses,phoneNumbers",
                pageSize=5,
            )
            .execute()
        )

        entries = results.get("results", [])
        if not entries:
            return f"No contacts found matching '{query}'."

        matches = []
        for entry in entries:
            person = entry.get("person", {})
            names = person.get("names", [])
            display_name = (
                names[0].get("displayName", "Unnamed") if names else "Unnamed"
            )

            emails = [
                e.get("value") for e in person.get("emailAddresses", [])
            ]
            phones = [
                p.get("value") for p in person.get("phoneNumbers", [])
            ]

            email_str = ", ".join(emails) if emails else "No email"
            phone_str = ", ".join(phones) if phones else "No phone"

            matches.append(
                f"• {display_name} | Email: {email_str} | Phone: {phone_str}"
            )

        return "\n".join(matches)
    except Exception as e:
        return f"Failed to search contacts: {str(e)}"