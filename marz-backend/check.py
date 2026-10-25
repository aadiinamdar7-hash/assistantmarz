"""Smoke test for the MARZ backend. Run with: python check.py"""
# flake8: noqa: E501
import io
import os
import time
import sys
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "check.db")
os.environ["OPENROUTER_API_KEY"] = "test-key"

import app as marz  # noqa: E402
import live  # noqa: E402

FAILED = []
HEAD = {"X-User-ID": "check-user-0123456789"}
IMAGE = "data:image/jpeg;base64,AAAA"
FACT = "User studies GCSE Computer Science"


def check(name, ok):
    print(("PASS  " if ok else "FAIL  ") + name)
    if not ok:
        FAILED.append(name)


SEEN = {}


def fake_model(messages):
    SEEN["system"] = messages[0]["content"]
    SEEN["user"] = messages[-1]["content"]
    return "Hello! [[MEMORY: " + FACT + "]]"


marz.call_openrouter = fake_model
client = marz.app.test_client()


def post_chat(**body):
    return client.post("/api/chat", json=body, headers=HEAD)


def upload(size, mime="image/png"):
    picture = (io.BytesIO(b"x" * size), "a.png", mime)
    return client.post(
        "/api/chat",
        data={"message": "what is this", "image": picture},
        headers=HEAD,
        content_type="multipart/form-data",
    )


def get_json(path):
    return client.get(path, headers=HEAD).json


health = client.get("/api/health")
check("health endpoint", health.status_code == 200 and health.json["ok"])
check("missing user id rejected", client.get("/api/conversations").status_code == 401)

first = post_chat(message="I am studying GCSE Computer Science")
cid = first.json.get("conversation_id")
check("chat returns a clean reply", first.status_code == 200 and "[[" not in first.json["reply"])
check("memory marker stored", first.json["memories"] == [FACT])
check("conversation listed", len(get_json("/api/conversations")) == 1)
check("history has 2 messages", len(get_json("/api/conversations/%d" % cid)) == 2)
check("long-term memory saved", [m["content"] for m in get_json("/api/r/memories")] == [FACT])

SAMPLES = (
    ("notes", {"title": "t", "body": "b"}),
    ("projects", {"name": "p", "description": "d"}),
    ("saved", {"content": "c"}),
)
for res, body in SAMPLES:
    client.post("/api/r/" + res, json=body, headers=HEAD)
    rows = get_json("/api/r/" + res)
    check(res + " create and list", len(rows) == 1)
    done = client.delete("/api/r/%s/%d" % (res, rows[0]["id"]), headers=HEAD)
    check(res + " delete", done.status_code == 200 and get_json("/api/r/" + res) == [])

saved = client.put("/api/profile", json={"name": "Sam", "bio": "Student"}, headers=HEAD)
check("profile saved", saved.json["name"] == "Sam")
prefs = client.put("/api/settings", json={"theme": "dark"}, headers=HEAD)
check("settings saved", prefs.json["theme"] == "dark")

check("JSON image accepted", post_chat(message="hi", image=IMAGE).status_code == 200)
check("bad image rejected", post_chat(message="hi", image="data:text/html;base64,AAAA").status_code == 400)
check("file upload accepted", upload(10).status_code == 200)
check("oversized upload rejected", upload(4 * 1024 * 1024 + 1).status_code == 400)
check("wrong upload type rejected", upload(10, "text/plain").status_code == 400)

client.delete("/api/conversations/%d" % cid, headers=HEAD)
check("conversation deleted", client.get("/api/conversations/%d" % cid, headers=HEAD).status_code == 404)
check("unknown resource 404", client.get("/api/r/nope", headers=HEAD).status_code == 404)

# ---------- live time and weather ----------
FORECAST = {
    "current_units": {
        "temperature_2m": "C",
        "wind_speed_10m": "km/h",
        "precipitation": "mm",
    },
    "current": {
        "time": "2026-10-19T14:15",
        "temperature_2m": 12.5,
        "apparent_temperature": 10.1,
        "relative_humidity_2m": 80,
        "precipitation": 0.2,
        "weather_code": 61,
        "wind_speed_10m": 14.0,
    },
    "daily": {
        "weather_code": [61, 3],
        "temperature_2m_max": [14, 13],
        "temperature_2m_min": [8, 7],
        "precipitation_probability_max": [70, 20],
        "wind_speed_10m_max": [20, 25],
        "time": ["2026-10-23", "2026-10-24"],
    },
}
SPOTS = {
    "tokyo": {"name": "Tokyo, Japan", "lat": 35.7, "lon": 139.7,
              "tz": "Asia/Tokyo"},
    "london": {"name": "London, England", "lat": 51.5, "lon": -0.1,
               "tz": "Europe/London"},
}


