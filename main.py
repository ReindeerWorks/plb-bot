from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from supabase import create_client
from twilio.twiml.messaging_response import MessagingResponse
from datetime import datetime, timedelta, timezone, date
from collections import defaultdict
from pathlib import Path
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

DISPLAY_OWNER = {
    "richard": "Richard",
    "nick": "Nick",
}

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
    "u": "update",
    "update": "update",
    "updates": "update",
}

DISPLAY_CATEGORY = {
    "daily": "Daily",
    "action": "Action Items",
    "long_term": "LT",
    "update": "Updates",
}

FAVICON_FILE = Path("BH BOT.jpg")


# -------------------------
# Helpers
# -------------------------

def twiml(message: str) -> HTMLResponse:
    resp = MessagingResponse()
    resp.message(message.strip())
    return HTMLResponse(str(resp), media_type="application/xml")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_utc_iso() -> str:
    return now_utc().isoformat()


def today_utc() -> date:
    return now_utc().date()


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def esc(text: str) -> str:
    return html.escape(text or "")


def parse_date_safe(value) -> Optional[date]:
    if not value:
        return None

    if isinstance(value, date) and not isinstance(value, datetime):
        return value

    if isinstance(value, datetime):
        return value.date()

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except Exception:
        try:
            return date.fromisoformat(str(value))
        except Exception:
            return None


def format_time_short(value: str) -> str:
    if not value:
        return ""

    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%-I:%M %p UTC")
    except Exception:
        return ""


def is_daily_completed_today(task: dict) -> bool:
    if task.get("category") != "daily":
        return False
    return parse_date_safe(task.get("last_completed_date")) == today_utc()


def task_age_days(task: dict) -> int:
    created = task.get("created_at")
    if not created:
        return 0

    try:
        created_dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)
        return max(0, (now_utc() - created_dt).days)
    except Exception:
        return 0


def is_overdue(task: dict) -> bool:
    return (
        task.get("category") == "action"
        and task.get("status") == "open"
        and not task.get("priority")
        and task_age_days(task) >= 3
    )


def sort_key(task: dict):
    category = task.get("category") or "action"

    if task.get("priority") and category == "action":
        bucket = 0
    elif is_overdue(task):
        bucket = 1
    elif category == "action":
        bucket = 2
    elif category == "daily":
        bucket = 3
    elif category == "long_term":
        bucket = 4
    elif category == "update":
        bucket = 5
    else:
        bucket = 6

    return (
        bucket,
        -(task_age_days(task)),
        str(task.get("created_at") or ""),
        int(task.get("id") or 0),
    )


def format_task_line(task: dict, include_age: bool = False) -> str:
    prefix = ""

    if task.get("priority") and task.get("category") == "action":
        prefix = "🔥 "
    elif is_overdue(task):
        prefix = "⚠ "

    time_part = ""
    if task.get("category") == "update":
        ts = format_time_short(task.get("created_at"))
        if ts:
            time_part = f" — {ts}"

    age_text = ""
    if include_age and task_age_days(task) > 0:
        age_text = f" · {task_age_days(task)}d old"

    return f"{prefix}{task.get('task', '')}{time_part} [Task ID: {task.get('id')}]{age_text}"


# -------------------------
# Data access
# -------------------------

def get_owner_rows(owner: str) -> list:
    response = supabase.table("tasks").select("*").eq("owner", owner).execute()
    return response.data or []


def get_open_visible_tasks(owner: str) -> list:
    rows = get_owner_rows(owner)
    visible = []

    for task in rows:
        category = task.get("category") or "action"
        status = task.get("status") or "open"

        if category == "daily":
            if not is_daily_completed_today(task):
                visible.append(task)
        else:
            if status == "open":
                visible.append(task)

    visible.sort(key=sort_key)
    return visible


def get_completed_tasks_since(since_dt: datetime, owner: Optional[str] = None) -> list:
    response = (
        supabase.table("tasks")
        .select("*")
        .gte("completed_at", since_dt.isoformat())
        .execute()
    )

    tasks = response.data or []
    filtered = []

    for task in tasks:
        if owner and task.get("owner") != owner:
            continue
        if task.get("completed_at"):
            filtered.append(task)

    filtered.sort(key=lambda t: str(t.get("completed_at") or ""), reverse=True)
    return filtered


