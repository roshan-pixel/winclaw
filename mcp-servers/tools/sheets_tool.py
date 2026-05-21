"""sheets_tool.py - Google Sheets MCP tool for OpenClaw"""
import json
from pathlib import Path

TOKEN_PATH = Path(r"C:\Users\sgarm\.openclaw\credentials\google_workspace_token.json")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def _get_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return build("sheets", "v4", credentials=creds)

def sheets_action(action: str, **kwargs) -> dict:
    """
    Actions: read, write, append, clear, info, create
    """
    try:
        svc = _get_service().spreadsheets()

        if action == "read":
            sid = kwargs["id"]
            range_ = kwargs.get("range", "Sheet1")
            res = svc.values().get(spreadsheetId=sid, range=range_).execute()
            return {"values": res.get("values", []), "range": res.get("range","")}

        elif action == "write":
            sid = kwargs["id"]
            range_ = kwargs.get("range", "Sheet1!A1")
            values = kwargs.get("values", [])
            res = svc.values().update(spreadsheetId=sid, range=range_,
                valueInputOption="USER_ENTERED", body={"values": values}).execute()
            return {"updated_cells": res.get("updatedCells",0), "range": res.get("updatedRange","")}

        elif action == "append":
            sid = kwargs["id"]
            range_ = kwargs.get("range", "Sheet1")
            values = kwargs.get("values", [])
            res = svc.values().append(spreadsheetId=sid, range=range_,
                valueInputOption="USER_ENTERED", body={"values": values}).execute()
            return {"appended": True, "range": res.get("updates",{}).get("updatedRange","")}

        elif action == "clear":
            sid = kwargs["id"]
            range_ = kwargs.get("range", "Sheet1")
            svc.values().clear(spreadsheetId=sid, range=range_, body={}).execute()
            return {"cleared": True, "range": range_}

        elif action == "info":
            sid = kwargs["id"]
            res = _get_service().spreadsheets().get(spreadsheetId=sid,
                fields="properties,sheets.properties").execute()
            return {
                "title": res["properties"]["title"],
                "sheets": [s["properties"]["title"] for s in res.get("sheets", [])]
            }

        elif action == "create":
            title = kwargs.get("title", "New Spreadsheet")
            res = _get_service().spreadsheets().create(body={"properties": {"title": title}}).execute()
            return {"id": res["spreadsheetId"], "title": title, "url": res["spreadsheetUrl"]}

        else:
            return {"error": f"Unknown action: {action}"}

    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    import sys
    action = sys.argv[1] if len(sys.argv) > 1 else "info"
    extra = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    print(json.dumps(sheets_action(action, **extra), indent=2, default=str))
