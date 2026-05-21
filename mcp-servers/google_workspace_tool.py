"""google_workspace_tool.py - Google Workspace MCP Tool for OpenClaw
Provides Gmail, Drive, Sheets, Docs access via WhatsApp commands
"""
import json
import sys
import os
from pathlib import Path
from typing import Sequence

# Add parent dir to path
sys.path.insert(0, str(Path(__file__).parent))
from tools import BaseTool
from mcp.types import Tool, TextContent

DEFAULT_STATE_DIR = Path(os.environ.get("OPENCLAW_HOME", str(Path.home()))).expanduser() / ".openclaw"
OPENCLAW_STATE_DIR = Path(os.environ.get("OPENCLAW_STATE_DIR", str(DEFAULT_STATE_DIR))).expanduser()
TOKEN_PATH = Path(
    os.environ.get(
        "OPENCLAW_GOOGLE_TOKEN_PATH",
        str(OPENCLAW_STATE_DIR / "credentials" / "google_workspace_token.json"),
    )
)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]

def _get_creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    if not TOKEN_PATH.exists():
        setup_cmd = os.environ.get(
            "OPENCLAW_GOOGLE_AUTH_SETUP_CMD",
            "python <path-to-google_auth_setup.py>",
        )
        raise FileNotFoundError(
            f"Google Workspace token not found at {TOKEN_PATH}. "
            f"Create one and/or set OPENCLAW_GOOGLE_TOKEN_PATH. Suggested command: {setup_cmd}"
        )
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return creds


