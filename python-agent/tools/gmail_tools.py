import base64
import io
import json
import mimetypes
import os
import re
import tempfile
import time
import uuid
from email import encoders
from email.header import Header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from langchain_core.tools import tool

from tools.auth_helper import build_google_credentials

GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.readonly",
]

# ---------------------------------------------------------------------------
# Attachment settings
# ---------------------------------------------------------------------------
# Gmail's limit is 25 MB for the *encoded* message (base64 adds ~37%),
# so keep the raw total at or below 18 MB.
MAX_TOTAL_BYTES = 18 * 1024 * 1024
MAX_FILES = 10
SIMPLE_SEND_LIMIT = 4 * 1024 * 1024  # above this, use resumable upload
STAGE_TTL_SEC = 60 * 60  # staged files are deleted after 1 hour
STAGE_DIR = Path(os.getenv("ATTACHMENT_STAGE_DIR") or (Path(tempfile.gettempdir()) / "otis_attachments"))
REF_RE = re.compile(r"^[0-9a-f]{12}$")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

# Google-native files cannot be downloaded as-is, so they are exported.
EXPORT_MAP = {
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.drawing": ("image/png", ".png"),
}


def get_gmail_service(creds_data: dict):
    creds = build_google_credentials(creds_data)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as err:
            print(f"[AUTH REFRESH WARNING] {err}")
    return build("gmail", "v1", credentials=creds, static_discovery=False)


def get_drive_service(creds_data: dict):
    creds = build_google_credentials(creds_data)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as err:
            print(f"[AUTH REFRESH WARNING] {err}")
    return build("drive", "v3", credentials=creds, static_discovery=False)


# ---------------------------------------------------------------------------
# Attachment staging (files wait on disk until the user taps "Send Email")
# ---------------------------------------------------------------------------

def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 ** 2:.1f} MB"


def _clean_filename(name: str, default: str = "attachment") -> str:
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    name = re.sub(r'[\x00-\x1f"<>|:*?`]', "_", name)
    return name[:120] or default


def _split(value: str, pattern: str = r"[|\n]+") -> list:
    return [p.strip() for p in re.split(pattern, value or "") if p.strip()]


def _cleanup_stage() -> None:
    try:
        now = time.time()
        for p in STAGE_DIR.glob("*"):
            try:
                if now - p.stat().st_mtime > STAGE_TTL_SEC:
                    p.unlink()
            except OSError:
                pass
    except Exception:
        pass


def _stage(data: bytes, filename: str, mime: str = None) -> dict:
    STAGE_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_stage()
    filename = _clean_filename(filename)
    mime = mime or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    if not Path(filename).suffix:
        filename += mimetypes.guess_extension(mime) or ""
    ref = uuid.uuid4().hex[:12]
    (STAGE_DIR / f"{ref}.bin").write_bytes(data)
    meta = {"ref": ref, "filename": filename, "mime": mime, "size": len(data)}
    (STAGE_DIR / f"{ref}.json").write_text(json.dumps(meta), encoding="utf-8")
    return meta


def _load_stage(ref: str):
    if not REF_RE.match(ref or ""):
        return None
    try:
        meta = json.loads((STAGE_DIR / f"{ref}.json").read_text(encoding="utf-8"))
        data = (STAGE_DIR / f"{ref}.bin").read_bytes()
        return meta, data
    except Exception:
        return None


def _discard(ref: str) -> None:
    if not REF_RE.match(ref or ""):
        return
    for ext in (".bin", ".json"):
        try:
            (STAGE_DIR / f"{ref}{ext}").unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Attachment sources
# ---------------------------------------------------------------------------

def _get_chat_attachments() -> list:
    """Files/photos the user sent in the current Telegram message.
    Each item: {"data": <base64>, "mime_type": str, "filename": str | None}
    Set per request by main.py through tools/attachment_context.py."""
    try:
        from tools.attachment_context import get_current_attachments
        return get_current_attachments() or []
    except Exception:
        return []


def _allowed_image_dirs() -> list:
    raw = os.getenv("ATTACHMENT_ALLOWED_DIRS", "")
    dirs = [Path(p.strip().strip("\"'")) for p in raw.split(os.pathsep) if p.strip()]
    dirs += [Path(tempfile.gettempdir()), Path.cwd() / "generated_images"]
    out = []
    for d in dirs:
        try:
            out.append(d.resolve())
        except Exception:
            pass
    return out


