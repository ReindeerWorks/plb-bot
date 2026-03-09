from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from supabase import create_client
from twilio.twiml.messaging_response import MessagingResponse
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import Optional
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
# Constants
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
    "lt": "long_term",
    "l": "long_term",
    "long_term": "long_term",
    "longterm": "long_term",
}

DISPLAY_OWNER = {
    "richard": "Richard",
    "nick": "Nick",
}

DISPLAY_CATEGORY = {
    "daily": "Daily",
    "action": "Action Items",
    "long_term": "LT",
}

SECTION_ORDER = ["daily", "action", "long_term"]


# -------------------------
# Helpers
# -------------------------

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


def format_task_line(task: dict, show_priority: bool = True) -> str:
    flame = "🔥 " if task.get("priority") and show_priority else ""
    return f"{flame}{task['task']} [Task ID: {task['id']}]"


def get_tasks(owner: str, status: str = "open") -> list:
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
            0 if t.get("priority") and t.get("category") == "action" else 1,
            SECTION_ORDER.index(t["category"]) if t.get("category") in SECTION_ORDER else 999,
            t.get("created_at") or "",
            t.get("id") or 0,
        )
    )
    return tasks


def group_tasks(tasks: list) -> dict:
    grouped = defaultdict(list)
    for task in tasks:
        grouped[task.get("category", "action")].append(task)
    return grouped


def add_task(owner: str, category: str, task_text: str, priority: bool = False) -> str:
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

    insert_data = {
        "owner": owner,
        "category": category,
        "task": task_text,
        "status": "open",
        "priority": priority,
    }

    supabase.table("tasks").insert(insert_data).execute()

    prefix = "🔥 " if priority else ""
    return f"✅ Added for {DISPLAY_OWNER[owner]}\n{DISPLAY_CATEGORY[category]}: {prefix}{task_text}"


def complete_task_by_id(owner: str, task_id: int) -> str:
    lookup = (
        supabase.table("tasks")
        .select("id, owner, task, status")
        .eq("id", task_id)
        .execute()
    )

    if not lookup.data:
        return "Task ID not found."

    task = lookup.data[0]

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

    return f"✅ Completed\n{task['task']} [Task ID: {task_id}]"


def move_task_by_id(owner: str, task_id: int, new_category: str) -> str:
    lookup = (
        supabase.table("tasks")
        .select("id, owner, task, status, category")
        .eq("id", task_id)
        .execute()
    )

    if not lookup.data:
        return "Task ID not found."

    task = lookup.data[0]

    if task["owner"] != owner:
        return f"Task {task_id} does not belong to {DISPLAY_OWNER[owner]}."

    if task["status"] != "open":
        return f"Task {task_id} is not open and cannot be moved."

    (
        supabase.table("tasks")
        .update({"category": new_category})
        .eq("id", task_id)
        .execute()
    )

    return (
        f"✅ Task moved\n"
        f"{task['task']} [Task ID: {task_id}]\n"
        f"Category → {DISPLAY_CATEGORY[new_category]}"
    )


def clear_daily(owner: str) -> str:
    daily_tasks = (
        supabase.table("tasks")
        .select("id")
        .eq("owner", owner)
        .eq("status", "open")
        .eq("category", "daily")
        .execute()
    )

    rows = daily_tasks.data or []

    if not rows:
        return f"{DISPLAY_OWNER[owner]} has no open Daily tasks."

    ids = [row["id"] for row in rows]

    (
        supabase.table("tasks")
        .update({"status": "completed", "completed_at": now_utc_iso()})
        .in_("id", ids)
        .execute()
    )

    return f"✅ Daily tasks cleared\n{len(ids)} task(s) completed for {DISPLAY_OWNER[owner]}"


def get_owner_full_list(owner: str) -> str:
    tasks = get_tasks(owner, status="open")
    grouped = group_tasks(tasks)

    lines = [f"{DISPLAY_OWNER[owner]} — Punch List", ""]

    priority_action = [t for t in grouped.get("action", []) if t.get("priority")]
    regular_action = [t for t in grouped.get("action", []) if not t.get("priority")]
    daily = grouped.get("daily", [])
    long_term = grouped.get("long_term", [])

    if priority_action:
        lines.append(f"High Priority ({len(priority_action)})")
        for task in priority_action:
            lines.append(f"• {format_task_line(task)}")
        lines.append("")

    if regular_action:
        lines.append(f"Action Items ({len(regular_action)})")
        for task in regular_action:
            lines.append(f"• {format_task_line(task, show_priority=False)}")
        lines.append("")

    if daily:
        lines.append(f"Daily ({len(daily)})")
        for task in daily:
            lines.append(f"• {format_task_line(task, show_priority=False)}")
        lines.append("")

    if long_term:
        lines.append(f"LT ({len(long_term)})")
        for task in long_term:
            lines.append(f"• {format_task_line(task, show_priority=False)}")
        lines.append("")

    if len(lines) == 2:
        return f"{DISPLAY_OWNER[owner]} — Punch List\n\nNo open tasks."

    return "\n".join(lines).strip()


