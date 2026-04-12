from bs4 import BeautifulSoup
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os
import re
from datetime import datetime
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# Load .env if present (for local runs via launchd)
_env_file = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_file):
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

# --- IS24 ---
IS24_PROFILE_DIR = os.path.expanduser("~/.is24-browser-profile")
IS24_BASE_URL = "https://www.immobilienscout24.de"

# Each entry: (url, require_schweinfurt_filter)
# Landkreis-wide searches need the filter to exclude Bamberg etc.;
# radius searches around Grettstadt are already geo-constrained — no filter needed.
IS24_SEARCHES = [
    # Häuser zur Miete im Landkreis Schweinfurt (filter required)
    ("https://www.immobilienscout24.de/Suche/de/bayern/schweinfurt-kreis/haus-mieten", True),
    # Wohnungen ab 90 m² zur Miete im Landkreis Schweinfurt (filter required)
    ("https://www.immobilienscout24.de/Suche/de/bayern/schweinfurt-kreis/wohnung-mieten?livingspace=90.0-", True),
    # Häuser zur Miete im 10km-Umkreis um Grettstadt (no filter — catches cross-Landkreis results)
    (
        "https://www.immobilienscout24.de/Suche/radius/haus-mieten"
        "?centerofsearchaddress=Grettstadt;97508;;;;"
        "&geocoordinates=49.9476;10.4731;10.0",
        False,
    ),
]

# Only keep listings from Landkreis Schweinfurt (address must contain "Schweinfurt")
IS24_REQUIRED_LANDKREIS = "Schweinfurt"

# --- Immowelt ---
IW_BASE_URL = "https://www.immowelt.de"
IW_URLS = [
    # Häuser zur Miete, 20km Radius um Grettstadt
    "https://www.immowelt.de/suche/mieten/haus/bayern/grettstadt-97508/ad08de8146?radius=20",
    # Wohnungen ab 90 m² zur Miete, 20km Radius um Grettstadt
    "https://www.immowelt.de/suche/mieten/wohnung/bayern/grettstadt-97508/ad08de8146?radius=20&flaeche_von=90",
]

SEEN_FILE = "seen_grettstadt.json"


# ---------------------------------------------------------------------------
# IS24 fetching
# ---------------------------------------------------------------------------

