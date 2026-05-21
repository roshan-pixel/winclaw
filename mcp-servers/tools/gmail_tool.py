"""gmail_tool.py - Gmail MCP tool for OpenClaw"""
import json
import base64
import os
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

DEFAULT_STATE_DIR = Path(os.environ.get("OPENCLAW_HOME", str(Path.home()))).expanduser() / ".openclaw"
OPENCLAW_STATE_DIR = Path(os.environ.get("OPENCLAW_STATE_DIR", str(DEFAULT_STATE_DIR))).expanduser()
TOKEN_PATH = Path(
    os.environ.get(
        "OPENCLAW_GOOGLE_TOKEN_PATH",
        str(OPENCLAW_STATE_DIR / "credentials" / "google_workspace_token.json"),
    )
)
SCOPES = ["https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/gmail.send"]

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
    return build("gmail", "v1", credentials=creds)

def gmail_action(action: str, **kwargs) -> dict:
    """
    Actions: search, read, send, draft, stats, unread
    """
    try:
        svc = _get_service()

        if action == "stats":
            p = svc.users().getProfile(userId="me").execute()
            return {"email": p["emailAddress"], "total": p["messagesTotal"], "threads": p["threadsTotal"]}

        elif action == "unread":
            res = svc.users().messages().list(userId="me", q="is:unread", maxResults=kwargs.get("max", 10)).execute()
            msgs = []
            for m in res.get("messages", []):
                msg = svc.users().messages().get(userId="me", id=m["id"], format="metadata",
                    metadataHeaders=["From","Subject","Date"]).execute()
                hdrs = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
                msgs.append({"id": m["id"], "from": hdrs.get("From",""), "subject": hdrs.get("Subject",""), "date": hdrs.get("Date","")})
            return {"unread": len(msgs), "messages": msgs}

        elif action == "search":
            q = kwargs.get("query", "")
            res = svc.users().messages().list(userId="me", q=q, maxResults=kwargs.get("max", 10)).execute()
            msgs = []
            for m in res.get("messages", []):
                msg = svc.users().messages().get(userId="me", id=m["id"], format="metadata",
                    metadataHeaders=["From","Subject","Date"]).execute()
                hdrs = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
                msgs.append({"id": m["id"], "from": hdrs.get("From",""), "subject": hdrs.get("Subject",""), "date": hdrs.get("Date","")})
            return {"count": len(msgs), "messages": msgs}

        elif action == "read":
            msg_id = kwargs.get("id", "")
            msg = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
            hdrs = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            body = ""
            if "parts" in msg["payload"]:
                for p in msg["payload"]["parts"]:
                    if p["mimeType"] == "text/plain":
                        body = base64.urlsafe_b64decode(p["body"]["data"]).decode("utf-8", errors="replace")
                        break
            elif "data" in msg["payload"].get("body", {}):
                body = base64.urlsafe_b64decode(msg["payload"]["body"]["data"]).decode("utf-8", errors="replace")
            return {"from": hdrs.get("From",""), "subject": hdrs.get("Subject",""), "date": hdrs.get("Date",""), "body": body[:2000]}

        elif action == "send":
            to = kwargs.get("to", "")
            subject = kwargs.get("subject", "")
            body = kwargs.get("body", "")
            msg = MIMEText(body)
            msg["to"] = to
            msg["subject"] = subject
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            res = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
            return {"sent": True, "id": res["id"]}

        elif action == "draft":
            to = kwargs.get("to", "")
            subject = kwargs.get("subject", "")
            body = kwargs.get("body", "")
            msg = MIMEText(body)
            msg["to"] = to
            msg["subject"] = subject
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            res = svc.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
            return {"drafted": True, "id": res["id"]}

        else:
            return {"error": f"Unknown action: {action}"}

    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    action = args[0] if args else "stats"
    print(json.dumps(gmail_action(action), indent=2))
