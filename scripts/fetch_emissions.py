import json
import os
import ssl
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

IST = timezone(timedelta(hours=2))
START_TIME = datetime(2026, 11, 22, 9, 0, 0, tzinfo=IST)

FLOW_RATES_NM3_H = {
    "CH6": float(os.environ.get("FLOW_RATE_CH6", 102100)),
    "CH7": float(os.environ.get("FLOW_RATE_CH7", 102100)),
}

API_BASE_URL = os.environ.get(
    "SVIVA_API_URL",
    "https://air-fapi.sviva.gov.il/v1/envista/stations"
).rstrip("/")

STATIONS_CONFIG = {
    "CH6": {
        "station_id": os.environ.get("CH6_STATION_ID", "461").strip(),
        "filter_channels": os.environ.get("CH6_CHANNELS", "2,4,8").strip(),
        "nox_channel_id": os.environ.get("CH6_NOX_CHANNEL_ID", "2").strip(),
        "co_channel_id": os.environ.get("CH6_CO_CHANNEL_ID", "4").strip(),
    },
    "CH7": {
        "station_id": os.environ.get("CH7_STATION_ID", "462").strip(),
        "filter_channels": os.environ.get("CH7_CHANNELS", "2,4,8").strip(),
        "nox_channel_id": os.environ.get("CH7_NOX_CHANNEL_ID", "2").strip(),
        "co_channel_id": os.environ.get("CH7_CO_CHANNEL_ID", "4").strip(),
    },
}

OUTPUT_PATH = Path("data/emissions.json")


