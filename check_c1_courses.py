import os
import json
import requests
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# CONFIGURATION FROM ENVIRONMENT
# ---------------------------------------------------------------------------
NTFY_TOPIC = os.getenv("NTFY_TOPIC")
TARGET_START_YEAR = int(os.getenv("TARGET_START_YEAR", "2026"))
TARGET_START_MONTH = int(os.getenv("TARGET_START_MONTH", "10"))

# Expected format: YYYY-MM-DD
max_end_raw = os.getenv("MAX_END_DATE", "2027-03-01")
MAX_END_DATE = datetime.strptime(max_end_raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)

EXCLUDE_SPECIALIZED = os.getenv("EXCLUDE_SPECIALIZED", "true").lower() == "true"
SPECIALIZED_KEYWORDS = [
    "frühpädagogik", "heilberufe", "humanmedizin", 
    "apotheker", "pharmazie", "erzieher", "kita"
]

STATE_FILE = "notified_termine.json"
API_URL = "https://rest.arbeitsagentur.de/infosysbub/sprachfoerderung/pc/v1/bildungsangebot"
PARAMS = {
    "systematiken": "MC",
    "sprachniveaus": "MC 01 4",
    "umkreis": "Bundesweit",
    "sort": "basc",
    "size": 50,
}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "X-API-Key": "sprachfoerderung-suche"
}

# ---------------------------------------------------------------------------
# FILTER LOGIC
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
        
    for syst in angebot.get("systematiken", []):
        if syst.get("codeNr") in ["MC 02", "MC 03"]:
            return True
            
    return False

def evaluate_termin(termin):
    """
    Returns (status, reason):
    status in ['MATCH', 'SPECIALIZED', 'BAD_START', 'BAD_END', 'BAD_LOCATION_FORMAT']
    """
    if EXCLUDE_SPECIALIZED and is_specialized_course(termin):
        return "SPECIALIZED", "Fachspezifischer Kurs (Frühpädagogik/Medizin/etc.)"

    start_dt = ms_to_datetime(termin.get("beginn"))
    if not start_dt or start_dt.year != TARGET_START_YEAR or start_dt.month != TARGET_START_MONTH:
        return "BAD_START", f"Start {start_dt.strftime('%m/%Y') if start_dt else 'N/A'} != {TARGET_START_MONTH:02d}/{TARGET_START_YEAR}"

    end_dt = ms_to_datetime(termin.get("ende"))
    if not end_dt or end_dt >= MAX_END_DATE:
        return "BAD_END", f"Ende {end_dt.strftime('%d.%m.%Y') if end_dt else 'N/A'} >= {MAX_END_DATE.strftime('%d.%m.%Y')}"

    adresse = termin.get("adresse", {})
    ort_info = adresse.get("ortStrasse", {})
    city = ort_info.get("name", "")
    bundesland = ort_info.get("land", {}).get("bundeslandCode", "")
    is_berlin = (city == "Berlin" or bundesland == "BER")

    form_id = termin.get("unterrichtsform", {}).get("id")
    titel = termin.get("angebot", {}).get("titel", "").lower()
    zeiten = (termin.get("unterrichtszeiten") or "").lower()

    is_virtual_form = (form_id == 5)
    is_hybrid_in_person = ("teilweise virtuell" in titel) or ("in präsenz" in zeiten and not is_berlin)
    is_pure_virtual = is_virtual_form and not is_hybrid_in_person

    if not (is_berlin or is_pure_virtual):
        return "BAD_LOCATION_FORMAT", "Weder Berlin noch 100% virtuell"

    return "MATCH", "Kriterien erfüllt"

def send_ntfy_notification(termin):
    if not NTFY_TOPIC:
        print("[NTFY] Skipped: NTFY_TOPIC is not configured.")
        return

    angebot = termin.get("angebot", {})
    provider = angebot.get("bildungsanbieter", {}).get("name", "Unbekannter Anbieter")
    title = angebot.get("titel", "Berufssprachkurs C1")
    
    start_str = ms_to_datetime(termin.get("beginn")).strftime("%d.%m.%Y")
    end_str = ms_to_datetime(termin.get("ende")).strftime("%d.%m.%Y")
    city = termin.get("adresse", {}).get("ortStrasse", {}).get("name", "Online")
    contact_email = angebot.get("bildungsanbieter", {}).get("email") or "N/A"
    course_url = termin.get("link") or angebot.get("link") or "https://web.arbeitsagentur.de/sprachfoerderung/suche/berufssprachkurse"

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
        "click": course_url
    }

    try:
        resp = requests.post("https://ntfy.sh", json=payload, timeout=10)
        resp.raise_for_status()
        print(f"[NTFY] Alert sent for termin ID {termin.get('id')}")
    except Exception as e:
        print(f"[ERROR] Failed to send NTFY notification: {e}")

def write_github_summary(stats, newly_matched_items):
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    markdown = []
    markdown.append("## C1 Berufssprachkurs Checker Summary\n")
    markdown.append(f"**Target Window:** Starts `{TARGET_START_MONTH:02d}/{TARGET_START_YEAR}` | Ends before `{MAX_END_DATE.strftime('%d.%m.%Y')}`\n")
    
    markdown.append("| Metric | Count |")
    markdown.append("| :--- | :--- |")
    markdown.append(f"| Total Termine Evaluated | **{stats['total']}** |")
    markdown.append(f"| Excluded: Specialized Courses (Frühpädagogik/Medizin) | {stats['specialized']} |")
    markdown.append(f"| Excluded: Start Date Mismatch (Not Oct 2026) | {stats['bad_start']} |")
    markdown.append(f"| Excluded: End Date Mismatch (>= March 2027) | {stats['bad_end']} |")
    markdown.append(f"| Excluded: Format/Location Mismatch | {stats['bad_location_format']} |")
    markdown.append(f"| Already Notified (Previous Runs) | {stats['already_notified']} |")
    markdown.append(f"| **New Matching Courses Notified** | **{stats['new_matches']}** |\n")

    if newly_matched_items:
        markdown.append("### Newly Found & Notified Courses\n")
        markdown.append("| ID | Provider | Location | Duration | Title | Link |")
        markdown.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
        for item in newly_matched_items:
            markdown.append(
                f"| `{item['id']}` | {item['provider']} | {item['city']} | "
                f"{item['start']} - {item['end']} | {item['title']} | [Course Link]({item['link']}) |"
            )
    else:
        markdown.append("> *No new matching course offerings found in this run.*\n")

    with open(summary_path, "a", encoding="utf-8") as f:
        f.write("\n".join(markdown) + "\n")

# ---------------------------------------------------------------------------
# MAIN EXECUTION
# ---------------------------------------------------------------------------
def main():
    notified_ids = set()
    if os.path.exists(STATE_FILE):
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

    while True:
        p = {**PARAMS, "page": page}
        resp = requests.get(API_URL, params=p, headers=HEADERS, timeout=25)
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
                        "link": t.get("link") or angebot.get("link") or "https://web.arbeitsagentur.de/sprachfoerderung/suche/berufssprachkurse"
                    })

        page_info = data.get("page", {})
        if page >= page_info.get("totalPages", 1) - 1:
            break
        page += 1

    # Update state file
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(list(notified_ids)), f, indent=2)

    # Render GitHub Summary
    write_github_summary(stats, newly_matched_items)

    # Set workflow output
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a") as gh_out:
            gh_out.write(f"new_courses_notified={str(bool(newly_matched_items)).lower()}\n")

if __name__ == "__main__":
    main()
