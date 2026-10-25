"""MARZ tools: functions the assistant can call.

Every tool takes a Ctx plus keyword arguments and returns a dict with
"text" (what the model sees), "ok", and optional "data" and "open".
Timers and the stopwatch live in SQLite so they survive page reloads.
"""
import inspect
import json
import logging
import re
import time
from urllib.parse import urlparse

import requests

import live

log = logging.getLogger("marz")

HTTPS = "https" + "://"
MAX_TIMERS = 20
MAX_SECONDS = 7 * 24 * 3600
MAX_CALLS = 4
TOOLS = {}
TOOL_ALIASES = {
    "getDate": "getCurrentDate", "getTimers": "listTimers",
    "getCurrentTime": "getLocalTime", "prioritizeTasks": "prioritiseTasks",
    "suggestPriority": "prioritiseTasks", "showReminders": "listReminders",
    "readFile": "openFile", "searchFile": "searchFiles",
    "extractText": "openFile", "showToday'sSchedule": "showTodaysSchedule",
}
ALIASES = {
    "time": ("when", "due"), "datetime": ("when", "due"),
    "date": ("when", "due"), "due": ("when",), "when": ("due",),
}
ID_PARAMS = ("timerId", "reminderId", "alarmId", "task", "name")
SNAPSHOT_HOOKS = []
QUICK_HOOKS = []

NUMBER_WORDS = {
    "half an": 0.5, "half a": 0.5, "an": 1, "a": 1, "one": 1, "two": 2,
    "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30,
    "forty": 40, "forty five": 45, "fifty": 50, "sixty": 60,
}
UNIT_SECONDS = {"h": 3600, "m": 60, "s": 1, "d": 86400, "w": 604800}
DURATION_RE = re.compile(
    r"\b(\d+(?:\.\d+)?|half an?|forty five|an?|one|two|three|four|five|"
    r"six|seven|eight|nine|ten|fifteen|twenty|thirty|forty|fifty|sixty)"
    r"[\s-]*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s|days?|weeks?)"
    r"\b",
    re.I,
)
TOOL_RE = re.compile(r"\[\[TOOL:\s*(\{.*?\})\s*\]\]", re.S)
SET_TIMER_RE = re.compile(
    r"\b(set|start|create|make|begin|add|new)\b[^.?!]*\btimer\b"
    r"|\btimer (?:for|of)\b"
    r"|^\W*\d+[\s-]*(?:seconds?|minutes?|hours?|mins?|secs?|hrs?)\s+timer",
    re.I,
)
STOPWATCH_OPS = (
    ("lapStopwatch", r"\blap\b"),
    ("resetStopwatch", r"\b(reset|clear)\b"),
    ("pauseStopwatch", r"\bpause\b"),
    ("resumeStopwatch", r"\b(resume|continue|unpause)\b"),
    ("stopStopwatch", r"\b(stop|end|finish)\b"),
    ("startStopwatch", r"\b(start|begin|run|launch)\b"),
    ("getStopwatchTime", r"\b(time|elapsed|how long|check|show|read|what)\b"),
)
STOPWATCH_TEXT = {
    "start": "Stopwatch started.",
    "pause": "Stopwatch paused.",
    "resume": "Stopwatch resumed.",
    "stop": "Stopwatch stopped.",
    "reset": "Stopwatch reset.",
    "lap": "Lap recorded.",
    "get": "Stopwatch reading.",
}
TOOL_RULES = (
    "You can call tools. To call one, write a line like "
    '[[TOOL: {"name": "setTimer", "args": {"duration": "5 minutes"}}]] '
    "(several lines are allowed) and nothing else; the results are then "
    "returned to you and you answer in plain language. Always use a tool "
    "for the time, date, weather, timers, the stopwatch, notes, saved "
    "memories, reminders, alarms, tasks, files and opening websites; "
    "never guess these. Use createReminder for reminders (remind me to "
    "revise at 6 pm), setAlarm for wake-up calls, createTask for to-dos, "
    "getBriefing and prioritiseTasks to plan the day, and openFile or "
    "searchFiles to read, summarise or search saved documents. "
    "Timers and the stopwatch run while the user's chat page is open. Do "
    "not call a tool when the question does not need one. Some tools are "
    "skills: they return a method to follow, so apply it in your answer. "
    "Page tools (voice, music, gestures, dashboards) are carried out by "
    "the user's page. Never delete files, projects, notes or memories "
    "unless the user clearly asked; for deleteFile and deleteProject ask "
    "the user to confirm, then call again with confirm=true. Tools:\n"
)