class GoogleWorkspaceTool(BaseTool):
    def __init__(self):
        super().__init__(
            name="google-workspace",
            description=(
                "Control Google Workspace: Gmail, Drive, Sheets, Docs. "
                "service: gmail|drive|sheets|docs. "
                "Gmail actions: stats, unread, search(query), read(id), send(to,subject,body), draft(to,subject,body). "
                "Drive actions: list, search(query), info(id), download(id,path), share(id,email,role). "
                "Sheets actions: read(id,range), write(id,range,values), append(id,range,values), create(title), info(id). "
                "Docs actions: read(id), append(id,text), create(title,content), replace(id,find,replace)."
            )
        )

    def get_tool_definition(self) -> Tool:
        return Tool(
            name=self.name,
            description=self.description,
            inputSchema={
                "type": "object",
                "properties": {
                    "service": {
                        "type": "string",
                        "enum": ["gmail", "drive", "sheets", "docs"],
                        "description": "Which Google service to use"
                    },
                    "action": {
                        "type": "string",
                        "description": "Action to perform (see tool description)"
                    },
                    "params": {
                        "type": "object",
                        "description": "Parameters for the action (id, query, to, subject, body, range, values, etc.)"
                    }
                },
                "required": ["service", "action"]
            }
        )

    async def execute(self, arguments: dict) -> Sequence[TextContent]:
        service = arguments.get("service", "").lower()
        action = arguments.get("action", "").lower()
        params = arguments.get("params", {})

        try:
            creds = _get_creds()
            result = {}

            # ── GMAIL ──────────────────────────────────────────────────────────
            if service == "gmail":
                import base64
                from email.mime.text import MIMEText
                from googleapiclient.discovery import build
                svc = build("gmail", "v1", credentials=creds)

                if action == "stats":
                    p = svc.users().getProfile(userId="me").execute()
                    result = {"email": p["emailAddress"], "total_messages": p["messagesTotal"]}

                elif action == "unread":
                    res = svc.users().messages().list(
                        userId="me", q="is:unread", maxResults=params.get("max", 10)).execute()
                    msgs = []
                    for m in res.get("messages", []):
                        msg = svc.users().messages().get(userId="me", id=m["id"], format="metadata",
                            metadataHeaders=["From","Subject","Date"]).execute()
                        hdrs = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
                        msgs.append({"id": m["id"], "from": hdrs.get("From",""),
                                     "subject": hdrs.get("Subject",""), "date": hdrs.get("Date","")})
                    result = {"unread_count": len(msgs), "messages": msgs}

                elif action == "search":
                    res = svc.users().messages().list(
                        userId="me", q=params.get("query",""), maxResults=params.get("max", 10)).execute()
                    msgs = []
                    for m in res.get("messages", []):
                        msg = svc.users().messages().get(userId="me", id=m["id"], format="metadata",
                            metadataHeaders=["From","Subject","Date"]).execute()
                        hdrs = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
                        msgs.append({"id": m["id"], "from": hdrs.get("From",""),
                                     "subject": hdrs.get("Subject",""), "date": hdrs.get("Date","")})
                    result = {"count": len(msgs), "messages": msgs}

                elif action == "read":
                    msg = svc.users().messages().get(
                        userId="me", id=params["id"], format="full").execute()
                    hdrs = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
                    body = ""
                    if "parts" in msg["payload"]:
                        for p in msg["payload"]["parts"]:
                            if p["mimeType"] == "text/plain" and "data" in p.get("body", {}):
                                body = base64.urlsafe_b64decode(p["body"]["data"]).decode("utf-8", errors="replace")
                                break
                    elif "data" in msg["payload"].get("body", {}):
                        body = base64.urlsafe_b64decode(msg["payload"]["body"]["data"]).decode("utf-8", errors="replace")
                    result = {"from": hdrs.get("From",""), "subject": hdrs.get("Subject",""),
                              "date": hdrs.get("Date",""), "body": body[:2000]}

                elif action == "send":
                    msg = MIMEText(params.get("body",""))
                    msg["to"] = params.get("to","")
                    msg["subject"] = params.get("subject","")
                    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
                    res = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
                    result = {"sent": True, "id": res["id"], "to": params.get("to","")}

                elif action == "draft":
                    msg = MIMEText(params.get("body",""))
                    msg["to"] = params.get("to","")
                    msg["subject"] = params.get("subject","")
                    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
                    res = svc.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
                    result = {"drafted": True, "id": res["id"]}

            # ── DRIVE ──────────────────────────────────────────────────────────
            elif service == "drive":
                from googleapiclient.discovery import build
                svc = build("drive", "v3", credentials=creds)

                if action == "list":
                    folder_id = params.get("folder_id", "root")
                    res = svc.files().list(
                        q=f"'{folder_id}' in parents and trashed=false",
                        pageSize=params.get("max", 20),
                        fields="files(id,name,mimeType,size,modifiedTime)").execute()
                    result = {"files": res.get("files", [])}

                elif action == "search":
                    res = svc.files().list(
                        q=f"name contains '{params.get('query','')}' and trashed=false",
                        pageSize=params.get("max", 20),
                        fields="files(id,name,mimeType,size,modifiedTime,webViewLink)").execute()
                    result = {"count": len(res.get("files",[])), "files": res.get("files",[])}

                elif action == "info":
                    f = svc.files().get(fileId=params["id"],
                        fields="id,name,mimeType,size,modifiedTime,webViewLink").execute()
                    result = f

                elif action == "share":
                    perm = {"type": "user", "role": params.get("role","reader"),
                            "emailAddress": params.get("email","")}
                    svc.permissions().create(fileId=params["id"], body=perm,
                        sendNotificationEmail=False).execute()
                    result = {"shared": True, "with": params.get("email",""), "role": params.get("role","reader")}

            # ── SHEETS ─────────────────────────────────────────────────────────
            elif service == "sheets":
                from googleapiclient.discovery import build
                svc = build("sheets", "v4", credentials=creds).spreadsheets()

                if action == "read":
                    res = svc.values().get(
                        spreadsheetId=params["id"], range=params.get("range","Sheet1")).execute()
                    result = {"values": res.get("values",[]), "range": res.get("range","")}

                elif action == "write":
                    res = svc.values().update(
                        spreadsheetId=params["id"], range=params.get("range","Sheet1!A1"),
                        valueInputOption="USER_ENTERED", body={"values": params.get("values",[])}).execute()
                    result = {"updated_cells": res.get("updatedCells",0)}

                elif action == "append":
                    res = svc.values().append(
                        spreadsheetId=params["id"], range=params.get("range","Sheet1"),
                        valueInputOption="USER_ENTERED", body={"values": params.get("values",[])}).execute()
                    result = {"appended": True}

                elif action == "info":
                    from googleapiclient.discovery import build as build2
                    res = build2("sheets", "v4", credentials=creds).spreadsheets().get(
                        spreadsheetId=params["id"], fields="properties,sheets.properties").execute()
                    result = {"title": res["properties"]["title"],
                              "sheets": [s["properties"]["title"] for s in res.get("sheets",[])]}

                elif action == "create":
                    from googleapiclient.discovery import build as build2
                    res = build2("sheets", "v4", credentials=creds).spreadsheets().create(
                        body={"properties": {"title": params.get("title","New Sheet")}}).execute()
                    result = {"id": res["spreadsheetId"], "url": res["spreadsheetUrl"],
                              "title": params.get("title","New Sheet")}

            # ── DOCS ───────────────────────────────────────────────────────────
            elif service == "docs":
                from googleapiclient.discovery import build
                svc = build("docs", "v1", credentials=creds)

                if action == "read":
                    doc = svc.documents().get(documentId=params["id"]).execute()
                    text = ""
                    for el in doc.get("body",{}).get("content",[]):
                        for pe in el.get("paragraph",{}).get("elements",[]):
                            text += pe.get("textRun",{}).get("content","")
                    result = {"title": doc["title"], "text": text[:3000], "total_chars": len(text)}

                elif action == "append":
                    reqs = [{"insertText": {"location": {"index": 1}, "text": params.get("text","") + "\n"}}]
                    svc.documents().batchUpdate(documentId=params["id"], body={"requests": reqs}).execute()
                    result = {"appended": True}

                elif action == "create":
                    doc = svc.documents().create(body={"title": params.get("title","New Doc")}).execute()
                    doc_id = doc["documentId"]
                    if params.get("content"):
                        reqs = [{"insertText": {"location": {"index": 1}, "text": params["content"]}}]
                        svc.documents().batchUpdate(documentId=doc_id, body={"requests": reqs}).execute()
                    result = {"id": doc_id, "title": params.get("title","New Doc"),
                              "url": f"https://docs.google.com/document/d/{doc_id}/edit"}

                elif action == "replace":
                    reqs = [{"replaceAllText": {
                        "containsText": {"text": params.get("find",""), "matchCase": True},
                        "replaceText": params.get("replace","")
                    }}]
                    res = svc.documents().batchUpdate(documentId=params["id"], body={"requests": reqs}).execute()
                    count = res.get("replies",[{}])[0].get("replaceAllText",{}).get("occurrencesChanged",0)
                    result = {"replaced": True, "occurrences": count}

            else:
                result = {"error": f"Unknown service: {service}. Use: gmail, drive, sheets, docs"}

        except FileNotFoundError as e:
            result = {"error": str(e), "setup_required": True}
        except Exception as e:
            result = {"error": str(e)}

        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]
