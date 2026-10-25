"""MARZ assistant tools: reminders, alarms, tasks, projects, briefings and
the JARVIS layer (time, weather, memory, calendar, proactive checks,
gestures and page commands)."""
import json
import re
import time
from datetime import datetime, timedelta

import requests

import live
import tools
from tools import (
    confirmed, fmt_secs, like, need, one, parse_duration, pick, reply, tool,
    whole,
)

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


def next_occurrence(stamp, rule, now):
    """Next time after now at the same clock time for a repeating alarm."""
    due = datetime.fromtimestamp(stamp, now.tzinfo)
    step = timedelta(days=7 if rule == "weekly" else 1)
    while due <= now or (rule == "weekdays" and due.weekday() > 4):
        due += step
    return due.timestamp()


def mark_fired(ctx, kind, rid):
    if kind not in KINDS:
        return
    table = KINDS[kind][0]
    ctx.store.execute(
        "UPDATE " + table + " SET status='done' WHERE id=? AND user_id=? "
        "AND status='pending' AND due_at <= ?",
        (rid, ctx.uid, time.time() + 2),
    )
    if kind == "alarm":
        row = ctx.store.query(
            "SELECT due_at, repeat_rule FROM alarms WHERE id=? AND "
            "user_id=? AND status='done'", (rid, ctx.uid))
        if row and row[0]["repeat_rule"]:
            later = next_occurrence(row[0]["due_at"], row[0]["repeat_rule"],
                                    live.local_now(ctx.tz)[0])
            ctx.store.execute(
                "UPDATE alarms SET due_at=?, status='pending' WHERE id=?",
                (later, rid))


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
    if not str(task).strip():
        return reply("Say which task to delete (number or name).", ok=False)
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


# ---------- constants for the JARVIS layer ----------
HTTPS = "https" + "://"
WIKI_URL = HTTPS + "en.wikipedia.org/w/api.php"
SEVERE = (65, 67, 75, 82, 86, 95, 96, 99)
ESTIMATES = (
    ("essay", 120), ("revis", 45), ("homework", 60), ("project", 90),
    ("code", 90), ("read", 30), ("email", 10), ("call", 15), ("clean", 30),
    ("shop", 45), ("exercise", 45), ("workout", 45),
)
PROBLEM = (requests.RequestException, KeyError, TypeError, ValueError,
           IndexError)

SKILLS = {
    # Coding
    "generateCode": ("generateCode(request, language?)", "Coding",
                     "Write complete, runnable code in one fenced block, "
                     "then two or three lines on how to run it and any "
                     "assumptions."),
    "fixCode": ("fixCode(code, problem?)", "Coding",
                "Find the cause, give the corrected code in a fenced "
                "block, and explain the fix in plain words."),
    "reviewCode": ("reviewCode(code)", "Coding",
                   "Review for bugs, security, readability and structure; "
                   "list the most important issues first with fixes."),
    "explainCode": ("explainCode(code)", "Coding",
                    "Explain what the code does step by step in plain "
                    "language, then note anything risky."),
    "refactorCode": ("refactorCode(code, goal?)", "Coding",
                     "Refactor without changing behaviour; show the new "
                     "code and summarise what changed and why."),
    "findBug": ("findBug(code, symptom?)", "Coding",
                "Locate the bug, explain the cause, and give a minimal fix."),
    "generateProjectStructure": (
        "generateProjectStructure(idea, stack?)", "Coding",
        "Propose a folder tree and one line per file, then the first "
        "three build steps."),
    # Study
    "createFlashcards": ("createFlashcards(topic, count?)", "Study",
                         "Make concise Q and A flashcards, one fact each."),
    "generateQuiz": ("generateQuiz(topic, count?)", "Study",
                     "Write a short quiz, then an answer key with brief "
                     "explanations."),
    "generateRevisionPlan": ("generateRevisionPlan(subject, exam_date?)",
                             "Study",
                             "Build a day-by-day revision plan that fits the "
                             "user's tasks and free time."),
    "summarizeTopic": ("summarizeTopic(topic)", "Study",
                       "Summarise the topic in short sections with key "
                       "terms in bold."),
    "generateExamQuestions": ("generateExamQuestions(topic, level?)",
                              "Study", "Write exam-style questions with "
                              "marks."),
    "generateModelAnswers": ("generateModelAnswers(question, level?)",
                             "Study", "Write a model answer and show how "
                             "marks are earned."),
    # Planning (these also receive the user's real data)
    "generateDailyPlan": ("generateDailyPlan(focus?)", "Planning",
                          "Write a time-blocked plan from now until bed "
                          "using the tasks, events and reminders below."),
    "generateStudyPlan": ("generateStudyPlan(subject?)", "Planning",
                          "Write a realistic study plan using the study "
                          "topics and free time below."),
    "generateFocusPlan": ("generateFocusPlan(task?)", "Planning",
                          "Write a focus plan with work blocks, short "
                          "breaks and one clear goal per block."),
    "generateSchedule": ("generateSchedule(days?)", "Planning",
                         "Write a schedule that fits the commitments below."),
    "generateRoutine": ("generateRoutine(kind?)", "Planning",
                        "Write a routine (morning, evening or study) that "
                        "suits the user's preferences below."),
    "generateProjectPlan": ("generateProjectPlan(project)", "Planning",
                            "Write a project plan: milestones, next steps "
                            "and rough effort, using the data below."),
    # Email (drafting only: MARZ has no mailbox access)
    "draftEmail": ("draftEmail(to, purpose, tone?)", "Email",
                   "Write a clear email draft with a subject line."),
    "composeEmail": ("composeEmail(to, points)", "Email",
                     "Turn the points into a polished email with a subject."),
    "replyToEmail": ("replyToEmail(email, intent)", "Email",
                     "Write a reply that answers every point."),
    "summarizeResearch": ("summarizeResearch(text)", "Research",
                          "Summarise the findings, state what is certain "
                          "and what is not, and list open questions."),
    # Vision (the images are already part of the user's message)
    "analyzeScreenshot": ("analyzeScreenshot(question?)", "Vision",
                          "Look at the attached screenshot(s) and answer "
                          "the question."),
    "compareScreenshots": ("compareScreenshots()", "Vision",
                           "Compare the attached screenshots and list the "
                           "differences."),
    "extractTextFromScreenshot": ("extractTextFromScreenshot()", "Vision",
                                  "Transcribe all readable text exactly."),
    "explainScreenshot": ("explainScreenshot()", "Vision",
                          "Explain what the screenshot shows."),
    "findBugInScreenshot": ("findBugInScreenshot()", "Vision",
                            "Read the code or error in the screenshot, find "
                            "the bug and give the fix."),
    "describeImage": ("describeImage()", "Vision",
                      "Describe the image clearly and concisely."),
    "extractImageText": ("extractImageText()", "Vision",
                         "Transcribe all text in the image."),
    "analyzeDiagram": ("analyzeDiagram()", "Vision",
                       "Explain the diagram's parts and how they relate."),
    "analyzeChart": ("analyzeChart()", "Vision",
                     "Read the chart: axes, values, trends and takeaways."),
    "analyzeGraph": ("analyzeGraph()", "Vision",
                     "Read the graph: relationship, trend and notable "
                     "points."),
}
CONTEXT_SKILLS = {
    "generateRevisionPlan", "generateDailyPlan", "generateStudyPlan",
    "generateFocusPlan", "generateSchedule", "generateRoutine",
    "generateProjectPlan",
}