class Ctx:
    """Who is asking and where, plus database access for tools."""

    def __init__(self, uid, store, conv_id=None, tz="", locale="",
                 coords=None, city=""):
        self.uid = uid
        self.store = store
        self.conv_id = conv_id
        self.tz = tz
        self.locale = locale
        self.coords = coords
        self.city = city
        self.images = 0


def tool(name, signature, doc="", group="core"):
    def register(fn):
        TOOLS[name] = {
            "fn": fn, "signature": signature, "doc": doc, "group": group,
        }
        return fn
    return register


def reply(text, ok=True, **extra):
    return dict(extra, text=text, ok=ok)


def fmt_secs(seconds):
    seconds = int(round(max(seconds, 0)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def parse_duration(value):
    """Turn '1 hour 30 minutes' (or a number of seconds) into seconds."""
    if isinstance(value, (int, float)):
        return float(value) or None
    text = str(value).strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text) or None
    total = 0.0
    for number, unit in DURATION_RE.findall(text):
        amount = NUMBER_WORDS.get(number.lower())
        if amount is None:
            amount = float(number)
        total += amount * UNIT_SECONDS[unit.lower()[0]]
    return total or None


def like(term):
    safe = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + safe + "%"


# ---------- time, date and weather ----------
def clock_for(ctx, location):
    if location:
        spot = live.geocode(location)
        if spot is None or not spot["tz"]:
            return None
        now, label = live.local_now(spot["tz"])
        return now, label, spot["name"]
    now, label = live.local_now(ctx.tz)
    return now, label, "your area"


@tool("getLocalTime", "getLocalTime(location?)",
      "Current local time and timezone, for the user or a named city.")
def get_local_time(ctx, location=""):
    found = clock_for(ctx, location)
    if found is None:
        return reply("Could not find " + location + ".", ok=False)
    now, label, place = found
    stamp = live.format_time(now, ctx.locale)
    clock, date = stamp.split(", ")[-1], stamp.split(", ")[0]
    return reply(
        f"Local time in {place}: {clock}, timezone {label}.",
        data={"time": clock, "date": date, "timezone": label},
    )


@tool("getCurrentDate", "getCurrentDate(location?)",
      "Today's date, for the user or a named city.")
def get_current_date(ctx, location=""):
    found = clock_for(ctx, location)
    if found is None:
        return reply("Could not find " + location + ".", ok=False)
    now, label, place = found
    date = live.format_time(now, ctx.locale).split(", ")[0]
    return reply(
        f"Today's date in {place}: {date} (timezone {label}).",
        data={"date": date, "timezone": label},
    )


def save_city(ctx, city):
    ctx.store.execute(
        "INSERT OR REPLACE INTO settings(user_id, key, value) "
        "VALUES(?, 'city', ?)",
        (ctx.uid, city[:80]),
    )


@tool("getWeather", "getWeather(location?)",
      "Current weather (temperature, conditions, wind, humidity) and the "
      "forecast. Leave location empty for the user's own place.")
def get_weather(ctx, location=""):
    spot = live.geocode(location) if location else None
    if location and spot is None:
        return reply("Could not find a place called " + location + ".",
                     ok=False)
    if spot is None and ctx.coords:
        spot = {"name": "your location", "lat": ctx.coords[0],
                "lon": ctx.coords[1], "tz": ""}
    if spot is None and ctx.city:
        spot = live.geocode(ctx.city)
    if spot is None:
        return reply("The user's location is unknown. Ask which city they "
                     "mean (once).", ok=False, ask="location")
    imperial = ctx.locale.startswith("en-US")
    key = ("wx", round(spot["lat"], 2), round(spot["lon"], 2), imperial)
    try:
        data = live.cached(key, lambda: live.fetch_forecast(spot, imperial))
        text = live.summarise(spot["name"], data)
        cur, units = data["current"], data["current_units"]
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return reply("Live weather could not be fetched right now.", ok=False)
    if location and not ctx.city and not ctx.coords:
        save_city(ctx, location)
    return reply(text, data={
        "place": spot["name"],
        "temp": cur["temperature_2m"],
        "unit": units["temperature_2m"],
        "conditions": live.WMO.get(cur["weather_code"], "unknown"),
        "wind": cur["wind_speed_10m"],
        "wind_unit": units["wind_speed_10m"],
        "humidity": cur["relative_humidity_2m"],
    })


# ---------- timers ----------
def active_timers(ctx):
    return ctx.store.query(
        "SELECT id, label, duration, ends_at, status, elapsed FROM timers "
        "WHERE user_id=? AND kind='timer' AND status IN ('active', "
        "'paused') ORDER BY id",
        (ctx.uid,),
    )


@tool("setTimer", "setTimer(duration, label?)",
      "Start a countdown timer. duration like '30 minutes' or '90 seconds'.")
def set_timer(ctx, duration="", label=""):
    seconds = parse_duration(duration)
    if seconds is None or seconds > MAX_SECONDS:
        return reply("Could not understand that duration. Use something "
                     "like '5 minutes' (up to 7 days).", ok=False)
    if len(active_timers(ctx)) >= MAX_TIMERS:
        return reply("Too many active timers (20). Cancel one first.",
                     ok=False)
    now = time.time()
    tid = ctx.store.execute(
        "INSERT INTO timers(user_id, kind, label, duration, ends_at, "
        "created) VALUES(?, 'timer', ?, ?, ?, ?)",
        (ctx.uid, label[:80], seconds, now + seconds, now),
    )
    note = f" ({label})" if label else ""
    return reply(f"Timer #{tid} set for {fmt_secs(seconds)}{note}.",
                 data={"id": tid, "ends_at": now + seconds})


@tool("cancelTimer", "cancelTimer(timerId)",
      "Cancel a timer by its number or label, or 'all'.")
def cancel_timer(ctx, timerId=""):
    timers, ref = active_timers(ctx), str(timerId).strip().lower()
    if ref in ("all", "everything"):
        chosen = timers
    elif ref.isdigit():
        chosen = [t for t in timers if t["id"] == int(ref)]
    elif ref:
        chosen = [t for t in timers if ref in (t["label"] or "").lower()]
    else:
        chosen = timers if len(timers) == 1 else []
    if not chosen:
        if not timers:
            return reply("There are no active timers.", ok=False)
        hint = "" if ref else " Several are running, so ask which one."
        return reply("No matching timer." + hint, ok=False)
    for timer in chosen:
        ctx.store.execute(
            "UPDATE timers SET status='cancelled' WHERE id=? AND user_id=?",
            (timer["id"], ctx.uid),
        )
    return reply("Cancelled timer " + ", ".join(
        "#" + str(t["id"]) for t in chosen) + ".")


@tool("listTimers", "listTimers()", "List the active timers.")
def list_timers(ctx):
    timers, now = active_timers(ctx), time.time()
    if not timers:
        return reply("There are no active timers.")
    parts = [
        "#%d %s: %s left" % (
            t["id"], t["label"] or fmt_secs(t["duration"]),
            fmt_secs(time_left(t, now)))
        for t in timers
    ]
    return reply("Active timers: " + "; ".join(parts) + ".")


def time_left(timer, now):
    if timer["status"] == "paused":
        return timer["elapsed"]
    return max(timer["ends_at"] - now, 0)


def pick(rows, ref, key, allow_all=True):
    """Choose rows by number, 'all' or a fragment of their title."""
    ref = str(ref).strip().lower()
    if allow_all and ref in ("all", "everything"):
        return rows
    if ref.isdigit():
        return [r for r in rows if r["id"] == int(ref)]
    if ref:
        return [r for r in rows if ref in str(r[key] or "").lower()]
    return rows if len(rows) == 1 else []


def switch_timer(ctx, ref, source):
    rows = [t for t in active_timers(ctx) if t["status"] == source]
    chosen = pick(rows, ref, "label", allow_all=False)
    if len(chosen) != 1:
        return reply("Say which timer (number or name)." if chosen
                     else "No " + source + " timer found.", ok=False)
    timer, now = chosen[0], time.time()
    if source == "active":
        left = max(timer["ends_at"] - now, 0)
        ctx.store.execute(
            "UPDATE timers SET status='paused', elapsed=? WHERE id=?",
            (left, timer["id"]),
        )
        return reply(
            f"Timer #{timer['id']} paused with {fmt_secs(left)} left."
        )
    ctx.store.execute(
        "UPDATE timers SET status='active', ends_at=?, elapsed=0 "
        "WHERE id=?",
        (now + timer["elapsed"], timer["id"]),
    )
    return reply(
        f"Timer #{timer['id']} resumed with {fmt_secs(timer['elapsed'])} left."
    )


@tool("pauseTimer", "pauseTimer(timerId?)", "Pause a running timer.")
def pause_timer(ctx, timerId=""):
    return switch_timer(ctx, timerId, "active")


@tool("resumeTimer", "resumeTimer(timerId?)", "Resume a paused timer.")
def resume_timer(ctx, timerId=""):
    return switch_timer(ctx, timerId, "paused")


def mark_done(ctx, tid):
    ctx.store.execute(
        "UPDATE timers SET status='done' WHERE id=? AND user_id=? "
        "AND status='active' AND ends_at <= ?",
        (tid, ctx.uid, time.time() + 2),
    )


# ---------- stopwatch ----------
def stopwatch_row(ctx):
    rows = ctx.store.query(
        "SELECT * FROM timers WHERE user_id=? AND kind='stopwatch' "
        "ORDER BY id DESC LIMIT 1",
        (ctx.uid,),
    )
    return rows[0] if rows else None


def elapsed(row, now):
    return row["elapsed"] + (now - row["started_at"] if row["running"] else 0)


def stopwatch_data(row, now):
    return {
        "base": row["elapsed"],
        "started_at": row["started_at"],
        "running": bool(row["running"]),
        "status": row["status"],
        "elapsed": elapsed(row, now),
        "laps": json.loads(row["laps"] or "[]"),
    }


def stopwatch_op(ctx, op):
    row, now = stopwatch_row(ctx), time.time()
    values = None
    if op == "start":
        values = (0.0, now, 1, "active")
    elif row is None:
        return reply("There is no stopwatch yet. Start one first.", ok=False)
    elif op == "pause" and row["running"]:
        values = (elapsed(row, now), None, 0, "paused")
    elif op == "resume" and row["status"] == "paused":
        values = (row["elapsed"], now, 1, "active")
    elif op == "stop":
        values = (elapsed(row, now), None, 0, "stopped")
    elif op == "reset":
        values = (0.0, None, 0, "idle")
    elif op == "lap" and row["running"]:
        laps = json.loads(row["laps"] or "[]") + [round(elapsed(row, now), 2)]
        ctx.store.execute(
            "UPDATE timers SET laps=? WHERE id=?",
            (json.dumps(laps), row["id"]),
        )
    elif op != "get":
        state = "running" if row["running"] else row["status"]
        return reply("The stopwatch is " + state + ".", ok=False,
                     data=stopwatch_data(row, now))
    if values and row is None:
        ctx.store.execute(
            "INSERT INTO timers(user_id, kind, elapsed, started_at, running,"
            " status, created) VALUES(?, 'stopwatch', ?, ?, ?, ?, ?)",
            (ctx.uid, *values, now),
        )
    elif values:
        ctx.store.execute(
            "UPDATE timers SET elapsed=?, started_at=?, running=?, status=?, "
            "laps=CASE WHEN ? THEN '' ELSE laps END WHERE id=?",
            (*values, op in ("start", "reset"), row["id"]),
        )
    row = stopwatch_row(ctx)
    return reply(
        STOPWATCH_TEXT[op] + " Elapsed: " + fmt_secs(elapsed(row, now)) + ".",
        data=stopwatch_data(row, now),
    )


def make_stopwatch_tool(name, op, doc):
    def run(ctx):
        return stopwatch_op(ctx, op)
    tool(name, name + "()", doc)(run)


for _name, _op, _doc in (
    ("startStopwatch", "start", "Start the stopwatch from zero."),
    ("pauseStopwatch", "pause", "Pause the running stopwatch."),
    ("resumeStopwatch", "resume", "Resume a paused stopwatch."),
    ("lapStopwatch", "lap", "Record a lap on the running stopwatch."),
    ("stopStopwatch", "stop", "Stop the stopwatch and keep its time."),
    ("resetStopwatch", "reset", "Reset the stopwatch to zero."),
    ("getStopwatchTime", "get", "Read the elapsed stopwatch time."),
):
    make_stopwatch_tool(_name, _op, _doc)


# ---------- notes, memory and websites ----------
@tool("createNote", "createNote(title, content)", "Save a note.")
def create_note(ctx, title="", content=""):
    if not (title or content):
        return reply("A note needs a title or some content.", ok=False)
    nid = ctx.store.execute(
        "INSERT INTO notes(user_id, title, body, created) "
        "VALUES(?, ?, ?, ?)",
        (ctx.uid, (title or content)[:80], content[:5000], time.time()),
    )
    return reply(f"Saved note #{nid}: {(title or content)[:60]}.",
                 data={"id": nid})


@tool("searchNotes", "searchNotes(query)", "Find saved notes.")
def search_notes(ctx, query=""):
    rows = ctx.store.query(
        "SELECT id, title, body FROM notes WHERE user_id=? AND "
        "(title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\') "
        "ORDER BY id DESC LIMIT 5",
        (ctx.uid, like(query), like(query)),
    )
    if not rows:
        return reply("No notes match " + query + ".")
    lines = [f"#{r['id']} {r['title']}: {r['body'][:200]}" for r in rows]
    return reply("Matching notes: " + " | ".join(lines))


@tool("saveMemory", "saveMemory(content)",
      "Remember a lasting fact about the user.")
def save_memory(ctx, content=""):
    if not content.strip():
        return reply("Nothing to remember.", ok=False)
    ctx.store.execute(
        "INSERT OR IGNORE INTO memories(user_id, kind, content, created) "
        "VALUES(?, 'tool', ?, ?)",
        (ctx.uid, content.strip()[:200], time.time()),
    )
    return reply("Remembered: " + content.strip()[:200] + ".")


@tool("searchMemory", "searchMemory(query)", "Search saved memories.")
def search_memory(ctx, query=""):
    rows = ctx.store.query(
        "SELECT content FROM memories WHERE user_id=? "
        "AND content LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT 5",
        (ctx.uid, like(query)),
    )
    if not rows:
        return reply("No memories match " + query + ".")
    return reply("Memories: " + " | ".join(r["content"] for r in rows))


def clean_url(raw):
    text = str(raw).strip()
    if re.fullmatch(r"[\w-]+(\.[\w-]+)+(/\S*)?", text):
        text = HTTPS + text
    parts = urlparse(text)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    if "@" in parts.netloc or " " in text or len(text) > 500:
        return None
    return text


@tool("openWebsite", "openWebsite(url)", "Open a website in a new tab.")
def open_website(ctx, url=""):
    link = clean_url(url)
    if link is None:
        return reply("That is not a valid web address.", ok=False)
    return reply("Opening " + link + ".", **{"open": link})


# ---------- running tools ----------
def normalise_args(fn, args):
    if not isinstance(args, dict):
        return {}
    params = inspect.signature(fn).parameters
    if any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return {
            str(k)[:40]: v if isinstance(v, (int, float)) else str(v)[:2000]
            for k, v in args.items()
        }
    wanted = {
        p.replace("_", "").lower(): p for p in params if p != "ctx"
    }
    out = {}
    for key, value in args.items():
        norm = str(key).replace("_", "").lower()
        param = wanted.get(norm)
        if param is None and norm == "id":
            param = next((p for p in wanted.values() if p in ID_PARAMS), None)
        if param is None:
            for target in ALIASES.get(norm, ()):
                if target in wanted:
                    param = wanted[target]
                    break
        if param:
            number = isinstance(value, (int, float))
            out[param] = value if number else str(value)
    if not out and len(wanted) == 1 and len(args) == 1:
        value = next(iter(args.values()))
        number = isinstance(value, (int, float))
        out[next(iter(wanted.values()))] = value if number else str(value)
    return out


def record(ctx, name, args, result):
    try:
        ctx.store.execute(
            "INSERT INTO tool_history(user_id, conv_id, tool, args, result, "
            "ok, created) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (ctx.uid, ctx.conv_id, name, json.dumps(args, default=str)[:500],
             result["text"][:1000], int(result["ok"]), time.time()),
        )
    except Exception:
        log.exception("Could not record tool call")


