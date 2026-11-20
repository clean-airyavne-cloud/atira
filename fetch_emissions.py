import json
import os
import ssl
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ==============================================================================
# הגדרות תצורה רשמיות לפי היתר פליטה מס' 2211 (אתר סביבתי 360767)
# תחנת כוח מתקן התפלה אשדוד - שפיר ג'י.אי.אס זכיין תחנת כח בע"מ
# ==============================================================================

# נקודת ההתחלה לספירה: יום ראשון, 22 בנובמבר 2026, שעה 09:00 (שעון ישראל UTC+2)
IST = timezone(timedelta(hours=2))
START_TIME = datetime(2026, 11, 22, 9, 0, 0, tzinfo=IST)

SITE_ENV_ID = "360767"
PERMIT_NUMBER = "2211"

# ספיקה תקנית נומינלית הנגזרת מהספק תרמי נקוב 33.3 MWth בנרמול ל-15% O2 יבש = 102,100 Nm^3/h לכל ארובה
# הערה: אם קיים ערוץ ספיקה מדוד בנתוני הניטור הרציף (סעיף 18(א)(2) להיתר), הסקריפט ישתמש בו אוטומטית.
NOMINAL_FLOW_RATES_NM3_H = {
    "CH6": float(os.environ.get("FLOW_RATE_CH6", 102100)),
    "CH7": float(os.environ.get("FLOW_RATE_CH7", 102100)),
}

# כתובת ה-API הפנימית של מערכת ניטור הארובות (air-factories.sviva.gov.il)
API_BASE_URL = os.environ.get(
    "SVIVA_API_URL",
    "https://air-factories.sviva.gov.il/api/v1/envista/stations"
)

# מזהי הארובות הרשמיים מתוך טבלאות א'1 ו-א'2 בהיתר פליטה 2211:
# CH6 -> מזהה ארובה 203712 (מנוע MAN-6)
# CH7 -> מזהה ארובה 203713 (מנוע MAN-7)
STATIONS_CONFIG = {
    "CH6": {
        "permit_stack_id": "203712",
        "station_id": os.environ.get("CH6_STATION_ID", "203712"),
        "nox_channel": os.environ.get("CH6_NOX_CHANNEL", "NOx"),
        "co_channel": os.environ.get("CH6_CO_CHANNEL", "CO"),
        "flow_channel": os.environ.get("CH6_FLOW_CHANNEL", "flow"),
    },
    "CH7": {
        "permit_stack_id": "203713",
        "station_id": os.environ.get("CH7_STATION_ID", "203713"),
        "nox_channel": os.environ.get("CH7_NOX_CHANNEL", "NOx"),
        "co_channel": os.environ.get("CH7_CO_CHANNEL", "CO"),
        "flow_channel": os.environ.get("CH7_FLOW_CHANNEL", "flow"),
    },
}

OUTPUT_PATH = Path("data/emissions.json")