# (tool name, signature, page command, confirmation text)
CLIENT = (
    ("startListening", "startListening()", "voice.start", "Listening."),
    ("stopListening", "stopListening()", "voice.stop", "Stopped listening."),
    ("toggleVoiceMode", "toggleVoiceMode()", "voice.toggle", "Voice toggled."),
    ("enableWakeWord", "enableWakeWord()", "wake.on", "Wake word is on."),
    ("disableWakeWord", "disableWakeWord()", "wake.off", "Wake word is off."),
    ("speakText", "speakText(text)", "speak", "Speaking."),
    ("stopSpeaking", "stopSpeaking()", "speak.stop", "Quiet."),
    ("interruptSpeech", "interruptSpeech()", "speak.stop", "Interrupted."),
    ("changeVoice", "changeVoice(name)", "voice.set", "Voice changed."),
    ("changeSpeechRate", "changeSpeechRate(rate 0.5 to 2)", "rate",
     "Speech rate changed."),
    ("changeSpeechPitch", "changeSpeechPitch(pitch 0 to 2)", "pitch",
     "Speech pitch changed."),
    ("enableGestures", "enableGestures()", "gestures.on",
     "Gesture control is starting (the browser will ask for the camera)."),
    ("disableGestures", "disableGestures()", "gestures.off",
     "Gesture control is off."),
    ("showGestures", "showGestures()", "view:gestures", "Gesture dashboard."),
    ("playMusic", "playMusic(query or audio URL)", "music.play",
     "Playing music."),
    ("pauseMusic", "pauseMusic()", "music.pause", "Music paused."),
    ("stopMusic", "stopMusic()", "music.stop", "Music stopped."),
    ("setVolume", "setVolume(level 0 to 100)", "music.volume",
     "Volume set."),
    ("nextTrack", "nextTrack()", "music.next", "Next track."),
    ("previousTrack", "previousTrack()", "music.prev", "Previous track."),
    ("openDashboard", "openDashboard(name: assistant, tasks, reminders, "
     "weather, memory, projects, gestures, system, files)", "view",
     "Opening the dashboard."),
)

GESTURES = {
    "OPEN_PALM": "voice.start",
    "FIST": "voice.stop",
    "PEACE_SIGN": "voice.toggle",
    "THUMBS_UP": "confirm",
    "THUMBS_DOWN": "cancel",
    "POINT_UP": "view:assistant",
    "POINT_DOWN": "view:home",
    "PINCH": "tools.open",
    "THREE_FINGERS": "view:tasks",
    "FOUR_FINGERS": "view:reminders",
    "FIVE_FINGERS": "briefing",
    "TWO_HANDS_OPEN": "view:system",
}
VIEWS = (
    "home", "assistant", "tasks", "reminders", "weather", "memories",
    "projects", "gestures", "system", "files", "notes", "settings",
)
COMMANDS = (
    "voice.start", "voice.stop", "voice.toggle", "wake.on", "wake.off",
    "speak.stop", "confirm", "cancel", "tools.open", "briefing", "evening",
    "gestures.off", "music.pause", "music.stop", "none",
) + tuple("view:" + v for v in VIEWS)


# ---------- helpers ----------
def local(ctx):
    return live.local_now(ctx.tz)[0]


def day_bounds(ctx, offset=0):
    start = local(ctx).replace(hour=0, minute=0, second=0, microsecond=0)
    start += timedelta(days=offset)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def clock_text(ctx, stamp):
    moment = datetime.fromtimestamp(stamp, local(ctx).tzinfo)
    return live.format_time(moment, ctx.locale).split(", ")[-1]


def get_pref(store, uid, key, default=""):
    rows = store.query(
        "SELECT value FROM assistant_preferences WHERE user_id=? AND key=?",
        (uid, key),
    )
    return rows[0]["value"] if rows else default


def set_pref(store, uid, key, value):
    store.execute(
        "INSERT OR REPLACE INTO assistant_preferences(user_id, key, value, "
        "updated) VALUES(?, ?, ?, ?)",
        (uid, key, str(value)[:300], time.time()),
    )


# ---------- time ----------
def make_time_tool(template, make):
    def run(ctx, location=""):
        found = tools.clock_for(ctx, location)
        if found is None:
            return reply("Could not find " + location + ".", ok=False)
        now, label, place = found
        value = make(now, label)
        return reply(template.format(place=place, value=value),
                     data={"value": value})
    return run


for _name, _text, _make in (
    ("getTimezone", "Timezone for {place}: {value}.",
     lambda n, lab: f"{lab} (UTC{n.strftime('%z')})"),
    ("getDayOfWeek", "It is {value} in {place}.",
     lambda n, lab: n.strftime("%A")),
    ("getCurrentMonth", "The month in {place} is {value}.",
     lambda n, lab: n.strftime("%B")),
    ("getCurrentYear", "The year in {place} is {value}.",
     lambda n, lab: str(n.year)),
):
    tool(_name, _name + "(location?)", group="Time")(
        make_time_tool(_text, _make))


# ---------- weather ----------
def spot_for(ctx, location):
    spot = live.geocode(location) if location else None
    if location and spot is None:
        return None, "Could not find a place called " + location + "."
    if spot is None and ctx.coords:
        spot = {"name": "your location", "lat": ctx.coords[0],
                "lon": ctx.coords[1], "tz": ""}
    if spot is None and ctx.city:
        spot = live.geocode(ctx.city)
    if spot is None:
        return None, "The user's location is unknown. Ask which city (once)."
    return spot, ""


def forecast_for(ctx, location, days=2):
    """Return ((spot, data, imperial), None) or (None, error reply)."""
    spot, err = spot_for(ctx, location)
    if spot is None:
        return None, reply(err, ok=False, ask="location")
    imperial = ctx.locale.startswith("en-US")
    key = ("fc", round(spot["lat"], 2), round(spot["lon"], 2), imperial, days)
    try:
        data = live.cached(
            key, lambda: live.fetch_forecast(spot, imperial, days))
    except PROBLEM:
        return None, reply("Live weather could not be fetched right now.",
                           ok=False)
    return (spot, data, imperial), None


@tool("getForecast", "getForecast(location?, days?)", group="Weather")
def get_forecast(ctx, location="", days="5"):
    count = whole(days, 5, 1, 7)
    found, err = forecast_for(ctx, location, count)
    if err:
        return err
    spot, data, _ = found
    daily, deg = data["daily"], data["current_units"]["temperature_2m"]
    lines = []
    for i in range(min(count, len(daily["weather_code"]))):
        stamp = (daily.get("time") or [""] * 9)[i]
        try:
            stamp = datetime.strptime(stamp, "%Y-%m-%d").strftime("%a %d %b")
        except ValueError:
            stamp = f"Day {i + 1}"
        lines.append(
            f"{stamp}: {live.WMO.get(daily['weather_code'][i], 'unknown')}, "
            f"high {daily['temperature_2m_max'][i]}{deg}, "
            f"low {daily['temperature_2m_min'][i]}{deg}, rain chance "
            f"{daily['precipitation_probability_max'][i]}%")
    return reply(f"Forecast for {spot['name']}:\n" + "\n".join(lines),
                 data={"days": lines})


def metric_tool(label, show):
    def run(ctx, location=""):
        found, err = forecast_for(ctx, location, 2)
        if err:
            return err
        spot, data, _ = found
        value = show(data["current"], data["current_units"])
        return reply(f"{label} in {spot['name']}: {value}.")
    return run


for _name, _label, _show in (
    ("getTemperature", "Temperature",
     lambda c, u: f"{c['temperature_2m']}{u['temperature_2m']} (feels like "
                  f"{c['apparent_temperature']}{u['temperature_2m']})"),
    ("getHumidity", "Humidity", lambda c, u: f"{c['relative_humidity_2m']}%"),
    ("getWindSpeed", "Wind speed",
     lambda c, u: f"{c['wind_speed_10m']} {u['wind_speed_10m']}"),
):
    tool(_name, _name + "(location?)", group="Weather")(
        metric_tool(_label, _show))