def fake_geocode(name):
    return SPOTS.get(name.lower())


def fake_forecast(spot, imperial, days=2):
    return FORECAST


def failing_forecast(spot, imperial, days=2):
    raise live.requests.RequestException("offline")


def has(lines, text):
    return any(text in line for line in lines)


live.geocode = fake_geocode
live.fetch_forecast = fake_forecast
live._cache.clear()

lines = live.live_context("hello there", tz="Europe/London")
check("local time always included", len(lines) == 1 and "Europe/London" in lines[0])
lines = live.live_context("What time is it in Tokyo?", tz="Europe/London")
check("time in another city", has(lines, "Local date and time in Tokyo"))
lines = live.live_context("What's the weather in London right now?")
check("weather for a named city", has(lines, "Current weather in London") and has(lines, "light rain"))
check("tomorrow forecast included", has(live.live_context("weather in London"), "Tomorrow: overcast"))
check("weather without a location asks", has(live.live_context("how is the weather?"), "location is unknown"))
check("weather from browser coordinates", has(live.live_context("weather?", coords=(51.5, -0.1)), "the user's location"))
check("weather from saved home city", has(live.live_context("weather today", city="London"), "Current weather in London"))
check("place finder", live.find_place("weather in New York right now?") == "New York" and live.find_place("is it raining in the garden") is None)
live.fetch_forecast = failing_forecast
lines = live.live_context("weather?", coords=(10.0, 20.0))
check("weather failure handled", has(lines, "could not be fetched"))
live.fetch_forecast = fake_forecast

city = client.put("/api/profile", json={"city": "London"}, headers=HEAD)
check("home city saved", city.json["city"] == "London")
post_chat(message="how's the weather?", timezone="Europe/London", locale="en-GB")
check("chat prompt carries live weather", "Live data" in SEEN["system"] and "Current weather in London" in SEEN["system"])
post_chat(message="hello", timezone="Asia/Tokyo")
check("chat prompt carries local time", "Asia/Tokyo" in SEEN["system"])

# ---------- tools, timers, notes, files ----------
import io as _io  # noqa: E402
import zipfile  # noqa: E402

import tools  # noqa: E402


def run(name, /, **args):
    body = {"name": name, "args": args, "timezone": "Europe/London"}
    reply = client.post("/api/tool", json=body, headers=HEAD)
    return reply.json["result"], reply.json["state"]


check("duration parsing", [tools.parse_duration(x) for x in ("30 minutes", "1 hour 30 minutes", "half an hour", "90 seconds", "5-minute timer", "gibberish")] == [1800, 5400, 1800, 90, 300, None])
result, state = run("setTimer", duration="5 minutes", label="tea")
check("setTimer", result["ok"] and len(state["timers"]) == 1 and 290 < state["timers"][0]["remaining"] <= 300)
check("bad duration refused", not run("setTimer", duration="soon")[0]["ok"])
check("listTimers", "tea" in run("listTimers")[0]["text"])
result, state = run("cancelTimer", timer_id="tea")
check("cancelTimer by label", result["ok"] and state["timers"] == [])
check("cancel with none active", not run("cancelTimer", timerId="all")[0]["ok"])
check("stopwatch needs a start", not run("pauseStopwatch")[0]["ok"])
states = [run(n)[1]["stopwatch"] for n in ("startStopwatch", "pauseStopwatch", "resumeStopwatch", "stopStopwatch", "resetStopwatch")]
check("stopwatch lifecycle", [s["status"] for s in states] == ["active", "paused", "active", "stopped", "idle"] and states[0]["running"] and not states[1]["running"] and states[4]["elapsed"] == 0)
check("getStopwatchTime", run("getStopwatchTime")[0]["ok"])
check("getLocalTime", run("getLocalTime")[0]["data"]["timezone"] == "Europe/London")
check("getLocalTime for a city", run("getLocalTime", location="Tokyo")[0]["data"]["timezone"] == "Asia/Tokyo")
check("getCurrentDate", "2026" in run("getCurrentDate")[0]["text"] or run("getCurrentDate")[0]["ok"])
weather = run("getWeather", location="London")[0]
check("getWeather data for the widget", weather["data"]["place"].startswith("London") and weather["data"]["humidity"] == 80)
check("createNote and searchNotes", run("createNote", title="Shopping", content="milk and eggs")[0]["ok"] and "milk" in run("searchNotes", query="eggs")[0]["text"])
check("saveMemory and searchMemory", run("saveMemory", content="Likes jazz")[0]["ok"] and "jazz" in run("searchMemory", query="jazz")[0]["text"])
check("search treats percent literally", "No notes" in run("searchNotes", query="100%")[0]["text"])
check("openWebsite", run("openWebsite", url="example.com")[0]["open"].startswith("https://example.com"))
check("openWebsite rejects bad schemes", not run("openWebsite", url="javascript:alert(1)")[0]["ok"])
check("unknown tool", not run("hackTheMoon")[0]["ok"])
check("tool history kept", len(client.get("/api/tools", headers=HEAD).json["history"]) >= 10)
check("quick intents", [tools.quick_calls(t)[0][0] for t in ("Set a timer for 5 minutes", "What time is it?", "Start a stopwatch", "show my timers", "cancel the timer", "What's the weather in London?")] == ["setTimer", "getLocalTime", "startStopwatch", "listTimers", "cancelTimer", "getWeather"])
check("no false timer trigger", tools.quick_calls("My timer app crashes after 5 minutes") == [])

