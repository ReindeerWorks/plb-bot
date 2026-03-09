from fastapi import FastAPI, Form
from fastapi.responses import Response
from twilio.twiml.messaging_response import MessagingResponse
from supabase import create_client
import os
from datetime import datetime, timedelta
import re

app = FastAPI()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def normalize_command(text):
    text = text.strip()
    text = re.sub(r"\s+", " ", text)
    return text


def get_help():

    message = (
        "Punch List Bot Commands\n\n"

        "LISTS\n"
        "R?  → Richard Daily + Action tasks\n"
        "N?  → Nick Daily + Action tasks\n"
        "NEXT → Next steps list\n\n"

        "TASK MANAGEMENT\n"
        "R: task → Add task to Richard\n"
        "N: task → Add task to Nick\n"
        "R: X # → Complete Richard task\n"
        "N: X # → Complete Nick task\n\n"

        "REPORTS\n"
        "WDW → Work Done This Week\n\n"

        "HELP\n"
        "HELP / CODES → Show this guide"
    )

    return message


def get_punch_list(owner):

    response = supabase.table("tasks") \
        .select("id,category,task") \
        .eq("owner", owner) \
        .eq("status", "open") \
        .execute()

    daily = []
    action = []

    for row in response.data:

        line = f"{row['id']} {row['task']}"

        if row["category"] == "daily":
            daily.append(line)

        elif row["category"] == "action":
            action.append(line)

    message = f"{owner} — Punch List\n\n"

    if daily:
        message += "Daily\n"
        message += "\n".join(daily)
        message += "\n\n"

    if action:
        message += "Action\n"
        message += "\n".join(action)

    return message


def get_next_steps():

    response = supabase.table("tasks") \
        .select("id,owner,task,category") \
        .eq("status", "open") \
        .execute()

    message = "Next Steps\n\n"

    for row in response.data:

        if row["category"] in ["daily", "action"]:
            message += f"{row['owner']} {row['id']} {row['task']}\n"

    return message


def get_wdw():

    week_start = datetime.utcnow() - timedelta(days=7)

    response = supabase.table("tasks") \
        .select("owner,task") \
        .eq("status", "completed") \
        .gte("completed_at", week_start.isoformat()) \
        .execute()

    richard = []
    nick = []

    for row in response.data:

        if row["owner"] == "Richard":
            richard.append(f"✔ {row['task']}")

        if row["owner"] == "Nick":
            nick.append(f"✔ {row['task']}")

    message = "Work Done This Week\n\n"

    if richard:
        message += "Richard\n"
        message += "\n".join(richard)
        message += "\n\n"

    if nick:
        message += "Nick\n"
        message += "\n".join(nick)

    return message


@app.post("/sms")
async def sms(Body: str = Form(...)):

    body = normalize_command(Body)
    body_upper = body.upper()

    resp = MessagingResponse()

    # HELP / CODES
    if body_upper in ["HELP", "CODES", "COMMANDS"]:
        resp.message(get_help())
        return Response(str(resp), media_type="application/xml")

    # R?
    if re.match(r"^R\s*\?$", body_upper):
        resp.message(get_punch_list("Richard"))
        return Response(str(resp), media_type="application/xml")

    # N?
    if re.match(r"^N\s*\?$", body_upper):
        resp.message(get_punch_list("Nick"))
        return Response(str(resp), media_type="application/xml")

    # NEXT
    if body_upper == "NEXT":
        resp.message(get_next_steps())
        return Response(str(resp), media_type="application/xml")

    # WDW
    if body_upper == "WDW":
        resp.message(get_wdw())
        return Response(str(resp), media_type="application/xml")

    # ADD OR COMPLETE TASKS

    if re.match(r"^R\s*:", body_upper):

        task_text = body.split(":", 1)[1].strip()

        if task_text.upper().startswith("X"):

            task_id = task_text[1:].strip()

            supabase.table("tasks") \
                .update({
                    "status": "completed",
                    "completed_at": datetime.utcnow().isoformat()
                }) \
                .eq("id", task_id) \
                .execute()

            resp.message("✔ Richard task completed")
            return Response(str(resp), media_type="application/xml")

        supabase.table("tasks").insert({
            "owner": "Richard",
            "task": task_text,
            "category": "action"
        }).execute()

        resp.message("✔ Task added for Richard")
        return Response(str(resp), media_type="application/xml")

    if re.match(r"^N\s*:", body_upper):

        task_text = body.split(":", 1)[1].strip()

        if task_text.upper().startswith("X"):

            task_id = task_text[1:].strip()

            supabase.table("tasks") \
                .update({
                    "status": "completed",
                    "completed_at": datetime.utcnow().isoformat()
                }) \
                .eq("id", task_id) \
                .execute()

            resp.message("✔ Nick task completed")
            return Response(str(resp), media_type="application/xml")

        supabase.table("tasks").insert({
            "owner": "Nick",
            "task": task_text,
            "category": "action"
        }).execute()

        resp.message("✔ Task added for Nick")
        return Response(str(resp), media_type="application/xml")

    resp.message("Unknown command. Text HELP for command list.")
    return Response(str(resp), media_type="application/xml")