def create_ssl_context() -> ssl.SSLContext:
    """יוצר הקשר SSL מותאם לתקשורת מול שרתי gov.il."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def fetch_station_records(station_id: str, start_dt: datetime, end_dt: datetime) -> list:
    """
    מושך את רשומות הניטור הרציף עבור תחנת ארובה בטווח התאריכים מ-START_TIME ועד עתה.
    """
    from_str = start_dt.strftime("%Y/%m/%d %H:%M")
    to_str = end_dt.strftime("%Y/%m/%d %H:%M")
    query = urllib.parse.urlencode({"from": from_str, "to": to_str})
    url = f"{API_BASE_URL}/{station_id}/data?{query}"

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; AshdodPermit2211Monitor/1.0)",
            "Accept": "application/json",
            "Referer": "https://air-factories.sviva.gov.il/",
        },
    )

    with urllib.request.urlopen(req, context=create_ssl_context(), timeout=30) as resp:
        raw_body = resp.read().decode("utf-8")
        payload = json.loads(raw_body)
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            return payload.get("data", payload.get("items", []))
        return []


def parse_record_time(ts_raw: str) -> datetime:
    """ממיר חותמת זמן מהשרת לאובייקט datetime באזור זמן ישראל (IST)."""
    normalized = ts_raw.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def integrate_station_emissions(records: list, station_key: str, cfg: dict) -> dict:
    """
    מבצע אינטגרציה על המדידות מ-START_TIME ומחשב מסה מצטברת בק"ג:
    Mass (kg) = (Concentration [mg/Nm^3] * FlowRate [Nm^3/h] * Duration [h]) / 1,000,000
    """
    default_flow = NOMINAL_FLOW_RATES_NM3_H[station_key]
    nox_key = cfg["nox_channel"].lower()
    co_key = cfg["co_channel"].lower()
    flow_key = cfg["flow_channel"].lower()

    parsed_rows = []
    for row in records:
        ts_str = row.get("datetime") or row.get("date") or row.get("time")
        if not ts_str:
            continue
        dt = parse_record_time(ts_str)
        if dt < START_TIME:
            continue

        nox_val = None
        co_val = None
        measured_flow = None

        if "channels" in row and isinstance(row["channels"], list):
            for ch in row["channels"]:
                ch_name = str(ch.get("name", "")).lower()
                val = ch.get("value")
                if val is None or ch.get("valid") is False:
                    continue
                if nox_key in ch_name or "no2" in ch_name:
                    nox_val = float(val)
                elif co_key == ch_name or f" {co_key}" in ch_name or ch_name.startswith("co"):
                    co_val = float(val)
                elif flow_key in ch_name or "ספיקה" in ch_name:
                    measured_flow = float(val)
        else:
            for k, v in row.items():
                if v is None:
                    continue
                kl = k.lower()
                if nox_key in kl:
                    nox_val = float(v)
                elif kl == co_key or kl.startswith("co_"):
                    co_val = float(v)
                elif flow_key in kl:
                    measured_flow = float(v)

        parsed_rows.append((dt, nox_val, co_val, measured_flow))

    parsed_rows.sort(key=lambda r: r[0])

    total_nox_kg = 0.0
    total_co_kg = 0.0
    nox_conc_sum = 0.0
    co_conc_sum = 0.0
    nox_samples = 0
    co_samples = 0
    latest_flow_used = default_flow

    for idx, (dt, nox_val, co_val, measured_flow) in enumerate(parsed_rows):
        if idx > 0:
            delta_h = (dt - parsed_rows[idx - 1][0]).total_seconds() / 3600.0
            duration_h = delta_h if 0 < delta_h <= 2.0 else 0.5
        else:
            duration_h = 0.5

        active_flow = measured_flow if (measured_flow is not None and measured_flow > 0) else default_flow
        latest_flow_used = active_flow

        if nox_val is not None and nox_val >= 0:
            total_nox_kg += (nox_val * active_flow * duration_h) / 1_000_000.0
            nox_conc_sum += nox_val
            nox_samples += 1

        if co_val is not None and co_val >= 0:
            total_co_kg += (co_val * active_flow * duration_h) / 1_000_000.0
            co_conc_sum += co_val
            co_samples += 1

    return {
        "permitStackId": cfg["permit_stack_id"],
        "NOx_kg": round(total_nox_kg, 2),
        "CO_kg": round(total_co_kg, 2),
        "avg_NOx_mg_nm3": round(nox_conc_sum / nox_samples, 2) if nox_samples else 0,
        "avg_CO_mg_nm3": round(co_conc_sum / co_samples, 2) if co_samples else 0,
        "activeFlowNm3h": round(latest_flow_used, 1),
        "samplesCount": max(nox_samples, co_samples),
    }


def main():
    now_ist = datetime.now(IST)

    existing_payload = {}
    if OUTPUT_PATH.exists():
        with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
            existing_payload = json.load(f)

    if now_ist < START_TIME:
        print(f"טרם הגיע מועד הדיון ({START_TIME.isoformat()}). נשמר מצב המתנה.")
        return

    stations_out = {}
    all_succeeded = True

    for st_key, cfg in STATIONS_CONFIG.items():
        try:
            records = fetch_station_records(cfg["station_id"], START_TIME, now_ist)
            stations_out[st_key] = integrate_station_emissions(records, st_key, cfg)
            print(f"[{st_key} | מזהה {cfg['permit_stack_id']}] עודכן בהצלחה: {stations_out[st_key]}")
        except Exception as err:
            all_succeeded = False
            print(f"[{st_key} | מזהה {cfg['permit_stack_id']}] שגיאה במשיכת נתונים: {err}")
            stations_out[st_key] = existing_payload.get("stations", {}).get(
                st_key,
                {
                    "permitStackId": cfg["permit_stack_id"],
                    "NOx_kg": 0,
                    "CO_kg": 0,
                    "avg_NOx_mg_nm3": 0,
                    "avg_CO_mg_nm3": 0,
                    "activeFlowNm3h": NOMINAL_FLOW_RATES_NM3_H[st_key],
                    "samplesCount": 0,
                },
            )

    total_nox_kg = round(stations_out["CH6"]["NOx_kg"] + stations_out["CH7"]["NOx_kg"], 2)
    total_co_kg = round(stations_out["CH6"]["CO_kg"] + stations_out["CH7"]["CO_kg"], 2)

    final_payload = {
        "lastUpdated": now_ist.isoformat(),
        "startDate": START_TIME.isoformat(),
        "permitNumber": PERMIT_NUMBER,
        "siteEnvId": SITE_ENV_ID,
        "sourceStatus": "live" if all_succeeded else "cached_or_pending_endpoint",
        "flowRatesNm3h": {
            "CH6": stations_out["CH6"]["activeFlowNm3h"],
            "CH7": stations_out["CH7"]["activeFlowNm3h"],
        },
        "permitLimitsMgNm3": {
            "NOx_daily": 55,
            "NOx_annual": 50,
            "CO_annual": 40,
            "Formaldehyde_limit": 5,
        },
        "stations": stations_out,
        "totals": {
            "NOx_kg": total_nox_kg,
            "CO_kg": total_co_kg,
            "NOx_tons": round(total_nox_kg / 1000.0, 3),
            "CO_tons": round(total_co_kg / 1000.0, 3),
        },
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(final_payload, f, ensure_ascii=False, indent=2)

    print("קובץ data/emissions.json עודכן ונשמר בהצלחה.")


if __name__ == "__main__":
    main()