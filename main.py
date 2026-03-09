from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from supabase import create_client
from twilio.twiml.messaging_response import MessagingResponse
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import os
import html
import re

app = FastAPI()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_KEY environment variables.")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# -------------------------
# Helpers
# -------------------------

VALID_OWNERS = {
    "r": "richard",
    "richard": "richard",
    "n": "nick",
    "nick": "nick",
    "rn": "both",
    "both": "both",
}

VALID_CATEGORIES = {
    "d": "daily",
    "daily": "daily",
    "a": "action",
    "action": "action",
    "l": "long_term",
    "long": "long_term",
    "long_term": "long_term",
    "lt": "long_term",
}

DISPLAY_OWNER = {
    "richard": "Richard",
    "nick": "Nick",
}

DISPLAY_CATEGORY = {
    "daily": "Daily",
    "action": "Action Items",
    "long_term": "Long Term",
}

SECTION_ORDER = ["daily", "action", "long_term"]


def twiml(message: str) -> HTMLResponse:
    resp = MessagingResponse()
    resp.message(message.strip())
    return HTMLResponse(str(resp), media_type="application/xml")


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def escape_text(text: str) -> str:
    return html.escape(text or "")


def get_tasks(owner: str, status: str = "open"):
    response = (
        supabase.table("tasks")
        .select("*")
        .eq("owner", owner)
        .eq("status", status)
        .execute()
    )
    tasks = response.data or []
    tasks.sort(
        key=lambda t: (
            0 if t.get("priority") else 1,
            SECTION_ORDER.index(t["category"]) if t.get("category") in SECTION_ORDER else 999,
            t.get("created_at") or "",
            t.get("id") or 0,
        )
    )
    return tasks


def group_tasks(tasks):
    grouped = defaultdict(list)
    for task in tasks:
        grouped[task.get("category", "action")].append(task)
    return grouped


def format_task_line(task, show_id: bool = True, show_priority: bool = True):
    flame = "🔥 " if task.get("priority") and show_priority else ""
    task_id = f"{task['id']} " if show_id else ""
    return f"{task_id}{flame}{task['task']}"


def get_help_text():
    return """
PLB COMMANDS

LISTS
R?              = Richard full list
N?              = Nick full list
R TODAY         = Richard today view
N TODAY         = Nick today view
R NEXT          = Richard next action
N NEXT          = Nick next action
NEXT            = Global next priority/action task
WDW             = Work done this week
R DONE          = Richard completed today
N DONE          = Nick completed today

ADD TASKS
R D task        = add daily task to Richard
R A task        = add action item to Richard
R L task        = add long term task to Richard
N D task        = add daily task to Nick
N A task        = add action item to Nick
N L task        = add long term task to Nick

HIGH PRIORITY
R A ! task      = add high priority action item
N A ! task      = add high priority action item

COMPLETE
R X 12          = complete Richard task by ID
N X 15          = complete Nick task by ID

WEB
/dashboard      = full dashboard
/today          = today command center
/focus          = high priority focus page

HELP
HELP / CODES    = show this guide
""".strip()


def add_task(owner: str, category: str, task_text: str, priority: bool = False):
    task_text = normalize_spaces(task_text)
    if not task_text:
        return "Task text cannot be empty."

    existing = (
        supabase.table("tasks")
        .select("id")
        .eq("owner", owner)
        .eq("status", "open")
        .eq("category", category)
        .eq("task", task_text)
        .execute()
    )

    if existing.data:
        return f"Already exists for {DISPLAY_OWNER[owner]}."

    (
        supabase.table("tasks")
        .insert(
            {
                "owner": owner,
                "category": category,
                "task": task_text,
                "status": "open",
                "priority": priority,
            }
        )
        .execute()
    )

    prefix = "🔥 " if priority else ""
    return f"✅ Added for {DISPLAY_OWNER[owner]}\n{DISPLAY_CATEGORY[category]}: {prefix}{task_text}"


