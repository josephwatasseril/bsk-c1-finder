import os
import json
import requests
from pathlib import Path
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# PATHS & CACHE CONFIGURATION
# ---------------------------------------------------------------------------
CACHE_DIR = Path(".cache")
STATE_FILE = CACHE_DIR / "state.json"

# ---------------------------------------------------------------------------
# ENVIRONMENT VARIABLES & DEFAULTS
# ---------------------------------------------------------------------------
NTFY_TOPIC = os.getenv("NTFY_TOPIC")
BA_API_KEY = os.getenv("BA_API_KEY")

min_start_raw = os.getenv("MIN_START_DATE", "2026-10-01")
MIN_START_DATE = datetime.strptime(min_start_raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)

max_start_raw = os.getenv("MAX_START_DATE")
MAX_START_DATE = (
    datetime.strptime(max_start_raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if max_start_raw
    else None
)

max_end_raw = os.getenv("MAX_END_DATE", "2027-03-01")
MAX_END_DATE = datetime.strptime(max_end_raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)

EXCLUDE_SPECIALIZED = os.getenv("EXCLUDE_SPECIALIZED", "true").lower() == "true"
SPECIALIZED_KEYWORDS = [
    "frühpädagogik",
    "heilberufe",
    "humanmedizin",
    "apotheker",
    "pharmazie",
    "erzieher",
    "kita",
]

# ---------------------------------------------------------------------------
# API CONSTANTS
# ---------------------------------------------------------------------------
API_URL = "https://rest.arbeitsagentur.de/infosysbub/sprachfoerderung/pc/v1/bildungsangebot"
PARAMS = {
    "systematiken": "MC",
    "sprachniveaus": "MC 01 4",  # C1 level
    "umkreis": "Bundesweit",
    "sort": "basc",
    "size": 50,
}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.7",
    "Origin": "https://web.arbeitsagentur.de",
    "Referer": "https://web.arbeitsagentur.de/",
    "X-API-Key": BA_API_KEY,
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "DNT": "1",
}

# ---------------------------------------------------------------------------
# HELPER FUNCTIONS
# ---------------------------------------------------------------------------
def ms_to_datetime(ms):
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)

def is_specialized_course(termin):
    angebot = termin.get("angebot", {})
    titel = angebot.get("titel", "").lower()

    if any(keyword in titel for keyword in SPECIALIZED_KEYWORDS):
        return True

    # BAMF codes: MC 02 = Fachspezifisch, MC 03 = Anerkennung Heilberufe
    for syst in angebot.get("systematiken", []):
        if syst.get("codeNr") in ["MC 02", "MC 03"]:
            return True

    return False

def evaluate_termin(termin):
    """
    Evaluates an offering against user-defined filters.
    Returns (status, reason):
      status in ['MATCH', 'SPECIALIZED', 'BAD_START', 'BAD_END', 'BAD_LOCATION_FORMAT']
    """
    if EXCLUDE_SPECIALIZED and is_specialized_course(termin):
        return "SPECIALIZED", "Fachspezifischer Kurs (Frühpädagogik/Medizin/etc.)"

    # 1. Start Date Check
    start_dt = ms_to_datetime(termin.get("beginn"))
    if not start_dt or start_dt < MIN_START_DATE:
        return "BAD_START", f"Start {start_dt.strftime('%d.%m.%Y') if start_dt else 'N/A'} < {MIN_START_DATE.strftime('%d.%m.%Y')}"

    if MAX_START_DATE and start_dt > MAX_START_DATE:
        return "BAD_START", f"Start {start_dt.strftime('%d.%m.%Y')} > {MAX_START_DATE.strftime('%d.%m.%Y')}"

    # 2. End Date Check
    end_dt = ms_to_datetime(termin.get("ende"))
    if not end_dt or end_dt >= MAX_END_DATE:
        return "BAD_END", f"Ende {end_dt.strftime('%d.%m.%Y') if end_dt else 'N/A'} >= {MAX_END_DATE.strftime('%d.%m.%Y')}"

    # 3. Location & Format Check
    adresse = termin.get("adresse", {})
    ort_info = adresse.get("ortStrasse", {})
    city = ort_info.get("name", "")
    bundesland = ort_info.get("land", {}).get("bundeslandCode", "")
    is_berlin = (city == "Berlin" or bundesland == "BER")

    form_id = termin.get("unterrichtsform", {}).get("id")
    titel = termin.get("angebot", {}).get("titel", "").lower()
    zeiten = (termin.get("unterrichtszeiten") or "").lower()

    # Form ID 5 corresponds to E-Learning / Virtuelles Klassenzimmer
    is_virtual_form = (form_id == 5)
    is_hybrid_in_person = ("teilweise virtuell" in titel) or ("in präsenz" in zeiten and not is_berlin)
    is_pure_virtual = is_virtual_form and not is_hybrid_in_person

    if not (is_berlin or is_pure_virtual):
        return "BAD_LOCATION_FORMAT", "Weder Berlin noch 100% virtuell"

    return "MATCH", "Kriterien erfüllt"