marz.call_openrouter = fake_model
chat = post_chat(message="Set a timer for 2 minutes", timezone="Europe/London")
check("chat runs a timer tool", chat.json["tools"][0]["name"] == "setTimer" and len(chat.json["state"]["timers"]) == 1 and "setTimer" in SEEN["system"])
chat = post_chat(message="What time is it?", timezone="Europe/London")
check("chat time tool result reaches the model", "Local time in your area" in SEEN["system"])

replies = iter(['[[TOOL: {"name": "createNote", "args": {"title": "Idea", "content": "robots"}}]]', "Saved your note."])


def scripted_model(messages):
    return next(replies)


marz.call_openrouter = scripted_model
chat = post_chat(message="Jot down an idea about robots")
check("model-chosen tool call", chat.json["reply"] == "Saved your note." and chat.json["tools"][0]["name"] == "createNote")
check("note saved by the model", "robots" in run("searchNotes", query="robots")[0]["text"])
marz.call_openrouter = fake_model

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 20
DOCX = _io.BytesIO()
with zipfile.ZipFile(DOCX, "w") as archive:
    archive.writestr("word/document.xml", "\x3cw:p\x3e\x3cw:t\x3eHello docx\x3c/w:t\x3e\x3c/w:p\x3e")


def send_files(*files, message="look"):
    data = {"message": message, "files": [(_io.BytesIO(b), n, "application/octet-stream") for n, b in files]}
    return client.post("/api/chat", data=data, headers=HEAD, content_type="multipart/form-data")


reply = send_files(("notes.txt", b"Plain text file"), ("shot1.png", PNG), ("shot2.png", PNG), ("report.docx", DOCX.getvalue()))
parts = SEEN["user"]
check("multiple files and images in one message", reply.status_code == 200 and len(parts) == 3 and "--- file: notes.txt ---" in parts[0]["text"] and "Hello docx" in parts[0]["text"])
check("image only message", send_files(("a.png", PNG), message="").status_code == 200 and "Describe" in SEEN["user"][0]["text"])
reply = send_files(("virus.exe", b"MZ\x90\x00" + b"x" * 20), ("fake.pdf", b"%PDF-1.4 broken"))
check("unsafe and broken files are reported", reply.status_code == 200 and len(reply.json["warnings"]) == 2)
reply = send_files(("n.txt", b"hi"), message="")
check("file only message", reply.status_code == 200 and "Summarise" in SEEN["user"])

# ---------- assistant: times, reminders, alarms, tasks, library ----------
from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import assistant  # noqa: E402

NOW = datetime(2026, 10, 23, 14, 0, tzinfo=ZoneInfo("Europe/London"))  # a Friday
USER = "check-user-0123456789"


def when(text):
    due = assistant.parse_when(text, NOW)
    return due.strftime("%a %d %H:%M") if due else None


