#!/usr/bin/env python3
"""Стягує дані з gmc.uzhgorod.ua і зберігає data.json для дашборду."""

import json
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

OUT = Path(__file__).parent / "data.json"
WARNING_URL = "http://www.gmc.uzhgorod.ua/mstorm.php"
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
    return rows


def fetch_forecast():
    url = "http://www.gmc.uzhgorod.ua/metbul.php"
    r = requests.get(url, timeout=25)
    r.encoding = "windows-1251"
    text = r.text
    result = {"uzh": "—", "region": "—", "next": "—"}

    idx = text.find("ПРОГНОЗ ПОГОДИ")
    if idx < 0:
        return result

    snippet = re.sub(r"<[^>]+>", " ", text[idx : idx + 2800])
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


def main():
    print("Fetching station data…")
    rows = fetch_station()
    print(f"  {len(rows)} rows, latest: {rows[0]['dt'] if rows else '—'}")

    print("Fetching forecast…")
    forecast = fetch_forecast()

    now = datetime.now(timezone.utc)
    print("Fetching storm warnings…")
    warnings = fetch_warnings(now.astimezone(ZoneInfo("Europe/Kyiv")))
    payload = {
        "updated_at": now.isoformat(),
        "source": "http://www.gmc.uzhgorod.ua/metdata.php?StNo=33631",
        "data": rows,
        "forecast": forecast,
        "warnings": warnings,
    }

    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
