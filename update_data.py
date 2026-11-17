#!/usr/bin/env python3
"""Стягує дані з gmc.uzhgorod.ua і зберігає data.json для дашборду."""

import json
import math
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

OUT = Path(__file__).parent / "data.json"
WARNING_URL = "http://www.gmc.uzhgorod.ua/mstorm.php"
CITY_STATION_IDS = (21050, 23257, 21765)
WARNING_LEVELS = {"I": 1, "II": 2, "III": 3, "1": 1, "2": 2, "3": 3}
MONTHS = (
    "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
)
WARNING_DATE = re.compile(
    r"(?<![\d.])(\d{1,2})(?:\s*[-–—]\s*(\d{1,2}))?\s+"
    r"(" + "|".join(MONTHS) + r")\b(?:\s+(\d{4})\b)?",
    re.IGNORECASE,
)
WARNING_LEVEL = re.compile(r"\b([IІ]{1,3}|[1-3])\s+рівень\s+небезп", re.IGNORECASE)
ISSUED = re.compile(
    r"<h3\b[^>]*>\s*Дата і час випуску:\s*(\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2})\s*</h3>",
    re.IGNORECASE,
)
WARNING_NONE = re.compile(
    r"(?:\bнемає\s+(?:чинних\s+)?(?:штормових\s+)?попереджень\b|"
    r"\bпопередження\s+(?:відсутні|не оголошено)\b)",
    re.IGNORECASE,
)


def fetch_station():
    url = "http://www.gmc.uzhgorod.ua/metdata.php?StNo=33631"
    r = requests.get(url, timeout=25)
    r.raise_for_status()
    r.encoding = "windows-1251"
    matches = re.findall(
        r"(\d{2}\.\d{2}\.\d{4}\s+\d{1,2}:\d{2})</td><td>([^<]*)</td><td>([^<]*)</td>"
        r"<td>([^<]*)</td><td>([^<]*)</td><td>([^<]*)</td><td>([^<]*)</td><td>([^<]*)",
        r.text,
    )
    rows = []
    for m in matches:
        rows.append({
            "dt": m[0],
            "temp": m[1].replace(",", ".").strip() or None,
            "dew": m[2].replace(",", ".").strip() or None,
            "precip": m[3].strip() or None,
            "pressure": m[4].replace(",", ".").strip() or None,
            "wind_dir": m[5].strip() or None,
            "wind_speed": m[6].strip() or None,
            "rad": m[7].strip() or None,
        })
    if not rows:
        raise ValueError('Station observations missing')
    return rows


def fetch_forecast():
    url = "http://www.gmc.uzhgorod.ua/metbul.php"
    r = requests.get(url, timeout=25)
    r.raise_for_status()
    r.encoding = "windows-1251"
    text = r.text
    result = {"uzh": "—", "region": "—", "next": "—"}

    idx = text.find("ПРОГНОЗ ПОГОДИ")
    if idx < 0:
        raise ValueError('Official forecast missing')

    content = re.split(r'<!--\s*start rightbar\s*-->', text[idx:], maxsplit=1, flags=re.I)[0]
    snippet = unescape(re.sub(r"<[^>]+>", " ", content))
    snippet = re.sub(r"\s+", " ", snippet).strip()

    m = re.search(
        r"по Закарпатській області:\s*(.+?)(?=Автошляхи:|по м\. Ужгород:|На наступні)",
        snippet,
    )
    if m:
        result["region"] = m.group(1).strip()

    m = re.search(
        r"по м\. Ужгород:\s*(.+?)(?=На наступні|Консультація|$)",
        snippet,
    )
    if m:
        result["uzh"] = m.group(1).strip()

    m = re.search(
        r"На наступні 2 доби\s*(.+?)(?=Консультація|Рівень радіації|$)",
        snippet,
    )
    if m:
        result["next"] = m.group(1).strip()

    if not any(value != '—' for value in result.values()):
        raise ValueError('Official forecast sections missing')
    return result


