from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from langchain_core.tools import tool


def get_drive_service():
  from agent import get_current_google_tokens

  tokens = get_current_google_tokens()
  if not tokens:
    raise ValueError(
        "No active Google OAuth session. Please link Google Workspace."
    )

  creds = Credentials(
      token=tokens.get("access_token"),
      refresh_token=tokens.get("refresh_token"),
      token_uri="https://oauth2.googleapis.com/token",
      client_id=tokens.get("client_id"),
      client_secret=tokens.get("client_secret"),
  )
  return build("drive", "v3", credentials=creds)


@tool
def search_drive_files(query: str, max_results: int = 5) -> str:
  """Searches Google Drive for files, spreadsheets, presentations, and PDFs by title or text content.

  Args:
      query: Keyword, document title, or topic to search for (e.g., 'resume',
        'invoice').
      max_results: Max files to return (default: 5).
  """
  try:
    print(f"--> [DRIVE SEARCH INVOKED] Query: '{query}'")
    service = get_drive_service()
    clean_query = query.replace("'", "\\'").strip()

    # Search by filename only first (most reliable)
    q = f"name contains '{clean_query}' and trashed = false"

    results = (
        service.files()
        .list(
            q=q,
            pageSize=max_results,
            fields="files(id, name, mimeType, webViewLink)",
            spaces="drive",
        )
        .execute()
    )

    files = results.get("files", [])
    print(f"--> [DRIVE SEARCH RESULT] Found: {len(files)} files")

    if not files:
      # If no match by name, try an unconstrained search for the keyword in content
      content_q = f"fullText contains '{clean_query}' and trashed = false"
      results = (
          service.files()
          .list(
              q=content_q,
              pageSize=max_results,
              fields="files(id, name, mimeType, webViewLink)",
              spaces="drive",
          )
          .execute()
      )
      files = results.get("files", [])

    if not files:
      return (
          f"📁 No Google Drive files found matching '{query}'. Verify the file"
          " name."
      )

    output = [f"📁 Found {len(files)} Drive file(s):"]
    for f in files:
      name = f.get("name")
      link = f.get("webViewLink")
      output.append(f"• **{name}**\n  🔗 [Open in Drive]({link})")

    return "\n\n".join(output)
  except Exception as e:
    print(f"--> [DRIVE ERROR] {str(e)}")
    return f"Drive search error: {str(e)}"