def _read_local_image(raw_path: str):
    """Reads a generated image from disk. Only image files inside allowed folders
    are accepted, so a prompt-injected path can never leak tokens.json or .env."""
    try:
        p = Path(raw_path.strip().strip("\"'")).resolve()
    except Exception:
        return None, f"'{raw_path}' is not a valid path."
    if p.suffix.lower() not in IMAGE_EXTS:
        return None, f"'{p.name}' is not an image file."
    if not any(p == d or d in p.parents for d in _allowed_image_dirs()):
        return None, (
            f"'{p.name}' is outside the allowed image folders. "
            "Set ATTACHMENT_ALLOWED_DIRS to the folder where generated images are saved."
        )
    if not p.is_file():
        return None, f"Image file not found: {p.name}"
    if p.stat().st_size > MAX_TOTAL_BYTES:
        return None, f"'{p.name}' is too large ({_fmt_size(p.stat().st_size)})."
    return (p.read_bytes(), p.name, mimetypes.guess_type(p.name)[0] or "image/png"), None


def _resolve_drive_file(service, entry: str):
    """Finds one Drive file by ID or by name. Returns (file_dict, error)."""
    fields = "id,name,mimeType,size,modifiedTime"

    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", entry):
        try:
            f = service.files().get(fileId=entry, fields=fields, supportsAllDrives=True).execute()
            return f, None
        except HttpError:
            pass  # not an ID, treat it as a name

    safe = entry.replace("\\", "\\\\").replace("'", "\\'")
    q = f"name contains '{safe}' and trashed = false and mimeType != 'application/vnd.google-apps.folder'"
    res = service.files().list(
        q=q,
        pageSize=5,
        orderBy="modifiedTime desc",
        fields=f"files({fields})",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
    ).execute()
    files = res.get("files", [])

    if not files:
        return None, f"No Drive file found matching '{entry}'."

    exact = [f for f in files if f["name"].lower() == entry.lower()]
    if len(exact) == 1:
        return exact[0], None
    if len(files) == 1:
        return files[0], None

    lines = "\n".join(
        f"- {f['name']} (id: {f['id']}, modified {f.get('modifiedTime', '?')[:10]})" for f in files
    )
    return None, (
        f"Several Drive files match '{entry}':\n{lines}\n"
        "Ask the user which one, then call send_email again with that file's id in drive_files."
    )


def _download_drive_file(service, f: dict):
    """Returns ((bytes, filename, mime), error)."""
    name = f["name"]
    mime = f.get("mimeType", "")

    if mime.startswith("application/vnd.google-apps."):
        export = EXPORT_MAP.get(mime)
        if not export:
            return None, f"'{name}' is a Google file type that can't be attached."
        export_mime, ext = export
        request = service.files().export_media(fileId=f["id"], mimeType=export_mime)
        if not name.lower().endswith(ext):
            name += ext
        out_mime = export_mime
    else:
        size = int(f.get("size") or 0)
        if size > MAX_TOTAL_BYTES:
            return None, f"'{name}' is too large to email ({_fmt_size(size)}). Limit is {_fmt_size(MAX_TOTAL_BYTES)}."
        request = service.files().get_media(fileId=f["id"], supportsAllDrives=True)
        out_mime = mime or None

    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
        if buf.tell() > MAX_TOTAL_BYTES:
            return None, f"'{name}' is too large to email."
    return (buf.getvalue(), name, out_mime), None


# ---------------------------------------------------------------------------
# Read tools (unchanged)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Send email (with attachments)
# ---------------------------------------------------------------------------