def fetch_is24_listings(already_seen=None):
    """Fetch IS24 listings and enrich new ones with expose page details (single browser session)."""
    already_seen = already_seen or set()
    listings = []
    card_seen_ids = set()

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            IS24_PROFILE_DIR,
            headless=False,
            channel="chrome",
            locale="de-DE",
            viewport={"width": 1280, "height": 800},
            args=["--window-position=-2000,0"],
        )
        page = context.new_page()
        Stealth().apply_stealth_sync(page)

        # 1. Fetch search pages
        for url, require_filter in IS24_SEARCHES:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(5000)
            title = page.title()
            if "Roboter" in title:
                context.close()
                _send_session_alert("grettstadt_mieten.py")
                raise RuntimeError(
                    "IS24 robot challenge — run auth_is24.py to refresh session"
                )
            soup = BeautifulSoup(page.content(), "html.parser")
            listings.extend(_parse_is24(soup, card_seen_ids, require_filter=require_filter))

        # 2. Enrich new listings with Baujahr + Energieklasse from expose pages
        new_listings = [l for l in listings if l["id"] not in already_seen]
        for l in new_listings:
            l.setdefault("baujahr", "N/A")
            try:
                page.goto(l["url"], wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(3000)
                # Expand hidden sections (Baujahr lives behind "Mehr anzeigen")
                for btn in page.query_selector_all("button:has-text('Mehr anzeigen')"):
                    try:
                        btn.click()
                        page.wait_for_timeout(400)
                    except Exception:
                        pass
                page.wait_for_timeout(800)
                soup = BeautifulSoup(page.content(), "html.parser")
                text = soup.get_text(" ", strip=True)

                # Baujahr from dt/dd: check "Baujahr" first, then "Baujahr laut Energieausweis"
                for dt in soup.find_all("dt"):
                    label = dt.get_text(strip=True).replace("\xad", "")
                    if label in ("Baujahr", "Baujahr laut Energieausweis"):
                        dd = dt.find_next_sibling("dd")
                        if dd:
                            m_yr = re.match(r"(1[89]\d{2}|20[012]\d)", dd.get_text(strip=True))
                            if m_yr:
                                l["baujahr"] = m_yr.group(1)
                                if label == "Baujahr":
                                    break  # prefer exact Baujahr over the laut-Energieausweis one

                if l.get("energy_class", "N/A") == "N/A":
                    for dt in soup.find_all("dt"):
                        if "Energieklasse" in dt.get_text():
                            dd = dt.find_next_sibling("dd")
                            if dd:
                                val = dd.get_text(strip=True)
                                m = re.match(r"(A\+|[A-H])$", val)
                                if m:
                                    l["energy_class"] = m.group(1)
                                    break
                    if l.get("energy_class", "N/A") == "N/A":
                        m_energy = re.search(r"Energieklasse\s*(A\+|[A-H])\b", text)
                        if m_energy:
                            l["energy_class"] = m_energy.group(1)

                print(f"    [{l['id']}] Baujahr={l.get('baujahr','N/A')} Energie={l.get('energy_class','N/A')}")
            except Exception as e:
                print(f"    [{l['id']}] expose fetch error: {e}")

        context.close()

    return listings


def _parse_is24(soup, seen_ids, require_filter=True):
    # Card text format: [Badge] Title Price Area Rooms [Grundstück m²] [EnergyClass] Address [PriceTag]
    # Radius search cards also prepend "N.NN km | " before the address.
    PRICE_TAGS = r"(?:Guter Preis|Sehr guter Preis|Nur exklusiv|Angemessener Preis|Hoher Preis|$)"

    listings = []
    for card in soup.select("div.listing-card[data-obid]"):
        listing_id = "is24_" + card.get("data-obid", "").strip()
        if not listing_id or listing_id in seen_ids:
            continue
        seen_ids.add(listing_id)

        url = f"{IS24_BASE_URL}/expose/{listing_id[5:]}"
        text = card.get_text(" ", strip=True)

        # Price (Kaltmiete)
        m_price = re.search(r"([\d\.]+)\s*€", text)
        price_str = f"{m_price.group(1)} €" if m_price else "N/A"

        # Rooms
        m_rooms = re.search(r"(\d+(?:[,\.]\d+)?)\s*Zi(?:mmer)?\.?", text)
        rooms = float(m_rooms.group(1).replace(",", ".")) if m_rooms else None

        # Living space (first m² after price, not Grundstück)
        m_space = re.search(r"€\s+([\d,]+)\s*m²", text)
        space_str = f"{m_space.group(1)} m²" if m_space else "N/A"

        # Card format after last m²: [N Zi.]? [EnergyClass]? Address [PriceTag]?
        after_last_m2 = ""
        for m in re.finditer(r"\d+(?:[,\.]\d+)?\s*m²\s*", text):
            after_last_m2 = text[m.end():]

        # Skip past "N Zi." if present (happens when there's no Grundstück block)
        after_last_m2 = re.sub(r"^\d+(?:[,\.]\d+)?\s*Zi\.?\s*", "", after_last_m2)

        # Energy class — optional A+/A/B/C... at start
        energy_class = "N/A"
        m_energy = re.match(r"(A\+|[A-H])\s+", after_last_m2)
        if m_energy:
            energy_class = m_energy.group(1)
            after_last_m2 = after_last_m2[m_energy.end():]

        # Address — everything up to price tag keywords or end
        # Radius search cards prepend "N.NN km | " distance — strip it
        PRICE_TAGS = r"(?:Guter Preis|Sehr guter Preis|Nur exklusiv|Angemessener Preis|Hoher Preis|Ausgezeichneter Preis|Fairer Preis)"
        m_addr = re.match(rf"(.+?)(?:\s+{PRICE_TAGS})?\s*$", after_last_m2.strip())
        address = m_addr.group(1).strip() if m_addr and m_addr.group(1).strip() else "N/A"
        address = re.sub(r"^\d+[\.,]\d+\s*km\s*\|\s*", "", address)

        # For Landkreis-wide searches, require address to contain "Schweinfurt"
        # (filters out Bamberg etc. that slip through). Radius searches skip this.
        if require_filter and IS24_REQUIRED_LANDKREIS not in address:
            continue

        # Skip commercial/multi-unit listings (>20 rooms)
        if rooms is not None and rooms > 20:
            continue

        # Price per m²
        price_per_m2 = _calc_price_per_m2(price_str, space_str)

        listings.append({
            "id": listing_id,
            "source": "IS24",
            "title": text[:80],
            "address": address,
            "rooms": rooms,
            "space": space_str,
            "price": price_str,
            "price_per_m2": price_per_m2,
            "energy_class": energy_class,
            "url": url,
        })

    return listings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _send_session_alert(script_name):
    """Send an email when IS24 session expires."""
    try:
        sender = os.environ["GMAIL_USER"]
        password = os.environ["GMAIL_APP_PASSWORD"]
        recipient = "wenzhizi@foxmail.com"
        msg = MIMEMultipart()
        msg["From"] = sender
        msg["To"] = recipient
        msg["Subject"] = f"[Immo-Monitor] IS24 Session abgelaufen – bitte neu authentifizieren"
        body = (
            f"Der IS24-Monitor ({script_name}) hat eine Robot-Challenge erkannt.\n\n"
            f"Bitte führe folgenden Befehl aus, um die Session zu erneuern:\n\n"
            f"  cd '/Users/zhiziwen/Documents/vibe coding项目/immo-monitor' && "
            f"/opt/anaconda3/bin/python3 auth_is24.py\n\n"
            f"Zeitpunkt: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
        )
        msg.attach(MIMEText(body, "plain", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(sender, password)
            smtp.sendmail(sender, [recipient], msg.as_string())
        print(f"Session alert sent to {recipient}")
    except Exception as e:
        print(f"Failed to send session alert: {e}")


def _calc_price_per_m2(price_str, space_str):
    """Return formatted €/m² string or 'N/A'."""
    try:
        price = float(re.sub(r"[^\d,]", "", price_str).replace(",", "."))
        space = float(re.sub(r"[^\d,]", "", space_str).replace(",", "."))
        if price > 0 and space > 0:
            return f"{price / space:.2f} €/m²"
    except (ValueError, ZeroDivisionError):
        pass
    return "N/A"


# ---------------------------------------------------------------------------
# Immowelt fetching
# ---------------------------------------------------------------------------

def fetch_iw_listings():
    listings = []
    seen_ids = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="de-DE",
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        for url in IW_URLS:
            page.goto(url, wait_until="networkidle", timeout=60000)
            # Remove cookie consent overlay if present
            page.evaluate("document.getElementById('usercentrics-root')?.remove()")
            soup = BeautifulSoup(page.content(), "html.parser")
            listings.extend(_parse_iw(soup, seen_ids))

        browser.close()

    return listings


def _parse_iw(soup, seen_ids):
    listings = []
    cards = soup.select('[data-testid="serp-core-classified-card-testid"]')

    for card in cards:
        link = card.select_one('a[data-testid="card-mfe-covering-link-testid"]')
        if not link:
            continue
        url = link["href"].split("?")[0]
        m = re.search(r"/expose/([a-f0-9\-]+)", url)
        listing_id = "iw_" + (m.group(1) if m else url)
        if listing_id in seen_ids:
            continue
        seen_ids.add(listing_id)

        title = link.get("title", "N/A")

        price_el = card.select_one('[data-testid="cardmfe-price-testid"]')
        price_str = "N/A"
        if price_el:
            raw = price_el.get_text(strip=True)
            m_price = re.match(r"([\d\.\,]+\s*€)", raw)
            price_str = m_price.group(1).strip() if m_price else re.split(r"\d+[,\.]?\d*\s*€/m", raw)[0].strip()

        addr_el = card.select_one('[data-testid="cardmfe-description-box-address"]')
        address = addr_el.get_text(strip=True) if addr_el else "N/A"

        facts_el = card.select_one('[data-testid="cardmfe-keyfacts-testid"]')
        rooms = None
        space_str = "N/A"
        if facts_el:
            facts = facts_el.get_text(strip=True)
            m_rooms = re.search(r"(\d+(?:,\d+)?)\s+Zimmer", facts)
            if m_rooms:
                rooms = float(m_rooms.group(1).replace(",", "."))
            m_space = re.search(r"([\d,]+)\s*m²", facts)
            if m_space:
                space_str = f"{m_space.group(1)} m²"

        energy_el = card.select_one('[data-testid="card-mfe-energy-performance-class"]')
        energy_class = energy_el.get_text(strip=True) if energy_el else "N/A"

        # Filter: Wohnungen must be ≥90m²; all listings need ≥3 rooms
        space_val = float(re.sub(r"[^\d,]", "", space_str).replace(",", ".")) if space_str != "N/A" else 0
        if space_val > 0 and space_val < 90:
            continue
        if rooms is not None and rooms < 3:
            continue

        listings.append({
            "id": listing_id,
            "source": "Immowelt",
            "title": title,
            "address": address,
            "rooms": rooms,
            "space": space_str,
            "price": price_str,
            "price_per_m2": _calc_price_per_m2(price_str, space_str),
            "energy_class": energy_class,
            "url": url,
        })

    return listings


# ---------------------------------------------------------------------------
# Seen file
# ---------------------------------------------------------------------------

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_seen(seen_ids):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen_ids), f, indent=2)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(new_listings):
    sender = os.environ["GMAIL_USER"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    recipients = [r.strip() for r in os.environ.get("NOTIFY_EMAIL", sender).split(",")]

    count = len(new_listings)
    subject = f"[Grettstadt Umgebung] {count} neue{'s' if count == 1 else ''} Mietobjekt{'' if count == 1 else 'e'} gefunden!"

    sections = []
    for l in new_listings:
        sections.append(
            f"Quelle:      {l['source']}\n"
            f"Titel:       {l['title']}\n"
            f"Adresse:     {l['address']}\n"
            f"Zimmer:      {l['rooms']}\n"
            f"Fläche:      {l['space']}\n"
            f"Kaltmiete:   {l['price']}\n"
            f"Preis/m²:    {l['price_per_m2']}\n"
            f"Energieklasse: {l.get('energy_class', 'N/A')}\n"
            f"Baujahr:     {l.get('baujahr', 'N/A')}\n"
            f"Link:        {l['url']}"
        )

    body = (
        f"Neue Mietobjekte (Häuser + Wohnungen ≥90m²) im Landkreis Schweinfurt / Grettstadt Umgebung\n"
        f"({datetime.now().strftime('%d.%m.%Y %H:%M')}):\n\n"
        + ("\n\n" + "-" * 60 + "\n\n").join(sections)
    )

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sender, password)
        smtp.sendmail(sender, recipients, msg.as_string())

    print(f"Email sent to {recipients} with {count} new listing(s)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import random, time
    delay = random.randint(0, 600)
    print(f"[{datetime.now().strftime('%d.%m.%Y %H:%M')}] Waiting {delay}s before checking...")
    time.sleep(delay)
    print(f"[{datetime.now().strftime('%d.%m.%Y %H:%M')}] Checking Grettstadt Umgebung (Miete, LK Schweinfurt)...")

    seen = load_seen()
    listings = []

    try:
        # Pass seen so IS24 fetcher can enrich new listings within the same browser session
        is24 = fetch_is24_listings(already_seen=seen)
        print(f"  IS24: {len(is24)} listing(s)")
        listings.extend(is24)
    except Exception as e:
        print(f"  IS24 error: {e}")

    try:
        iw = fetch_iw_listings()
        print(f"  Immowelt: {len(iw)} listing(s)")
        listings.extend(iw)
    except Exception as e:
        print(f"  Immowelt error: {e}")

    print(f"Total: {len(listings)} listing(s) found")

    new_listings = [l for l in listings if l["id"] not in seen]

    if new_listings:
        print(f"NEW: {len(new_listings)} new listing(s)")

        # Self-check before sending email
        ok_count = sum(1 for l in new_listings if l.get("baujahr", "N/A") != "N/A" or l.get("energy_class", "N/A") != "N/A")
        print(f"  Self-check: {ok_count}/{len(new_listings)} listings have Baujahr or Energieklasse")
        for l in new_listings:
            print(f"    [{l['source']}] addr={l['address']!r} energy={l.get('energy_class')} baujahr={l.get('baujahr')}")

        send_email(new_listings)
        seen.update(l["id"] for l in new_listings)
        save_seen(seen)
        os.system(
            "cd '/Users/zhiziwen/Documents/vibe coding项目/immo-monitor' && "
            "git add seen_grettstadt.json && "
            "git diff --staged --quiet || ("
            "git commit -m 'chore: update seen listings [skip ci]' && "
            "git stash && git pull --rebase && git stash pop && git push)"
        )
    else:
        print("No new listings.")


if __name__ == "__main__":
    main()
