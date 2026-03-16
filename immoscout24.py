from bs4 import BeautifulSoup
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os
import re
from datetime import datetime
from playwright.sync_api import sync_playwright

# Load .env if present (for local Mac runs via launchd)
_env_file = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_file):
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

# --- Configuration ---
SEARCH_URL = (
    "https://www.immobilienscout24.de/Suche/de/bayern/nuernberg/haus-kaufen"
    "?numberofrooms=4.0-&constructionYear=2000-&energyefficiencyclass=A_PLUS,A,B,C,D"
)
SEEN_FILE = "seen_immoscout24.json"
BASE_URL = "https://www.immobilienscout24.de"

# Persistent browser profile — user must run auth_is24.py once to pass the
# robot challenge manually. Subsequent headless runs reuse the saved session.
PROFILE_DIR = os.path.expanduser("~/.is24-browser-profile")

# --- Filters ---
MIN_ROOMS = 4
MIN_CONSTRUCTION_YEAR = 2000
ENERGY_CLASSES_OK = {"A_PLUS", "A", "B", "C", "D"}


def fetch_listings():
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,
            channel="chrome",
            locale="de-DE",
            viewport={"width": 1280, "height": 800},
            args=["--window-position=-2000,0"],  # off-screen, won't bother user
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(5000)
        title = page.title()
        if "Roboter" in title:
            context.close()
            raise RuntimeError(
                "IS24 robot challenge detected — run auth_is24.py locally to refresh session"
            )
        content = page.content()
        context.close()

    soup = BeautifulSoup(content, "html.parser")
    return _parse_listings(soup)


def _parse_listings(soup):
    listings = []
    seen_ids = set()

    for card in soup.select("div.listing-card[data-obid]"):
        listing_id = card.get("data-obid", "").strip()
        if not listing_id or listing_id in seen_ids:
            continue
        seen_ids.add(listing_id)

        url = f"{BASE_URL}/expose/{listing_id}"

        # Title
        title_el = card.select_one("div.card-listing-title, h5, h2, [class*='title']")
        # Fallback: get text around the price area
        text = card.get_text(" ", strip=True)

        # Price — find first "NNN.NNN €" pattern
        m_price = re.search(r"[\d\.]+\.?\d*\s*€", text)
        price_str = m_price.group(0).strip() if m_price else "N/A"

        # Rooms — "N Zi." or "N Zimmer"
        m_rooms = re.search(r"(\d+(?:[,\.]\d+)?)\s*Zi(?:mmer)?\.?", text)
        rooms = float(m_rooms.group(1).replace(",", ".")) if m_rooms else None

        # Space — first "NNN m²" before Grundstück
        m_space = re.search(r"([\d,]+)\s*m²", text)
        space_str = f"{m_space.group(1)} m²" if m_space else "N/A"

        # Energy class — single letter label like "A" or "A+"
        energy_el = card.select_one("[class*='energy'], [data-testid*='energy']")
        energy_class = energy_el.get_text(strip=True) if energy_el else ""
        if not energy_class:
            m_energy = re.search(r"\b(A\+|A|B|C|D|E|F|G|H)\b", text)
            energy_class = m_energy.group(1) if m_energy else ""

        # Address — last meaningful line (usually city/district)
        addr_el = card.select_one("[class*='address'], address")
        if addr_el:
            address = addr_el.get_text(strip=True)
        else:
            # Heuristic: find comma-separated location after last price mention
            m_addr = re.search(r"(?:[\d\.]+ m²[^,]*,\s*)(.+?Nürnberg[^<\n]*)", text)
            address = m_addr.group(1).strip() if m_addr else "N/A"

        # Apply filters
        if rooms is not None and rooms < MIN_ROOMS:
            continue
        if energy_class and energy_class not in ENERGY_CLASSES_OK:
            continue

        listings.append({
            "id": listing_id,
            "title": text[:80],
            "address": address,
            "rooms": rooms,
            "space": space_str,
            "price": price_str,
            "energy_class": energy_class or "N/A",
            "construction_year": "N/A",
            "url": url,
        })

    return listings


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_seen(seen_ids):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen_ids), f, indent=2)


def send_email(new_listings):
    sender = os.environ["GMAIL_USER"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    recipients = [r.strip() for r in os.environ.get("NOTIFY_EMAIL", sender).split(",")]

    count = len(new_listings)
    subject = f"[ImmoScout24 Nürnberg] {count} neue{'s' if count == 1 else ''} Haus zum Kauf!"

    sections = []
    for l in new_listings:
        sections.append(
            f"Titel:    {l['title']}\n"
            f"Adresse:  {l['address']}\n"
            f"Zimmer:   {l['rooms']}\n"
            f"Fläche:   {l['space']}\n"
            f"Preis:    {l['price']}\n"
            f"Energie:  {l['energy_class']}\n"
            f"Link:     {l['url']}"
        )

    body = (
        f"Neue Häuser auf ImmobilienScout24 "
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


def main():
    import random, time
    delay = random.randint(0, 600)  # 0–10 minutes random delay
    print(f"[{datetime.now().strftime('%d.%m.%Y %H:%M')}] Waiting {delay}s before checking...")
    time.sleep(delay)
    print(f"[{datetime.now().strftime('%d.%m.%Y %H:%M')}] Checking ImmobilienScout24 (Nürnberg, Haus kaufen)...")

    listings = fetch_listings()
    print(f"Found {len(listings)} listing(s) matching filters")

    seen = load_seen()
    new_listings = [l for l in listings if l["id"] not in seen]

    if new_listings:
        print(f"NEW: {len(new_listings)} new listing(s)")
        for l in new_listings:
            print(f"  + [{l['id']}] {l['title'][:60]} — {l['price']}")
        send_email(new_listings)
        seen.update(l["id"] for l in new_listings)
        save_seen(seen)
    else:
        print("No new listings.")


if __name__ == "__main__":
    main()
