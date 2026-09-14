#!/usr/bin/env python3
import datetime as datetime_module
import email.utils
import re
import zoneinfo
from typing import Any

from operation_common import (
    Operation,
    bounded_int,
    choice,
    optional_bool,
    optional_string,
    require_text,
    typed,
)

MAX_FORMAT = 200
EPOCH_PATTERN = re.compile(r"^[+-]?\d{1,19}(?:\.\d+)?$")
# strftime is passed to the C library, so only documented directives are allowed through.
SAFE_DIRECTIVES = set("aAbBcdfHIjmMpSUwWxXyYzZG%uV")


def resolve_zone(name: str) -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(name)
    except zoneinfo.ZoneInfoNotFoundError as error:
        # An empty database is a node packaging problem, not a bad argument from the caller.
        if not zoneinfo.available_timezones():
            raise ValueError("timezone conversion requires the tzdata package, which is not installed on this node") from error
        raise ValueError(f"unknown timezone: {name}") from error
    except (ValueError, KeyError) as error:
        raise ValueError(f"unknown timezone: {name}") from error


def optional_zone(arguments: dict[str, Any], name: str) -> zoneinfo.ZoneInfo | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError(f"{name} must be an IANA timezone name")
    return resolve_zone(value)


def validate_format(value: str) -> str:
    for match in re.finditer(r"%(.)", value):
        if match.group(1) not in SAFE_DIRECTIVES:
            raise ValueError(f"unsupported strftime directive: %{match.group(1)}")
    if re.search(r"%$", value):
        raise ValueError("format ends with a dangling %")
    return value


def parse_moment(arguments: dict[str, Any], name: str = "text") -> tuple[datetime_module.datetime, str]:
    """Accept ISO-8601, epoch seconds/milliseconds, RFC 2822, or an explicit strptime format."""
    text = require_text(arguments, name).strip()
    if not text:
        raise ValueError(f"{name} must not be empty")
    if len(text) > 200:
        raise ValueError(f"{name} exceeds 200 characters")

    explicit = arguments.get("inputFormat")
    if explicit is not None:
        if not isinstance(explicit, str) or len(explicit) > MAX_FORMAT:
            raise ValueError("inputFormat must be a strftime string")
        try:
            return datetime_module.datetime.strptime(text, validate_format(explicit)), "inputFormat"
        except ValueError as error:
            raise ValueError(f"{name} does not match inputFormat: {error}") from error

    if EPOCH_PATTERN.match(text):
        number = float(text)
        unit = choice(arguments, "epochUnit", {"auto", "seconds", "milliseconds"}, "auto")
        if unit == "milliseconds" or (unit == "auto" and abs(number) > 1e11):
            number /= 1000
        try:
            return datetime_module.datetime.fromtimestamp(number, datetime_module.timezone.utc), "epoch"
        except (OverflowError, OSError, ValueError) as error:
            raise ValueError(f"{name} is not a representable epoch timestamp") from error

    candidate = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        return datetime_module.datetime.fromisoformat(candidate), "iso8601"
    except ValueError:
        pass

    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"{name} is not ISO-8601, an epoch timestamp, or RFC 2822; pass inputFormat for other layouts"
        ) from error
    return parsed, "rfc2822"


def with_zone(moment: datetime_module.datetime, assumed: zoneinfo.ZoneInfo | None) -> datetime_module.datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=assumed or datetime_module.timezone.utc)
    return moment


def describe(moment: datetime_module.datetime) -> dict[str, Any]:
    iso_year, iso_week, iso_weekday = moment.isocalendar()
    return {
        "iso8601": moment.isoformat(),
        "epochSeconds": moment.timestamp(),
        "date": moment.date().isoformat(),
        "time": moment.time().isoformat(),
        "timezone": str(moment.tzinfo),
        "utcOffsetMinutes": int(moment.utcoffset().total_seconds() // 60) if moment.utcoffset() else 0,
        "year": moment.year,
        "month": moment.month,
        "day": moment.day,
        "weekday": moment.strftime("%A"),
        "isoWeek": {"year": iso_year, "week": iso_week, "weekday": iso_weekday},
        "dayOfYear": int(moment.strftime("%j")),
    }


def parse_operation(arguments: dict[str, Any]) -> dict[str, Any]:
    moment, detected = parse_moment(arguments)
    moment = with_zone(moment, optional_zone(arguments, "assumeTimezone"))
    return {"detectedFormat": detected, **describe(moment)}


def format_operation(arguments: dict[str, Any]) -> dict[str, Any]:
    moment, _detected = parse_moment(arguments)
    moment = with_zone(moment, optional_zone(arguments, "assumeTimezone"))
    target = optional_zone(arguments, "timezone")
    if target is not None:
        moment = moment.astimezone(target)
    pattern = validate_format(optional_string(arguments, "format", "%Y-%m-%d %H:%M:%S %Z", MAX_FORMAT))
    return {"text": moment.strftime(pattern), "iso8601": moment.isoformat()}


def convert_timezone(arguments: dict[str, Any]) -> dict[str, Any]:
    moment, _detected = parse_moment(arguments)
    moment = with_zone(moment, optional_zone(arguments, "assumeTimezone"))
    target = arguments.get("toTimezone")
    if not isinstance(target, str):
        raise ValueError("toTimezone must be an IANA timezone name")
    converted = moment.astimezone(resolve_zone(target))
    return {"text": converted.isoformat(), "from": {"iso8601": moment.isoformat(), "timezone": str(moment.tzinfo)}, **describe(converted)}


UNIT_SECONDS = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400, "weeks": 604800}