check("time parsing", [when(t) for t in ("tomorrow at 6 pm", "in 45 minutes", "7 am", "friday at 5pm", "friday at 1pm", "tomorrow", "noon", "in 2 days", "tomorrow afternoon", "whenever")] == ["Sat 24 18:00", "Fri 23 14:45", "Sat 24 07:00", "Fri 23 17:00", "Fri 30 13:00", "Sat 24 09:00", "Sat 24 12:00", "Sun 25 14:00", "Sat 24 15:00", None])

result, state = run("createReminder", title="revise English", when="tomorrow at 6 pm")
check("createReminder", result["ok"] and len(state["reminders"]) == 1 and "18:00" in result["text"])
check("reminder accepts a time alias", run("createReminder", title="call mum", time="in 20 minutes")[0]["ok"])
check("reminder needs a readable time", not run("createReminder", title="x", when="whenever")[0]["ok"])
check("listReminders", "revise English" in run("listReminders")[0]["text"])
early = client.post("/api/fire/reminder/%d" % state["reminders"][0]["id"], headers=HEAD).json
check("early fire is ignored", len(early["reminders"]) == 2)
result, state = run("deleteReminder", id=str(state["reminders"][0]["id"]))
check("deleteReminder by id", result["ok"] and len(state["reminders"]) == 1)
check("setAlarm", run("setAlarm", when="7 AM", label="Wake up")[0]["ok"] and "Wake up" in run("listAlarms")[0]["text"])
result, state = run("cancelAlarm", alarmId="all")
check("cancelAlarm", result["ok"] and state["alarms"] == [])

run("createTask", title="Finish MARZ", priority="high", due="tomorrow at 5 pm", project="MARZ")
run("createTask", title="Tidy desk", priority="low")
rank = run("prioritiseTasks")[0]["text"]
check("tasks ranked by due date and priority", rank.index("Finish MARZ") < rank.index("Tidy desk"))
with marz.app.app_context():
    marz.execute("INSERT INTO tasks(user_id, title, priority, due_at, created) VALUES(?, 'Hand in essay', 1, ?, ?)", (USER, time.time() - 3600, time.time()))
    marz.execute("INSERT INTO projects(user_id, name, description, created) VALUES(?, 'Robotics', '', ?)", (USER, time.time() - 5 * 86400))
check("overdue task listed", "Hand in essay" in run("listTasks", scope="overdue")[0]["text"])
check("overdue task ranks first", run("prioritiseTasks")[0]["text"].index("Hand in essay") < rank.index("Finish MARZ") + 100 and "1. #" in run("prioritiseTasks")[0]["text"])
brief = run("getBriefing")[0]["text"]
check("briefing is proactive", "overdue" in brief and "Robotics for 5 days" in brief)
check("listProjects", "Robotics" in run("listProjects")[0]["text"])
check("completeTask", run("completeTask", task="Hand in essay")[0]["ok"] and "Hand in essay" in run("listTasks", scope="done")[0]["text"])
check("deleteTask", run("deleteTask", task="Tidy desk")[0]["ok"] and "Tidy desk" not in run("listTasks")[0]["text"])
check("task state for the panel", [t["title"] for t in run("listTasks")[1]["tasks"]] == ["Finish MARZ"])

run("cancelTimer", timerId="all")
run("setTimer", duration="5 minutes", label="tea")
paused = run("pauseTimer")[1]["timers"][0]
check("pauseTimer", paused["paused"] and 290 < paused["remaining"] <= 300)
resumed = run("resumeTimer")[1]["timers"][0]
check("resumeTimer", not resumed["paused"] and 290 < resumed["remaining"] <= 300)
run("cancelTimer", timerId="all")
run("startStopwatch")
run("lapStopwatch")
laps = run("lapStopwatch")[1]["stopwatch"]["laps"]
check("stopwatch laps", len(laps) == 2 and run("resetStopwatch")[1]["stopwatch"]["laps"] == [])
check("tool aliases", run("getDate")[0]["ok"] and run("getTimers")[0]["ok"])
check("new quick intents", [tools.quick_calls(t)[0][0] for t in ("Wake me at 7 AM", "What's overdue?", "What should I do today?", "show my reminders", "show my alarms", "pause the timer", "my projects")] == ["setAlarm", "listTasks", "getBriefing", "listReminders", "listAlarms", "pauseTimer", "listProjects"])