def parse_warning_date(match, issued_day):
    month = MONTHS.index(match[3].lower()) + 1
    day = int(match[2] or match[1])
    if match[4]:
        return date(int(match[4]), month, day)

    # Dates in the text may omit the year. Anchor them to the dated bulletin.
    candidates = []
    for year in (issued_day.year - 1, issued_day.year, issued_day.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    return min(candidates, key=lambda candidate: abs(candidate - issued_day))


def warning_text(fragment):
    fragment = re.sub(r"<br\b[^>]*>|</(?:p|h[1-6]|li|div)>", "\n", fragment, flags=re.I)
    plain = unescape(re.sub(r"<[^>]+>", "", fragment))
    lines = (" ".join(line.split()) for line in plain.splitlines())
    return "\n".join(line for line in lines if line)


def parse_warnings(html, now):
    content = re.search(r'<div\b[^>]*\bid=["\']content["\'][^>]*>(.*?)<!--\s*start rightbar\s*-->', html, re.S | re.I)
    if not content or "МЕТЕОРОЛОГІЧНЕ ШТОРМОВЕ ПОПЕРЕДЖЕННЯ" not in warning_text(content[1]).upper():
        raise ValueError("Regional warning section missing")
    notices = list(ISSUED.finditer(content[1]))
    if not notices:
        if WARNING_NONE.search(warning_text(content[1])):
            return []
        raise ValueError("Regional warning issue date missing")

    warnings = []
    for index, notice in enumerate(notices):
        next_notice = notices[index + 1].start() if index + 1 < len(notices) else len(content[1])
        body = re.split(r"<hr\b[^>]*>", content[1][notice.end():next_notice], maxsplit=1, flags=re.I)[0]
        message = warning_text(body)
        issued = datetime.strptime(" ".join(notice[1].split()), "%d.%m.%Y %H:%M").replace(tzinfo=now.tzinfo)
        dates = [parse_warning_date(match, issued.date()) for match in WARNING_DATE.finditer(message)]
        levels = {WARNING_LEVELS[level.upper().replace("І", "I")] for level in WARNING_LEVEL.findall(message)}
        if not dates:
            raise ValueError("Regional warning validity date not recognized")
        last_day = max(dates)
        if last_day < now.date():
            continue

        # The source gives dates and periods in prose, so keep the complete period
        # wording and expire at the end of the final date it mentions.
        expires = datetime.combine(last_day + timedelta(days=1), time.min, tzinfo=now.tzinfo)
        warnings.append({
            "text": message,
            "level": levels.pop() if len(levels) == 1 else None,
            "issued": issued.strftime("%d.%m.%Y %H:%M"),
            "expires_at": expires.isoformat(),
        })
    return warnings


def fetch_warnings(now):
    status = {"source": WARNING_URL, "checked_at": now.isoformat(), "items": []}
    try:
        response = requests.get(WARNING_URL, timeout=15)
        response.raise_for_status()
        status["items"] = parse_warnings(response.content.decode("cp1251"), now)
        status["status"] = "ok"
    except (requests.RequestException, ValueError) as error:
        print(f"Warning bulletin unavailable: {error}", file=sys.stderr)
        status["status"] = "unavailable"
    return status


def fetch_weather_scene(now):
    result = {"status": "unavailable", "checked_at": now.isoformat()}
    try:
        response = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": 48.62, "longitude": 22.30,
                    "current": "weather_code,is_day", "timeformat": "unixtime"},
            timeout=15,
        )
        response.raise_for_status()
        current = response.json()["current"]
        code = current["weather_code"]
        daylight = current["is_day"]
        valid_at = datetime.fromtimestamp(current["time"], timezone.utc)
        if type(code) is not int or code not in range(100) or daylight not in (0, 1):
            raise ValueError("Invalid scene conditions")
        if not -900 <= (now - valid_at).total_seconds() <= 7200:
            raise ValueError("Stale scene conditions")
        result.update(status="ok", code=code, is_day=bool(daylight), valid_at=valid_at.isoformat())
    except (requests.RequestException, ValueError, KeyError, TypeError, OverflowError) as error:
        print(f"Weather scene unavailable: {error}", file=sys.stderr)
    return result


def fetch_city_station(station_id):
    source = f"https://www.saveecobot.com/station/{station_id}.json"
    station = {"id": station_id, "source": source, "status": "unavailable"}
    try:
        response = requests.get(source, timeout=15)
        response.raise_for_status()
        data = response.json()
        if data.get("id") != station_id or not isinstance(data.get("last_data"), list):
            raise ValueError("Unexpected station response")
        station.update({key: data.get(key) for key in (
            "sensor_name", "latitude", "longitude", "platform_name",
            "aqi", "aqi_is_old", "aqi_updated_at",
        )})
        station["last_data"] = [item for item in data["last_data"]
                                if isinstance(item, dict) and item.get("phenomenon")
                                in ("temperature", "humidity", "pm25")]
        station["status"] = "ok"
    except (requests.RequestException, ValueError, TypeError, AttributeError) as error:
        print(f"City station {station_id} unavailable: {error}", file=sys.stderr)
    station["checked_at"] = datetime.now(timezone.utc).isoformat()
    return station


