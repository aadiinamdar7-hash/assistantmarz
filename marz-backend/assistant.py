"""MARZ assistant tools: reminders, alarms, tasks, projects and briefings."""
import re
import time
from datetime import datetime, timedelta

import live
import tools
from tools import like, parse_duration, pick, reply, tool

WEEKDAYS = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday",
)
DAY_PARTS = (
    ("midnight", 0, 0), ("noon", 12, 0), ("tonight", 20, 0),
    ("evening", 18, 0), ("afternoon", 15, 0), ("morning", 9, 0),
)
MERIDIEM_RE = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)(?!\w)", re.I
)
KINDS = {"reminder": ("reminders", 100), "alarm": ("alarms", 30)}
PRIORITY = {"high": 1, "urgent": 1, "medium": 2, "normal": 2, "low": 3}
PRIORITY_NAME = {1: "high", 2: "medium", 3: "low"}


# ---------- understanding times ----------
def find_clock(low):
    """Return (hour, minute) from '6 pm', '18:30', 'at 7' or 'noon'."""
    found = MERIDIEM_RE.search(low)
    if found:
        hour, minute = int(found.group(1)) % 12, int(found.group(2) or 0)
        if found.group(3).lower().startswith("p"):
            hour += 12
    else:
        found = (re.search(r"\b(\d{1,2}):(\d{2})\b", low)
                 or re.search(r"\bat\s+(\d{1,2})()\b", low))
        if found:
            hour, minute = int(found.group(1)), int(found.group(2) or 0)
        else:
            for word, hour, minute in DAY_PARTS:
                if re.search(r"\b" + word + r"\b", low):
                    return hour, minute
            return None
    if hour in range(24) and minute in range(60):
        return hour, minute
    return None


def parse_when(text, now):
    """Turn 'tomorrow at 6 pm' or 'in 45 minutes' into a datetime."""
    low = str(text).lower().strip()
    clock = find_clock(low)
    seconds = parse_duration(low)
    relative = re.search(r"\b(in|after)\b|from now", low)
    if clock is None and seconds and relative:
        return now + timedelta(seconds=seconds)
    day = None
    if "tomorrow" in low:
        day = now.date() + timedelta(days=1)
    else:
        for index, name in enumerate(WEEKDAYS):
            if name in low:
                day = now.date() + timedelta(days=(index - now.weekday()) % 7)
                break
    if day is None and clock is None and "today" not in low:
        return None
    hour, minute = clock or (9, 0)
    base = day or now.date()
    due = datetime(base.year, base.month, base.day, hour, minute,
                   tzinfo=now.tzinfo)
    if due <= now:
        due += timedelta(days=1 if day is None else 7)
    return due if (due - now).days < 367 else None


def when_text(ctx, stamp):
    now = live.local_now(ctx.tz)[0]
    moment = datetime.fromtimestamp(stamp, now.tzinfo)
    return live.format_time(moment, ctx.locale)


# ---------- reminders and alarms ----------
def pending(ctx, table):
    return ctx.store.query(
        "SELECT id, title, due_at FROM " + table + " WHERE user_id=? "
        "AND status='pending' ORDER BY due_at",
        (ctx.uid,),
    )


def add_item(ctx, kind, title, when):
    table, limit = KINDS[kind]
    due = parse_when(when, live.local_now(ctx.tz)[0])
    if due is None:
        return reply("Could not understand the time. Try 'tomorrow at 6 pm' "
                     "or 'in 45 minutes'.", ok=False)
    if len(pending(ctx, table)) >= limit:
        return reply("Too many pending " + table + ". Remove some first.",
                     ok=False)
    stamp = due.timestamp()
    rid = ctx.store.execute(
        "INSERT INTO " + table + "(user_id, title, due_at, created) "
        "VALUES(?, ?, ?, ?)",
        (ctx.uid, title[:120], stamp, time.time()),
    )
    return reply(
        f"{kind.title()} #{rid} set for {when_text(ctx, stamp)}: {title}.",
        data={"id": rid, "due_at": stamp},
    )