def to_c(value, imperial):
    return (value - 32) * 5 / 9 if imperial else value


def derive_alerts(data, imperial):
    alerts, daily = [], data.get("daily", {})
    speed = 1.609 if imperial else 1.0
    for i, code in enumerate(daily.get("weather_code", [])):
        day = ("Today", "Tomorrow")[i] if i < 2 else f"Day {i + 1}"
        if code in SEVERE:
            alerts.append(f"{day}: {live.WMO.get(code, 'severe weather')}")
        if to_c(daily["temperature_2m_max"][i], imperial) >= 30:
            alerts.append(f"{day}: very hot")
        if to_c(daily["temperature_2m_min"][i], imperial) <= -2:
            alerts.append(f"{day}: frost or freezing")
        gusts = (daily.get("wind_speed_10m_max") or [0] * 9)[i] or 0
        if gusts * speed >= 50:
            alerts.append(f"{day}: strong winds")
    return alerts


@tool("getWeatherAlerts", "getWeatherAlerts(location?)", group="Weather")
def get_weather_alerts(ctx, location=""):
    found, err = forecast_for(ctx, location, 3)
    if err:
        return err
    spot, data, imperial = found
    alerts = derive_alerts(data, imperial)
    note = ("derived from the forecast, not official warnings; check your "
            "national weather service for those")
    if not alerts:
        return reply(f"No significant weather concerns for {spot['name']} "
                     f"in the next 3 days ({note}).", data={"alerts": []})
    return reply(f"Possible concerns for {spot['name']} ({note}): "
                 + "; ".join(alerts) + ".", data={"alerts": alerts})


def clothes_advice(feels, rain, wind):
    if feels <= 0:
        base = "a heavy coat, hat, gloves and a scarf"
    elif feels <= 8:
        base = "a warm coat with layers underneath"
    elif feels <= 14:
        base = "a jacket or jumper over a long-sleeved top"
    elif feels <= 19:
        base = "a light jacket or long sleeves"
    elif feels <= 25:
        base = "a t-shirt and light trousers or a skirt"
    else:
        base = "light breathable clothes, sun protection and water"
    extras = []
    if rain >= 50:
        extras.append("a waterproof coat or umbrella")
    if wind >= 30:
        extras.append("a windproof layer")
    return base + ("; also " + " and ".join(extras) if extras else "")


@tool("recommendClothes", "recommendClothes(location?)", group="Weather")
def recommend_clothes(ctx, location=""):
    found, err = forecast_for(ctx, location, 2)
    if err:
        return err
    spot, data, imperial = found
    cur = data["current"]
    rain = (data.get("daily", {}).get("precipitation_probability_max")
            or [0])[0] or 0
    wind = cur["wind_speed_10m"] * (1.609 if imperial else 1.0)
    advice = clothes_advice(to_c(cur["apparent_temperature"], imperial),
                            rain, wind)
    return reply(f"For {spot['name']} today, wear {advice}.")


# ---------- memory, preferences, habits ----------
def remember(ctx, kind, content):
    text = str(content).strip()[:200]
    if not text:
        return reply("There is nothing to remember.", ok=False)
    ctx.store.execute(
        "INSERT OR IGNORE INTO memories(user_id, kind, content, created) "
        "VALUES(?, ?, ?, ?)",
        (ctx.uid, kind, text, time.time()),
    )
    return reply(f"Remembered ({kind}): {text}.")


@tool("rememberGoal", "rememberGoal(goal)", group="Memory")
def remember_goal(ctx, goal=""):
    return remember(ctx, "goal", goal)


@tool("rememberPreference", "rememberPreference(preference)", group="Memory")
def remember_preference(ctx, preference=""):
    return remember(ctx, "preference", preference)


@tool("rememberPerson", "rememberPerson(name, details?)", group="Memory")
def remember_person(ctx, name="", details=""):
    return remember(ctx, "person", (name + ": " + details).strip(": "))


@tool("rememberStudyTopic", "rememberStudyTopic(topic, subject?)",
      group="Memory")
def remember_study_topic(ctx, topic="", subject=""):
    return remember(ctx, "study", (subject + " - " + topic).strip(" -"))


@tool("rememberProject", "rememberProject(name, description?)",
      group="Memory")
def remember_project(ctx, name="", description=""):
    made = create_project(ctx, name, description)
    return made if not made["ok"] else remember(
        ctx, "project", (name + ": " + description).strip(": "))


@tool("rememberTask", "rememberTask(title, due?)", group="Memory")
def remember_task(ctx, title="", due=""):
    return create_task(ctx, title, due)


def memory_rows(ctx):
    return ctx.store.query(
        "SELECT id, kind, content FROM memories WHERE user_id=? "
        "ORDER BY id DESC", (ctx.uid,))


@tool("deleteMemory", "deleteMemory(memory)", group="Memory")
def delete_memory(ctx, memory=""):
    if need(memory, "memory"):
        return need(memory, "memory")
    row, err = one(pick(memory_rows(ctx), memory, "content", False),
                   "memory", memory)
    if err:
        return err
    ctx.store.execute("DELETE FROM memories WHERE id=? AND user_id=?",
                      (row["id"], ctx.uid))
    return reply("Forgot: " + row["content"] + ".")


@tool("updateMemory", "updateMemory(memory, content)", group="Memory")
def update_memory(ctx, memory="", content=""):
    row, err = one(pick(memory_rows(ctx), memory, "content", False),
                   "memory", memory)
    if err:
        return err
    if not content.strip():
        return reply("Say what the memory should become.", ok=False)
    ctx.store.execute(
        "UPDATE memories SET content=? WHERE id=? AND user_id=?",
        (content.strip()[:200], row["id"], ctx.uid))
    return reply("Updated memory to: " + content.strip()[:200] + ".")


@tool("setPreference", "setPreference(key, value)", group="Memory")
def set_preference(ctx, key="", value=""):
    if not re.fullmatch(r"[a-z_]{2,30}", key.strip().lower()):
        return reply("Use a short key such as wake_time or study_hours.",
                     ok=False)
    set_pref(ctx.store, ctx.uid, key.strip().lower(), value)
    return reply(f"Preference {key.strip().lower()} set to {value}.")


@tool("getPreferences", "getPreferences()", group="Memory")
def get_preferences(ctx):
    rows = ctx.store.query(
        "SELECT key, value FROM assistant_preferences WHERE user_id=? "
        "AND key NOT LIKE '\\_%' ESCAPE '\\' ORDER BY key", (ctx.uid,))
    if not rows:
        return reply("No preferences are saved yet.")
    return reply("Preferences: " + "; ".join(
        f"{r['key']}={r['value']}" for r in rows) + ".")


def habits(ctx):
    rows = ctx.store.query(
        "SELECT m.created AS t FROM messages m JOIN conversations c ON "
        "c.id = m.conv_id WHERE c.user_id=? AND m.role='user' "
        "ORDER BY m.created DESC LIMIT 500", (ctx.uid,))
    zone = local(ctx).tzinfo
    hours = {}
    for row in rows:
        hour = datetime.fromtimestamp(row["t"], zone).hour
        hours[hour] = hours.get(hour, 0) + 1
    used = ctx.store.query(
        "SELECT tool, COUNT(*) AS n FROM tool_history WHERE user_id=? "
        "GROUP BY tool ORDER BY n DESC LIMIT 5", (ctx.uid,))
    return hours, used


@tool("getHabits", "getHabits()", group="Memory")
def get_habits(ctx):
    hours, used = habits(ctx)
    if not hours:
        return reply("Not enough activity yet to see any habits.")
    busiest = sorted(hours, key=hours.get, reverse=True)[:2]
    text = "Most active around " + " and ".join(f"{h}:00" for h in busiest)
    if used:
        text += "; most used tools: " + ", ".join(u["tool"] for u in used)
    return reply(text + ".")


