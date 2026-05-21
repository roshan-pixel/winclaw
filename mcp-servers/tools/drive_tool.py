"""drive_tool.py - Google Drive MCP tool for OpenClaw"""
import json
import os
from pathlib import Path

TOKEN_PATH = Path(r"C:\Users\sgarm\.openclaw\credentials\google_workspace_token.json")
SCOPES = ["https://www.googleapis.com/auth/drive"]

def _get_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
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
            out_path = kwargs.get("path", r"C:\Users\sgarm\Downloads\drive_file")
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
            return {"downloaded": True, "path": out_path, "size": len(fh.getvalue())}

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