def make_pdf(text):
    lt, gt = "\x3c\x3c", "\x3e\x3e"
    stream = "BT /F1 18 Tf 20 100 Td (" + text + ") Tj ET"
    objs = [
        lt + "/Type/Catalog/Pages 2 0 R" + gt,
        lt + "/Type/Pages/Kids[3 0 R]/Count 1" + gt,
        lt + "/Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R/Resources" + lt + "/Font" + lt + "/F1 5 0 R" + gt + gt + gt,
        lt + "/Length " + str(len(stream)) + gt + "\nstream\n" + stream + "\nendstream",
        lt + "/Type/Font/Subtype/Type1/BaseFont/Helvetica" + gt,
    ]
    out, offsets = "%PDF-1.4\n", []
    for number, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += "%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = "xref\n0 6\n0000000000 65535 f \n" + "".join("%010d 00000 n \n" % o for o in offsets)
    tail = "trailer\n" + lt + "/Root 1 0 R/Size 6" + gt + "\nstartxref\n%d\n%%%%EOF\n" % len(out)
    return (out + xref + tail).encode("latin-1")


send_files(("revision notes.txt", b"Python is great.\nRevise Python loops."), ("plan.txt", b"Python basics.\nRevise loops."), message="save these")
listing = client.get("/api/files", headers=HEAD).json["files"]
check("uploads are saved to the library", {"plan.txt", "revision notes.txt"} <= {f["name"] for f in listing})
check("openFile by partial name", "Revise Python loops" in run("openFile", name="revision")[0]["text"])
check("searchFiles across documents", all(n in run("searchFiles", query="python")[0]["text"] for n in ("revision notes.txt", "plan.txt")))
check("compareFiles", "Similarity" in run("compareFiles", first="revision", second="plan")[0]["text"])
check("status block lists saved files", "revision notes.txt" in SEEN["system"])
send_files(("real.pdf", make_pdf("Hello PDF")), message="read it")
check("real PDF text is extracted", "Hello PDF" in SEEN["user"])
view = client.get("/api/files/%d" % listing[0]["id"], headers=HEAD)
check("view saved file", view.status_code == 200 and view.json["text"])
before = len(client.get("/api/files", headers=HEAD).json["files"])
client.delete("/api/files/%d" % listing[0]["id"], headers=HEAD)
check("delete saved file", len(client.get("/api/files", headers=HEAD).json["files"]) == before - 1)
upload_only = client.post("/api/files", data={"files": (io.BytesIO(b"Library note"), "lib.txt", "text/plain")}, headers=HEAD, content_type="multipart/form-data")
check("upload straight to the library", upload_only.json["saved"] == ["lib.txt"])

check("Netlify frontend is always allowed", marz.ORIGINS.count("https://marzatron.netlify.app") == 1)

# ---------- JARVIS layer ----------
from datetime import datetime as _dt  # noqa: E402

SPEC = """getCurrentTime getCurrentDate getTimezone getDayOfWeek getCurrentMonth getCurrentYear generateDailyBriefing generateEveningBriefing getWeather getForecast getTemperature getHumidity getWindSpeed getWeatherAlerts recommendClothes startListening stopListening toggleVoiceMode enableWakeWord disableWakeWord speakText stopSpeaking changeVoice changeSpeechRate changeSpeechPitch interruptSpeech saveMemory searchMemory deleteMemory updateMemory rememberGoal rememberPreference rememberProject rememberPerson rememberTask rememberStudyTopic generateDailyPlan generateStudyPlan generateFocusPlan generateSchedule generateRoutine suggestNextTask suggestBreak suggestPriority createTask editTask deleteTask completeTask listTasks listDueTasks listOverdueTasks prioritizeTasks estimateTaskTime createProject editProject deleteProject trackProject showProjectProgress showProjectDashboard generateProjectPlan createReminder updateReminder deleteReminder showReminders showUpcomingReminders setAlarm cancelAlarm listAlarms snoozeAlarm enableRepeatingAlarm disableAlarm setTimer pauseTimer resumeTimer cancelTimer listTimers labelTimer startStopwatch pauseStopwatch resetStopwatch resumeStopwatch lapStopwatch createNote editNote deleteNote showNotes searchNotes pinNote openFile readFile searchFile deleteFile renameFile summarizeFile compareFiles analyzeDocument extractText analyzeScreenshot compareScreenshots extractTextFromScreenshot explainScreenshot findBugInScreenshot describeImage extractImageText analyzeDiagram analyzeChart analyzeGraph generateCode fixCode reviewCode explainCode refactorCode findBug generateProjectStructure createFlashcards generateQuiz generateRevisionPlan summarizeTopic generateExamQuestions generateModelAnswers trackStudyTime researchTopic compareOptions summarizeResearch factCheck generateSources draftEmail composeEmail replyToEmail summarizeEmails searchEmails showCalendar createEvent editEvent deleteEvent showTodaysSchedule showWeeklySchedule openWebsite bookmarkWebsite searchBookmarks openDashboard playMusic pauseMusic stopMusic setVolume nextTrack previousTrack enableGestures disableGestures showGestures saveGestureMapping removeGestureMapping listGestureMappings checkDeadlines checkOverdueTasks checkUpcomingEvents checkReminders suggestWork suggestRevision suggestBreak suggestHydration suggestNextProjectStep""".split()
missing = [n for n in SPEC if n not in tools.TOOLS and n not in tools.TOOL_ALIASES]
check("every function in the spec exists (%d of them)" % len(SPEC), not missing)
if missing:
    print("   missing:", missing)