# -------------------------
# Task actions
# -------------------------

def add_task(owner: str, category: str, task_text: str, priority: bool = False) -> str:
    task_text = normalize_spaces(task_text)

    if not task_text:
        return "Task text cannot be empty."

    if category == "daily":
        existing = (
            supabase.table("tasks")
            .select("id")
            .eq("owner", owner)
            .eq("category", category)
            .eq("task", task_text)
            .execute()
        )
    else:
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

    payload = {
        "owner": owner,
        "category": category,
        "task": task_text,
        "status": "open",
        "priority": priority if category == "action" else False,
    }

    if category == "daily":
        payload["last_completed_date"] = None

    supabase.table("tasks").insert(payload).execute()

    prefix = "🔥 " if priority and category == "action" else ""
    return f"✅ Added for {DISPLAY_OWNER[owner]}\n{DISPLAY_CATEGORY[category]}: {prefix}{task_text}"


def complete_task_by_id(owner: str, task_id: int) -> str:
    lookup = supabase.table("tasks").select("*").eq("id", task_id).execute()

    if not lookup.data:
        return "Task ID not found."

    task = lookup.data[0]

    if task.get("owner") != owner:
        return f"Task {task_id} does not belong to {DISPLAY_OWNER[owner]}."

    category = task.get("category") or "action"

    if category == "daily":
        supabase.table("tasks").update(
            {
                "last_completed_date": str(today_utc()),
                "completed_at": now_utc_iso(),
                "status": "open",
            }
        ).eq("id", task_id).execute()

        return f"✅ Daily task completed for today\n{task.get('task')} [Task ID: {task_id}]"

    if task.get("status") == "completed":
        return f"Task {task_id} is already completed."

    supabase.table("tasks").update(
        {
            "status": "completed",
            "completed_at": now_utc_iso(),
        }
    ).eq("id", task_id).execute()

    return f"✅ Completed\n{task.get('task')} [Task ID: {task_id}]"


def move_task_by_id(owner: str, task_id: int, new_category: str) -> str:
    lookup = supabase.table("tasks").select("*").eq("id", task_id).execute()

    if not lookup.data:
        return "Task ID not found."

    task = lookup.data[0]

    if task.get("owner") != owner:
        return f"Task {task_id} does not belong to {DISPLAY_OWNER[owner]}."

    update_payload = {"category": new_category}

    if new_category == "daily":
        update_payload["status"] = "open"
        update_payload["last_completed_date"] = None
        update_payload["priority"] = False

    if new_category != "action":
        update_payload["priority"] = False

    supabase.table("tasks").update(update_payload).eq("id", task_id).execute()

    return f"✅ Task moved\n{task.get('task')} [Task ID: {task_id}]\nCategory → {DISPLAY_CATEGORY[new_category]}"


def clear_daily(owner: str) -> str:
    daily_rows = (
        supabase.table("tasks")
        .select("id")
        .eq("owner", owner)
        .eq("category", "daily")
        .execute()
    )

    rows = daily_rows.data or []

    if not rows:
        return f"{DISPLAY_OWNER[owner]} has no Daily tasks."

    supabase.table("tasks").update(
        {
            "last_completed_date": str(today_utc()),
            "completed_at": now_utc_iso(),
            "status": "open",
        }
    ).eq("owner", owner).eq("category", "daily").execute()

    return f"✅ Daily tasks cleared\n{len(rows)} task(s) completed for {DISPLAY_OWNER[owner]}"


# -------------------------
# List helpers
# -------------------------

def split_sections(tasks: list):
    high_priority = [t for t in tasks if t.get("category") == "action" and t.get("priority")]
    overdue = [t for t in tasks if is_overdue(t)]
    regular_action = [t for t in tasks if t.get("category") == "action" and not t.get("priority") and not is_overdue(t)]
    daily = [t for t in tasks if t.get("category") == "daily"]
    long_term = [t for t in tasks if t.get("category") == "long_term"]
    updates = [t for t in tasks if t.get("category") == "update"]

    updates.sort(key=lambda t: str(t.get("created_at") or ""), reverse=True)

    return high_priority, overdue, regular_action, daily, long_term, updates