@tool("setJarvisMode", "setJarvisMode(enabled)", group="System")
def set_jarvis_mode(ctx, enabled=""):
    on = str(enabled).strip().lower() not in ("false", "no", "0", "off", "")
    set_pref(ctx.store, ctx.uid, "jarvis_mode", "1" if on else "0")
    text = ("JARVIS mode is on: voice, gestures, briefings and proactive "
            "suggestions are active." if on else "JARVIS mode is off.")
    return reply(text, data={"jarvis": on},
                 ui=[{"cmd": "jarvis", "arg": "on" if on else "off"}])


# ---------- more tasks and projects ----------
@tool("editTask", "editTask(task, title?, due?, priority?, project?)",
      group="Tasks")
def edit_task(ctx, task="", title="", due="", priority="", project=""):
    row, err = one(pick(ranked(ctx), task, "title", False), "task", task)
    if err:
        return err
    sets, args = [], []
    if title:
        sets.append("title=?")
        args.append(title[:200])
    if due:
        moment = parse_when(due, local(ctx))
        if moment is None:
            return reply("Could not read that due time.", ok=False)
        sets.append("due_at=?")
        args.append(moment.timestamp())
    if priority:
        sets.append("priority=?")
        args.append(PRIORITY.get(priority.lower(), row["priority"]))
    if project:
        sets.append("project=?")
        args.append(project[:60])
    if not sets:
        return reply("Nothing to change.", ok=False)
    ctx.store.execute(
        "UPDATE tasks SET " + ", ".join(sets) + " WHERE id=? AND user_id=?",
        (*args, row["id"], ctx.uid))
    return reply(f"Updated task #{row['id']}.")


def task_lines(ctx, rows):
    now = time.time()
    return [task_line(ctx, t, now) for t in rows[:15]]


@tool("listDueTasks", "listDueTasks(days?)", group="Tasks")
def list_due_tasks(ctx, days="2"):
    now, span = time.time(), whole(days, 2, 1, 60) * 86400
    rows = [t for t in ranked(ctx)
            if t["due_at"] and now <= t["due_at"] < now + span]
    return reply("Due soon: " + "; ".join(task_lines(ctx, rows)) + "."
                 if rows else "Nothing is due in that time.")


@tool("listOverdueTasks", "listOverdueTasks()", group="Tasks")
def list_overdue_tasks(ctx):
    return list_tasks(ctx, "overdue")


@tool("estimateTaskTime", "estimateTaskTime(task)", group="Tasks")
def estimate_task_time(ctx, task=""):
    rows = pick(ranked(ctx), task, "title", False)
    title = rows[0]["title"] if len(rows) == 1 else str(task)
    low = title.lower()
    minutes = next((m for word, m in ESTIMATES if word in low), 30)
    minutes += 15 if len(low.split()) > 8 else 0
    if len(rows) == 1:
        ctx.store.execute("UPDATE tasks SET estimate=? WHERE id=?",
                          (minutes, rows[0]["id"]))
    return reply(f"Estimated time for '{title}': about {minutes} minutes "
                 "(a rough guess from the wording).",
                 data={"minutes": minutes})


@tool("suggestNextTask", "suggestNextTask()", group="Planning")
def suggest_next_task(ctx):
    rows = ranked(ctx)
    if not rows:
        return reply("There are no open tasks. Want to add one?")
    return reply("Next up: " + task_lines(ctx, rows)[0] + ".")


def project_rows(ctx):
    return ctx.store.query(
        "SELECT * FROM projects WHERE user_id=? ORDER BY id DESC",
        (ctx.uid,))


def progress_of(ctx, project):
    stats = ctx.store.query(
        "SELECT COUNT(*) AS total, SUM(status='done') AS done FROM tasks "
        "WHERE user_id=? AND LOWER(project)=LOWER(?) AND status!='deleted'",
        (ctx.uid, project["name"]))[0]
    if stats["total"]:
        return round(100 * (stats["done"] or 0) / stats["total"]), \
            stats["total"]
    return project.get("progress") or 0, 0


@tool("createProject", "createProject(name, description?)", group="Projects")
def create_project(ctx, name="", description=""):
    if not name.strip():
        return reply("A project needs a name.", ok=False)
    if any(p["name"].lower() == name.strip().lower()
           for p in project_rows(ctx)):
        return reply("That project already exists.", ok=False)
    pid = ctx.store.execute(
        "INSERT INTO projects(user_id, name, description, created, updated) "
        "VALUES(?, ?, ?, ?, ?)",
        (ctx.uid, name.strip()[:80], description[:500], time.time(),
         time.time()))
    return reply(f"Project #{pid} created: {name.strip()}.", data={"id": pid})


@tool("editProject",
      "editProject(project, name?, description?, status?, progress?)",
      group="Projects")
def edit_project(ctx, project="", name="", description="", status="",
                 progress=""):
    row, err = one(pick(project_rows(ctx), project, "name", False),
                   "project", project)
    if err:
        return err
    fields = (("name", name[:80]), ("description", description[:500]),
              ("status", status[:20]))
    sets = [(k + "=?", v) for k, v in fields if v]
    if str(progress).strip():
        sets.append(("progress=?", whole(progress, 0, 0, 100)))
    if not sets:
        return reply("Nothing to change.", ok=False)
    ctx.store.execute(
        "UPDATE projects SET " + ", ".join(s for s, _ in sets)
        + ", updated=? WHERE id=? AND user_id=?",
        (*[v for _, v in sets], time.time(), row["id"], ctx.uid))
    return reply("Updated project " + row["name"] + ".")


@tool("deleteProject", "deleteProject(project, confirm)", group="Projects")
def delete_project(ctx, project="", confirm=""):
    if need(project, "project"):
        return need(project, "project")
    row, err = one(pick(project_rows(ctx), project, "name", False),
                   "project", project)
    if err:
        return err
    if not confirmed(confirm):
        return reply(f"Ask the user to confirm deleting project "
                     f"{row['name']}, then call again with confirm=true.",
                     ok=False)
    ctx.store.execute("DELETE FROM projects WHERE id=? AND user_id=?",
                      (row["id"], ctx.uid))
    return reply("Deleted project " + row["name"] + ".")


@tool("trackProject", "trackProject(project, progress?, note?)",
      group="Projects")
def track_project(ctx, project="", progress="", note=""):
    row, err = one(pick(project_rows(ctx), project, "name", False),
                   "project", project)
    if err:
        return err
    if str(progress).strip():
        ctx.store.execute("UPDATE projects SET progress=? WHERE id=?",
                          (whole(progress, 0, 0, 100), row["id"]))
    ctx.store.execute("UPDATE projects SET updated=? WHERE id=?",
                      (time.time(), row["id"]))
    if note.strip():
        ctx.store.execute(
            "INSERT INTO notes(user_id, title, body, created) "
            "VALUES(?, ?, ?, ?)",
            (ctx.uid, "[Project] " + row["name"], note[:2000], time.time()))
    return reply("Logged work on " + row["name"] + ".")


def project_line(ctx, project):
    percent, total = progress_of(ctx, project)
    idle = dict(idle_projects(ctx, days=1)).get(project["name"])
    tail = f", quiet for {idle}d" if idle else ""
    return (f"{project['name']}: {percent}% ({total} tasks, "
            f"{project.get('status') or 'active'}{tail})")


@tool("showProjectProgress", "showProjectProgress(project?)",
      group="Projects")
def show_project_progress(ctx, project=""):
    rows = project_rows(ctx)
    if project:
        rows = pick(rows, project, "name", False)
    if not rows:
        return reply("No matching projects.", ok=False)
    return reply("Progress: " + "; ".join(project_line(ctx, p)
                                          for p in rows[:10]) + ".")