@tool
def send_email(
    recipient: str,
    subject: str,
    body: str,
    drive_files: str = "",
    image_paths: str = "",
    attach_chat_files: bool = False,
    attachment_refs: str = "",
    image_base64: str = None,
) -> str:
    """
    Stages an email draft (optionally with attachments) for user confirmation before sending.
    Always use this when sending an email to a recipient.

    Args:
        recipient: Email address of the recipient.
        subject: Subject line of the email.
        body: Plain text content of the email.
        drive_files: Google Drive files to attach. Use file names or file IDs, separated by " | ".
            Google Docs/Slides are attached as PDF, Sheets as XLSX.
            If several files match a name, this tool lists them: ask the user which one, then retry with the ID.
        image_paths: Generated image ids from generate_image (looks like "gen:ab12cd34ef"),
            separated by " | ". Use this to attach a generated image.
        attach_chat_files: Set True to attach the photo(s) or file(s) the user sent in THIS chat message.
        attachment_refs: Refs of attachments from an earlier staged draft (comma separated).
            Use when the user asks to revise a draft, so the files are kept.
        image_base64: Legacy. Avoid; use image_paths instead.
    """
    recipient = (recipient or "").strip()
    if not EMAIL_RE.match(recipient):
        return (
            f"Error: '{recipient}' is not a valid email address. "
            "Call search_contact to find the exact address, or ask the user for it. Do not stage the draft yet."
        )

    new_refs: list = []   # staged by this call (discarded if we fail)
    metas: list = []      # everything that will be attached

    def fail(msg: str) -> str:
        for r in new_refs:
            _discard(r)
        return msg

    def add(data: bytes, filename: str, mime: str = None) -> None:
        m = _stage(data, filename, mime)
        new_refs.append(m["ref"])
        metas.append(m)

    try:
        # 1. Reuse files from an earlier draft (revise flow)
        for ref in _split(attachment_refs, r"[,|\s]+"):
            loaded = _load_stage(ref)
            if not loaded:
                return fail(f"Error: attachment '{ref}' has expired. Ask the user to send or choose the file again.")
            metas.append(loaded[0])

        # 2. Files/photos sent in this chat message
        if attach_chat_files:
            items = _get_chat_attachments()
            if not items:
                return fail(
                    "Error: the user did not send any file or photo in this message. "
                    "Ask them to send it here, or pick a Drive file instead."
                )
            for i, it in enumerate(items, 1):
                try:
                    raw = base64.b64decode(re.sub(r"^data:[^;]+;base64,", "", it.get("data") or ""))
                except Exception:
                    return fail("Error: could not read the file that was sent in chat.")
                mime = it.get("mime_type") or "application/octet-stream"
                default = f"photo_{i}" if mime.startswith("image/") else f"file_{i}"
                add(raw, it.get("filename") or default, mime)

        # 3. Generated images (local paths)
        for p in _split(image_paths):
            if p.lower().startswith("gen:"):  # generated image kept in memory
                from tools.image_store import get_image
                found = get_image(p)
                if not found:
                    return fail("Error: that generated image has expired. Generate it again, then retry.")
                img, mime = found
                add(img, "generated_image" + (mimetypes.guess_extension(mime) or ".png"), mime)
                continue
            result, err = _read_local_image(p)
            if err:
                return fail(f"Error: {err}")
            add(*result)

        # 4. Legacy base64 image
        if image_base64:
            try:
                raw = base64.b64decode(re.sub(r"^data:image/\w+;base64,", "", image_base64))
            except Exception:
                return fail("Error: the image data is not valid base64.")
            add(raw, "image.png", "image/png")

        # 5. Google Drive files
        entries = _split(drive_files)
        if entries:
            from agent import get_current_google_tokens
            tokens = get_current_google_tokens()
            if not tokens:
                return fail("Error: No Google authentication tokens available.")
            drive = get_drive_service(tokens)
            for entry in entries:
                f, err = _resolve_drive_file(drive, entry)
                if err:
                    return fail(err)
                result, err = _download_drive_file(drive, f)
                if err:
                    return fail(f"Error: {err}")
                add(*result)

    except HttpError as e:
        status = getattr(e.resp, "status", "?")
        return fail(f"Error reading attachment from Google Drive: HTTP {status}. Check that Drive access is granted.")
    except Exception as e:
        return fail(f"Error preparing attachments: {str(e)}")

    if len(metas) > MAX_FILES:
        return fail(f"Error: too many attachments ({len(metas)}). The limit is {MAX_FILES}.")
    total = sum(m["size"] for m in metas)
    if total > MAX_TOTAL_BYTES:
        return fail(
            f"Error: attachments total {_fmt_size(total)}, over the {_fmt_size(MAX_TOTAL_BYTES)} email limit. "
            "Tell the user and offer to send a Drive link instead."
        )

    payload = {
        "action": "send_email",
        "recipient": recipient,
        "subject": subject,
        "body": body,
        "image_base64": None,
        # comma separated on purpose: no brackets inside the [APPROVAL_REQUIRED:...] tag
        "attachment_refs": ",".join(m["ref"] for m in metas),
    }

    attachment_note = ""
    if metas:
        lines = "\n".join(f"  • `{m['filename']}` ({_fmt_size(m['size'])})" for m in metas)
        attachment_note = f"\n📎 *Attachments ({len(metas)}):*\n{lines}"

    return (
        f"✉️ **Email Staged for Approval**\n\n"
        f"**To:** {recipient}\n"
        f"**Subject:** {subject}{attachment_note}\n\n"
        f"**Body:**\n{body}\n\n"
        f"[APPROVAL_REQUIRED:{json.dumps(payload)}]"
    )