def complete_task_by_id(owner: str, task_id: int):
    task_lookup = (
        supabase.table("tasks")
        .select("id, owner, task, status")
        .eq("id", task_id)
        .execute()
    )

    if not task_lookup.data:
        return "Task ID not found."

    task = task_lookup.data[0]

    if task["owner"] != owner:
        return f"Task {task_id} does not belong to {DISPLAY_OWNER[owner]}."

    if task["status"] == "completed":
        return f"Task {task_id} is already completed."

    (
        supabase.table("tasks")
        .update({"status": "completed", "completed_at": now_utc_iso()})
        .eq("id", task_id)
        .execute()
    )

    return f"✅ Completed\n{task_id} {task['task']}"


def get_owner_today(owner: str):
    tasks = get_tasks(owner, status="open")
    grouped = group_tasks(tasks)

    lines = [f"{DISPLAY_OWNER[owner]} — Today", ""]

    for category in ["daily", "action", "long_term"]:
        section_tasks = grouped.get(category, [])
        if not section_tasks:
            continue
        lines.append(f"{DISPLAY_CATEGORY[category]} ({len(section_tasks)})")
        for task in section_tasks:
            lines.append(f"• {format_task_line(task, show_id=True, show_priority=True)}")
        lines.append("")

    if len(lines) == 2:
        return f"{DISPLAY_OWNER[owner]} — Today\n\nNo open tasks."

    return "\n".join(lines).strip()


def get_owner_next(owner: str):
    tasks = get_tasks(owner, status="open")
    action_tasks = [t for t in tasks if t.get("category") == "action"]

    if not action_tasks:
        return f"{DISPLAY_OWNER[owner]} has no open action items."

    next_task = action_tasks[0]
    return f"{DISPLAY_OWNER[owner]} — Next\n\n{format_task_line(next_task, show_id=True, show_priority=True)}"


def get_global_next():
    all_tasks = []
    for owner in ["richard", "nick"]:
        all_tasks.extend(get_tasks(owner, status="open"))

    action_tasks = [t for t in all_tasks if t.get("category") == "action"]

    if not action_tasks:
        return "No open action items."

    action_tasks.sort(
        key=lambda t: (
            0 if t.get("priority") else 1,
            t.get("created_at") or "",
            t.get("id") or 0,
        )
    )

    next_task = action_tasks[0]
    owner_name = DISPLAY_OWNER[next_task["owner"]]
    return f"Next Task\n\n{owner_name}: {format_task_line(next_task, show_id=True, show_priority=True)}"


def get_owner_full_list(owner: str):
    tasks = get_tasks(owner, status="open")
    grouped = group_tasks(tasks)

    lines = [f"{DISPLAY_OWNER[owner]} — Punch List", ""]

    for category in SECTION_ORDER:
        section_tasks = grouped.get(category, [])
        if not section_tasks:
            continue
        lines.append(f"{DISPLAY_CATEGORY[category]} ({len(section_tasks)})")
        for task in section_tasks:
            lines.append(f"• {format_task_line(task, show_id=True, show_priority=True)}")
        lines.append("")

    if len(lines) == 2:
        return f"{DISPLAY_OWNER[owner]} — Punch List\n\nNo open tasks."

    return "\n".join(lines).strip()


def get_completed_since(owner: str | None, since_dt: datetime, title: str):
    query = (
        supabase.table("tasks")
        .select("id, owner, task, completed_at")
        .eq("status", "completed")
        .gte("completed_at", since_dt.isoformat())
        .execute()
    )

    tasks = query.data or []
    tasks.sort(key=lambda t: (t.get("owner") or "", t.get("completed_at") or ""))

    if owner:
        tasks = [t for t in tasks if t["owner"] == owner]

    if not tasks:
        if owner:
            return f"{title}\n\nNo completed tasks for {DISPLAY_OWNER[owner]}."
        return f"{title}\n\nNo completed tasks."

    lines = [title, ""]
    grouped = defaultdict(list)
    for task in tasks:
        grouped[task["owner"]].append(task)

    for owner_key in ["richard", "nick"]:
        owner_tasks = grouped.get(owner_key, [])
        if not owner_tasks:
            continue
        lines.append(DISPLAY_OWNER[owner_key])
        for task in owner_tasks:
            lines.append(f"✓ {task['id']} {task['task']}")
        lines.append("")

    return "\n".join(lines).strip()