def get_owner_full_list(owner: str) -> str:
    tasks = get_open_visible_tasks(owner)
    high_priority, overdue, regular_action, daily, long_term, updates = split_sections(tasks)

    lines = [f"{DISPLAY_OWNER[owner]} — Punch List", ""]

    def add_section(title, items, include_age=False):
        if items:
            lines.append(f"{title} ({len(items)})")
            for t in items:
                lines.append(f"• {format_task_line(t, include_age=include_age)}")
            lines.append("")

    add_section("High Priority", high_priority)
    add_section("Overdue", overdue, include_age=True)
    add_section("Action Items", regular_action)
    add_section("Daily", daily)
    add_section("LT", long_term)
    add_section("Updates", updates)

    if len(lines) == 2:
        return f"{DISPLAY_OWNER[owner]} — Punch List\n\nNo open tasks."

    return "\n".join(lines).strip()


def get_owner_today(owner: str) -> str:
    tasks = get_open_visible_tasks(owner)
    high_priority, overdue, regular_action, daily, long_term, updates = split_sections(tasks)

    lines = [f"{DISPLAY_OWNER[owner]} — Today", ""]

    def add_section(title, items, include_age=False):
        if items:
            lines.append(f"{title} ({len(items)})")
            for t in items:
                lines.append(f"• {format_task_line(t, include_age=include_age)}")
            lines.append("")

    add_section("High Priority", high_priority)
    add_section("Overdue", overdue, include_age=True)
    add_section("Daily", daily)
    add_section("Action Items", regular_action)
    add_section("LT", long_term)
    add_section("Updates", updates)

    if len(lines) == 2:
        return f"{DISPLAY_OWNER[owner]} — Today\n\nNo open tasks."

    return "\n".join(lines).strip()


def get_owner_next(owner: str) -> str:
    tasks = get_open_visible_tasks(owner)
    action_like = [t for t in tasks if t.get("category") == "action"]

    if not action_like:
        return f"{DISPLAY_OWNER[owner]} has no open Action Items."

    next_task = sorted(action_like, key=sort_key)[0]
    return f"{DISPLAY_OWNER[owner]} — Next\n\n{format_task_line(next_task, include_age=is_overdue(next_task))}"


def get_global_next() -> str:
    tasks = get_open_visible_tasks("richard") + get_open_visible_tasks("nick")
    action_like = [t for t in tasks if t.get("category") == "action"]

    if not action_like:
        return "No open Action Items."

    next_task = sorted(action_like, key=sort_key)[0]
    return f"Next Task\n\n{DISPLAY_OWNER[next_task['owner']]}: {format_task_line(next_task, include_age=is_overdue(next_task))}"


def get_completed_since_message(since_dt: datetime, title: str, owner: Optional[str] = None) -> str:
    tasks = get_completed_tasks_since(since_dt, owner=owner)

    if not tasks:
        return f"{title}\n\nNo completed tasks."

    lines = [title, ""]
    grouped = defaultdict(list)

    for task in tasks:
        grouped[task.get("owner")].append(task)

    for owner_key in ["richard", "nick"]:
        owner_tasks = grouped.get(owner_key, [])
        if not owner_tasks:
            continue
        lines.append(DISPLAY_OWNER[owner_key])
        for task in owner_tasks:
            lines.append(f"✓ {format_task_line(task)}")
        lines.append("")

    return "\n".join(lines).strip()


def get_completed_today_message(owner: str) -> str:
    start = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    tasks = get_completed_tasks_since(start, owner=owner)

    if not tasks:
        return f"{DISPLAY_OWNER[owner]} — Completed Today\n\nNo completed tasks."

    lines = [f"{DISPLAY_OWNER[owner]} — Completed Today", ""]
    for task in tasks:
        lines.append(f"✓ {format_task_line(task)}")
    return "\n".join(lines)


def get_wdw_message() -> str:
    start = now_utc() - timedelta(days=7)
    return get_completed_since_message(start, "Work Done This Week")


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
R U update text     = Add Update
R UPDATE text       = Add Update
R + U text          = Add Update

HIGH PRIORITY
R ! task            = Add high priority Action Item
R + ! task          = Add high priority Action Item
R A ! task          = Add high priority Action Item

COMPLETE
R X 12              = Complete task by ID
N X 15              = Complete task by ID