def get_owner_today(owner: str) -> str:
    tasks = get_tasks(owner, status="open")
    grouped = group_tasks(tasks)

    priority_action = [t for t in grouped.get("action", []) if t.get("priority")]
    regular_action = [t for t in grouped.get("action", []) if not t.get("priority")]
    daily = grouped.get("daily", [])
    long_term = grouped.get("long_term", [])

    lines = [f"{DISPLAY_OWNER[owner]} — Today", ""]

    if priority_action:
        lines.append(f"High Priority ({len(priority_action)})")
        for task in priority_action:
            lines.append(f"• {format_task_line(task)}")
        lines.append("")

    if daily:
        lines.append(f"Daily ({len(daily)})")
        for task in daily:
            lines.append(f"• {format_task_line(task, show_priority=False)}")
        lines.append("")

    if regular_action:
        lines.append(f"Action Items ({len(regular_action)})")
        for task in regular_action:
            lines.append(f"• {format_task_line(task, show_priority=False)}")
        lines.append("")

    if long_term:
        lines.append(f"LT ({len(long_term)})")
        for task in long_term:
            lines.append(f"• {format_task_line(task, show_priority=False)}")
        lines.append("")

    if len(lines) == 2:
        return f"{DISPLAY_OWNER[owner]} — Today\n\nNo open tasks."

    return "\n".join(lines).strip()


def get_owner_next(owner: str) -> str:
    tasks = get_tasks(owner, status="open")
    action_tasks = [t for t in tasks if t.get("category") == "action"]

    if not action_tasks:
        return f"{DISPLAY_OWNER[owner]} has no open Action Items."

    next_task = action_tasks[0]
    return f"{DISPLAY_OWNER[owner]} — Next\n\n{format_task_line(next_task)}"


def get_global_next() -> str:
    all_tasks = []
    for owner in ["richard", "nick"]:
        all_tasks.extend(get_tasks(owner, status="open"))

    action_tasks = [t for t in all_tasks if t.get("category") == "action"]

    if not action_tasks:
        return "No open Action Items."

    action_tasks.sort(
        key=lambda t: (
            0 if t.get("priority") else 1,
            t.get("created_at") or "",
            t.get("id") or 0,
        )
    )

    next_task = action_tasks[0]
    owner_name = DISPLAY_OWNER[next_task["owner"]]
    return f"Next Task\n\n{owner_name}: {format_task_line(next_task)}"


def get_completed_since(owner: Optional[str], since_dt: datetime, title: str) -> str:
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
            lines.append(f"✓ {task['task']} [Task ID: {task['id']}]")
        lines.append("")

    return "\n".join(lines).strip()


def get_completed_today(owner: str) -> str:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return get_completed_since(owner, start, f"{DISPLAY_OWNER[owner]} — Completed Today")


def get_wdw() -> str:
    start = datetime.now(timezone.utc) - timedelta(days=7)
    return get_completed_since(None, start, "Work Done This Week")


def get_help_text() -> str:
    return """
PLB COMMANDS

LISTS
R?                  = Richard full list
N?                  = Nick full list
R TODAY             = Richard today view
N TODAY             = Nick today view
R NEXT              = Richard next action
N NEXT              = Nick next action
NEXT                = Global next action item
WDW                 = Work done this week
R DONE              = Richard completed today
N DONE              = Nick completed today

ADD TASKS
R D task            = Add Daily task
R A task            = Add Action Item
R + task            = Add Action Item
R LT task           = Add LT task

HIGH PRIORITY
R A ! task          = Add high priority Action Item
R + ! task          = Add high priority Action Item

COMPLETE
R X 12              = Complete task by ID
N X 15              = Complete task by ID

MOVE
R MOVE 12 DAILY     = Move task to Daily
R MOVE 12 ACTION    = Move task to Action Items
R MOVE 12 LT        = Move task to LT

CLEAR DAILY
R CLEAR DAILY       = Complete all open Daily tasks
N CLEAR DAILY       = Complete all open Daily tasks

WEB
/dashboard          = Full dashboard
/today              = Today command center
/focus              = Focus mode

HELP
HELP / CODES        = Show this guide
""".strip()


# -------------------------
# SMS Parsing
# -------------------------

