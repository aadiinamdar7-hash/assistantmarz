"""Live data for MARZ: the user's local time and current weather.

Weather comes from Open-Meteo (free, no API key, non-commercial use).
"""
import logging
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

log = logging.getLogger("marz")

HTTPS = "https" + "://"
GEO_URL = HTTPS + "geocoding-api" + ".open-meteo.com/v1/search"
FORECAST_URL = HTTPS + "api" + ".open-meteo.com/v1/forecast"
CACHE_SECONDS = 600
_cache = {}

WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 56: "freezing drizzle", 57: "heavy freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain",
    67: "heavy freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
    77: "snow grains", 80: "light rain showers", 81: "rain showers",
    82: "violent rain showers", 85: "light snow showers",
    86: "heavy snow showers", 95: "thunderstorm",
    96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}
WEATHER_RE = re.compile(
    r"\b(weather|forecast|raining|snowing|umbrella|"
    r"how (?:hot|cold|warm) is it|like outside)\b",
    re.I,
)
TIME_RE = re.compile(
    r"\b(what(?:'s|\u2019s| is) the (?:current )?(?:time|date|day)|"
    r"what time|current time|time (?:is it|now)|what day is it|"
    r"today(?:'s|\u2019s) date)\b",
    re.I,
)
PLACE_RE = re.compile(
    r"\b(?:in|for|at|near|around)\s+([a-z][a-z .'-]{1,40}?)\s*"
    r"(?:[?.,!]|$|\b(?:today|tonight|tomorrow|now|right now|"
    r"at the moment|currently|this)\b)",
    re.I,
)
BAD_START = ("the ", "a ", "an ", "my ", "this ", "that ", "next ", "last ")
BAD_WORDS = frozenset(
    "today tonight tomorrow now here there me you us general fact case "
    "order time monday tuesday wednesday thursday friday saturday sunday "
    "weekend".split()
)


def cached(key, loader):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = loader()
    if len(_cache) >= 300:
        _cache.clear()
    _cache[key] = (time.time(), value)
    return value


def local_now(tz_name):
    """Return (current datetime, timezone label) for a timezone name."""
    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError, OSError):
        return datetime.now(timezone.utc), "UTC"
    return datetime.now(zone), tz_name


def format_time(moment, locale):
    if locale.startswith("en-US"):
        clock = moment.strftime("%I:%M %p").lstrip("0")
    else:
        clock = moment.strftime("%H:%M")
    return moment.strftime("%A %d %B %Y") + ", " + clock


def find_place(text):
    """Return a place name mentioned in the text, or None."""
    for match in PLACE_RE.finditer(text):
        place = match.group(1).strip(" .'-")
        low = place.lower()
        if len(place) >= 2 and low not in BAD_WORDS:
            if not low.startswith(BAD_START):
                return place
    return None


def lookup_place(name):
    resp = requests.get(
        GEO_URL,
        params={"name": name, "count": 1, "language": "en", "format": "json"},
        timeout=6,
    )
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if not results:
        return None
    hit = results[0]
    label = ", ".join(
        str(hit[k]) for k in ("name", "admin1", "country") if hit.get(k)
    )
    return {
        "name": label,
        "lat": hit["latitude"],
        "lon": hit["longitude"],
        "tz": hit.get("timezone", ""),
    }


def geocode(name):
    """Return a spot dict for a place name, or None if it is not found."""
    try:
        return cached(("geo", name.lower()), lambda: lookup_place(name))
    except (requests.RequestException, KeyError, TypeError, ValueError) as err:
        log.warning("Geocoding failed: %s", err)
        return None


def fetch_forecast(spot, imperial):
    params = {
        "latitude": spot["lat"],
        "longitude": spot["lon"],
        "current": (
            "temperature_2m,apparent_temperature,relative_humidity_2m,"
            "precipitation,weather_code,wind_speed_10m"
        ),
        "daily": (
            "weather_code,temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max"
        ),
        "forecast_days": 2,
        "timezone": "auto",
    }
    if imperial:
        params["temperature_unit"] = "fahrenheit"
        params["wind_speed_unit"] = "mph"
    resp = requests.get(FORECAST_URL, params=params, timeout=8)
    resp.raise_for_status()
    return resp.json()


def summarise(name, data):
    cur, units = data["current"], data["current_units"]
    deg, wind, rain = (
        units["temperature_2m"],
        units["wind_speed_10m"],
        units["precipitation"],
    )
    text = (
        f"Current weather in {name} (observed {cur['time']} local time): "
        f"{WMO.get(cur['weather_code'], 'unknown conditions')}, "
        f"{cur['temperature_2m']}{deg}, feels like "
        f"{cur['apparent_temperature']}{deg}, humidity "
        f"{cur['relative_humidity_2m']}%, wind {cur['wind_speed_10m']} "
        f"{wind}, precipitation {cur['precipitation']} {rain}."
    )
    daily = data.get("daily", {})
    for i, label in enumerate(("Today", "Tomorrow")):
        try:
            text += (
                f" {label}: {WMO.get(daily['weather_code'][i], 'unknown')}, "
                f"high {daily['temperature_2m_max'][i]}{deg}, "
                f"low {daily['temperature_2m_min'][i]}{deg}, "
                f"rain chance {daily['precipitation_probability_max'][i]}%."
            )
        except (KeyError, IndexError, TypeError):
            break
    return text


def weather_line(spot, imperial):
    key = ("wx", round(spot["lat"], 2), round(spot["lon"], 2), imperial)
    try:
        data = cached(key, lambda: fetch_forecast(spot, imperial))
        return summarise(spot["name"], data)
    except (requests.RequestException, KeyError, TypeError, ValueError) as err:
        log.warning("Weather lookup failed: %s", err)
        return (
            "Live weather could not be fetched right now. Tell the user "
            "that honestly and do not guess."
        )


def live_context(text, tz="", locale="", coords=None, city=""):
    """Return lines of live data to give the model for this message."""
    now, label = local_now(tz)
    lines = [
        "The user's current local date and time: "
        + format_time(now, locale) + " (" + label + ")."
    ]
    wants_weather = bool(WEATHER_RE.search(text))
    wants_time = bool(TIME_RE.search(text))
    if not (wants_weather or wants_time):
        return lines

    named = find_place(text)
    spot = geocode(named) if named else None
    named_ok = spot is not None
    if named and not named_ok:
        lines.append("The place named " + named + " could not be found.")
    if spot is None and coords:
        spot = {
            "name": "the user's location",
            "lat": coords[0],
            "lon": coords[1],
            "tz": "",
        }
    if spot is None and city:
        spot = geocode(city)

    if wants_time and named_ok and spot["tz"]:
        there, zone = local_now(spot["tz"])
        lines.append(
            "Local date and time in " + spot["name"] + ": "
            + format_time(there, locale) + " (" + zone + ")."
        )
    if wants_weather:
        if spot is None:
            lines.append(
                "The user's location is unknown, so ask which city they "
                "mean before giving weather."
            )
        else:
            lines.append(weather_line(spot, locale.startswith("en-US")))
    return lines


def time_line(tz="", locale=""):
    """One line giving the user's current local date and time."""
    now, label = local_now(tz)
    return (
        "The user's current local date and time: "
        + format_time(now, locale) + " (" + label + ")."
    )