def remove_item(ctx, kind, ref):
    table = KINDS[kind][0]
    rows = pending(ctx, table)
    chosen = pick(rows, ref, "title")
    if not chosen:
        hint = "" if str(ref).strip() or len(rows) < 2 else " Say which one."
        return reply("No matching " + kind + "." + hint, ok=False)
    for row in chosen:
        ctx.store.execute(
            "UPDATE " + table + " SET status='deleted' WHERE id=? "
            "AND user_id=?",
            (row["id"], ctx.uid),
        )
    return reply("Removed " + kind + " " + ", ".join(
        "#" + str(r["id"]) for r in chosen) + ".")


def list_items(ctx, kind):
    rows = pending(ctx, KINDS[kind][0])
    if not rows:
        return reply("There are no pending " + kind + "s.")
    parts = [f"#{r['id']} {r['title']} ({when_text(ctx, r['due_at'])})"
             for r in rows[:15]]
    return reply(kind.title() + "s: " + "; ".join(parts) + ".")


def mark_fired(ctx, kind, rid):
    if kind in KINDS:
        ctx.store.execute(
            "UPDATE " + KINDS[kind][0] + " SET status='done' WHERE id=? "
            "AND user_id=? AND status='pending' AND due_at <= ?",
            (rid, ctx.uid, time.time() + 2),
        )


@tool("createReminder", "createReminder(title, when)",
      "Create a reminder. when is natural language such as 'tomorrow at "
      "6 pm', 'friday' or 'in 20 minutes'.")
def create_reminder(ctx, title="", when=""):
    if not title.strip():
        return reply("A reminder needs a title.", ok=False)
    return add_item(ctx, "reminder", title.strip(), when)


@tool("deleteReminder", "deleteReminder(reminderId)",
      "Delete a reminder by number or part of its title, or 'all'.")
def delete_reminder(ctx, reminderId=""):
    return remove_item(ctx, "reminder", reminderId)


@tool("listReminders", "listReminders()", "List pending reminders.")
def list_reminders(ctx):
    return list_items(ctx, "reminder")


@tool("setAlarm", "setAlarm(when, label?)",
      "Set an alarm that rings at a time, such as '7 am' or 'in 45 minutes'.")
def set_alarm(ctx, when="", label=""):
    return add_item(ctx, "alarm", label.strip() or "Alarm", when)


@tool("cancelAlarm", "cancelAlarm(alarmId)",
      "Cancel an alarm by number or label, or 'all'.")
def cancel_alarm(ctx, alarmId=""):
    return remove_item(ctx, "alarm", alarmId)


@tool("listAlarms", "listAlarms()", "List pending alarms.")
def list_alarms(ctx):
    return list_items(ctx, "alarm")


# ---------- tasks and projects ----------
def ranked(ctx):
    """Open tasks: overdue first, then soonest due, then priority."""
    now = time.time()
    rows = ctx.store.query(
        "SELECT * FROM tasks WHERE user_id=? AND status='open'", (ctx.uid,)
    )
    rows.sort(key=lambda t: (
        not (t["due_at"] and t["due_at"] < now),
        t["due_at"] or 9e18, t["priority"], t["created"],
    ))
    return rows


def task_line(ctx, task, now):
    due = ""
    if task["due_at"]:
        due = ", due " + when_text(ctx, task["due_at"])
        due += " (overdue)" if task["due_at"] < now else ""
    project = f" [{task['project']}]" if task["project"] else ""
    level = PRIORITY_NAME.get(task["priority"], "medium")
    return f"#{task['id']} {task['title']}{project} ({level}{due})"


@tool("createTask", "createTask(title, due?, priority?, project?)",
      "Add a to-do. priority is high, medium or low; due is natural "
      "language such as 'friday at 5 pm'.")
def create_task(ctx, title="", due="", priority="", project=""):
    if not title.strip():
        return reply("A task needs a title.", ok=False)
    stamp = None
    if due:
        moment = parse_when(due, live.local_now(ctx.tz)[0])
        stamp = moment.timestamp() if moment else None
    level = PRIORITY.get(str(priority).strip().lower(), 2)
    tid = ctx.store.execute(
        "INSERT INTO tasks(user_id, title, priority, due_at, project, "
        "created) VALUES(?, ?, ?, ?, ?, ?)",
        (ctx.uid, title.strip()[:200], level, stamp, project[:60],
         time.time()),
    )
    note = " (no due time set: I could not read it)" if due and not stamp \
        else ""
    return reply(f"Task #{tid} added: {title.strip()}.{note}",
                 data={"id": tid})


