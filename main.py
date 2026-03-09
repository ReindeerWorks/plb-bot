from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from supabase import create_client
from twilio.twiml.messaging_response import MessagingResponse
import os

app = FastAPI()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# ---------------------------
# Utility: Twilio Response
# ---------------------------

def twiml(message):
    resp = MessagingResponse()
    resp.message(message)
    return HTMLResponse(str(resp), media_type="application/xml")


# ---------------------------
# Add Task
# ---------------------------

def add_task(owner, task):

    supabase.table("tasks").insert({
        "owner": owner,
        "category": "action",
        "task": task,
        "status": "open"
    }).execute()

    return f"✅ Task added\n{owner.capitalize()}: {task}"


# ---------------------------
# Get Today View
# ---------------------------

def get_today(owner):

    daily = supabase.table("tasks") \
        .select("*") \
        .eq("owner", owner) \
        .eq("category", "daily") \
        .eq("status", "open") \
        .execute()

    action = supabase.table("tasks") \
        .select("*") \
        .eq("owner", owner) \
        .eq("category", "action") \
        .eq("status", "open") \
        .execute()

    msg = f"{owner.upper()} — TODAY\n\n"

    if daily.data:
        msg += "DAILY\n"
        for i, t in enumerate(daily.data, start=1):
            msg += f"{i}) {t['task']}\n"

    if action.data:
        msg += "\nNEXT ACTION\n"
        msg += action.data[0]["task"]

    return msg


# ---------------------------
# Get Next Task
# ---------------------------

def get_next():

    action = supabase.table("tasks") \
        .select("*") \
        .eq("category", "action") \
        .eq("status", "open") \
        .limit(1) \
        .execute()

    if action.data:
        return f"NEXT TASK\n{action.data[0]['task']}"

    return "No open tasks."


# ---------------------------
# Help Command
# ---------------------------

def help_text():

    return """
PLB COMMANDS

ADD TASK
R: task → add task to Richard
N: task → add task to Nick
RN: task → add to both

TODAY
r today
n today

NEXT TASK
next

REPORTING
wdw → tasks completed this week
wlm → last week completed
wdm → month completed

SYSTEM
help / codes → show commands
"""


# ---------------------------
# SMS Endpoint
# ---------------------------

@app.post("/sms")
async def sms_reply(request: Request):

    form = await request.form()
    body = form.get("Body", "")

    msg = body.lower().strip()


    # HELP

    if msg in ["help", "codes"]:
        return twiml(help_text())


    # TODAY

    if "today" in msg:

        if msg.startswith("r") or "richard" in msg:
            return twiml(get_today("richard"))

        if msg.startswith("n") or "nick" in msg:
            return twiml(get_today("nick"))


    # NEXT

    if msg == "next":
        return twiml(get_next())


    # ADD TASK RICHARD

    if msg.startswith("r:") or msg.startswith("richard:"):

        task = body.split(":",1)[1].strip()
        return twiml(add_task("richard", task))


    # ADD TASK NICK

    if msg.startswith("n:") or msg.startswith("nick:"):

        task = body.split(":",1)[1].strip()
        return twiml(add_task("nick", task))


    # ADD TASK BOTH

    if msg.startswith("rn:"):

        task = body.split(":",1)[1].strip()

        supabase.table("tasks").insert([
            {"owner":"richard","category":"action","task":task,"status":"open"},
            {"owner":"nick","category":"action","task":task,"status":"open"}
        ]).execute()

        return twiml(f"✅ Task added to both\n{task}")


    return twiml("Command not recognized. Text HELP.")


# ---------------------------
# Dashboard Webpage
# ---------------------------

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():

    richard = supabase.table("tasks") \
        .eq("status","open") \
        .execute().data

    nick = supabase.table("tasks") \
        .select("*") \
        .eq("owner","nick") \
        .eq("status","open") \

        .execute().data


    def render(owner, tasks):

        html = f"<h2>{owner}</h2><ul>"


        for t in tasks:
            html += f"<li><b>{t['category']}</b>: {t['task']}</li>"

        html += "</ul>"
        return html


    page = f"""
    <html>

    <head>

    <title>Punch List Dashboard</title>

    <style>

    body {{
        font-family: Arial;
        padding:40px;
        background:#f7f7f7
    }}

    h1 {{
        margin-bottom:30px
    }}

    h2 {{
        margin-top:40px
    }}

    li {{
        margin:6px 0
    }}

    </style>

    </head>

    <body>

    <h1>Briefly Home Punch Lists</h1>

    {render("Richard", richard)}

    {render("Nick", nick)}

    </body>

    </html>
    """

    return page