def get_completed_today(owner: str):
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return get_completed_since(owner, start, f"{DISPLAY_OWNER[owner]} — Completed Today")


def get_wdw():
    start = datetime.now(timezone.utc) - timedelta(days=7)
    return get_completed_since(None, start, "Work Done This Week")


def parse_sms_command(body: str):
    raw = normalize_spaces(body)
    lowered = raw.lower()

    if lowered in {"help", "codes", "commands"}:
        return {"type": "help"}

    if re.fullmatch(r"r\s*\?", lowered):
        return {"type": "full_list", "owner": "richard"}

    if re.fullmatch(r"n\s*\?", lowered):
        return {"type": "full_list", "owner": "nick"}

    if lowered == "wdw":
        return {"type": "wdw"}

    if lowered == "next":
        return {"type": "global_next"}

    owner_today = re.fullmatch(r"(richard|r|nick|n)\s*:?\s*today", lowered)
    if owner_today:
        owner = VALID_OWNERS[owner_today.group(1)]
        return {"type": "today", "owner": owner}

    owner_next = re.fullmatch(r"(richard|r|nick|n)\s*:?\s*next", lowered)
    if owner_next:
        owner = VALID_OWNERS[owner_next.group(1)]
        return {"type": "owner_next", "owner": owner}

    owner_done = re.fullmatch(r"(richard|r|nick|n)\s*:?\s*done", lowered)
    if owner_done:
        owner = VALID_OWNERS[owner_done.group(1)]
        return {"type": "done_today", "owner": owner}

    complete_match = re.fullmatch(r"(richard|r|nick|n)\s*:?\s*x\s+(\d+)", lowered)
    if complete_match:
        owner = VALID_OWNERS[complete_match.group(1)]
        task_id = int(complete_match.group(2))
        return {"type": "complete", "owner": owner, "task_id": task_id}

    add_match = re.fullmatch(
        r"(richard|r|nick|n|rn|both)\s*:?\s+(daily|d|action|a|long_term|long|long term|l)\s+(!)?\s*(.+)",
        lowered,
    )
    if add_match:
        owner_raw = add_match.group(1)
        category_raw = add_match.group(2).replace(" ", "_")
        priority_flag = bool(add_match.group(3))
        task_text = raw.split(maxsplit=3)[-1]

        owner = VALID_OWNERS[owner_raw]
        category = VALID_CATEGORIES[category_raw]

        if priority_flag and task_text.startswith("!"):
            task_text = task_text[1:].strip()

        return {
            "type": "add",
            "owner": owner,
            "category": category,
            "priority": priority_flag,
            "task_text": task_text,
        }

    legacy_add_match = re.fullmatch(r"(richard|r|nick|n|rn)\s*:\s*(.+)", lowered)
    if legacy_add_match:
        owner_raw = legacy_add_match.group(1)
        owner = VALID_OWNERS[owner_raw]
        task_text = raw.split(":", 1)[1].strip()
        return {
            "type": "legacy_add",
            "owner": owner,
            "category": "action",
            "priority": False,
            "task_text": task_text,
        }

    return {"type": "unknown"}


# -------------------------
# SMS Endpoint
# -------------------------

@app.post("/sms")
async def sms_reply(request: Request):
    form = await request.form()
    body = form.get("Body", "")
    command = parse_sms_command(body)

    if command["type"] == "help":
        return twiml(get_help_text())

    if command["type"] == "full_list":
        return twiml(get_owner_full_list(command["owner"]))

    if command["type"] == "today":
        return twiml(get_owner_today(command["owner"]))

    if command["type"] == "owner_next":
        return twiml(get_owner_next(command["owner"]))

    if command["type"] == "global_next":
        return twiml(get_global_next())

    if command["type"] == "done_today":
        return twiml(get_completed_today(command["owner"]))

    if command["type"] == "wdw":
        return twiml(get_wdw())

    if command["type"] == "complete":
        return twiml(complete_task_by_id(command["owner"], command["task_id"]))

    if command["type"] == "add":
        if command["owner"] == "both":
            result_one = add_task("richard", command["category"], command["task_text"], command["priority"])
            result_two = add_task("nick", command["category"], command["task_text"], command["priority"])
            return twiml(f"{result_one}\n\n{result_two}")

        return twiml(
            add_task(
                command["owner"],
                command["category"],
                command["task_text"],
                command["priority"],
            )
        )

    if command["type"] == "legacy_add":
        if command["owner"] == "both":
            result_one = add_task("richard", "action", command["task_text"], False)
            result_two = add_task("nick", "action", command["task_text"], False)
            return twiml(f"{result_one}\n\n{result_two}")

        return twiml(add_task(command["owner"], "action", command["task_text"], False))

    return twiml("Command not recognized. Text HELP for command list.")