@tool("completeTask", "completeTask(task)",
      "Mark a task done, by number or part of its title.")
def complete_task(ctx, task=""):
    chosen = pick(ranked(ctx), task, "title", allow_all=False)
    if len(chosen) != 1:
        return reply("Say which task (number or name)." if chosen
                     else "No matching open task.", ok=False)
    ctx.store.execute(
        "UPDATE tasks SET status='done', done_at=? WHERE id=? AND user_id=?",
        (time.time(), chosen[0]["id"], ctx.uid),
    )
    return reply("Completed: " + chosen[0]["title"] + ".")


@tool("deleteTask", "deleteTask(task)",
      "Delete a task, by number or part of its title.")
def delete_task(ctx, task=""):
    rows = ctx.store.query(
        "SELECT id, title FROM tasks WHERE user_id=?", (ctx.uid,)
    )
    chosen = pick(rows, task, "title", allow_all=False)
    if len(chosen) != 1:
        return reply("Say which task (number or name)." if chosen
                     else "No matching task.", ok=False)
    ctx.store.execute(
        "DELETE FROM tasks WHERE id=? AND user_id=?",
        (chosen[0]["id"], ctx.uid),
    )
    return reply("Deleted task: " + chosen[0]["title"] + ".")


@tool("listTasks", "listTasks(scope?)",
      "List tasks. scope: open (default), overdue, done, or a project name.")
def list_tasks(ctx, scope=""):
    scope = str(scope).strip().lower()
    now = time.time()
    if scope == "done":
        rows = ctx.store.query(
            "SELECT * FROM tasks WHERE user_id=? AND status='done' "
            "ORDER BY done_at DESC LIMIT 10",
            (ctx.uid,),
        )
    else:
        rows = ranked(ctx)
        if scope == "overdue":
            rows = [t for t in rows if t["due_at"] and t["due_at"] < now]
        elif scope not in ("", "open", "all", "tasks"):
            rows = [t for t in rows if scope in (t["project"] or "").lower()
                    or scope in t["title"].lower()]
    if not rows:
        return reply("No " + (scope or "open") + " tasks.")
    lines = [task_line(ctx, t, now) for t in rows[:15]]
    return reply("Tasks: " + "; ".join(lines) + ".", data={"count": len(rows)})


@tool("prioritiseTasks", "prioritiseTasks()",
      "Rank the open tasks by what to do next (overdue, due soon, priority).")
def prioritise_tasks(ctx):
    rows, now = ranked(ctx), time.time()
    if not rows:
        return reply("There are no open tasks.")
    lines = [str(i) + ". " + task_line(ctx, t, now)
             for i, t in enumerate(rows[:7], 1)]
    return reply("Suggested order:\n" + "\n".join(lines))