MOVE
R MOVE 12 DAILY     = Move task to Daily
R MOVE 12 ACTION    = Move task to Action Items
R MOVE 12 LT        = Move task to LT
R MOVE 12 UPDATE    = Move task to Updates

CLEAR DAILY
R CLEAR DAILY       = Mark today's Daily tasks complete
N CLEAR DAILY       = Mark today's Daily tasks complete

WEB
/dashboard          = Full dashboard
/today              = Today command center
/focus              = Focus mode
/completed-today    = Completed today
/completed-week     = Completed this week
/command            = Command center

HELP
HELP / CODES        = Show this guide
""".strip()


# -------------------------
# Token-based SMS parser
# -------------------------

def parse_sms_command(body: str) -> dict:
    raw = normalize_spaces(body)
    lowered = raw.lower()

    if lowered in {"help", "codes", "commands"}:
        return {"type": "help"}

    if lowered == "wdw":
        return {"type": "wdw"}

    if lowered == "next":
        return {"type": "global_next"}

    if re.fullmatch(r"(r|n)\s*\?", lowered):
        owner = "richard" if lowered.startswith("r") else "nick"
        return {"type": "full_list", "owner": owner}

    owner_match = re.match(r"^(richard|r|nick|n|rn|both)\s*:?\s*(.*)$", raw, flags=re.IGNORECASE)
    if not owner_match:
        return {"type": "unknown"}

    owner_token = owner_match.group(1).lower()
    owner = VALID_OWNERS.get(owner_token)
    remainder_raw = normalize_spaces(owner_match.group(2))
    remainder_lower = remainder_raw.lower()
    tokens = remainder_lower.split()

    if not owner or not remainder_raw:
        return {"type": "unknown"}

    # owner-level commands
    if remainder_lower == "today":
        return {"type": "today", "owner": owner}

    if remainder_lower == "next":
        return {"type": "owner_next", "owner": owner}

    if remainder_lower == "done":
        return {"type": "done_today", "owner": owner}

    if remainder_lower == "clear daily":
        return {"type": "clear_daily", "owner": owner}

    # complete
    if len(tokens) >= 2 and tokens[0] == "x" and tokens[1].isdigit():
        return {"type": "complete", "owner": owner, "task_id": int(tokens[1])}

    # move
    if len(tokens) >= 3 and tokens[0] == "move" and tokens[1].isdigit():
        category = VALID_CATEGORIES.get(tokens[2])
        if category:
            return {
                "type": "move",
                "owner": owner,
                "task_id": int(tokens[1]),
                "category": category,
            }

    # update shortcuts
    # n u something
    # n update something
    if len(tokens) >= 2 and tokens[0] in {"u", "update", "updates"}:
        task_text = normalize_spaces(remainder_raw.split(" ", 1)[1])
        return {
            "type": "add",
            "owner": owner,
            "category": "update",
            "priority": False,
            "task_text": task_text,
        }

    # n + u something
    # n + update something
    if len(tokens) >= 3 and tokens[0] == "+" and tokens[1] in {"u", "update", "updates"}:
        parts = remainder_raw.split(" ", 2)
        if len(parts) >= 3:
            task_text = normalize_spaces(parts[2])
            return {
                "type": "add",
                "owner": owner,
                "category": "update",
                "priority": False,
                "task_text": task_text,
            }

    # high-priority action shortcuts
    # n ! call plumber
    if len(tokens) >= 2 and tokens[0] == "!":
        task_text = normalize_spaces(remainder_raw[1:])
        return {
            "type": "add",
            "owner": owner,
            "category": "action",
            "priority": True,
            "task_text": task_text,
        }

    # n + ! call plumber
    if len(tokens) >= 3 and tokens[0] == "+" and tokens[1] == "!":
        parts = remainder_raw.split(" ", 2)
        if len(parts) >= 3:
            task_text = normalize_spaces(parts[2])
            return {
                "type": "add",
                "owner": owner,
                "category": "action",
                "priority": True,
                "task_text": task_text,
            }

    # category-based add
    # n d check gmail
    # n a call plumber
    # n lt review pricing
    # n u update note
    if len(tokens) >= 2 and tokens[0] in VALID_CATEGORIES:
        category = VALID_CATEGORIES[tokens[0]]
        priority = False

        if category == "action" and len(tokens) >= 3 and tokens[1] == "!":
            priority = True
            parts = remainder_raw.split(" ", 2)
            if len(parts) >= 3:
                task_text = normalize_spaces(parts[2])
            else:
                task_text = ""
        else:
            task_text = normalize_spaces(remainder_raw.split(" ", 1)[1])

        return {
            "type": "add",
            "owner": owner,
            "category": category,
            "priority": priority,
            "task_text": task_text,
        }

    # plus means action item
    # n + call plumber
    if len(tokens) >= 2 and tokens[0] == "+":
        task_text = normalize_spaces(remainder_raw.split(" ", 1)[1])
        return {
            "type": "add",
            "owner": owner,
            "category": "action",
            "priority": False,
            "task_text": task_text,
        }

    # implicit natural add
    # n call plumber
    return {
        "type": "add",
        "owner": owner,
        "category": "action",
        "priority": False,
        "task_text": remainder_raw,
    }


# -------------------------
# SMS endpoint
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
        return twiml(get_completed_today_message(command["owner"]))

    if command["type"] == "wdw":
        return twiml(get_wdw_message())

    if command["type"] == "complete":
        return twiml(complete_task_by_id(command["owner"], command["task_id"]))

    if command["type"] == "move":
        return twiml(move_task_by_id(command["owner"], command["task_id"], command["category"]))

    if command["type"] == "clear_daily":
        return twiml(clear_daily(command["owner"]))

    if command["type"] == "add":
        if command["owner"] == "both":
            r1 = add_task("richard", command["category"], command["task_text"], command["priority"])
            r2 = add_task("nick", command["category"], command["task_text"], command["priority"])
            return twiml(f"{r1}\n\n{r2}")

        return twiml(
            add_task(
                command["owner"],
                command["category"],
                command["task_text"],
                command["priority"],
            )
        )

    return twiml("Command not recognized. Text HELP for command list.")


# -------------------------
# Web completion endpoint
# -------------------------

@app.post("/complete/{task_id}")
def complete_task_web(task_id: int):
    lookup = supabase.table("tasks").select("*").eq("id", task_id).execute()

    if not lookup.data:
        return JSONResponse({"success": False, "error": "Task not found"}, status_code=404)

    task = lookup.data[0]

    if (task.get("category") or "action") == "daily":
        supabase.table("tasks").update(
            {
                "last_completed_date": str(today_utc()),
                "completed_at": now_utc_iso(),
                "status": "open",
            }
        ).eq("id", task_id).execute()
    else:
        supabase.table("tasks").update(
            {
                "status": "completed",
                "completed_at": now_utc_iso(),
            }
        ).eq("id", task_id).execute()

    return JSONResponse({"success": True})


@app.get("/favicon.jpg")
def favicon():
    if FAVICON_FILE.exists():
        return FileResponse(FAVICON_FILE, media_type="image/jpeg")
    return HTMLResponse("", status_code=204)


# -------------------------
# HTML rendering helpers
# -------------------------

def render_section(title: str, tasks: list, include_age: bool = False) -> str:
    if not tasks:
        return ""

    html_out = f'<div class="section"><div class="section-title">{esc(title)} ({len(tasks)})</div><div class="task-list">'

    for task in tasks:
        prefix = ""
        if task.get("priority") and task.get("category") == "action":
            prefix = '<span class="flame">🔥</span>'
        elif is_overdue(task):
            prefix = '<span class="warn">⚠</span>'

        age = ""
        if include_age and task_age_days(task) > 0:
            age = f' <span class="age">· {task_age_days(task)}d old</span>'

        time_part = ""
        if task.get("category") == "update":
            ts = format_time_short(task.get("created_at"))
            if ts:
                time_part = f' <span class="age">— {esc(ts)}</span>'

        html_out += f'''
        <label class="task-row">
            <input type="checkbox" onclick="completeTask({task['id']})">
            <span class="task-text">{prefix}{esc(task['task'])} <span class="task-id">[Task ID: {task['id']}]</span>{time_part}{age}</span>
        </label>
        '''

    html_out += "</div></div>"
    return html_out


def render_owner_panel(owner: str, tasks: list, include_long_term: bool = True, include_updates: bool = True) -> str:
    high_priority, overdue, regular_action, daily, long_term, updates = split_sections(tasks)

    content = [
        f"<div class='owner-panel'><h2>{esc(DISPLAY_OWNER[owner])}</h2>",
        render_section("High Priority", high_priority),
        render_section("Overdue", overdue, include_age=True),
        render_section("Action Items", regular_action),
        render_section("Daily", daily),
    ]

    if include_long_term:
        content.append(render_section("LT", long_term))

    if include_updates:
        content.append(render_section("Updates", updates))

    content.append("</div>")
    return "".join(content)


def base_page(title: str, content: str) -> str:
    return f'''
    <html>
    <head>
        <title>{esc(title)}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <link rel="icon" type="image/jpeg" href="/favicon.jpg">
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
            .owner-panel, .summary-card, .focus-card, .stat {{
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
            .flame, .warn {{
                margin-right: 8px;
            }}
            .age {{
                color: #666;
                font-size: 0.92rem;
            }}
            .stats-grid {{
                display: grid;
                grid-template-columns: repeat(5, 1fr);
                gap: 18px;
                margin-bottom: 24px;
            }}
            .stat-label {{
                color: #666;
                font-size: 0.95rem;
                margin-bottom: 8px;
            }}
            .stat-value {{
                font-size: 2rem;
                font-weight: bold;
            }}
            .focus-board {{
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 24px;
            }}
            .empty {{
                color: #666;
                font-style: italic;
            }}
            @media (max-width: 1200px) {{
                .stats-grid {{
                    grid-template-columns: 1fr 1fr;
                }}
            }}
            @media (max-width: 900px) {{
                .board, .focus-board, .stats-grid {{
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
                fetch("/complete/" + id, {{ method: "POST" }})
                    .then(() => location.reload());
            }}
            setInterval(function() {{
                location.reload();
            }}, 5000);
        </script>
    </head>
    <body>
        <div class="topbar">
            <div><h1>{esc(title)}</h1></div>
            <div class="meta">
                ✨ Updates every 5 seconds<br>
                <a href="/dashboard">Dashboard</a>
                <a href="/today">Today</a>
                <a href="/focus">Focus</a>
                <a href="/completed-today">Completed Today</a>
                <a href="/completed-week">Completed Week</a>
                <a href="/command">Command Center</a>
            </div>
        </div>
        {content}
    </body>
    </html>
    '''


# -------------------------
# Pages
# -------------------------

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    content = f'''
    <div class="board">
        {render_owner_panel("richard", get_open_visible_tasks("richard"), include_long_term=True, include_updates=True)}
        {render_owner_panel("nick", get_open_visible_tasks("nick"), include_long_term=True, include_updates=True)}
    </div>
    '''
    return HTMLResponse(base_page("Briefly Home Punch Lists", content))


@app.get("/today", response_class=HTMLResponse)
def today_page():
    content = f'''
    <div class="board">
        {render_owner_panel("richard", get_open_visible_tasks("richard"), include_long_term=False, include_updates=True)}
        {render_owner_panel("nick", get_open_visible_tasks("nick"), include_long_term=False, include_updates=True)}
    </div>
    '''
    return HTMLResponse(base_page("Briefly Home — Today", content))


@app.get("/focus", response_class=HTMLResponse)
def focus_page():
    def render_focus(owner: str):
        tasks = [t for t in get_open_visible_tasks(owner) if t.get("priority") or is_overdue(t)]
        if not tasks:
            body = '<div class="empty">No high-priority or overdue Action Items.</div>'
        else:
            body = render_section("Focus", tasks, include_age=True)
        return f"<div class='focus-card'><h2>{esc(DISPLAY_OWNER[owner])}</h2>{body}</div>"

    content = f'''
    <div class="focus-board">
        {render_focus("richard")}
        {render_focus("nick")}
    </div>
    '''
    return HTMLResponse(base_page("Briefly Home — Focus Mode", content))


def render_completed_page(title: str, since_dt: datetime):
    tasks = get_completed_tasks_since(since_dt)
    grouped = defaultdict(list)

    for task in tasks:
        grouped[task.get("owner")].append(task)

    def section_for(owner: str):
        owner_tasks = grouped.get(owner, [])
        if not owner_tasks:
            return f"<div class='owner-panel'><h2>{esc(DISPLAY_OWNER[owner])}</h2><div class='empty'>No completed tasks.</div></div>"

        task_html = "<div class='task-list'>"
        for task in owner_tasks:
            task_html += f"<div class='task-row'><span class='task-text'>{esc(format_task_line(task))}</span></div>"
        task_html += "</div>"

        return f"<div class='owner-panel'><h2>{esc(DISPLAY_OWNER[owner])}</h2>{task_html}</div>"

    content = f'''
    <div class="board">
        {section_for("richard")}
        {section_for("nick")}
    </div>
    '''
    return HTMLResponse(base_page(title, content))


@app.get("/completed-today", response_class=HTMLResponse)
def completed_today_page():
    start = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    return render_completed_page("Completed Today", start)


@app.get("/completed-week", response_class=HTMLResponse)
def completed_week_page():
    start = now_utc() - timedelta(days=7)
    return render_completed_page("Completed This Week", start)


@app.get("/command", response_class=HTMLResponse)
def command_center():
    r_open = get_open_visible_tasks("richard")
    n_open = get_open_visible_tasks("nick")
    all_open = r_open + n_open

    high_priority = [t for t in all_open if t.get("priority") and t.get("category") == "action"]
    overdue = [t for t in all_open if is_overdue(t)]

    r_daily_remaining = len([t for t in r_open if t.get("category") == "daily"])
    n_daily_remaining = len([t for t in n_open if t.get("category") == "daily"])
    r_action_open = len([t for t in r_open if t.get("category") == "action"])
    n_action_open = len([t for t in n_open if t.get("category") == "action"])
    r_updates_open = len([t for t in r_open if t.get("category") == "update"])
    n_updates_open = len([t for t in n_open if t.get("category") == "update"])

    today_start = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = now_utc() - timedelta(days=7)

    completed_today = get_completed_tasks_since(today_start)
    completed_week = get_completed_tasks_since(week_start)

    r_completed_today = len([t for t in completed_today if t.get("owner") == "richard"])
    n_completed_today = len([t for t in completed_today if t.get("owner") == "nick"])
    r_completed_week = len([t for t in completed_week if t.get("owner") == "richard"])
    n_completed_week = len([t for t in completed_week if t.get("owner") == "nick"])

    def simple_task_block(title: str, tasks: list, include_age: bool = False):
        if not tasks:
            return f"<div class='summary-card'><h2>{esc(title)}</h2><div class='empty'>None.</div></div>"

        html_out = f"<div class='summary-card'><h2>{esc(title)}</h2><div class='task-list'>"
        for task in tasks:
            html_out += f"<div class='task-row'><span class='task-text'>{esc(format_task_line(task, include_age=include_age))}</span></div>"
        html_out += "</div></div>"
        return html_out

    stats = f'''
    <div class="stats-grid">
        <div class="stat"><div class="stat-label">Richard Daily Remaining</div><div class="stat-value">{r_daily_remaining}</div></div>
        <div class="stat"><div class="stat-label">Nick Daily Remaining</div><div class="stat-value">{n_daily_remaining}</div></div>
        <div class="stat"><div class="stat-label">Richard Action Open</div><div class="stat-value">{r_action_open}</div></div>
        <div class="stat"><div class="stat-label">Nick Action Open</div><div class="stat-value">{n_action_open}</div></div>
        <div class="stat"><div class="stat-label">Richard Updates Open</div><div class="stat-value">{r_updates_open}</div></div>
        <div class="stat"><div class="stat-label">Nick Updates Open</div><div class="stat-value">{n_updates_open}</div></div>
        <div class="stat"><div class="stat-label">Richard Completed Today</div><div class="stat-value">{r_completed_today}</div></div>
        <div class="stat"><div class="stat-label">Nick Completed Today</div><div class="stat-value">{n_completed_today}</div></div>
        <div class="stat"><div class="stat-label">Richard Completed Week</div><div class="stat-value">{r_completed_week}</div></div>
        <div class="stat"><div class="stat-label">Nick Completed Week</div><div class="stat-value">{n_completed_week}</div></div>
    </div>
    '''

    content = stats + f'''
    <div class="board">
        {simple_task_block("High Priority Issues", high_priority)}
        {simple_task_block("Overdue Tasks", overdue, include_age=True)}
    </div>
    '''

    return HTMLResponse(base_page("Briefly Home — Command Center", content))