check("apostrophe alias", tools.TOOL_ALIASES["showToday'sSchedule"] == "showTodaysSchedule")
text_of = lambda *a, **k: run(*a, **k)[0]["text"]  # noqa: E731

check("time fields", run("getDayOfWeek")[0]["data"]["value"] in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday") and "Europe/London" in text_of("getTimezone") and run("getCurrentYear")[0]["data"]["value"].isdigit() and run("getCurrentMonth")[0]["ok"])
fc = run("getForecast", days="2")[0]
check("getForecast", "Forecast for London" in fc["text"] and len(fc["data"]["days"]) == 2)
check("temperature, humidity and wind", "12.5" in text_of("getTemperature") and "80%" in text_of("getHumidity") and "14.0" in text_of("getWindSpeed"))
severe = {"daily": {"weather_code": [95], "temperature_2m_max": [32], "temperature_2m_min": [-3], "wind_speed_10m_max": [60]}}
check("weather alerts are derived", len(assistant.derive_alerts(severe, False)) == 4 and "not official" in text_of("getWeatherAlerts"))
check("recommendClothes", "jacket" in text_of("recommendClothes") and "waterproof" in text_of("recommendClothes"))

for fn, args in (("rememberGoal", {"goal": "Get a grade 9 in Computer Science"}), ("rememberPreference", {"preference": "Prefers short answers"}), ("rememberPerson", {"name": "Sam", "details": "my brother"}), ("rememberStudyTopic", {"topic": "binary", "subject": "Computer Science"}), ("rememberProject", {"name": "Garden", "description": "tidy up"}), ("rememberTask", {"title": "Water plants"})):
    run(fn, **args)
kinds = {m["kind"] for m in get_json("/api/r/memories")}
check("remember goal, preference, person, study topic, project", {"goal", "preference", "person", "study", "project"} <= kinds)
check("updateMemory", run("updateMemory", memory="Prefers short", content="Prefers concise answers")[0]["ok"])
check("deleteMemory", run("deleteMemory", memory="concise")[0]["ok"] and "concise" not in str(get_json("/api/r/memories")))
check("preferences", run("setPreference", key="wake_time", value="06:30")[0]["ok"] and "wake_time=06:30" in text_of("getPreferences") and not run("setPreference", key="Bad Key!", value="x")[0]["ok"])
check("habits", text_of("getHabits").startswith("Most active"))

run("createTask", title="Write essay", priority="medium")
check("editTask", run("editTask", task="Write essay", priority="high", due="friday at 5 pm")[0]["ok"])
check("listDueTasks", "Write essay" in text_of("listDueTasks", days="14"))
check("estimateTaskTime", run("estimateTaskTime", task="Write essay")[0]["data"]["minutes"] == 120)
check("suggestNextTask", "Next up" in text_of("suggestNextTask"))

run("createProject", name="Garden club", description="plan")
check("duplicate project refused", not run("createProject", name="garden club")[0]["ok"])
run("editProject", project="Garden club", progress="40")
check("showProjectProgress", "Garden club: 40%" in text_of("showProjectProgress", project="Garden club"))
check("trackProject logs a note", run("trackProject", project="Garden club", note="Bought seeds")[0]["ok"] and "Bought seeds" in text_of("searchNotes", query="seeds"))
dash = run("showProjectDashboard")[0]
check("showProjectDashboard", "Garden club" in dash["text"] and dash["ui"][0]["arg"] == "system")
check("deleteProject needs confirmation", not run("deleteProject", project="Garden club")[0]["ok"] and "Garden club" in text_of("showProjectProgress"))
check("deleteProject when confirmed", run("deleteProject", project="Garden club", confirm="true")[0]["ok"])
check("deleting needs a name", not any(run(n)[0]["ok"] for n in ("deleteProject", "deleteNote", "deleteMemory", "deleteEvent", "deleteTask")))

run("createReminder", title="call nan", when="in 20 minutes")
check("updateReminder", run("updateReminder", reminderId="call nan", when="tomorrow at 8 am")[0]["ok"] and "call nan" not in text_of("showUpcomingReminders", hours="1"))
run("setAlarm", when="7 am", label="Gym")
check("enableRepeatingAlarm", run("enableRepeatingAlarm", alarmId="Gym", repeat="daily")[0]["ok"] and not run("enableRepeatingAlarm", alarmId="Gym", repeat="sometimes")[0]["ok"])
check("disableAlarm and enableAlarm", run("disableAlarm", alarmId="Gym")[1]["alarms"] == [] and len(run("enableAlarm", alarmId="Gym")[1]["alarms"]) == 1)
alarm = run("snoozeAlarm", alarmId="Gym", minutes="15")[1]["alarms"][0]
check("snoozeAlarm", 14 * 60 < alarm["due_at"] - time.time() <= 15 * 60 + 5)
with marz.app.app_context():
    marz.execute("UPDATE alarms SET due_at=? WHERE id=?", (time.time() - 5, alarm["id"]))
after = client.post("/api/fire/alarm/%d" % alarm["id"], headers=HEAD).json
check("a repeating alarm reschedules itself", [a for a in after["alarms"] if a["id"] == alarm["id"]][0]["due_at"] > time.time())
once = run("setAlarm", when="8 am", label="Once")[1]["alarms"]
once_id = [a for a in once if a["title"] == "Once"][0]["id"]
with marz.app.app_context():
    marz.execute("UPDATE alarms SET due_at=? WHERE id=?", (time.time() - 5, once_id))
check("a one-off alarm is done after it fires", all(a["id"] != once_id for a in client.post("/api/fire/alarm/%d" % once_id, headers=HEAD).json["alarms"]))

run("cancelTimer", timerId="all")
run("setTimer", duration="3 minutes")
run("labelTimer", label="pasta")
check("labelTimer", "pasta" in text_of("listTimers"))
run("cancelTimer", timerId="all")

run("createNote", title="Zebra facts", content="stripes")
check("pinNote and showNotes", run("pinNote", note="Zebra")[0]["ok"] and "(pinned)" in text_of("showNotes") and text_of("showNotes").index("Zebra") < text_of("showNotes").index("Shopping"))
check("editNote and deleteNote", run("editNote", note="Zebra", content="black and white")[0]["ok"] and run("deleteNote", note="Zebra")[0]["ok"])

check("renameFile", run("renameFile", name="revision notes.txt", newName="revision v2.txt")[0]["ok"] and "revision v2" in text_of("listFiles"))
check("summarizeFile and analyzeDocument", "Summarise" in text_of("summarizeFile", name="revision v2") and "Analyse" in text_of("analyzeDocument", name="revision v2", question="tone"))
check("deleteFile needs confirmation", not run("deleteFile", name="revision v2")[0]["ok"] and run("deleteFile", name="revision v2", confirm="yes")[0]["ok"])

check("createEvent", run("createEvent", title="Dentist", when="tomorrow at 3 pm", duration="30 minutes")[0]["ok"])
check("showCalendar and schedules", "Dentist" in text_of("showCalendar", range="week") and "Dentist" in text_of("showWeeklySchedule") and run("showTodaysSchedule")[0]["ok"])
check("events reach the page state", [e["title"] for e in run("listTimers")[1]["events"]] == ["Dentist"])
check("editEvent and deleteEvent", run("editEvent", event="Dentist", when="tomorrow at 4 pm")[0]["ok"] and run("deleteEvent", event="Dentist")[0]["ok"])
check("bookmarks", run("bookmarkWebsite", url="example.com", title="Example")[0]["ok"] and "https://example.com" in text_of("searchBookmarks", query="exam"))
check("trackStudyTime", "45 minutes" in text_of("trackStudyTime", subject="maths", minutes="45"))


def fake_wiki(query, limit=3):
    return [{"title": "Python", "fullurl": "https://en.example.org/Python", "extract": "A language.", "index": 1}]


assistant.wiki_search = fake_wiki
check("research tools use real article links", all("https://en.example.org/Python" in text_of(n, **a) for n, a in (("researchTopic", {"topic": "python"}), ("generateSources", {"topic": "python"}), ("factCheck", {"claim": "python is a language"}), ("compareOptions", {"first": "python", "second": "java"}))))
check("email is honest", not run("summarizeEmails")[0]["ok"] and not run("searchEmails", query="x")[0]["ok"] and "subject" in text_of("draftEmail", to="Sam", purpose="thanks"))
check("skills carry a method and the user's data", "runnable" in text_of("generateCode", request="sort a list") and "Open tasks" in text_of("generateDailyPlan") and "Known goals" in text_of("generateStudyPlan"))
check("vision tools need an image", not run("analyzeScreenshot")[0]["ok"])
daily, evening = run("generateDailyBriefing")[0], run("generateEveningBriefing")[0]
check("briefings", "Weather in London" in daily["text"] and "Tasks completed today" in evening["text"])
with marz.app.app_context():
    marz.execute("INSERT INTO tasks(user_id, title, priority, due_at, created) VALUES(?, 'Hand in form', 1, ?, ?)", (USER, time.time() - 600, time.time()))
nudges = client.get("/api/proactive", headers=HEAD).json["items"]
check("proactive nudges", any("Overdue" in n["text"] for n in nudges) and len(nudges) <= 3)
check("proactive check tools", "Hand in form" in text_of("checkOverdueTasks") and run("suggestWork")[0]["ok"] and run("suggestBreak")[0]["ok"] and run("suggestHydration")[0]["ok"])

check("default gestures", len(run("listGestureMappings")[0]["data"]["mapping"]) == 12)
run("saveGestureMapping", gesture="thumbs up", command="view:tasks")
check("gesture mapping saved", client.get("/api/gestures", headers=HEAD).json["mapping"]["THUMBS_UP"] == "view:tasks")
check("gesture mapping validated", not run("saveGestureMapping", gesture="WAVE", command="view:tasks")[0]["ok"] and not run("saveGestureMapping", gesture="FIST", command="rm -rf")[0]["ok"] and run("saveGestureMapping", gesture="FIST", command="ask:what is next")[0]["ok"])
check("removeGestureMapping restores the default", "confirm" in text_of("removeGestureMapping", gesture="THUMBS_UP"))
check("page commands", run("setVolume", level="40")[0]["ui"] == [{"cmd": "music.volume", "arg": "40"}] and run("speakText", text="hello")[0]["ui"][0]["arg"] == "hello" and run("openDashboard", name="weather dashboard")[0]["ui"] == [{"cmd": "view", "arg": "weather"}] and not run("openDashboard", name="nonsense")[0]["ok"])

run("setJarvisMode", enabled="true")
post_chat(message="hello", timezone="Europe/London")
check("JARVIS mode", "JARVIS mode is on" in SEEN["system"] and client.get("/api/system", headers=HEAD).json["jarvis"] is True)
run("setJarvisMode", enabled="false")
system = client.get("/api/system", headers=HEAD).json
check("system status", system["configured"] and system["counts"]["tasks"] >= 1 and system["briefing"] is not None and system["memory_kinds"])
cases = (("Give me my daily briefing", "generateDailyBriefing"), ("evening briefing please", "generateEveningBriefing"), ("snooze", "snoozeAlarm"), ("What's on today?", "showTodaysSchedule"), ("show my weekly schedule", "showWeeklySchedule"), ("what should I wear today", "recommendClothes"), ("weather alerts for London", "getWeatherAlerts"), ("what's the forecast for tomorrow in London", "getForecast"))
check("more quick intents", all(any(c[0] == want for c in tools.quick_calls(text)) for text, want in cases))
prompt = tools.tool_block([])
check("prompt stays compact (%d characters)" % len(prompt), len(prompt) < 22000 and "Coding:" in prompt and "Gestures:" in prompt)

broken = []
for tool_name in sorted(tools.TOOLS):
    result, _ = run(tool_name)
    if "text" not in result or result["text"] == "The tool failed.":
        broken.append(tool_name)
check("every tool answers safely with no arguments (%d tools)" % len(tools.TOOLS), not broken)
if broken:
    print("   broken:", broken)

if FAILED:
    print("\n%d check(s) failed." % len(FAILED))
    sys.exit(1)
print("\nALL CHECKS PASSED")