def idle_projects(ctx, days=3):
    now, idle = time.time(), []
    for project in ctx.store.query(
            "SELECT name, created FROM projects WHERE user_id=?", (ctx.uid,)):
        talked = ctx.store.query(
            "SELECT MAX(m.created) AS t FROM messages m JOIN conversations "
            "c ON c.id = m.conv_id WHERE c.user_id=? AND m.role='user' "
            "AND m.content LIKE ? ESCAPE '\\'",
            (ctx.uid, like(project["name"])),
        )[0]["t"]
        worked = ctx.store.query(
            "SELECT MAX(created) AS a, MAX(done_at) AS b FROM tasks "
            "WHERE user_id=? AND LOWER(project)=LOWER(?)",
            (ctx.uid, project["name"]),
        )[0]
        last = max(project["created"] or 0, talked or 0,
                   worked["a"] or 0, worked["b"] or 0)
        if now - last >= days * 86400:
            idle.append((project["name"], int((now - last) // 86400)))
    return idle


@tool("listProjects", "listProjects()",
      "List the user's projects with open task counts.")
def list_projects(ctx):
    rows = ctx.store.query(
        "SELECT name, description FROM projects WHERE user_id=? "
        "ORDER BY id DESC",
        (ctx.uid,),
    )
    if not rows:
        return reply("There are no saved projects yet.")
    idle = dict(idle_projects(ctx, days=1))
    parts = []
    for row in rows[:10]:
        count = ctx.store.query(
            "SELECT COUNT(*) AS n FROM tasks WHERE user_id=? AND "
            "status='open' AND LOWER(project)=LOWER(?)",
            (ctx.uid, row["name"]),
        )[0]["n"]
        quiet = f", quiet for {idle[row['name']]}d" if row["name"] in idle \
            else ""
        parts.append(f"{row['name']} ({count} open tasks{quiet})")
    return reply("Projects: " + "; ".join(parts) + ".")


def briefing(ctx):
    """Short, factual lines MARZ can turn into proactive suggestions."""
    now, lines = time.time(), []
    tasks = ranked(ctx)
    overdue = [t for t in tasks if t["due_at"] and t["due_at"] < now]
    soon = [r for r in pending(ctx, "reminders") if r["due_at"] < now + 86400]
    alarms = pending(ctx, "alarms")
    if overdue:
        names = ", ".join(t["title"] for t in overdue[:3])
        lines.append(f"{len(overdue)} overdue task(s): {names}.")
    if soon:
        names = "; ".join(f"{r['title']} ({when_text(ctx, r['due_at'])})"
                          for r in soon[:3])
        lines.append("Reminders in the next 24 hours: " + names + ".")
    if alarms:
        lines.append("Next alarm: " + alarms[0]["title"] + " at "
                     + when_text(ctx, alarms[0]["due_at"]) + ".")
    for name, days in idle_projects(ctx)[:2]:
        lines.append(f"You have not worked on {name} for {days} days.")
    if tasks and not overdue:
        lines.append("Next task: " + tasks[0]["title"] + ".")
    if not tasks and not soon:
        lines.append("Nothing is scheduled yet. Want to plan something?")
    return lines


@tool("getBriefing", "getBriefing()",
      "Summarise what matters today: overdue tasks, reminders, alarms and "
      "neglected projects. Use it to plan the day.")
def get_briefing(ctx):
    lines = briefing(ctx)
    return reply("\n".join(lines) or "Nothing pressing.",
                 data={"suggestions": lines})


# ---------- hooks: page state and obvious commands ----------
def snapshot_hook(ctx):
    return {
        "alarms": pending(ctx, "alarms"),
        "reminders": pending(ctx, "reminders"),
        "tasks": ranked(ctx)[:40],
    }


def quick(text, low):
    calls = []
    if re.search(r"\bwake me\b|\b(set|create|add)\b[^.?!]*\balarm\b", low):
        label = "Wake up" if "wake" in low else "Alarm"
        calls.append(("setAlarm", {"when": low, "label": label}))
    elif re.search(r"\balarms?\b", low):
        if re.search(r"\b(cancel|delete|remove|turn off|stop)\b", low):
            ref = "all" if re.search(r"\ball\b", low) else ""
            calls.append(("cancelAlarm", {"alarmId": ref}))
        elif re.search(r"\b(show|list|what|which|any|active|my)\b", low):
            calls.append(("listAlarms", {}))
    if (re.search(r"\breminders?\b", low)
            and re.search(r"\b(show|list|what|which|any|upcoming)\b", low)
            and not re.search(r"\b(set|create|add|make|new)\b", low)):
        calls.append(("listReminders", {}))
    if re.search(r"\boverdue\b", low):
        calls.append(("listTasks", {"scope": "overdue"}))
    elif re.search(r"\b(show|list|view)\b[^.?!]*\b(tasks?|to-?dos?)\b", low):
        calls.append(("listTasks", {}))
    if re.search(r"what should i (do|work on)|what.s next|what next|"
                 r"plan my day|plan today|prioriti[sz]e", low):
        calls += [("getBriefing", {}), ("prioritiseTasks", {})]
    if re.search(r"\bmy projects\b|\bshow\b[^.?!]*\bprojects\b", low):
        calls.append(("listProjects", {}))
    return calls


tools.SNAPSHOT_HOOKS.append(snapshot_hook)
tools.QUICK_HOOKS.append(quick)