# -------------------------
# Task Complete Endpoint
# -------------------------

@app.post("/complete/{task_id}")
def complete_task_web(task_id: int):
    (
        supabase.table("tasks")
        .update({"status": "completed", "completed_at": now_utc_iso()})
        .eq("id", task_id)
        .execute()
    )
    return JSONResponse({"success": True})


# -------------------------
# Dashboard HTML Helpers
# -------------------------

def render_section(title: str, tasks: list, highlight_priority: bool = False):
    if not tasks:
        return ""

    section_html = f"""
    <div class="section">
        <div class="section-title">{escape_text(title)} ({len(tasks)})</div>
        <div class="task-list">
    """

    for task in tasks:
        flame = '<span class="flame">🔥</span>' if task.get("priority") and highlight_priority else ""
        section_html += f"""
            <label class="task-row">
                <input type="checkbox" onclick="completeTask({task['id']})">
                <span class="task-text">{flame}{escape_text(task['task'])}</span>
            </label>
        """

    section_html += """
        </div>
    </div>
    """
    return section_html


def render_owner_panel(owner: str, tasks: list):
    grouped = group_tasks(tasks)
    high_priority = [t for t in tasks if t.get("priority") and t.get("category") == "action"]
    regular_action = [t for t in grouped.get("action", []) if not t.get("priority")]
    daily = grouped.get("daily", [])
    long_term = grouped.get("long_term", [])

    return f"""
    <div class="owner-panel">
        <h2>{escape_text(DISPLAY_OWNER[owner])}</h2>
        {render_section("High Priority", high_priority, highlight_priority=True)}
        {render_section("Action Items", regular_action)}
        {render_section("Daily", daily)}
        {render_section("Long Term", long_term)}
    </div>
    """