@tool("showProjectDashboard", "showProjectDashboard()", group="Projects")
def show_project_dashboard(ctx):
    rows = project_rows(ctx)
    if not rows:
        return reply("There are no projects yet.", data={"projects": []})
    lines = [project_line(ctx, p) for p in rows[:12]]
    return reply("Projects:\n" + "\n".join(lines), data={"projects": lines},
                 ui=[{"cmd": "view", "arg": "system"}])


# ---------- more reminders, alarms, timers, notes ----------
@tool("updateReminder", "updateReminder(reminderId, title?, when?)",
      group="Reminders")
def update_reminder(ctx, reminderId="", title="", when=""):
    row, err = one(pick(pending(ctx, "reminders"), reminderId, "title",
                        False), "reminder", reminderId)
    if err:
        return err
    if when:
        moment = parse_when(when, local(ctx))
        if moment is None:
            return reply("Could not read that time.", ok=False)
        ctx.store.execute("UPDATE reminders SET due_at=? WHERE id=?",
                          (moment.timestamp(), row["id"]))
    if title:
        ctx.store.execute("UPDATE reminders SET title=? WHERE id=?",
                          (title[:120], row["id"]))
    return reply(f"Updated reminder #{row['id']}.")


@tool("showUpcomingReminders", "showUpcomingReminders(hours?)",
      group="Reminders")
def show_upcoming_reminders(ctx, hours="24"):
    limit = time.time() + whole(hours, 24, 1, 720) * 3600
    rows = [r for r in pending(ctx, "reminders") if r["due_at"] <= limit]
    return reply("Upcoming: " + "; ".join(
        f"{r['title']} ({when_text(ctx, r['due_at'])})"
        for r in rows[:10]) + "." if rows else "No reminders in that time.")


def alarm_rows(ctx):
    return ctx.store.query(
        "SELECT id, title, due_at, status, repeat_rule FROM alarms WHERE "
        "user_id=? AND status!='deleted' ORDER BY due_at", (ctx.uid,))


def pick_alarm(ctx, ref):
    rows = alarm_rows(ctx)
    if str(ref).strip():
        return one(pick(rows, ref, "title"), "alarm", ref)
    now = time.time()
    rows.sort(key=lambda r: abs(r["due_at"] - now))
    return (rows[0], None) if rows else (None, reply(
        "There are no alarms.", ok=False))


@tool("snoozeAlarm", "snoozeAlarm(alarmId?, minutes?)", group="Alarms")
def snooze_alarm(ctx, alarmId="", minutes="10"):
    row, err = pick_alarm(ctx, alarmId)
    if err:
        return err
    wait = whole(minutes, 10, 1, 120)
    stamp = time.time() + wait * 60
    ctx.store.execute(
        "UPDATE alarms SET due_at=?, status='pending' WHERE id=?",
        (stamp, row["id"]))
    return reply(f"Snoozed '{row['title']}' for {wait} minutes, until "
                 f"{clock_text(ctx, stamp)}.")


@tool("enableRepeatingAlarm", "enableRepeatingAlarm(alarmId, repeat)",
      group="Alarms")
def enable_repeating_alarm(ctx, alarmId="", repeat="daily"):
    rule = str(repeat).strip().lower()
    if rule not in ("daily", "weekdays", "weekly"):
        return reply("Repeat must be daily, weekdays or weekly.", ok=False)
    row, err = pick_alarm(ctx, alarmId)
    if err:
        return err
    stamp = next_occurrence(row["due_at"], rule, local(ctx))
    ctx.store.execute(
        "UPDATE alarms SET repeat_rule=?, due_at=?, status='pending' "
        "WHERE id=?", (rule, stamp, row["id"]))
    return reply(f"'{row['title']}' now repeats {rule}, next at "
                 f"{when_text(ctx, stamp)}.")


@tool("disableAlarm", "disableAlarm(alarmId)", group="Alarms")
def disable_alarm(ctx, alarmId=""):
    row, err = pick_alarm(ctx, alarmId)
    if err:
        return err
    ctx.store.execute("UPDATE alarms SET status='disabled' WHERE id=?",
                      (row["id"],))
    return reply(f"Alarm '{row['title']}' is switched off.")


@tool("enableAlarm", "enableAlarm(alarmId)", group="Alarms")
def enable_alarm(ctx, alarmId=""):
    row, err = pick_alarm(ctx, alarmId)
    if err:
        return err
    stamp = next_occurrence(
        row["due_at"], row["repeat_rule"] or "daily", local(ctx))
    ctx.store.execute(
        "UPDATE alarms SET status='pending', due_at=? WHERE id=?",
        (stamp, row["id"]))
    return reply(f"Alarm '{row['title']}' is on for {when_text(ctx, stamp)}.")


@tool("labelTimer", "labelTimer(timerId, label)", group="Timers")
def label_timer(ctx, timerId="", label=""):
    row, err = one(pick(tools.active_timers(ctx), timerId, "label", False),
                   "timer", timerId)
    if err:
        return err
    ctx.store.execute("UPDATE timers SET label=? WHERE id=?",
                      (label[:80], row["id"]))
    return reply(f"Timer #{row['id']} is now called '{label[:80]}'.")


def note_rows(ctx):
    return ctx.store.query(
        "SELECT id, title, body, pinned FROM notes WHERE user_id=? "
        "ORDER BY pinned DESC, id DESC", (ctx.uid,))


@tool("showNotes", "showNotes(limit?)", group="Notes")
def show_notes(ctx, limit="10"):
    rows = note_rows(ctx)[:whole(limit, 10, 1, 50)]
    if not rows:
        return reply("There are no notes yet.")
    return reply("Notes: " + " | ".join(
        f"#{r['id']}{' (pinned)' if r['pinned'] else ''} {r['title']}: "
        f"{r['body'][:100]}" for r in rows))


@tool("editNote", "editNote(note, title?, content?)", group="Notes")
def edit_note(ctx, note="", title="", content=""):
    row, err = one(pick(note_rows(ctx), note, "title", False), "note", note)
    if err:
        return err
    ctx.store.execute(
        "UPDATE notes SET title=?, body=? WHERE id=?",
        (title[:80] or row["title"], content[:5000] or row["body"],
         row["id"]))
    return reply(f"Updated note #{row['id']}.")


@tool("deleteNote", "deleteNote(note)", group="Notes")
def delete_note(ctx, note=""):
    if need(note, "note"):
        return need(note, "note")
    row, err = one(pick(note_rows(ctx), note, "title", False), "note", note)
    if err:
        return err
    ctx.store.execute("DELETE FROM notes WHERE id=? AND user_id=?",
                      (row["id"], ctx.uid))
    return reply("Deleted note: " + row["title"] + ".")


@tool("pinNote", "pinNote(note, pinned?)", group="Notes")
def pin_note(ctx, note="", pinned="true"):
    row, err = one(pick(note_rows(ctx), note, "title", False), "note", note)
    if err:
        return err
    flag = 0 if str(pinned).strip().lower() in ("false", "no", "0") else 1
    ctx.store.execute("UPDATE notes SET pinned=? WHERE id=?",
                      (flag, row["id"]))
    return reply(("Pinned" if flag else "Unpinned") + " note: "
                 + row["title"] + ".")


# ---------- calendar and bookmarks ----------
def event_rows(ctx, lo, hi):
    return ctx.store.query(
        "SELECT id, title, start_at, end_at, location FROM events WHERE "
        "user_id=? AND start_at >= ? AND start_at < ? ORDER BY start_at",
        (ctx.uid, lo, hi))


def all_events(ctx):
    return ctx.store.query(
        "SELECT * FROM events WHERE user_id=? ORDER BY start_at",
        (ctx.uid,))