def create_ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def fetch_window(station_id: str, filter_channels: str, start_dt: datetime, end_dt: datetime) -> list:
    params = {
        "filterChannels": filter_channels,
        "from": start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "to": end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "fromTimebase": "60",
        "toTimebase": "60",
        "precentValid": "75",
        "timeBeginning": "false",
        "useBackWard": "true",
        "unitConversion": "false",
        "includeSummary": "false",
        "onlySummary": "false",
    }
    url = f"{API_BASE_URL}/{station_id}/Average?{urllib.parse.urlencode(params)}"

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Origin": "https://air-factories.sviva.gov.il",
            "Referer": "https://air-factories.sviva.gov.il/",
        },
    )

    with urllib.request.urlopen(req, context=create_ssl_context(), timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            return payload.get("data", payload.get("items", []))
        return []


def fetch_station_data(cfg: dict, start_dt: datetime, end_dt: datetime) -> list:
    all_records = []
    cursor = start_dt
    step = timedelta(days=14)

    while cursor < end_dt:
        chunk_end = min(cursor + step, end_dt)
        records = fetch_window(cfg["station_id"], cfg["filter_channels"], cursor, chunk_end)
        all_records.extend(records)
        cursor = chunk_end

    return all_records


def parse_timestamp(ts_str: str) -> datetime:
    clean = ts_str.replace("Z", "+00:00")
    dt = datetime.fromisoformat(clean)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def extract_channel_values(item: dict, cfg: dict) -> tuple:
    nox_val = None
    co_val = None
    nox_id = str(cfg.get("nox_channel_id", "")).strip()
    co_id = str(cfg.get("co_channel_id", "")).strip()

    channels = item.get("channels", [])
    if isinstance(channels, list):
        for ch in channels:
            val = ch.get("value")
            if val is None or ch.get("valid") is False:
                continue

            ch_id = str(ch.get("id", ch.get("channelId", ""))).strip()
            if ch_id == nox_id:
                nox_val = float(val)
            elif ch_id == co_id:
                co_val = float(val)

    return nox_val, co_val


def calculate_station_totals(records: list, station_key: str, cfg: dict) -> dict:
    flow_rate = FLOW_RATES_NM3_H[station_key]
    by_time = {}

    for item in records:
        ts_raw = item.get("datetime") or item.get("date") or item.get("time")
        if not ts_raw:
            continue
        dt = parse_timestamp(ts_raw)
        if dt < START_TIME:
            continue
        nox_val, co_val = extract_channel_values(item, cfg)
        by_time[dt] = (nox_val, co_val)

    sorted_times = sorted(by_time.keys())
    total_nox_kg = 0.0
    total_co_kg = 0.0
    nox_sum = 0.0
    co_sum = 0.0
    nox_count = 0
    co_count = 0

    for idx, dt in enumerate(sorted_times):
        nox_val, co_val = by_time[dt]
        if idx > 0:
            delta_h = (dt - sorted_times[idx - 1]).total_seconds() / 3600.0
            duration_h = delta_h if 0 < delta_h <= 2.0 else 1.0
        else:
            duration_h = 1.0

        if nox_val is not None and nox_val >= 0:
            total_nox_kg += (nox_val * flow_rate * duration_h) / 1_000_000.0
            nox_sum += nox_val
            nox_count += 1

        if co_val is not None and co_val >= 0:
            total_co_kg += (co_val * flow_rate * duration_h) / 1_000_000.0
            co_sum += co_val
            co_count += 1

    return {
        "NOx_kg": round(total_nox_kg, 2),
        "CO_kg": round(total_co_kg, 2),
        "avg_NOx_mg_nm3": round(nox_sum / nox_count, 2) if nox_count else 0,
        "avg_CO_mg_nm3": round(co_sum / co_count, 2) if co_count else 0,
        "samplesCount": max(nox_count, co_count),
    }


def main():
    now_ist = datetime.now(IST)
    existing_data = {}
    if OUTPUT_PATH.exists():
        with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
            existing_data = json.load(f)

    if now_ist < START_TIME:
        print(f"טרם הגיע מועד ההתחלה ({START_TIME.isoformat()}). מבצע בדיקת תקשורת מול שתי הארובות...")
        for st_key, cfg in STATIONS_CONFIG.items():
            test_records = fetch_window(
                cfg["station_id"],
                cfg["filter_channels"],
                now_ist - timedelta(hours=24),
                now_ist,
            )
            print(f"[{st_key}] בדיקת תקשורת (תחנה {cfg['station_id']}) הצליחה! התקבלו {len(test_records)} רשומות שעתיות.")
        return

    stations_result = {}
    all_ok = True

    for st_key, cfg in STATIONS_CONFIG.items():
        try:
            records = fetch_station_data(cfg, START_TIME, now_ist)
            stations_result[st_key] = calculate_station_totals(records, st_key, cfg)
            print(f"[{st_key}] עודכן בהצלחה: {stations_result[st_key]}")
        except Exception as exc:
            all_ok = False
            print(f"[{st_key}] שגיאה במשיכת נתונים: {exc}")
            stations_result[st_key] = existing_data.get("stations", {}).get(
                st_key,
                {"NOx_kg": 0, "CO_kg": 0, "avg_NOx_mg_nm3": 0, "avg_CO_mg_nm3": 0, "samplesCount": 0},
            )

    total_nox_kg = round(stations_result["CH6"]["NOx_kg"] + stations_result["CH7"]["NOx_kg"], 2)
    total_co_kg = round(stations_result["CH6"]["CO_kg"] + stations_result["CH7"]["CO_kg"], 2)

    output = {
        "lastUpdated": now_ist.isoformat(),
        "startDate": START_TIME.isoformat(),
        "permitNumber": "2211",
        "siteId": "360767",
        "sourceStatus": "live" if all_ok else "partial_or_cached",
        "flowRatesNm3h": FLOW_RATES_NM3_H,
        "stations": stations_result,
        "totals": {
            "NOx_kg": total_nox_kg,
            "CO_kg": total_co_kg,
            "NOx_tons": round(total_nox_kg / 1000.0, 3),
            "CO_tons": round(total_co_kg / 1000.0, 3),
        },
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("קובץ data/emissions.json נשמר בהצלחה.")


if __name__ == "__main__":
    main()