def base_page(title: str, content: str, subtitle_links: str = ""):
    return f"""
    <html>
    <head>
        <title>{escape_text(title)}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{
                font-family: Arial, sans-serif;
                padding: 28px;
                background: #f6f6f6;
                color: #111;
                margin: 0;
            }}

            .topbar {{
                display: flex;
                justify-content: space-between;
                align-items: flex-start;
                gap: 20px;
                margin-bottom: 22px;
                flex-wrap: wrap;
            }}

            h1 {{
                margin: 0;
                font-size: 3rem;
                line-height: 1.1;
            }}

            .meta {{
                color: #444;
                font-size: 1rem;
                text-align: right;
                line-height: 1.8;
            }}

            .meta a {{
                color: #444;
                text-decoration: none;
                margin-left: 14px;
            }}

            .meta a:hover {{
                text-decoration: underline;
            }}

            .board {{
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 32px;
            }}

            .owner-panel {{
                background: #fff;
                border-radius: 14px;
                padding: 24px;
                box-shadow: 0 1px 4px rgba(0,0,0,0.08);
            }}

            h2 {{
                margin-top: 0;
                font-size: 2rem;
                margin-bottom: 24px;
            }}

            .section {{
                margin-bottom: 28px;
            }}

            .section-title {{
                font-size: 1.2rem;
                font-weight: bold;
                margin-bottom: 12px;
                padding-bottom: 8px;
                border-bottom: 1px solid #ddd;
                color: #364152;
            }}

            .task-list {{
                display: flex;
                flex-direction: column;
                gap: 10px;
            }}

            .task-row {{
                display: flex;
                align-items: flex-start;
                gap: 12px;
                font-size: 1rem;
                line-height: 1.45;
                padding: 6px 0;
                cursor: pointer;
            }}

            .task-row input[type="checkbox"] {{
                width: 22px;
                height: 22px;
                margin-top: 2px;
                cursor: pointer;
                flex: 0 0 auto;
            }}

            .task-text {{
                display: inline-block;
            }}

            .flame {{
                margin-right: 8px;
            }}

            .footer-tools {{
                margin-top: 18px;
                color: #555;
                font-size: 0.95rem;
            }}

            .focus-board {{
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 24px;
            }}

            .focus-card {{
                background: #fff;
                border-radius: 14px;
                padding: 24px;
                box-shadow: 0 1px 4px rgba(0,0,0,0.08);
            }}

            .empty {{
                color: #666;
                font-style: italic;
            }}

            @media (max-width: 900px) {{
                .board {{
                    grid-template-columns: 1fr;
                }}

                .focus-board {{
                    grid-template-columns: 1fr;
                }}

                h1 {{
                    font-size: 2.2rem;
                }}

                .meta {{
                    text-align: left;
                }}
            }}
        </style>
        <script>
            function completeTask(id) {{
                fetch("/complete/" + id, {{
                    method: "POST"
                }}).then(() => {{
                    location.reload();
                }});
            }}

            setInterval(function() {{
                location.reload();
            }}, 5000);
        </script>
    </head>
    <body>
        <div class="topbar">
            <div>
                <h1>{escape_text(title)}</h1>
            </div>
            <div class="meta">
                ✨ Updates every 5 seconds
                <br>
                <a href="/dashboard">Dashboard</a>
                <a href="/today">Today Command Center</a>
                <a href="/focus">Focus Mode</a>
                {subtitle_links}
            </div>
        </div>
        {content}
    </body>
    </html>
    """


# -------------------------
# Pages
# -------------------------

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    richard_tasks = get_tasks("richard", status="open")
    nick_tasks = get_tasks("nick", status="open")

    content = f"""
    <div class="board">
        {render_owner_panel("richard", richard_tasks)}
        {render_owner_panel("nick", nick_tasks)}
    </div>
    """

    return HTMLResponse(base_page("Briefly Home Punch Lists", content))


@app.get("/today", response_class=HTMLResponse)
def today_page():
    def render_today_panel(owner):
        tasks = get_tasks(owner, status="open")
        grouped = group_tasks(tasks)
        priority = [t for t in tasks if t.get("priority") and t.get("category") == "action"]
        daily = grouped.get("daily", [])
        action = [t for t in grouped.get("action", []) if not t.get("priority")]

        return f"""
        <div class="owner-panel">
            <h2>{escape_text(DISPLAY_OWNER[owner])}</h2>
            {render_section("High Priority", priority, highlight_priority=True)}
            {render_section("Daily", daily)}
            {render_section("Action Items", action)}
        </div>
        """

    content = f"""
    <div class="board">
        {render_today_panel("richard")}
        {render_today_panel("nick")}
    </div>
    """

    return HTMLResponse(base_page("Briefly Home — Today", content))


@app.get("/focus", response_class=HTMLResponse)
def focus_page():
    def render_focus_card(owner):
        tasks = [
            t for t in get_tasks(owner, status="open")
            if t.get("priority") and t.get("category") == "action"
        ]

        if not tasks:
            task_html = '<div class="empty">No high priority action items.</div>'
        else:
            task_html = ""
            for task in tasks:
                task_html += f"""
                <label class="task-row">
                    <input type="checkbox" onclick="completeTask({task['id']})">
                    <span class="task-text"><span class="flame">🔥</span>{escape_text(task['task'])}</span>
                </label>
                """

        return f"""
        <div class="focus-card">
            <h2>{escape_text(DISPLAY_OWNER[owner])}</h2>
            {task_html}
        </div>
        """

    content = f"""
    <div class="focus-board">
        {render_focus_card("richard")}
        {render_focus_card("nick")}
    </div>
    """

    return HTMLResponse(base_page("Briefly Home — Focus Mode", content))