def send_ntfy_notification(termin):
    if not NTFY_TOPIC:
        print("[NTFY] Skipped: NTFY_TOPIC environment variable is not set.")
        return

    angebot = termin.get("angebot", {})
    provider = angebot.get("bildungsanbieter", {}).get("name", "Unbekannter Anbieter")
    title = angebot.get("titel", "Berufssprachkurs C1")

    start_str = ms_to_datetime(termin.get("beginn")).strftime("%d.%m.%Y")
    end_str = ms_to_datetime(termin.get("ende")).strftime("%d.%m.%Y")
    city = termin.get("adresse", {}).get("ortStrasse", {}).get("name", "Online")
    contact_email = angebot.get("bildungsanbieter", {}).get("email") or "N/A"
    course_url = (
        termin.get("link")
        or angebot.get("link")
        or "https://web.arbeitsagentur.de/sprachfoerderung/suche/berufssprachkurse"
    )

    message = (
        f"Schule: {provider}\n"
        f"Ort: {city}\n"
        f"Laufzeit: {start_str} - {end_str}\n"
        f"Kontakt: {contact_email}\n\n"
        f"Titel: {title}"
    )

    payload = {
        "topic": NTFY_TOPIC,
        "title": f"C1 BSK Gefunden: {city} ({start_str})",
        "message": message,
        "priority": 4,
        "tags": ["mortar_board", "calendar"],
        "click": course_url,
    }

    try:
        resp = requests.post("https://ntfy.sh", json=payload, timeout=10)
        resp.raise_for_status()
        print(f"[NTFY] Notification successfully dispatched for termin ID {termin.get('id')}")
    except Exception as e:
        print(f"[ERROR] Failed to send NTFY notification: {e}")

def write_github_summary(stats, newly_matched_items):
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    date_range_str = f"Starts on/after `{MIN_START_DATE.strftime('%d.%m.%Y')}`"
    if MAX_START_DATE:
        date_range_str += f" and on/before `{MAX_START_DATE.strftime('%d.%m.%Y')}`"
    date_range_str += f" | Ends before `{MAX_END_DATE.strftime('%d.%m.%Y')}`"

    markdown = [
        "## C1 Berufssprachkurs Checker Summary\n",
        f"**Target Window:** {date_range_str}\n",
        "| Metric | Count |",
        "| :--- | :--- |",
        f"| Total Termine Evaluated | **{stats['total']}** |",
        f"| Excluded: Specialized (Frühpädagogik/Medizin/etc.) | {stats['specialized']} |",
        f"| Excluded: Start Date Mismatch | {stats['bad_start']} |",
        f"| Excluded: End Date Mismatch (>= Max End) | {stats['bad_end']} |",
        f"| Excluded: Format/Location Mismatch | {stats['bad_location_format']} |",
        f"| Already Notified (Cached) | {stats['already_notified']} |",
        f"| **New Matching Courses Notified** | **{stats['new_matches']}** |\n",
    ]

    if newly_matched_items:
        markdown.append("### Newly Found & Notified Courses\n")
        markdown.append("| ID | Provider | Location | Duration | Title | Link |")
        markdown.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
        for item in newly_matched_items:
            markdown.append(
                f"| `{item['id']}` | {item['provider']} | {item['city']} | "
                f"{item['start']} - {item['end']} | {item['title']} | [Open Link]({item['link']}) |"
            )
    else:
        markdown.append("> *No new matching course offerings detected in this run.*\n")

    with open(summary_path, "a", encoding="utf-8") as f:
        f.write("\n".join(markdown) + "\n")

