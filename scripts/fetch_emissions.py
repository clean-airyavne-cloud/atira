import http.cookiejar
import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

IST = timezone(timedelta(hours=2))
START_TIME = datetime(2026, 11, 22, 9, 0, 0, tzinfo=IST)


def get_env_str(key: str, default: str) -> str:
    val = os.environ.get(key)
    return val.strip() if val and val.strip() else default


def get_env_float(key: str, default: float) -> float:
    val = os.environ.get(key)
    return float(val.strip()) if val and val.strip() else default


RAW_API_TOKEN = get_env_str("SVIVA_API_TOKEN", "")
if RAW_API_TOKEN.lower().startswith("apitoken "):
    RAW_API_TOKEN = RAW_API_TOKEN[9:].strip()

FLOW_RATES_NM3_H = {
    "CH6": get_env_float("FLOW_RATE_CH6", 102100.0),
    "CH7": get_env_float("FLOW_RATE_CH7", 102100.0),
}

PORTAL_URL = "https://air-factories.sviva.gov.il/"
TOKEN_URL = get_env_str(
    "SVIVA_TOKEN_URL",
    "https://air-fapi.sviva.gov.il/v1/GenerateToken",
)
API_BASE_URL = get_env_str(
    "SVIVA_API_URL",
    "https://air-fapi.sviva.gov.il/v1/envista/stations",
).rstrip("/")

STATIONS_CONFIG = {
    "CH6": {
        "station_id": get_env_str("CH6_STATION_ID", "461"),
        "filter_channels": get_env_str("CH6_CHANNELS", "2,4,8"),
        "nox_channel_id": get_env_str("CH6_NOX_CHANNEL_ID", "2"),
        "co_channel_id": get_env_str("CH6_CO_CHANNEL_ID", "4"),
        "status_channel_id": get_env_str("CH6_STATUS_CHANNEL_ID", "8"),
    },
    "CH7": {
        "station_id": get_env_str("CH7_STATION_ID", "462"),
        "filter_channels": get_env_str("CH7_CHANNELS", "2,4,8"),
        "nox_channel_id": get_env_str("CH7_NOX_CHANNEL_ID", "2"),
        "co_channel_id": get_env_str("CH7_CO_CHANNEL_ID", "4"),
        "status_channel_id": get_env_str("CH7_STATUS_CHANNEL_ID", "8"),
    },
}

OUTPUT_PATH = Path("data/emissions.json")

ENVISTA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9,he;q=0.8",
    "domainname": "prod",
    "envi-data-source": "MANA_Factories2019",
    "Origin": "https://air-factories.sviva.gov.il",
    "Referer": "https://air-factories.sviva.gov.il/",
}


