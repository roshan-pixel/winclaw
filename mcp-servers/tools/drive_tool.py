"""drive_tool.py - Google Drive MCP tool for OpenClaw"""
import json
import os
from pathlib import Path

DEFAULT_STATE_DIR = Path(os.environ.get("OPENCLAW_HOME", str(Path.home()))).expanduser() / ".openclaw"
OPENCLAW_STATE_DIR = Path(os.environ.get("OPENCLAW_STATE_DIR", str(DEFAULT_STATE_DIR))).expanduser()
TOKEN_PATH = Path(
    os.environ.get(
        "OPENCLAW_GOOGLE_TOKEN_PATH",
        str(OPENCLAW_STATE_DIR / "credentials" / "google_workspace_token.json"),
    )
)
SCOPES = ["https://www.googleapis.com/auth/drive"]

def _get_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    if not TOKEN_PATH.exists():
        raise FileNotFoundError(
            f"Google token not found at {TOKEN_PATH}. Set OPENCLAW_GOOGLE_TOKEN_PATH."
        )
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return build("drive", "v3", credentials=creds)

def drive_action(action: str, **kwargs) -> dict:
    """
    Actions: list, search, info, download, delete, share
    """
    try:
        svc = _get_service()

        if action == "list":
            folder_id = kwargs.get("folder_id", "root")
            q = f"'{folder_id}' in parents and trashed=false"
            res = svc.files().list(q=q, pageSize=kwargs.get("max", 20),
                fields="files(id,name,mimeType,size,modifiedTime)").execute()
            return {"files": res.get("files", [])}

        elif action == "search":
            query = kwargs.get("query", "")
            q = f"name contains '{query}' and trashed=false"
            res = svc.files().list(q=q, pageSize=kwargs.get("max", 20),
                fields="files(id,name,mimeType,size,modifiedTime,webViewLink)").execute()
            return {"count": len(res.get("files", [])), "files": res.get("files", [])}

        elif action == "info":
            file_id = kwargs.get("id", "")
            f = svc.files().get(fileId=file_id,
                fields="id,name,mimeType,size,modifiedTime,webViewLink,owners").execute()
            return f

        elif action == "download":
            file_id = kwargs.get("id", "")
            default_out = Path.home() / "Downloads" / "drive_file"
            out_path = Path(
                kwargs.get("path")
                or os.environ.get("OPENCLAW_DRIVE_DOWNLOAD_PATH")
                or str(default_out)
            ).expanduser()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            from googleapiclient.http import MediaIoBaseDownload
            import io
            req = svc.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            dl = MediaIoBaseDownload(fh, req)
            done = False
            while not done:
                _, done = dl.next_chunk()
            with open(out_path, "wb") as f:
                f.write(fh.getvalue())
            return {"downloaded": True, "path": str(out_path), "size": len(fh.getvalue())}

        elif action == "delete":
            file_id = kwargs.get("id", "")
            svc.files().trash(fileId=file_id).execute()
            return {"trashed": True, "id": file_id}

        elif action == "share":
            file_id = kwargs.get("id", "")
            email = kwargs.get("email", "")
            role = kwargs.get("role", "reader")
            perm = {"type": "user", "role": role, "emailAddress": email}
            svc.permissions().create(fileId=file_id, body=perm, sendNotificationEmail=False).execute()
            return {"shared": True, "with": email, "role": role}

        else:
            return {"error": f"Unknown action: {action}"}

    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    import sys
    action = sys.argv[1] if len(sys.argv) > 1 else "list"
    print(json.dumps(drive_action(action), indent=2, default=str))