def difference(arguments: dict[str, Any]) -> dict[str, Any]:
    assumed = optional_zone(arguments, "assumeTimezone")
    left = with_zone(parse_moment(arguments)[0], assumed)
    right = with_zone(parse_moment(arguments, "otherText")[0], assumed)
    delta = right - left
    seconds = delta.total_seconds()
    unit = choice(arguments, "unit", set(UNIT_SECONDS), "seconds")
    return {
        "seconds": seconds,
        unit: seconds / UNIT_SECONDS[unit],
        "humanized": humanize_seconds(abs(seconds), 3),
        "direction": "after" if seconds > 0 else "before" if seconds < 0 else "same",
        "from": left.isoformat(),
        "to": right.isoformat(),
    }


def now_operation(arguments: dict[str, Any]) -> dict[str, Any]:
    zone = optional_zone(arguments, "timezone") or datetime_module.timezone.utc
    return describe(datetime_module.datetime.now(zone))


DURATION_UNITS = (("d", 86400), ("h", 3600), ("m", 60), ("s", 1))


def humanize_seconds(seconds: float, parts: int) -> str:
    if seconds < 1:
        return f"{seconds:.3g}s"
    remaining = int(seconds)
    pieces = []
    for label, size in DURATION_UNITS:
        if remaining >= size and len(pieces) < parts:
            pieces.append(f"{remaining // size}{label}")
            remaining %= size
    return " ".join(pieces) or "0s"


def duration_humanize(arguments: dict[str, Any]) -> dict[str, Any]:
    seconds = arguments.get("seconds")
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds != seconds or abs(seconds) == float("inf"):
        raise ValueError("seconds must be a finite number")
    if seconds < 0:
        raise ValueError("seconds must not be negative")
    parts = bounded_int(arguments, "parts", 2, 1, 4)
    return {"text": humanize_seconds(seconds, parts), "seconds": seconds, "iso8601": iso_duration(seconds)}


def iso_duration(seconds: float) -> str:
    whole = int(seconds)
    days, remainder = divmod(whole, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    fraction = seconds - whole
    time_part = "".join(part for part in (f"{hours}H" if hours else "", f"{minutes}M" if minutes else "", f"{secs + fraction:g}S" if secs or fraction or not (days or hours or minutes) else ""))
    return f"P{f'{days}D' if days else ''}" + (f"T{time_part}" if time_part else "")


DURATION_TOKEN = re.compile(r"(\d+(?:\.\d+)?)\s*(weeks?|w|days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)", re.IGNORECASE)
DURATION_SCALE = {"w": 604800, "week": 604800, "weeks": 604800, "d": 86400, "day": 86400, "days": 86400, "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600, "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60, "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1}


def duration_parse(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments).strip()
    if len(text) > 200:
        raise ValueError("text exceeds 200 characters")
    matches = list(DURATION_TOKEN.finditer(text))
    if not matches:
        raise ValueError("no duration components found; use forms such as '1h 30m' or '2 days'")
    if re.sub(r"[\s,]|and", "", DURATION_TOKEN.sub("", text), flags=re.IGNORECASE):
        raise ValueError("text contains characters that are not part of a duration")
    total = sum(float(match.group(1)) * DURATION_SCALE[match.group(2).lower()] for match in matches)
    return {"seconds": total, "text": humanize_seconds(total, 4), "iso8601": iso_duration(total), "components": len(matches)}


TEXT_MOMENT = {"text": typed("string", maxLength=200, description="ISO-8601, epoch seconds/milliseconds, or RFC 2822 timestamp.")}
ASSUMED = {"assumeTimezone": typed("string", maxLength=64), "inputFormat": typed("string", maxLength=MAX_FORMAT), "epochUnit": {"enum": ["auto", "seconds", "milliseconds"]}}

DATETIME_OPERATIONS = {
    "parse": Operation("Parse a timestamp and report its components.", {**TEXT_MOMENT, **ASSUMED}, ("text",), parse_operation),
    "format": Operation("Reformat a timestamp with a validated strftime pattern.", {**TEXT_MOMENT, **ASSUMED, "format": typed("string", maxLength=MAX_FORMAT), "timezone": typed("string", maxLength=64)}, ("text",), format_operation),
    "convert_timezone": Operation("Convert a timestamp to another IANA timezone.", {**TEXT_MOMENT, **ASSUMED, "toTimezone": typed("string", maxLength=64)}, ("text", "toTimezone"), convert_timezone),
    "difference": Operation("Measure the signed interval between two timestamps.", {**TEXT_MOMENT, **ASSUMED, "otherText": typed("string", maxLength=200), "unit": {"enum": sorted(UNIT_SECONDS)}}, ("text", "otherText"), difference),
    "now": Operation("Report the node's current time in an IANA timezone.", {"timezone": typed("string", maxLength=64)}, (), now_operation),
    "duration_humanize": Operation("Render a number of seconds as a compact duration.", {"seconds": typed("number", minimum=0), "parts": typed("integer", minimum=1, maximum=4)}, ("seconds",), duration_humanize),
    "duration_parse": Operation("Parse a duration such as '1h 30m' into seconds.", {"text": typed("string", maxLength=200)}, ("text",), duration_parse),
}