def create_ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class EnvistaClient:
    def __init__(self, api_token: str):
        self.api_token = api_token
        self.access_token = ""
        self.xsrf_token = ""
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=create_ssl_context()),
            urllib.request.HTTPCookieProcessor(self.cj),
        )

    def _extract_cookie_from_headers(self, headers_obj) -> str:
        for cookie in self.cj:
            if cookie.name == "X-Access-Token" and cookie.value:
                return cookie.value
        set_cookies = headers_obj.get_all("Set-Cookie") or []
        for header_val in set_cookies:
            match = re.search(r"X-Access-Token=([^;]+)", header_val)
            if match:
                return match.group(1).strip()
        return ""

    def _try_direct_generate_token(self) -> str:
        xsrf_token = ""
        try:
            init_req = urllib.request.Request(
                PORTAL_URL,
                headers={"User-Agent": ENVISTA_HEADERS["User-Agent"]},
            )
            with self.opener.open(init_req, timeout=20) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
                match = re.search(r'__RequestVerificationToken[^>]*value="([^"]+)"', html)
                if match:
                    xsrf_token = match.group(1)
        except Exception:
            pass

        candidate_tokens = [str(uuid.uuid4())]
        if self.api_token:
            candidate_tokens.append(self.api_token)

        for tok_candidate in candidate_tokens:
            headers = {
                **ENVISTA_HEADERS,
                "Authorization": f"ApiToken {tok_candidate}",
                "content-length": "0",
                "contentlength": "0",
            }
            if xsrf_token:
                headers["x-requestverificationtoken"] = xsrf_token

            req = urllib.request.Request(TOKEN_URL, data=b"", headers=headers, method="POST")
            try:
                with self.opener.open(req, timeout=20) as resp:
                    jwt_tok = self._extract_cookie_from_headers(resp.headers)
                    if jwt_tok:
                        self.xsrf_token = xsrf_token
                        print("התקבלה עוגיית X-Access-Token בהצלחה בחיבור ישיר.")
                        return jwt_tok
            except Exception:
                continue

        return ""

    def _obtain_token_via_browser(self) -> str:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("חבילת playwright אינה מותקנת בסביבה.") from exc

        print("מפעיל דפדפן סמוי לקבלת אסימון אבטחה ועוגיית X-Access-Token מהפורטל...")
        captured_xsrf = ""
        captured_api_token = ""

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=ENVISTA_HEADERS["User-Agent"],
                locale="he-IL",
                ignore_https_errors=True,
            )
            page = context.new_page()

            def on_request(request):
                nonlocal captured_xsrf, captured_api_token
                hdrs = request.headers
                if "x-requestverificationtoken" in hdrs and hdrs["x-requestverificationtoken"]:
                    captured_xsrf = hdrs["x-requestverificationtoken"]
                auth = hdrs.get("authorization", "")
                if auth.lower().startswith("apitoken "):
                    captured_api_token = auth.split(" ", 1)[1].strip()

            page.on("request", on_request)

            try:
                page.goto(PORTAL_URL, wait_until="networkidle", timeout=45000)
            except Exception:
                pass
            page.wait_for_timeout(4000)

            for c in context.cookies():
                if c.get("name") == "X-Access-Token" and c.get("value"):
                    tok = c["value"]
                    self.xsrf_token = captured_xsrf
                    browser.close()
                    print("התקבלה עוגיית X-Access-Token בהצלחה דרך הדפדפן הסמוי.")
                    return tok

            if captured_xsrf:
                api_tok = captured_api_token or self.api_token or str(uuid.uuid4())
                page.evaluate(
                    """async ([url, apiTok, xsrf]) => {
                        await fetch(url, {
                            method: 'POST',
                            credentials: 'include',
                            headers: {
                                'accept': 'application/json',
                                'authorization': 'ApiToken ' + apiTok,
                                'contentlength': '0',
                                'domainname': 'prod',
                                'envi-data-source': 'MANA_Factories2019',
                                'x-requestverificationtoken': xsrf
                            }
                        });
                    }""",
                    [TOKEN_URL, api_tok, captured_xsrf],
                )
                page.wait_for_timeout(2000)
                for c in context.cookies():
                    if c.get("name") == "X-Access-Token" and c.get("value"):
                        tok = c["value"]
                        self.xsrf_token = captured_xsrf
                        browser.close()
                        print("התקבלה עוגיית X-Access-Token בהצלחה לאחר הפעלת GenerateToken.")
                        return tok

            browser.close()

        raise RuntimeError("לא התקבלה עוגיית X-Access-Token גם לאחר טעינת הפורטל בדפדפן.")

    def ensure_access_token(self) -> str:
        if self.access_token:
            return self.access_token

        tok = self._try_direct_generate_token()
        if not tok:
            tok = self._obtain_token_via_browser()

        self.access_token = tok
        return tok

    def get_json(self, url: str):
        jwt_cookie = self.ensure_access_token()
        headers = {
            **ENVISTA_HEADERS,
            "Authorization": "JwtToken",
            "Cookie": f"X-Access-Token={jwt_cookie}",
        }
        if self.xsrf_token:
            headers["x-requestverificationtoken"] = self.xsrf_token

        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self.opener.open(req, timeout=30) as resp:
                raw_text = resp.read().decode("utf-8", errors="ignore").strip()
                # השרת מחזיר תשובה ריקה (HTTP 200/204) כאשר אין מדידות פעילות בחלון הזמן
                if not raw_text:
                    return []
                return json.loads(raw_text)
        except urllib.error.HTTPError as err:
            err_body = err.read().decode("utf-8", errors="ignore")[:300]
            raise RuntimeError(f"שליפת נתונים נכשלה (HTTP {err.code}): {err_body}") from err


