import os
import json
import requests
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# TIMEZONE & PATH CONFIGURATION
# ---------------------------------------------------------------------------
BERLIN_TZ = ZoneInfo("Europe/Berlin")
CACHE_DIR = Path(".cache")
STATE_FILE = CACHE_DIR / "state.json"

# ---------------------------------------------------------------------------
# ENVIRONMENT VARIABLES & DEFAULTS
# ---------------------------------------------------------------------------
NTFY_TOPIC = os.getenv("NTFY_TOPIC")
BA_API_KEY = os.getenv("BA_API_KEY")

min_start_raw = os.getenv("MIN_START_DATE", "2026-10-01")
MIN_START_DATE = datetime.strptime(min_start_raw, "%Y-%m-%d").date()

max_start_raw = os.getenv("MAX_START_DATE")
MAX_START_DATE = (
    datetime.strptime(max_start_raw, "%Y-%m-%d").date()
    if max_start_raw
    else None
)

max_end_raw = os.getenv("MAX_END_DATE", "2027-03-01")
MAX_END_DATE = datetime.strptime(max_end_raw, "%Y-%m-%d").date()

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
# SEARCH CONFIGURATIONS
# ---------------------------------------------------------------------------
SEARCH_TARGETS = [
    {
        "name": "Berlin & Umgebung (50km)",
        "category": "Berlin (50km)",
        "url_template": (
            "https://rest.arbeitsagentur.de/infosysbub/sprachfoerderung/pc/v1/bildungsangebot"
            "?systematiken=MC&page={page}&umkreis=50&orte=Berlin_13.4056_52.5178&sort=basc&sprachniveaus=MC%2001%204"
        ),
        "allow_in_person": True,
    },
    {
        "name": "Virtuell (Bundesweit)",
        "category": "Virtuell (Bundesweit)",
        "url_template": (
            "https://rest.arbeitsagentur.de/infosysbub/sprachfoerderung/pc/v1/bildungsangebot"
            "?systematiken=MC&page={page}&umkreis=Bundesweit&sort=basc&sprachniveaus=MC%2001%204"
        ),
        "allow_in_person": False,
    },
]

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.7",
    "dnt": "1",
    "origin": "https://web.arbeitsagentur.de",
    "priority": "u=1, i",
    "sec-ch-ua": '"Brave";v="153", "Not_A Brand";v="8", "Chromium";v="153"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "sec-gpc": "1",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
    "x-api-key": BA_API_KEY,
}

# ---------------------------------------------------------------------------
# HELPER FUNCTIONS
# ---------------------------------------------------------------------------
def ms_to_berlin_datetime(ms):
    """Converts epoch milliseconds to Europe/Berlin localized datetime."""
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=BERLIN_TZ)

def build_angebot_url(termin_id):
    return f"https://web.arbeitsagentur.de/sprachfoerderung/suche/berufssprachkurse/angebot/{termin_id}"

def is_specialized_course(termin):
    angebot = termin.get("angebot", {})
    titel = angebot.get("titel", "").lower()

    if any(keyword in titel for keyword in SPECIALIZED_KEYWORDS):
        return True

    for syst in angebot.get("systematiken", []):
        if syst.get("codeNr") in ["MC 02", "MC 03"]:
            return True

    return False

def evaluate_termin(termin, allow_in_person: bool):
    """
    Evaluates an offering against filters.
    Returns (status, reason):
      status in ['MATCH', 'SPECIALIZED', 'BAD_START', 'BAD_END', 'BAD_FORMAT']
    """
    if EXCLUDE_SPECIALIZED and is_specialized_course(termin):
        return "SPECIALIZED", "Fachspezifischer Kurs (Frühpädagogik/Medizin/etc.)"

    start_dt = ms_to_berlin_datetime(termin.get("beginn"))
    if not start_dt or start_dt.date() < MIN_START_DATE:
        return "BAD_START", f"Start {start_dt.strftime('%d.%m.%Y') if start_dt else 'N/A'} < {MIN_START_DATE.strftime('%d.%m.%Y')}"

    if MAX_START_DATE and start_dt.date() > MAX_START_DATE:
        return "BAD_START", f"Start {start_dt.strftime('%d.%m.%Y')} > {MAX_START_DATE.strftime('%d.%m.%Y')}"

    end_dt = ms_to_berlin_datetime(termin.get("ende"))
    if not end_dt or end_dt.date() >= MAX_END_DATE:
        return "BAD_END", f"Ende {end_dt.strftime('%d.%m.%Y') if end_dt else 'N/A'} >= {MAX_END_DATE.strftime('%d.%m.%Y')}"

    if not allow_in_person:
        form_id = termin.get("unterrichtsform", {}).get("id")
        titel = termin.get("angebot", {}).get("titel", "").lower()
        zeiten = (termin.get("unterrichtszeiten") or "").lower()

        is_virtual_form = (form_id == 5)
        is_hybrid_in_person = ("teilweise virtuell" in titel) or ("in präsenz" in zeiten)
        if not (is_virtual_form and not is_hybrid_in_person):
            return "BAD_FORMAT", "Präsenz- oder Hybridkurs außerhalb Berlins"

    return "MATCH", "Kriterien erfüllt"

