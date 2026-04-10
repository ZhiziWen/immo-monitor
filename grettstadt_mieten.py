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
IS24_URLS = [
    # Häuser zur Miete im Landkreis Schweinfurt
    "https://www.immobilienscout24.de/Suche/de/bayern/schweinfurt-kreis/haus-mieten",
    # Wohnungen ab 90 m² zur Miete im Landkreis Schweinfurt
    "https://www.immobilienscout24.de/Suche/de/bayern/schweinfurt-kreis/wohnung-mieten?livingspace=90.0-",
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

def fetch_is24_listings():
    listings = []
    seen_ids = set()

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            IS24_PROFILE_DIR,
            headless=False,
            channel="chrome",
            locale="de-DE",
            viewport={"width": 1280, "height": 800},
            args=["--window-position=-2000,0"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        Stealth().apply_stealth_sync(page)

        for url in IS24_URLS:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(5000)
            title = page.title()
            if "Roboter" in title:
                context.close()
                raise RuntimeError(
                    "IS24 robot challenge — run auth_is24.py to refresh session"
                )
            soup = BeautifulSoup(page.content(), "html.parser")
            listings.extend(_parse_is24(soup, seen_ids))

        context.close()

    return listings


def _parse_is24(soup, seen_ids):
    # Card text format: [Badge] Title Price Area Rooms [Grundstück m²] [EnergyClass] Address [PriceTag]
    # Example: "Neu von privat Titel 1.500 € 150 m² 5,5 Zi. 560 m² Bahnhofstraße 24, Werneck, Schweinfurt (Kreis) Guter Preis"
    PRICE_TAGS = r"(?:Guter Preis|Sehr guter Preis|Nur exklusiv|Angemessener Preis|Hoher Preis|$)"
    ENERGY_PATTERN = r"(A\+|A|B|C|D|E|F|G|H)"

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
        PRICE_TAGS = r"(?:Guter Preis|Sehr guter Preis|Nur exklusiv|Angemessener Preis|Hoher Preis)"
        m_addr = re.match(rf"(.+?)(?:\s+{PRICE_TAGS})?\s*$", after_last_m2.strip())
        address = m_addr.group(1).strip() if m_addr and m_addr.group(1).strip() else "N/A"

        # Filter: only Landkreis Schweinfurt listings
        if IS24_REQUIRED_LANDKREIS not in address:
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


def _fetch_baujahr(url):
    """Fetch construction year from IS24 or Immowelt expose page via requests."""
    import requests
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"}
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return "N/A"
        soup = BeautifulSoup(r.text, "html.parser")
        # IS24 expose: look for "Baujahr" label followed by value
        for el in soup.find_all(string=re.compile(r"Baujahr", re.I)):
            parent = el.parent
            # Try sibling or next element
            nxt = parent.find_next(string=re.compile(r"\b(1[89]\d{2}|20[012]\d)\b"))
            if nxt:
                m = re.search(r"\b(1[89]\d{2}|20[012]\d)\b", nxt)
                if m:
                    return m.group(1)
            # Try parent text
            m = re.search(r"\b(1[89]\d{2}|20[012]\d)\b", parent.get_text())
            if m:
                return m.group(1)
        return "N/A"
    except Exception:
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

    listings = []

    try:
        is24 = fetch_is24_listings()
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

    seen = load_seen()
    new_listings = [l for l in listings if l["id"] not in seen]

    if new_listings:
        print(f"NEW: {len(new_listings)} new listing(s)")
        for l in new_listings:
            print(f"  + [{l['source']}] {l['title'][:60]} — {l['price']}")
        # Fetch Baujahr from expose pages (requests works on detail pages)
        print("  Fetching Baujahr from expose pages...")
        for l in new_listings:
            l["baujahr"] = _fetch_baujahr(l["url"])
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
