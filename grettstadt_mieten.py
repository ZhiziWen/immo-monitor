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
    # Wohnungen ab 90 m² zur Miete
    "https://www.immobilienscout24.de/Suche/de/bayern/schweinfurt-kreis/wohnung-mieten?livingspace=90.0-",
]

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
    listings = []
    for card in soup.select("div.listing-card[data-obid]"):
        listing_id = "is24_" + card.get("data-obid", "").strip()
        if not listing_id or listing_id in seen_ids:
            continue
        seen_ids.add(listing_id)

        url = f"{IS24_BASE_URL}/expose/{listing_id[5:]}"
        text = card.get_text(" ", strip=True)

        m_price = re.search(r"[\d\.]+\.?\d*\s*€", text)
        price_str = m_price.group(0).strip() if m_price else "N/A"

        m_rooms = re.search(r"(\d+(?:[,\.]\d+)?)\s*Zi(?:mmer)?\.?", text)
        rooms = float(m_rooms.group(1).replace(",", ".")) if m_rooms else None

        m_space = re.search(r"([\d,]+)\s*m²", text)
        space_str = f"{m_space.group(1)} m²" if m_space else "N/A"

        addr_el = card.select_one("[class*='address'], address")
        if addr_el:
            address = addr_el.get_text(strip=True)
        else:
            m_addr = re.search(r"(?:[\d\.]+ m²[^,]*,\s*)(.+?(?:Schweinfurt|Grettstadt)[^<\n]*)", text)
            address = m_addr.group(1).strip() if m_addr else "N/A"

        listings.append({
            "id": listing_id,
            "source": "IS24",
            "title": text[:80],
            "address": address,
            "rooms": rooms,
            "space": space_str,
            "price": price_str,
            "url": url,
        })

    return listings


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

        listings.append({
            "id": listing_id,
            "source": "Immowelt",
            "title": title,
            "address": address,
            "rooms": rooms,
            "space": space_str,
            "price": price_str,
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
            f"Quelle:   {l['source']}\n"
            f"Titel:    {l['title']}\n"
            f"Adresse:  {l['address']}\n"
            f"Zimmer:   {l['rooms']}\n"
            f"Fläche:   {l['space']}\n"
            f"Miete:    {l['price']}\n"
            f"Link:     {l['url']}"
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