def run_tool(ctx, name, args):
    name = TOOL_ALIASES.get(name, name)
    entry = TOOLS.get(name)
    if entry is None:
        result = reply("Unknown tool " + str(name)[:40] + ".", ok=False)
    else:
        try:
            result = entry["fn"](ctx, **normalise_args(entry["fn"], args))
        except Exception:
            log.exception("Tool %s failed", name)
            result = reply("The tool failed.", ok=False)
    record(ctx, name, args, result)
    result["name"] = name
    return result


def parse_calls(raw):
    calls = []
    for chunk in TOOL_RE.findall(raw)[:MAX_CALLS]:
        try:
            call = json.loads(chunk)
            calls.append((str(call["name"]), call.get("args") or {}))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return calls


def strip_calls(raw):
    return TOOL_RE.sub("", raw).strip()


def quick_calls(text):
    """Spot obvious commands so common tools work with any model."""
    low, calls = text.lower(), []
    if re.search(r"\bstopwatch\b", low):
        for name, pattern in STOPWATCH_OPS:
            if re.search(pattern, low):
                calls.append((name, {}))
                break
    elif re.search(r"\btimers?\b", low):
        if re.search(r"\b(cancel|stop|delete|clear|remove|turn off)\b", low):
            ref = "all" if re.search(r"\ball\b", low) else ""
            calls.append(("cancelTimer", {"timerId": ref}))
        elif SET_TIMER_RE.search(low) and parse_duration(low):
            calls.append(("setTimer", {"duration": low}))
        elif re.search(r"\b(pause|hold)\b", low):
            calls.append(("pauseTimer", {}))
        elif re.search(r"\b(resume|continue|unpause)\b", low):
            calls.append(("resumeTimer", {}))
        elif re.search(r"\b(show|list|active|running|which|what|how)\b", low):
            calls.append(("listTimers", {}))
    if live.TIME_RE.search(text):
        place = live.find_place(text) or ""
        wants_date = re.search(r"\b(date|day)\b", low)
        if re.search(r"\btime\b", low) or not wants_date:
            calls.append(("getLocalTime", {"location": place}))
        if wants_date:
            calls.append(("getCurrentDate", {"location": place}))
    if live.WEATHER_RE.search(text):
        calls.append(("getWeather", {"location": live.find_place(text) or ""}))
    for hook in QUICK_HOOKS:
        calls += hook(text, low)
    return calls[:MAX_CALLS]