# ---------------------------------------------------------------------------
# MAIN SCRIPT EXECUTION
# ---------------------------------------------------------------------------
def main():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    notified_ids = set()
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            try:
                notified_ids = set(json.load(f))
            except json.JSONDecodeError:
                notified_ids = set()

    stats = {
        "total": 0,
        "specialized": 0,
        "bad_start": 0,
        "bad_end": 0,
        "bad_location_format": 0,
        "already_notified": 0,
        "new_matches": 0,
    }

    newly_matched_items = []
    page = 0

    print("Fetching C1 Berufssprachkurs offerings from Arbeitsagentur API...")

    while True:
        params = {**PARAMS, "page": page}
        resp = requests.get(API_URL, params=params, headers=HEADERS, timeout=25)
        if resp.status_code != 200:
            print(f"Fetch failed on page {page}: HTTP {resp.status_code}")
            break

        data = resp.json()
        termine = data.get("_embedded", {}).get("termine", [])
        if not termine:
            break

        for t in termine:
            stats["total"] += 1
            status, _ = evaluate_termin(t)

            if status == "SPECIALIZED":
                stats["specialized"] += 1
            elif status == "BAD_START":
                stats["bad_start"] += 1
            elif status == "BAD_END":
                stats["bad_end"] += 1
            elif status == "BAD_LOCATION_FORMAT":
                stats["bad_location_format"] += 1
            elif status == "MATCH":
                termin_id = str(t.get("id"))
                if termin_id in notified_ids:
                    stats["already_notified"] += 1
                else:
                    stats["new_matches"] += 1
                    send_ntfy_notification(t)
                    notified_ids.add(termin_id)

                    angebot = t.get("angebot", {})
                    newly_matched_items.append({
                        "id": termin_id,
                        "provider": angebot.get("bildungsanbieter", {}).get("name", "N/A"),
                        "city": t.get("adresse", {}).get("ortStrasse", {}).get("name", "N/A"),
                        "start": ms_to_datetime(t.get("beginn")).strftime("%d.%m.%Y"),
                        "end": ms_to_datetime(t.get("ende")).strftime("%d.%m.%Y"),
                        "title": angebot.get("titel", "N/A"),
                        "link": (
                            t.get("link")
                            or angebot.get("link")
                            or "https://web.arbeitsagentur.de/sprachfoerderung/suche/berufssprachkurse"
                        ),
                    })

        page_info = data.get("page", {})
        total_pages = page_info.get("totalPages", 1)
        if page >= total_pages - 1:
            break
        page += 1

    print(f"Scan complete. Evaluated: {stats['total']}, New notifications: {stats['new_matches']}.")

    # Persist state back to .cache/state.json
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(list(notified_ids)), f, indent=2)

    # Render summary table in GitHub Actions UI
    write_github_summary(stats, newly_matched_items)

    # Export output flag for actions/cache step condition
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a") as gh_out:
            gh_out.write(f"new_courses_notified={str(bool(newly_matched_items)).lower()}\n")

if __name__ == "__main__":
    main()