def parse_sms_command(body: str) -> dict:
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

    owner_clear_daily = re.fullmatch(r"(richard|r|nick|n)\s*:?\s*clear\s+daily", lowered)
    if owner_clear_daily:
        owner = VALID_OWNERS[owner_clear_daily.group(1)]
        return {"type": "clear_daily", "owner": owner}

    complete_match = re.fullmatch(r"(richard|r|nick|n)\s*:?\s*x\s+(\d+)", lowered)
    if complete_match:
        owner = VALID_OWNERS[complete_match.group(1)]
        task_id = int(complete_match.group(2))
        return {"type": "complete", "owner": owner, "task_id": task_id}

    move_match = re.fullmatch(
        r"(richard|r|nick|n)\s*:?\s*move\s+(\d+)\s+(daily|d|action|a|lt|l|long_term|longterm)",
        lowered,
    )
    if move_match:
        owner = VALID_OWNERS[move_match.group(1)]
        task_id = int(move_match.group(2))
        category = VALID_CATEGORIES[move_match.group(3)]
        return {"type": "move", "owner": owner, "task_id": task_id, "category": category}

    add_plus_match = re.fullmatch(r"(richard|r|nick|n|rn)\s*:?\s*\+\s+(!)?\s*(.+)", lowered)
    if add_plus_match:
        owner = VALID_OWNERS[add_plus_match.group(1)]
        priority = bool(add_plus_match.group(2))
        task_text = raw.split("+", 1)[1].strip()
        if priority and task_text.startswith("!"):
            task_text = task_text[1:].strip()
        return {
            "type": "add",
            "owner": owner,
            "category": "action",
            "priority": priority,
            "task_text": task_text,
        }

    add_match = re.fullmatch(
        r"(richard|r|nick|n|rn)\s*:?\s+(daily|d|action|a|lt|l|long_term|longterm)\s+(!)?\s*(.+)",
        lowered,
    )
    if add_match:
        owner = VALID_OWNERS[add_match.group(1)]
        category = VALID_CATEGORIES[add_match.group(2)]
        priority = bool(add_match.group(3))
        task_text = raw.split(maxsplit=2)[-1]

        # Clean task text for priority marker
        if priority:
            bang_index = task_text.find("!")
            if bang_index != -1:
                task_text = task_text[bang_index + 1:].strip()

        return {
            "type": "add",
            "owner": owner,
            "category": category,
            "priority": priority,
            "task_text": task_text,
        }

    legacy_add_match = re.fullmatch(r"(richard|r|nick|n|rn)\s*:\s*(.+)", lowered)
    if legacy_add_match:
        owner = VALID_OWNERS[legacy_add_match.group(1)]
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

    if command["type"] == "move":
        return twiml(move_task_by_id(command["owner"], command["task_id"], command["category"]))

    if command["type"] == "clear_daily":
        return twiml(clear_daily(command["owner"]))

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
# Web Completion Endpoint
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
# Dashboard Rendering
# -------------------------

def render_section(title: str, tasks: list, highlight_priority: bool = False) -> str:
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
                <span class="task-text">{flame}{escape_text(task['task'])} <span class="task-id">[Task ID: {task['id']}]</span></span>
            </label>
        """

    section_html += """
        </div>
    </div>
    """
    return section_html


def render_owner_panel(owner: str, tasks: list) -> str:
    grouped = group_tasks(tasks)
    high_priority = [t for t in grouped.get("action", []) if t.get("priority")]
    regular_action = [t for t in grouped.get("action", []) if not t.get("priority")]
    daily = grouped.get("daily", [])
    long_term = grouped.get("long_term", [])

    return f"""
    <div class="owner-panel">
        <h2>{escape_text(DISPLAY_OWNER[owner])}</h2>
        {render_section("High Priority", high_priority, highlight_priority=True)}
        {render_section("Action Items", regular_action)}
        {render_section("Daily", daily)}
        {render_section("LT", long_term)}
    </div>
    """


def base_page(title: str, content: str) -> str:
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

            .task-id {{
                color: #666;
                font-size: 0.95rem;
            }}

            .flame {{
                margin-right: 8px;
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
    def render_today_panel(owner: str) -> str:
        tasks = get_tasks(owner, status="open")
        grouped = group_tasks(tasks)
        priority = [t for t in grouped.get("action", []) if t.get("priority")]
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
    def render_focus_card(owner: str) -> str:
        tasks = [
            t for t in get_tasks(owner, status="open")
            if t.get("priority") and t.get("category") == "action"
        ]

        if not tasks:
            task_html = '<div class="empty">No high priority Action Items.</div>'
        else:
            task_html = ""
            for task in tasks:
                task_html += f"""
                <label class="task-row">
                    <input type="checkbox" onclick="completeTask({task['id']})">
                    <span class="task-text"><span class="flame">🔥</span>{escape_text(task['task'])} <span class="task-id">[Task ID: {task['id']}]</span></span>
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
