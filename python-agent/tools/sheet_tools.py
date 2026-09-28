from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from langchain_core.tools import tool

def get_sheets_service():
    from agent import get_current_google_tokens
    tokens = get_current_google_tokens()
    if not tokens:
        raise ValueError("No active Google OAuth session.")
    
    creds = Credentials(
        token=tokens.get("access_token"),
        refresh_token=tokens.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=tokens.get("client_id"),
        client_secret=tokens.get("client_secret"),
    )
    return build("sheets", "v4", credentials=creds)

@tool
def create_spreadsheet(title: str, header_columns: list[str]) -> str:
    """
    Creates a new Google Spreadsheet in your Google Drive with custom column headers.
    Args:
        title: Title of the spreadsheet (e.g., 'Expenses 2026', 'Client Tracker').
        header_columns: List of column names (e.g., ['Date', 'Category', 'Amount', 'Notes']).
    """
    try:
        service = get_sheets_service()
        spreadsheet_body = {
            "properties": {"title": title}
        }
        sheet = service.spreadsheets().create(body=spreadsheet_body, fields="spreadsheetId,spreadsheetUrl").execute()
        spreadsheet_id = sheet.get("spreadsheetId")
        url = sheet.get("spreadsheetUrl")

        # Insert header columns in row 1
        if header_columns:
            service.spreadsheets().values().append(
                spreadsheetId=spreadsheet_id,
                range="A1",
                valueInputOption="USER_ENTERED",
                body={"values": [header_columns]}
            ).execute()

        return f"✅ Spreadsheet created successfully!\n• Title: **{title}**\n• URL: {url}\n• ID: `{spreadsheet_id}`"
    except Exception as e:
        return f"Failed to create spreadsheet: {str(e)}"

@tool
def append_row_to_sheet(spreadsheet_id: str, row_values: list[str], sheet_name: str = "Sheet1") -> str:
    """
    Appends a new row of data to an existing Google Spreadsheet.
    Args:
        spreadsheet_id: The ID of the spreadsheet (from the URL).
        row_values: List of values to insert (e.g., ['2026-09-27', 'Dinner', '450', 'Cafe Coffee']).
        sheet_name: The tab name (default 'Sheet1').
    """
    try:
        service = get_sheets_service()
        range_target = f"{sheet_name}!A1"
        body = {"values": [row_values]}
        
        result = service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=range_target,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body
        ).execute()

        updated_rows = result.get("updates", {}).get("updatedRows", 1)
        return f"✅ Successfully added row to spreadsheet. (Rows updated: {updated_rows})"
    except Exception as e:
        return f"Failed to append row to spreadsheet: {str(e)}"

@tool
def read_sheet_data(spreadsheet_id: str, range_name: str = "Sheet1!A1:Z50") -> str:
    """
    Reads rows and columns of data from a Google Spreadsheet.
    Args:
        spreadsheet_id: The ID of the spreadsheet.
        range_name: Cell range to fetch in A1 notation (e.g. 'Sheet1!A1:E20').
    """
    try:
        service = get_sheets_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=range_name
        ).execute()
        
        rows = result.get("values", [])
        if not rows:
            return "Spreadsheet range contains no data."

        formatted_rows = []
        for r in rows:
            formatted_rows.append(" | ".join(str(cell) for cell in r))
            
        return "=== Google Sheet Data ===\n" + "\n".join(formatted_rows)
    except Exception as e:
        return f"Failed to read spreadsheet: {str(e)}"


from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from langchain_core.tools import tool

# (Keep your existing get_sheets_service, create_spreadsheet, append_row_to_sheet, read_sheet_data)

@tool
def clear_sheet_range(spreadsheet_id: str, range_name: str) -> str:
    """
    Clears data values from a specified range in a Google Sheet without altering formatting.
    Args:
        spreadsheet_id: The ID of the spreadsheet.
        range_name: Cell range to wipe clean (e.g., 'Sheet1!A2:E50' or 'Sheet1!B5').
    """
    try:
        service = get_sheets_service()
        service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            body={}
        ).execute()
        return f"🧹 Successfully cleared values from range `{range_name}`."
    except Exception as e:
        return f"Failed to clear sheet range: {str(e)}"

@tool
def delete_sheet_row(spreadsheet_id: str, row_number: int, sheet_id: int = 0) -> str:
    """
    Completely deletes an entire row by row number (1-indexed, e.g., row 2) from a Google Sheet.
    Args:
        spreadsheet_id: The ID of the spreadsheet.
        row_number: Row number as seen in the sheet UI (e.g., row 2, 3, etc.). Must be >= 1.
        sheet_id: The integer sheet tab ID (default is 0 for the first sheet tab).
    """
    try:
        service = get_sheets_service()
        # Google Sheets batchUpdate dimension indices are 0-indexed (Row 2 = index 1 to 2)
        start_index = max(0, row_number - 1)
        end_index = start_index + 1

        request_body = {
            "requests": [
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": start_index,
                            "endIndex": end_index
                        }
                    }
                }
            ]
        }

        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body=request_body
        ).execute()

        return f"🗑️ Successfully deleted row {row_number} from the spreadsheet."
    except Exception as e:
        return f"Failed to delete row {row_number}: {str(e)}"    