def send_ntfy_notification(termin, termin_id, category):
    if not NTFY_TOPIC:
        print("[NTFY] Skipped: NTFY_TOPIC environment variable is not configured.")
        return

    angebot = termin.get("angebot", {})
    provider = angebot.get("bildungsanbieter", {}).get("name", "Unbekannter Anbieter")
    title = angebot.get("titel", "Berufssprachkurs C1")

    start_str = ms_to_berlin_datetime(termin.get("beginn")).strftime("%d.%m.%Y")
    end_str = ms_to_berlin_datetime(termin.get("ende")).strftime("%d.%m.%Y")
    city = termin.get("adresse", {}).get("ortStrasse", {}).get("name", "N/A")
    form_label = termin.get("unterrichtsform", {}).get("bezeichnung", "N/A")
    contact_email = angebot.get("bildungsanbieter", {}).get("email") or "N/A"
    course_url = build_angebot_url(termin_id)

    message = (
        f"Kategorie: {category}\n"
        f"ID: {termin_id}\n"
        f"Schule: {provider}\n"
        f"Ort: {city} ({form_label})\n"
        f"Laufzeit: {start_str} - {end_str}\n"
        f"Kontakt: {contact_email}\n\n"
        f"Titel: {title}"
    )

    payload = {
        "topic": NTFY_TOPIC,
        "title": f"C1 BSK [{category}]: {city} ({start_str})",
        "message": message,
        "priority": 4,
        "tags": ["mortar_board", "calendar"],
        "click": course_url,
    }

    try:
        resp = requests.post("https://ntfy.sh", json=payload, timeout=10)
        resp.raise_for_status()
        print(f"[NTFY] Notification sent for ID {termin_id} ({category})")
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
        f"| Total Unique Termine Evaluated | **{stats['total']}** |",
        f"| Excluded: Specialized (Frühpädagogik/Medizin/etc.) | {stats['specialized']} |",
        f"| Excluded: Start Date Mismatch | {stats['bad_start']} |",
        f"| Excluded: End Date Mismatch (>= Max End) | {stats['bad_end']} |",
        f"| Excluded: Non-Virtual Outside Berlin | {stats['bad_format']} |",
        f"| Already Notified (Cached) | {stats['already_notified']} |",
        f"| **New Matches: Berlin & Umgebung (50km)** | **{stats['new_berlin']}** |",
        f"| **New Matches: Virtuell (Bundesweit)** | **{stats['new_virtual']}** |",
        f"| **Total New Courses Notified** | **{stats['new_berlin'] + stats['new_virtual']}** |\n",
    ]

    if newly_matched_items:
        markdown.append("### Newly Found & Notified Courses\n")
        markdown.append("| ID | Category | Provider | Location | Format | Duration | Title | Link |")
        markdown.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        for item in newly_matched_items:
            markdown.append(
                f"| `{item['id']}` | **{item['category']}** | {item['provider']} | {item['city']} | "
                f"{item['form']} | {item['start']} - {item['end']} | {item['title']} | [Open Angebot]({item['link']}) |"
            )
    else:
        markdown.append("> *No new matching course offerings found in this run.*\n")

    with open(summary_path, "a", encoding="utf-8") as f:
        f.write("\n".join(markdown) + "\n")

# ---------------------------------------------------------------------------
# MAIN EXECUTION
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
        "bad_format": 0,
        "already_notified": 0,
        "new_berlin": 0,
        "new_virtual": 0,
    }

    evaluated_in_run = set()
    newly_matched_items = []

    for target in SEARCH_TARGETS:
        print(f"Scanning search query: {target['name']}...")
        page = 0

        while True:
            url = target["url_template"].format(page=page)
            resp = requests.get(url, headers=HEADERS, timeout=25)

            if resp.status_code != 200:
                print(f"Fetch failed on page {page} for {target['name']}: HTTP {resp.status_code}")
                print(f"Server response body: {resp.text}")
                break

            data = resp.json()
            termine = data.get("_embedded", {}).get("termine", [])
            if not termine:
                break

            for t in termine:
                termin_id = str(t.get("id"))

                if termin_id in evaluated_in_run:
                    continue
                evaluated_in_run.add(termin_id)
                stats["total"] += 1

                status, _ = evaluate_termin(t, allow_in_person=target["allow_in_person"])

                if status == "SPECIALIZED":
                    stats["specialized"] += 1
                elif status == "BAD_START":
                    stats["bad_start"] += 1
                elif status == "BAD_END":
                    stats["bad_end"] += 1
                elif status == "BAD_FORMAT":
                    stats["bad_format"] += 1
                elif status == "MATCH":
                    if termin_id in notified_ids:
                        stats["already_notified"] += 1
                    else:
                        category = target["category"]
                        if category == "Berlin (50km)":
                            stats["new_berlin"] += 1
                        else:
                            stats["new_virtual"] += 1

                        send_ntfy_notification(t, termin_id, category)
                        notified_ids.add(termin_id)

                        angebot = t.get("angebot", {})
                        newly_matched_items.append({
                            "id": termin_id,
                            "category": category,
                            "provider": angebot.get("bildungsanbieter", {}).get("name", "N/A"),
                            "city": t.get("adresse", {}).get("ortStrasse", {}).get("name", "N/A"),
                            "form": t.get("unterrichtsform", {}).get("bezeichnung", "N/A"),
                            "start": ms_to_berlin_datetime(t.get("beginn")).strftime("%d.%m.%Y"),
                            "end": ms_to_berlin_datetime(t.get("ende")).strftime("%d.%m.%Y"),
                            "title": angebot.get("titel", "N/A"),
                            "link": build_angebot_url(termin_id),
                        })

            page_info = data.get("page", {})
            total_pages = page_info.get("totalPages", 1)
            if page >= total_pages - 1:
                break
            page += 1

    total_new = stats["new_berlin"] + stats["new_virtual"]
    print(f"Scan complete. Evaluated: {stats['total']}, New alerts dispatched: {total_new}.")

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(list(notified_ids)), f, indent=2)

    write_github_summary(stats, newly_matched_items)

    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a") as gh_out:
            gh_out.write(f"new_courses_notified={str(bool(newly_matched_items)).lower()}\n")

if __name__ == "__main__":
    main()