@tool("createEvent", "createEvent(title, when, duration?, location?)",
      group="Calendar")
def create_event(ctx, title="", when="", duration="1 hour", location=""):
    start = parse_when(when, local(ctx))
    if not title.strip() or start is None:
        return reply("An event needs a title and a readable time.", ok=False)
    length = parse_duration(duration) or 3600
    eid = ctx.store.execute(
        "INSERT INTO events(user_id, title, start_at, end_at, location, "
        "created) VALUES(?, ?, ?, ?, ?, ?)",
        (ctx.uid, title.strip()[:120], start.timestamp(),
         start.timestamp() + length, location[:120], time.time()))
    return reply(f"Event #{eid} added: {title.strip()} on "
                 f"{when_text(ctx, start.timestamp())} "
                 f"({fmt_secs(length)}).", data={"id": eid})


@tool("editEvent", "editEvent(event, title?, when?, duration?, location?)",
      group="Calendar")
def edit_event(ctx, event="", title="", when="", duration="", location=""):
    row, err = one(pick(all_events(ctx), event, "title", False),
                   "event", event)
    if err:
        return err
    start, length = row["start_at"], row["end_at"] - row["start_at"]
    if when:
        moment = parse_when(when, local(ctx))
        if moment is None:
            return reply("Could not read that time.", ok=False)
        start = moment.timestamp()
    if duration:
        length = parse_duration(duration) or length
    ctx.store.execute(
        "UPDATE events SET title=?, start_at=?, end_at=?, location=? "
        "WHERE id=?", (title[:120] or row["title"], start, start + length,
                       location[:120] or row["location"], row["id"]))
    return reply(f"Updated event #{row['id']}.")


@tool("deleteEvent", "deleteEvent(event)", group="Calendar")
def delete_event(ctx, event=""):
    if need(event, "event"):
        return need(event, "event")
    row, err = one(pick(all_events(ctx), event, "title", False),
                   "event", event)
    if err:
        return err
    ctx.store.execute("DELETE FROM events WHERE id=? AND user_id=?",
                      (row["id"], ctx.uid))
    return reply("Deleted event: " + row["title"] + ".")


def day_lines(ctx, offset=0):
    """Events, reminders, alarms and due tasks for a day (0 = today)."""
    lo, hi = day_bounds(ctx, offset)
    items = [(e["start_at"], "Event: " + e["title"])
             for e in event_rows(ctx, lo, hi)]
    for table, label in (("reminders", "Reminder"), ("alarms", "Alarm")):
        items += [(r["due_at"], f"{label}: {r['title']}")
                  for r in pending(ctx, table) if lo <= r["due_at"] < hi]
    items += [(t["due_at"], "Task due: " + t["title"]) for t in ranked(ctx)
              if t["due_at"] and lo <= t["due_at"] < hi]
    return [f"{clock_text(ctx, s)} {text}" for s, text in sorted(items)]


@tool("showTodaysSchedule", "showTodaysSchedule()", group="Calendar")
def show_todays_schedule(ctx):
    lines = day_lines(ctx, 0)
    return reply("Today: " + "; ".join(lines) + "." if lines
                 else "Nothing is scheduled today.", data={"lines": lines})


@tool("showWeeklySchedule", "showWeeklySchedule()", group="Calendar")
def show_weekly_schedule(ctx):
    now, out = local(ctx), []
    for offset in range(7):
        lines = day_lines(ctx, offset)
        if lines:
            day = (now + timedelta(days=offset)).strftime("%A %d %B")
            out.append(day + ": " + "; ".join(lines))
    return reply("\n".join(out) or "Nothing is scheduled this week.")


@tool("showCalendar", "showCalendar(range: today, tomorrow, week, month)",
      group="Calendar")
def show_calendar(ctx, range="week"):
    span = str(range).strip().lower()
    if span in ("today", "tomorrow"):
        lo, hi = day_bounds(ctx, 1 if span == "tomorrow" else 0)
    else:
        lo = time.time()
        hi = lo + (30 if span == "month" else 7) * 86400
    rows = event_rows(ctx, lo, hi)
    if not rows:
        return reply("No events in that range.")
    return reply("Events: " + "; ".join(
        f"#{e['id']} {e['title']} ({when_text(ctx, e['start_at'])})"
        for e in rows[:20]) + ".")


@tool("bookmarkWebsite", "bookmarkWebsite(url, title?)", group="Web")
def bookmark_website(ctx, url="", title=""):
    link = tools.clean_url(url)
    if link is None:
        return reply("That is not a valid web address.", ok=False)
    ctx.store.execute(
        "INSERT OR REPLACE INTO bookmarks(user_id, url, title, created) "
        "VALUES(?, ?, ?, ?)", (ctx.uid, link, (title or link)[:100],
                               time.time()))
    return reply("Bookmarked " + (title or link) + ".")


@tool("searchBookmarks", "searchBookmarks(query)", group="Web")
def search_bookmarks(ctx, query=""):
    rows = ctx.store.query(
        "SELECT title, url FROM bookmarks WHERE user_id=? AND (title LIKE ? "
        "ESCAPE '\\' OR url LIKE ? ESCAPE '\\') ORDER BY id DESC LIMIT 8",
        (ctx.uid, like(query), like(query)))
    return reply("Bookmarks: " + " | ".join(
        f"{r['title']} - {r['url']}" for r in rows) if rows
        else "No bookmarks match " + str(query) + ".")


# ---------- study and research ----------
@tool("trackStudyTime", "trackStudyTime(subject, minutes)", group="Study")
def track_study_time(ctx, subject="", minutes="30"):
    amount = whole(minutes, 0, 0, 1440)
    if not subject.strip() or not amount:
        return reply("Give a subject and the minutes studied.", ok=False)
    ctx.store.execute(
        "INSERT INTO study_log(user_id, subject, minutes, created) "
        "VALUES(?, ?, ?, ?)", (ctx.uid, subject.strip()[:60], amount,
                               time.time()))
    lo, hi = day_bounds(ctx)
    total = ctx.store.query(
        "SELECT SUM(minutes) AS m FROM study_log WHERE user_id=? AND "
        "created >= ? AND created < ?", (ctx.uid, lo, hi))[0]["m"] or 0
    return reply(f"Logged {amount} minutes of {subject.strip()}. "
                 f"Today's total: {int(total)} minutes.")


def wiki_search(query, limit=3):
    resp = requests.get(
        WIKI_URL,
        params={"action": "query", "generator": "search",
                "gsrsearch": query, "gsrlimit": limit,
                "prop": "extracts|info", "exintro": 1, "explaintext": 1,
                "exlimit": limit, "inprop": "url", "redirects": 1,
                "format": "json"},
        headers={"User-Agent": "MARZ-assistant/1.0"}, timeout=8)
    resp.raise_for_status()
    pages = resp.json().get("query", {}).get("pages", {}).values()
    return sorted(pages, key=lambda p: p.get("index", 99))


def wiki_text(query, limit=3, size=500):
    try:
        pages = wiki_search(query, limit)
    except PROBLEM:
        return None
    return [f"{p['title']} ({p.get('fullurl', '')}): "
            f"{(p.get('extract') or '')[:size]}" for p in pages]


def research_reply(query, intro):
    found = wiki_text(query)
    if found is None:
        return reply("Research lookup failed right now.", ok=False)
    if not found:
        return reply("No encyclopedia articles found for " + query + ".")
    return reply(intro + "\n" + "\n".join(found))


@tool("researchTopic", "researchTopic(topic)", group="Research")
def research_topic(ctx, topic=""):
    return research_reply(topic, "Encyclopedia (Wikipedia) results. MARZ has "
                          "no general web access, so say this is limited and "
                          "cite the links:")