def fetch_window(client: EnvistaClient, station_id: str, filter_channels: str, start_dt: datetime, end_dt: datetime) -> list:
    start_clean = start_dt.replace(minute=0, second=0, microsecond=0)
    end_clean = end_dt.replace(minute=0, second=0, microsecond=0)

    params = {
        "filterChannels": filter_channels,
        "from": start_clean.strftime("%Y-%m-%dT%H:%M:%S"),
        "to": end_clean.strftime("%Y-%m-%dT%H:%M:%S"),
        "fromTimebase": "60",
        "toTimebase": "60",
        "precentValid": "75",
        "timeBeginning": "false",
        "useBackWard": "true",
        "unitConversion": "false",
        "includeSummary": "false",
        "onlySummary": "false",
    }
    query_str = urllib.parse.urlencode(params, safe=",:")
    url = f"{API_BASE_URL}/{station_id}/Average?{query_str}"

    payload = client.get_json(url)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return payload.get("data", payload.get("items", []))
    return []


def fetch_station_data(client: EnvistaClient, cfg: dict, start_dt: datetime, end_dt: datetime) -> list:
    all_records = []
    cursor = start_dt
    step = timedelta(days=14)

    while cursor < end_dt:
        chunk_end = min(cursor + step, end_dt)
        records = fetch_window(client, cfg["station_id"], cfg["filter_channels"], cursor, chunk_end)
        all_records.extend(records)
        cursor = chunk_end

    return all_records


def parse_timestamp(ts_str: str) -> datetime:
    clean = ts_str.replace("Z", "+00:00")
    dt = datetime.fromisoformat(clean)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def is_unit_operating(status_val) -> bool:
    """
    מזהה האם היחידה פעילה לפי ערוץ 8:
    הפורטל מחזיר לרוב מחרוזת 'Opr' (Operating), או ערך מספרי גדול מ-0.
    """
    if status_val is None:
        return False
    if isinstance(status_val, (int, float)):
        return float(status_val) > 0
    s = str(status_val).strip().lower()
    if s in {"opr", "operating", "on", "run", "running", "active", "true", "1"}:
        return True
    try:
        return float(s) > 0
    except ValueError:
        return False


def extract_channel_values(item: dict, cfg: dict) -> tuple:
    nox_val = None
    co_val = None
    status_val = None

    nox_id = str(cfg.get("nox_channel_id", "")).strip()
    co_id = str(cfg.get("co_channel_id", "")).strip()
    status_id = str(cfg.get("status_channel_id", "8")).strip()

    channels = item.get("channels", [])
    if isinstance(channels, list):
        for ch in channels:
            val = ch.get("value")
            if val is None or ch.get("valid") is False:
                continue

            ch_id = str(ch.get("id", ch.get("channelId", ""))).strip()
            if ch_id == nox_id:
                try:
                    nox_val = float(val)
                except (ValueError, TypeError):
                    nox_val = None
            elif ch_id == co_id:
                try:
                    co_val = float(val)
                except (ValueError, TypeError):
                    co_val = None
            elif ch_id == status_id:
                # שומרים את הערך הגולמי של ערוץ 8 (יכול להיות 'Opr' או מספר)
                status_val = val

    return nox_val, co_val, status_val