def run_quick(ctx, text):
    return [run_tool(ctx, name, args) for name, args in quick_calls(text)]


def results_text(results):
    return "\n".join(
        "- " + r["name"] + (" (failed)" if not r["ok"] else "")
        + ": " + r["text"]
        for r in results
    )


def catalogue_list():
    return [
        {"name": n, "signature": t["signature"], "doc": t["doc"],
         "group": t["group"]}
        for n, t in TOOLS.items()
    ]


def tool_block(done):
    core = [
        "- " + t["signature"] + ": " + t["doc"]
        for t in TOOLS.values() if t["group"] == "core"
    ]
    groups = {}
    for t in TOOLS.values():
        if t["group"] != "core":
            groups.setdefault(t["group"], []).append(t["signature"])
    block = (
        TOOL_RULES + "\n".join(core) + "\nMore tools by area:\n"
        + "\n".join(f"- {g}: " + "; ".join(s) for g, s in groups.items())
    )
    if done:
        block += (
            "\nThese tools already ran for the latest user message. Use "
            "their results in your answer and do not call them again:\n"
            + results_text(done)
        )
    return block


def snapshot(ctx):
    """Timers, stopwatch and assistant lists for the page widgets."""
    now = time.time()
    timers = [
        dict(t, remaining=time_left(t, now), paused=t["status"] == "paused")
        for t in active_timers(ctx)
    ]
    row = stopwatch_row(ctx)
    snap = {
        "server_time": now,
        "timers": timers,
        "stopwatch": stopwatch_data(row, now) if row else None,
    }
    for hook in SNAPSHOT_HOOKS:
        snap.update(hook(ctx))
    return snap


def whole(value, default, low, high):
    try:
        return max(low, min(int(float(value)), high))
    except (TypeError, ValueError):
        return default


def one(rows, noun, ref):
    """Return (row, None) for exactly one match, else (None, error reply)."""
    if len(rows) == 1:
        return rows[0], None
    if rows:
        return None, reply(f"Several {noun}s match {ref!r}. Say which one.",
                           ok=False)
    return None, reply(f"No {noun} matches {ref!r}.", ok=False)


def need(ref, noun):
    """Deleting needs an explicit name or number, never a bare request."""
    if not str(ref).strip():
        return reply(f"Say which {noun} to delete (name or number).",
                     ok=False)
    return None


def confirmed(value):
    return str(value).strip().lower() in ("true", "yes", "1", "confirm")