def _attach(message: MIMEMultipart, data: bytes, filename: str, mime: str) -> None:
    maintype, _, subtype = (mime or "application/octet-stream").partition("/")
    if not maintype or not subtype:
        maintype, subtype = "application", "octet-stream"
    part = MIMEBase(maintype, subtype)
    part.set_payload(data)
    encoders.encode_base64(part)
    # RFC 2231 encoding keeps non-ASCII file names intact
    part.add_header("Content-Disposition", "attachment", filename=("utf-8", "", filename))
    message.attach(part)


def execute_send_email_direct(
    tokens: dict,
    recipient: str,
    subject: str,
    body: str,
    image_base64: str = None,
    attachment_refs: str = None,
    **_ignored,
) -> str:
    """Directly sends the email via Gmail API once confirmed by the user."""
    try:
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            return "❌ Failed to dispatch email: Google login is missing a refresh token. Please re-authorize."

        recipient = (recipient or "").strip()
        if not EMAIL_RE.match(recipient):
            return f"❌ Failed to dispatch email: '{recipient}' is not a valid email address."

        # Load staged attachments first, so an expired file never sends a half-complete email
        refs = _split(attachment_refs or "", r"[,|\s]+")
        files = []
        for ref in refs:
            loaded = _load_stage(ref)
            if not loaded:
                return "❌ Failed to dispatch email: an attachment expired. Please ask Otis to draft it again."
            meta, data = loaded
            files.append((data, meta["filename"], meta["mime"]))

        if image_base64:  # legacy drafts
            clean_b64 = re.sub(r"^data:image/\w+;base64,", "", image_base64)
            files.append((base64.b64decode(clean_b64), "attachment.png", "image/png"))

        client_id = tokens.get("client_id") or os.getenv("GOOGLE_CLIENT_ID")
        client_secret = tokens.get("client_secret") or os.getenv("GOOGLE_CLIENT_SECRET")
        token_uri = tokens.get("token_uri") or GOOGLE_TOKEN_URI

        creds = Credentials(
            token=tokens.get("token") or tokens.get("access_token"),
            refresh_token=refresh_token,
            token_uri=token_uri,
            client_id=client_id,
            client_secret=client_secret,
            scopes=GMAIL_SCOPES,
        )

        if (not creds.valid or creds.expired) and creds.refresh_token:
            creds.refresh(Request())

        service = build("gmail", "v1", credentials=creds, static_discovery=False)

        message = MIMEMultipart()
        message["To"] = recipient
        message["Subject"] = Header(subject or "", "utf-8")
        message.attach(MIMEText(body or "", "plain", "utf-8"))

        for data, filename, mime in files:
            _attach(message, data, filename, mime)

        raw_bytes = message.as_bytes()
        if len(raw_bytes) > SIMPLE_SEND_LIMIT:
            # big messages must go through the upload endpoint
            media = MediaIoBaseUpload(io.BytesIO(raw_bytes), mimetype="message/rfc822", resumable=True)
            sent = service.users().messages().send(userId="me", body={}, media_body=media).execute()
        else:
            raw_msg = base64.urlsafe_b64encode(raw_bytes).decode("utf-8")
            sent = service.users().messages().send(userId="me", body={"raw": raw_msg}).execute()

        print(f"[GMAIL] Sent message id={sent.get('id')} to {recipient} with {len(files)} attachment(s)")

        for ref in refs:
            _discard(ref)

        note = f" with {len(files)} attachment{'s' if len(files) != 1 else ''}" if files else ""
        return f"✅ Email successfully dispatched to {recipient}{note}."

    except HttpError as e:
        status = getattr(e.resp, "status", "?")
        reason = getattr(e, "reason", None) or str(e)
        return f"❌ Failed to dispatch email: HTTP {status}: {reason}"
    except Exception as e:
        return f"❌ Failed to dispatch email: {str(e)}"