def calculate_station_totals(records: list, station_key: str, cfg: dict) -> dict:
    flow_rate = FLOW_RATES_NM3_H[station_key]
    by_time = {}
    has_status_channel = False

    for item in records:
        ts_raw = item.get("datetime") or item.get("date") or item.get("time")
        if not ts_raw:
            continue
        dt = parse_timestamp(ts_raw)
        if dt < START_TIME:
            continue
        nox_val, co_val, status_val = extract_channel_values(item, cfg)
        if status_val is not None:
            has_status_channel = True
        by_time[dt] = (nox_val, co_val, status_val)

    sorted_times = sorted(by_time.keys())
    total_nox_kg = 0.0
    total_co_kg = 0.0
    nox_sum = 0.0
    co_sum = 0.0
    nox_count = 0
    co_count = 0
    active_hours = 0
    shutdown_hours = 0

    for idx, dt in enumerate(sorted_times):
        nox_val, co_val, status_val = by_time[dt]

        # סינון שעות הדממה לפי ערוץ 8 (Unit Status):
        # סופרים פליטות רק כאשר היחידה פעילה ('Opr' או ערך חיובי)
        if has_status_channel and not is_unit_operating(status_val):
            shutdown_hours += 1
            continue

        active_hours += 1

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
        "samplesCount": active_hours,
        "shutdownHours": shutdown_hours,
    }


def write_initial_json():
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    initial = {
        "lastUpdated": START_TIME.isoformat(),
        "startDate": START_TIME.isoformat(),
        "permitNumber": "2211",
        "siteId": "360767",
        "sourceStatus": "initialized",
        "flowRatesNm3h": FLOW_RATES_NM3_H,
        "stations": {
            "CH6": {
                "NOx_kg": 0,
                "CO_kg": 0,
                "avg_NOx_mg_nm3": 0,
                "avg_CO_mg_nm3": 0,
                "samplesCount": 0,
                "shutdownHours": 0,
            },
            "CH7": {
                "NOx_kg": 0,
                "CO_kg": 0,
                "avg_NOx_mg_nm3": 0,
                "avg_CO_mg_nm3": 0,
                "samplesCount": 0,
                "shutdownHours": 0,
            },
        },
        "totals": {"NOx_kg": 0, "CO_kg": 0, "NOx_tons": 0, "CO_tons": 0},
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(initial, f, ensure_ascii=False, indent=2)


def main():
    now_ist = datetime.now(IST)
    existing_data = {}
    if OUTPUT_PATH.exists():
        with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
            existing_data = json.load(f)

    client = EnvistaClient(RAW_API_TOKEN)

    if now_ist < START_TIME:
        print(f"טרם הגיע מועד ההתחלה ({START_TIME.isoformat()}). מבצע בדיקת תקשורת מול שתי הארובות...")
        sample_start = datetime(2026, 11, 13, 1, 0, 0, tzinfo=IST)
        sample_end = datetime(2026, 11, 14, 0, 0, 0, tzinfo=IST)

        for st_key, cfg in STATIONS_CONFIG.items():
            recent_records = fetch_window(
                client,
                cfg["station_id"],
                cfg["filter_channels"],
                now_ist - timedelta(hours=24),
                now_ist,
            )
            sample_records = fetch_window(
                client,
                cfg["station_id"],
                cfg["filter_channels"],
                sample_start,
                sample_end,
            )
            print(
                f"[{st_key}] בדיקת תקשורת (תחנה {cfg['station_id']}) הצליחה! "
                f"מדידות ב-24 שעות אחרונות: {len(recent_records)} | מדידות בחלון בדיקה (13/11): {len(sample_records)}."
            )
        if not OUTPUT_PATH.exists():
            write_initial_json()
        return

    stations_result = {}
    all_ok = True

    for st_key, cfg in STATIONS_CONFIG.items():
        try:
            records = fetch_station_data(client, cfg, START_TIME, now_ist)
            stations_result[st_key] = calculate_station_totals(records, st_key, cfg)
            print(
                f"[{st_key}] עודכן בהצלחה | שעות פעילות: {stations_result[st_key]['samplesCount']} | "
                f"שעות הדממה שסוננו: {stations_result[st_key]['shutdownHours']} | "
                f"נתונים: {stations_result[st_key]}"
            )
        except Exception as exc:
            all_ok = False
            print(f"[{st_key}] שגיאה במשיכת נתונים: {exc}")
            stations_result[st_key] = existing_data.get("stations", {}).get(
                st_key,
                {
                    "NOx_kg": 0,
                    "CO_kg": 0,
                    "avg_NOx_mg_nm3": 0,
                    "avg_CO_mg_nm3": 0,
                    "samplesCount": 0,
                    "shutdownHours": 0,
                },
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
