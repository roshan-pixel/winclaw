"""docs_tool.py - Google Docs MCP tool for OpenClaw"""
import json
from pathlib import Path

TOKEN_PATH = Path(r"C:\Users\sgarm\.openclaw\credentials\google_workspace_token.json")
SCOPES = ["https://www.googleapis.com/auth/documents", "https://www.googleapis.com/auth/drive"]

def _get_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return build("docs", "v1", credentials=creds)

def _extract_text(doc) -> str:
    text = ""
    for el in doc.get("body", {}).get("content", []):
        for pe in el.get("paragraph", {}).get("elements", []):
            text += pe.get("textRun", {}).get("content", "")
    return text

def docs_action(action: str, **kwargs) -> dict:
    """
    Actions: read, append, create, replace
    """
    try:
        svc = _get_service()

        if action == "read":
            doc_id = kwargs["id"]
            doc = svc.documents().get(documentId=doc_id).execute()
            text = _extract_text(doc)
            return {"title": doc["title"], "text": text[:3000], "chars": len(text)}

        elif action == "append":
            doc_id = kwargs["id"]
            text = kwargs.get("text", "")
            requests_ = [{"insertText": {"location": {"index": 1}, "text": text + "\n"}}]
            svc.documents().batchUpdate(documentId=doc_id, body={"requests": requests_}).execute()
            return {"appended": True, "chars": len(text)}

        elif action == "create":
            title = kwargs.get("title", "New Document")
            content = kwargs.get("content", "")
            doc = svc.documents().create(body={"title": title}).execute()
            doc_id = doc["documentId"]
            if content:
                requests_ = [{"insertText": {"location": {"index": 1}, "text": content}}]
                svc.documents().batchUpdate(documentId=doc_id, body={"requests": requests_}).execute()
            return {"id": doc_id, "title": title,
                    "url": f"https://docs.google.com/document/d/{doc_id}/edit"}

        elif action == "replace":
            doc_id = kwargs["id"]
            find = kwargs.get("find", "")
            replace = kwargs.get("replace", "")
            requests_ = [{"replaceAllText": {
                "containsText": {"text": find, "matchCase": True},
                "replaceText": replace
            }}]
            res = svc.documents().batchUpdate(documentId=doc_id, body={"requests": requests_}).execute()
            return {"replaced": True, "occurrences": res.get("replies", [{}])[0].get("replaceAllText", {}).get("occurrencesChanged", 0)}

        else:
            return {"error": f"Unknown action: {action}"}

    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    import sys
    action = sys.argv[1] if len(sys.argv) > 1 else "read"
    extra = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    print(json.dumps(docs_action(action, **extra), indent=2, default=str))
