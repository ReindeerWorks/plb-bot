from fastapi import FastAPI, Form
from fastapi.responses import Response
from twilio.twiml.messaging_response import MessagingResponse
import requests
from datetime import datetime

app = FastAPI()

SUPABASE_URL = "https://tznwonivnvbqwxpqrulf.supabase.co"
SUPABASE_KEY = "sb_publishable_zcQVfrbf5YtMuODbei27qg_1-IsyBUE"

headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}

def parse_command(text):

    text = text.strip()

    if text.startswith("R:"):
        owner = "Richard"
        command = text[2:].strip()

    elif text.startswith("N:"):
        owner = "Nick"
        command = text[2:].strip()

    elif text.startswith("RN:"):
        owner = "Both"
        command = text[3:].strip()

    elif text.startswith("PLB:"):
        return {"type": "query", "command": text[4:].strip()}

    else:
        return {"type": "unknown"}

    if command.startswith("+"):
        return {"type": "add", "owner": owner, "task": command[1:].strip()}

    if command.startswith("X") or command.startswith("-"):
        return {"type": "complete", "owner": owner, "task": command[1:].strip()}

    return {"type": "unknown"}

@app.post("/sms")
async def sms_reply(Body: str = Form(...)):

    cmd = parse_command(Body)

    resp = MessagingResponse()

    if cmd["type"] == "add":

        data = {
            "owner": cmd["owner"],
            "task": cmd["task"],
            "status": "open"
        }

        requests.post(
            f"{SUPABASE_URL}/rest/v1/tasks",
            headers=headers,
            json=data
        )

        resp.message(f"✔ Task added\n{cmd['owner']}: {cmd['task']}")

    elif cmd["type"] == "complete":

        task = cmd["task"]

        requests.patch(
            f"{SUPABASE_URL}/rest/v1/tasks?task=eq.{task}",
            headers=headers,
            json={
                "status": "completed",
                "completed_at": datetime.utcnow().isoformat()
            }
        )

        resp.message(f"✔ Task completed\n{task}")

    elif cmd["type"] == "query":

        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/tasks?status=eq.open",
            headers=headers
        )

        tasks = r.json()

        if not tasks:
            resp.message("No open tasks.")
        else:
            text = "Open Tasks:\n"
            for t in tasks[:10]:
                text += f"• {t['owner']}: {t['task']}\n"

            resp.message(text)

    else:

        resp.message("Command not recognized.")

    return str(resp)