@tool("generateSources", "generateSources(topic)", group="Research")
def generate_sources(ctx, topic=""):
    found = wiki_text(topic, 5, 0)
    if not found:
        return reply("Could not find sources for " + str(topic) + ".",
                     ok=False)
    return reply("Real article links (Wikipedia; also look for textbooks and "
                 "primary sources):\n" + "\n".join(found))


@tool("factCheck", "factCheck(claim)", group="Research")
def fact_check(ctx, claim=""):
    return research_reply(claim, "Compare the claim with these encyclopedia "
                          "extracts. Say supported, contradicted or "
                          "unverified, and note the limits:")


@tool("compareOptions", "compareOptions(first, second)", group="Research")
def compare_options(ctx, first="", second=""):
    a, b = wiki_text(first, 1), wiki_text(second, 1)
    if a is None or b is None:
        return reply("Research lookup failed right now.", ok=False)
    return reply("Compare these two options in a short table of pros, cons "
                 "and best use:\nA: " + " ".join(a or [first]) + "\nB: "
                 + " ".join(b or [second]))


@tool("summarizeEmails", "summarizeEmails()", group="Email")
def summarize_emails(ctx):
    return reply("Email access is not connected. Ask the user to paste the "
                 "emails and summarise them.", ok=False)


@tool("searchEmails", "searchEmails(query)", group="Email")
def search_emails(ctx, query=""):
    return reply("Email access is not connected, so inboxes cannot be "
                 "searched. Offer to draft or reply to pasted emails.",
                 ok=False)


# ---------- plans, briefings, proactive checks ----------
def plan_context(ctx):
    now = local(ctx)
    rows = ranked(ctx)
    memories = ctx.store.query(
        "SELECT kind, content FROM memories WHERE user_id=? AND kind IN "
        "('goal', 'preference', 'study', 'project') ORDER BY id DESC "
        "LIMIT 15", (ctx.uid,))
    parts = [
        live.format_time(now, ctx.locale),
        "Open tasks: " + ("; ".join(task_lines(ctx, rows)) or "none"),
        "Today: " + ("; ".join(day_lines(ctx, 0)) or "nothing scheduled"),
        "Tomorrow: " + ("; ".join(day_lines(ctx, 1)) or "nothing scheduled"),
        "Known goals and topics: " + ("; ".join(
            f"[{m['kind']}] {m['content']}" for m in memories) or "none"),
        get_preferences(ctx)["text"],
    ]
    return "\n".join(parts)


def make_skill(guidance, wants_context, needs_image):
    def run(ctx, **kw):
        if needs_image and not ctx.images:
            return reply("No image is attached. Ask the user to paste or "
                         "upload one.", ok=False)
        text = guidance
        if kw:
            text += "\nInputs: " + json.dumps(kw)[:1500]
        if wants_context:
            text += "\nContext:\n" + plan_context(ctx)
        return reply(text)
    return run


for _name, (_sig, _group, _how) in SKILLS.items():
    tool(_name, _sig, group=_group)(make_skill(
        _how, _name in CONTEXT_SKILLS, _group == "Vision"))


def weather_line(ctx):
    spot, _ = spot_for(ctx, "")
    if spot is None:
        return ""
    found, err = forecast_for(ctx, "", 2)
    if err:
        return ""
    cur, units = found[1]["current"], found[1]["current_units"]
    return (f"Weather in {spot['name']}: "
            f"{live.WMO.get(cur['weather_code'], 'unknown')}, "
            f"{cur['temperature_2m']}{units['temperature_2m']}.")


def build_briefing(ctx, kind):
    now, lines = local(ctx), []
    if kind == "daily":
        lines.append(weather_line(ctx))
        today = day_lines(ctx, 0)
        lines.append("Today's schedule: " + "; ".join(today) if today
                     else "Nothing is scheduled today.")
        lines += briefing(ctx)
    else:
        lo, hi = day_bounds(ctx)
        done = ctx.store.query(
            "SELECT COUNT(*) AS n FROM tasks WHERE user_id=? AND "
            "status='done' AND done_at >= ? AND done_at < ?",
            (ctx.uid, lo, hi))[0]["n"]
        studied = ctx.store.query(
            "SELECT SUM(minutes) AS m FROM study_log WHERE user_id=? AND "
            "created >= ? AND created < ?", (ctx.uid, lo, hi))[0]["m"] or 0
        tomorrow = day_lines(ctx, 1)
        lines += [f"Tasks completed today: {done}; still open: "
                  f"{len(ranked(ctx))}.",
                  f"Study time today: {int(studied)} minutes.",
                  "Tomorrow: " + "; ".join(tomorrow) if tomorrow
                  else "Tomorrow is clear so far.",
                  "Suggest a short wind-down and an early night."]
    lines = [x for x in lines if x]
    ctx.store.execute(
        "INSERT OR REPLACE INTO daily_briefings(user_id, kind, day, text, "
        "created) VALUES(?, ?, ?, ?, ?)",
        (ctx.uid, kind, now.strftime("%Y-%m-%d"), "\n".join(lines),
         time.time()))
    return lines


@tool("generateDailyBriefing", "generateDailyBriefing()", group="Planning")
def daily_briefing(ctx):
    lines = build_briefing(ctx, "daily")
    return reply("Daily briefing (present it warmly in a few sentences):\n"
                 + "\n".join(lines), data={"lines": lines})


@tool("generateEveningBriefing", "generateEveningBriefing()",
      group="Planning")
def evening_briefing(ctx):
    lines = build_briefing(ctx, "evening")
    return reply("Evening briefing (present it warmly and briefly):\n"
                 + "\n".join(lines), data={"lines": lines})


def deadlines(ctx):
    now = time.time()
    return task_lines(ctx, [t for t in ranked(ctx)
                            if t["due_at"] and now <= t["due_at"] < now
                            + 172800])


def overdue(ctx):
    now = time.time()
    return task_lines(ctx, [t for t in ranked(ctx)
                            if t["due_at"] and t["due_at"] < now])


def upcoming_events(ctx, minutes=90):
    now = time.time()
    return [f"{e['title']} at {clock_text(ctx, e['start_at'])}"
            for e in event_rows(ctx, now, now + minutes * 60)]


def soon_reminders(ctx, minutes=120):
    limit = time.time() + minutes * 60
    return [f"{r['title']} at {clock_text(ctx, r['due_at'])}"
            for r in pending(ctx, "reminders") if r["due_at"] <= limit]


def revision_ideas(ctx):
    topics = ctx.store.query(
        "SELECT content FROM memories WHERE user_id=? AND kind='study' "
        "ORDER BY id DESC LIMIT 5", (ctx.uid,))
    recent = ctx.store.query(
        "SELECT subject FROM study_log WHERE user_id=? AND created >= ?",
        (ctx.uid, time.time() - 172800))
    seen = " ".join(r["subject"].lower() for r in recent)
    return [f"Revise {t['content']}" for t in topics
            if t["content"].split(" - ")[0].lower() not in seen][:3]


def project_steps(ctx):
    out = []
    for project in project_rows(ctx)[:5]:
        rows = [t for t in ranked(ctx)
                if (t["project"] or "").lower() == project["name"].lower()]
        if rows:
            out.append(f"{project['name']}: next step is '{rows[0]['title']}'")
    return out


def busy_session(ctx):
    rows = ctx.store.query(
        "SELECT COUNT(*) AS n FROM messages m JOIN conversations c ON "
        "c.id = m.conv_id WHERE c.user_id=? AND m.role='user' AND "
        "m.created >= ?", (ctx.uid, time.time() - 5400))
    return rows[0]["n"] >= 12


def listing_tool(name, signature, source, empty, lead):
    def run(ctx):
        found = source(ctx)
        return reply(lead + "; ".join(found) + "." if found else empty,
                     data={"items": found})
    tool(name, signature, group="Proactive")(run)


