#!/usr/bin/env python3
"""Стягує дані з gmc.uzhgorod.ua і зберігає data.json для дашборду."""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

OUT = Path(__file__).parent / "data.json"


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


def main():
    print("Fetching station data…")
    rows = fetch_station()
    print(f"  {len(rows)} rows, latest: {rows[0]['dt'] if rows else '—'}")

    print("Fetching forecast…")
    forecast = fetch_forecast()

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "http://www.gmc.uzhgorod.ua/metdata.php?StNo=33631",
        "data": rows,
        "forecast": forecast,
    }

    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
