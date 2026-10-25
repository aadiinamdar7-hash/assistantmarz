# MARZ backend

Flask + SQLite + OpenRouter. Python 3.11.

## Run locally
1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and set OPENROUTER_API_KEY.
3. `python check.py` runs a self-test with a fake model (no key or network needed).
4. `python app.py` starts the API on port 5000.

## Deploy on Render
Push this folder as its own repo, create a Web Service from render.yaml (or set
build `pip install -r requirements.txt`, start `gunicorn app:app --workers 1 --threads 4 --timeout 90`),
then set OPENROUTER_API_KEY and ALLOWED_ORIGINS (your Netlify URL, no trailing slash).

## Time and weather
Every chat message carries the user's local date and time. When someone asks about the
weather, or the time in another city, MARZ looks it up live (Open-Meteo: free, no key,
non-commercial use) and the model answers from that data. The place comes from the message
("weather in Paris"), then the browser location if the user allowed it, then the Home city
saved in Profile.

## Good to know
- Render's free disk is wiped on restart, so SQLite data does not persist there.
- The free server sleeps when idle; the first request can take about 30 seconds.
- openrouter/auto can route to paid models. Set MARZ_MODEL to a :free model to avoid charges.

## Tools and uploads
tools.py holds the function registry (time, date, weather, timers, stopwatch, notes, memory, openWebsite).
The assistant calls them with [[TOOL: ...]] markers, and obvious commands are also detected directly, so
they work with any model. files.py reads images, PDF, TXT and DOCX uploads in memory only. New tables
(timers, tool_history) are created automatically on start-up; nothing existing is changed.

## JARVIS layer
assistant.py now also holds time, weather (forecast, alerts, clothes), memory kinds, preferences, habits,
projects, calendar, bookmarks, study log, research (Wikipedia), daily and evening briefings, proactive checks,
gesture mappings and page commands. Tools are grouped in the model prompt to keep it short; "skills" such as
generateCode return a method for the model to follow. Email reading is not connected, weather alerts are
derived from the forecast (not official warnings), and research uses Wikipedia only.
New tables: events, bookmarks, study_log, gesture_mappings, daily_briefings, assistant_preferences.
Columns are added to existing tables on start-up (laps, estimate, repeat_rule, pinned, status, progress, updated).