for _args in (
    ("checkDeadlines", "checkDeadlines()", deadlines,
     "No deadlines in the next 48 hours.", "Deadlines soon: "),
    ("checkOverdueTasks", "checkOverdueTasks()", overdue,
     "Nothing is overdue.", "Overdue: "),
    ("checkUpcomingEvents", "checkUpcomingEvents()", upcoming_events,
     "No events in the next 90 minutes.", "Coming up: "),
    ("checkReminders", "checkReminders()", soon_reminders,
     "No reminders in the next 2 hours.", "Reminders soon: "),
    ("suggestRevision", "suggestRevision()", revision_ideas,
     "No study topics are waiting. Tell me what you are studying.",
     "Revision ideas: "),
    ("suggestNextProjectStep", "suggestNextProjectStep()", project_steps,
     "No project has open tasks. Want me to plan one?", "Project steps: "),
):
    listing_tool(*_args)


@tool("suggestWork", "suggestWork()", group="Proactive")
def suggest_work(ctx):
    rows, idle = ranked(ctx), idle_projects(ctx)
    if rows:
        return reply("Best use of time now: " + task_lines(ctx, rows)[0] + ".")
    if idle:
        return reply(f"Nothing is queued. {idle[0][0]} has been quiet for "
                     f"{idle[0][1]} days: a good place to start.")
    return reply("Nothing is queued. Want to plan something new?")


@tool("suggestBreak", "suggestBreak()", group="Proactive")
def suggest_break(ctx):
    if busy_session(ctx):
        return reply("You have been working steadily for a while: take 10 "
                     "minutes away from the screen, stretch and rest your "
                     "eyes.")
    return reply("No break needed yet. A short stretch every hour helps.")


@tool("suggestHydration", "suggestHydration()", group="Proactive")
def suggest_hydration(ctx):
    return reply("A glass of water now would be a good idea.")


def proactive_items(ctx):
    """Prioritised nudges for the page; cooldowns stop repeats."""
    now, out = time.time(), []

    def add(rank, key, text):
        out.append({"rank": rank, "id": key, "text": text})

    for line in overdue(ctx)[:2]:
        add(1, "overdue:" + line[:30], "Overdue: " + line)
    for line in upcoming_events(ctx):
        add(1, "event:" + line, "Coming up soon: " + line)
    for line in soon_reminders(ctx, 60):
        add(2, "rem:" + line, "Reminder soon: " + line)
    for line in deadlines(ctx)[:2]:
        add(2, "due:" + line[:30], "Deadline approaching: " + line)
    for name, days in idle_projects(ctx)[:1]:
        add(3, f"idle:{name}", f"You have not worked on {name} for "
            f"{days} days.")
    for line in revision_ideas(ctx)[:1]:
        add(3, "rev:" + line, line + "?")
    hour = local(ctx).hour
    cool = {k: float(get_pref(ctx.store, ctx.uid, k, "0") or 0)
            for k in ("_last_break", "_last_water")}
    if busy_session(ctx) and now - cool["_last_break"] >= 3600:
        set_pref(ctx.store, ctx.uid, "_last_break", now)
        add(4, "break", "You have been at it a while. Time for a short "
            "break?")
    elif 8 <= hour < 21 and now - cool["_last_water"] >= 7200:
        set_pref(ctx.store, ctx.uid, "_last_water", now)
        add(5, "water", "A glass of water would be a good idea.")
    out.sort(key=lambda x: x["rank"])
    return out[:3]


# ---------- gestures and page commands ----------
def gesture_map(ctx):
    mapping = dict(GESTURES)
    for row in ctx.store.query(
            "SELECT gesture, command FROM gesture_mappings WHERE user_id=?",
            (ctx.uid,)):
        mapping[row["gesture"]] = row["command"]
    return mapping


@tool("saveGestureMapping", "saveGestureMapping(gesture, command)",
      group="Gestures")
def save_gesture_mapping(ctx, gesture="", command=""):
    name = str(gesture).strip().upper().replace(" ", "_")
    cmd = str(command).strip()
    ask = cmd.startswith("ask:") and 4 < len(cmd) < 200
    if name not in GESTURES:
        return reply("Gestures: " + ", ".join(GESTURES) + ".", ok=False)
    if cmd not in COMMANDS and not ask:
        return reply("Unknown command. Use one of: " + ", ".join(COMMANDS)
                     + ", or ask: followed by a message.", ok=False)
    ctx.store.execute(
        "INSERT OR REPLACE INTO gesture_mappings(user_id, gesture, command, "
        "created) VALUES(?, ?, ?, ?)", (ctx.uid, name, cmd, time.time()))
    return reply(f"{name} now runs {cmd}.")


@tool("removeGestureMapping", "removeGestureMapping(gesture)",
      group="Gestures")
def remove_gesture_mapping(ctx, gesture=""):
    name = str(gesture).strip().upper().replace(" ", "_")
    if name not in GESTURES:
        return reply("Unknown gesture.", ok=False)
    ctx.store.execute(
        "DELETE FROM gesture_mappings WHERE user_id=? AND gesture=?",
        (ctx.uid, name))
    return reply(f"{name} is back to its default: {GESTURES[name]}.")


@tool("listGestureMappings", "listGestureMappings()", group="Gestures")
def list_gesture_mappings(ctx):
    mapping = gesture_map(ctx)
    return reply("Gestures: " + "; ".join(f"{g} = {c}"
                                          for g, c in mapping.items()) + ".",
                 data={"mapping": mapping})


def make_client_tool(command, message):
    def run(ctx, value=""):
        arg = str(value).strip()[:300]
        if command == "view":
            arg = arg.lower().replace("dashboard", "").strip() or "assistant"
            arg = {"memory": "memories"}.get(arg, arg)
            if arg not in VIEWS:
                return reply("Dashboards: " + ", ".join(VIEWS) + ".",
                             ok=False)
        return reply(message + (f" ({arg})" if arg and command != "view"
                                else ""),
                     ui=[{"cmd": command, "arg": arg}])
    return run


for _name, _sig, _cmd, _msg in CLIENT:
    _area = "Voice" if _cmd.split(".")[0] in ("voice", "wake", "speak",
                                              "rate", "pitch") else "Page"
    tool(_name, _sig, group=_area)(make_client_tool(_cmd, _msg))


# ---------- hooks: more page state and obvious commands ----------
def snapshot_extra(ctx):
    now = time.time()
    return {"events": event_rows(ctx, now - 3600, now + 14 * 86400)[:20]}


def quick_extra(text, low):
    calls = []
    if re.search(r"\b(daily|morning) briefing\b|\bbrief me\b", low):
        calls.append(("generateDailyBriefing", {}))
    if re.search(r"evening briefing|wrap up my day|end of (the )?day", low):
        calls.append(("generateEveningBriefing", {}))
    if re.search(r"\bsnooze\b", low):
        calls.append(("snoozeAlarm", {}))
    if re.search(r"(what.s|what is) on today|today.s schedule", low):
        calls.append(("showTodaysSchedule", {}))
    if re.search(r"weekly schedule|my week\b|this week.s schedule", low):
        calls.append(("showWeeklySchedule", {}))
    if re.search(r"forecast|next few days|this week|tomorrow", low) \
            and live.WEATHER_RE.search(text):
        calls.append(("getForecast", {"location":
                                      live.find_place(text) or ""}))
    if re.search(r"what should i wear|what to wear|\bclothes\b|\bjacket\b",
                 low):
        calls.append(("recommendClothes", {}))
    if re.search(r"weather (alerts?|warnings?)", low):
        calls.append(("getWeatherAlerts", {}))
    return calls


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
tools.SNAPSHOT_HOOKS.append(snapshot_extra)
tools.QUICK_HOOKS.append(quick_extra)