def fetch_model(now):
    result = {'status': 'unavailable', 'checked_at': now.isoformat(),
              'source': 'https://open-meteo.com/'}
    try:
        response = requests.get('https://api.open-meteo.com/v1/forecast', params={
            'latitude': 48.62, 'longitude': 22.30, 'timezone': 'Europe/Kyiv',
            'wind_speed_unit': 'ms', 'forecast_days': 7,
            'current': 'temperature_2m,relative_humidity_2m,apparent_temperature,is_day,precipitation,weather_code,pressure_msl,wind_speed_10m,wind_direction_10m,wind_gusts_10m',
            'hourly': 'temperature_2m,precipitation_probability,precipitation,weather_code,wind_speed_10m,is_day',
            'daily': 'weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,wind_speed_10m_max',
        }, timeout=25)
        response.raise_for_status()
        model = response.json()
        current = model['current']
        for key in ('temperature_2m', 'apparent_temperature', 'weather_code', 'is_day'):
            if not isinstance(current[key], (int, float)) or not math.isfinite(current[key]):
                raise ValueError('Invalid model current values')
        valid = datetime.fromisoformat(current['time']).replace(tzinfo=ZoneInfo('Europe/Kyiv'))
        if not -900 <= (now - valid).total_seconds() <= 7200:
            raise ValueError('Stale model current time')
        for section, keys, minimum in (
            ('hourly', ('temperature_2m', 'weather_code', 'precipitation_probability'), 24),
            ('daily', ('temperature_2m_min', 'temperature_2m_max', 'weather_code',
                       'precipitation_sum', 'precipitation_probability_max', 'wind_speed_10m_max'), 7),
        ):
            count = len(model[section]['time'])
            if count < minimum or any(len(model[section][key]) != count for key in keys):
                raise ValueError('Incomplete model forecast')
        result.update(model)
        result.update(status='ok', checked_at=now.isoformat())
    except (requests.RequestException, ValueError, KeyError, TypeError, OverflowError) as error:
        print(f'Open-Meteo unavailable: {error}', file=sys.stderr)
    return result


def main():
    now = datetime.now(timezone.utc)
    try:
        previous = json.loads(OUT.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        previous = {}
    with ThreadPoolExecutor(max_workers=7) as executor:
        station_job = executor.submit(fetch_station)
        forecast_job = executor.submit(fetch_forecast)
        warning_job = executor.submit(fetch_warnings, now.astimezone(ZoneInfo('Europe/Kyiv')))
        model_job = executor.submit(fetch_model, now)
        city_jobs = [executor.submit(fetch_city_station, station_id) for station_id in CITY_STATION_IDS]
        statuses = {}
        def collect(name, job, fallback):
            try:
                value = job.result()
                statuses[name] = {'status': 'ok', 'checked_at': now.isoformat()}
                return value
            except Exception as error:
                print(f'{name} unavailable: {error}', file=sys.stderr)
                old_status = previous.get('source_status', {}).get(name, {})
                statuses[name] = {'status': 'unavailable', 'checked_at': old_status.get('checked_at')}
                return previous.get(name, fallback)
        rows = collect('data', station_job, [])
        forecast = collect('forecast', forecast_job, {})
        warnings = warning_job.result()
        model = model_job.result()
        city_stations = [job.result() for job in city_jobs]
    scene = {'status': 'unavailable', 'checked_at': now.isoformat()}
    if model['status'] == 'ok':
        valid_at = datetime.fromisoformat(model['current']['time']).replace(tzinfo=ZoneInfo('Europe/Kyiv'))
        scene.update(status='ok', code=model['current']['weather_code'],
                     is_day=bool(model['current']['is_day']), valid_at=valid_at.isoformat())
    payload = {
        'updated_at': now.isoformat(),
        'source': 'http://www.gmc.uzhgorod.ua/metdata.php?StNo=33631',
        'data': rows, 'forecast': forecast, 'warnings': warnings,
        'weather_scene': scene, 'city_stations': city_stations,
        'model': model, 'source_status': statuses,
    }
    temporary = OUT.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(OUT)
    print(f'Wrote {OUT}; observations={len(rows)}, model={model["status"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
