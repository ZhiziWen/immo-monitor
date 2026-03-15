from bs4 import BeautifulSoup
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os
import re
from datetime import datetime
from playwright.sync_api import sync_playwright

# --- Configuration ---
SEARCH_URL = (
    "https://www.immowelt.de/suche/nuernberg/haeuser/kaufen"
    "?rooms_from=4"
)
SEEN_FILE = "seen_immowelt.json"
BASE_URL = "https://www.immowelt.de"

# --- Filters (applied in code) ---
MIN_ROOMS = 4
MIN_CONSTRUCTION_YEAR = 2000
ENERGY_CLASSES_OK = {"A+", "A", "B", "C", "D"}


def fetch_listings():
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
        page.goto(SEARCH_URL, wait_until="networkidle", timeout=60000)
        content = page.content()
        browser.close()

    soup = BeautifulSoup(content, "html.parser")
    return _parse_listings(soup)


def _parse_listings(soup):
    listings = []
    cards = soup.select('[data-testid="serp-core-classified-card-testid"]')

    for card in cards:
        # URL and ID
        link = card.select_one('a[data-testid="card-mfe-covering-link-testid"]')
        if not link:
            continue
        url = link["href"].split("?")[0]
        # ID from expose UUID
        m = re.search(r"/expose/([a-f0-9\-]+)", url)
        listing_id = m.group(1) if m else url

        title = link.get("title", "N/A")

        # Price — take only the first price value before second €
        price_el = card.select_one('[data-testid="cardmfe-price-testid"]')
        price_str = "N/A"
        if price_el:
            raw = price_el.get_text(strip=True)
            m_price = re.match(r"([\d\.\,]+\s*€)", raw)
            if m_price:
                price_str = m_price.group(1).strip()
            else:
                # Take everything up to €/m²
                price_str = re.split(r"\d+[,\.]?\d*\s*€/m", raw)[0].strip()

        # Address
        addr_el = card.select_one('[data-testid="cardmfe-description-box-address"]')
        address = addr_el.get_text(strip=True) if addr_el else "N/A"

        # Key facts: "6 Zimmer·138,4 m²·..."
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

        # Energy class
        energy_el = card.select_one('[data-testid="card-mfe-energy-performance-class"]')
        energy_class = energy_el.get_text(strip=True) if energy_el else ""

        # Apply filters
        if rooms is not None and rooms < MIN_ROOMS:
            continue
        if energy_class and energy_class not in ENERGY_CLASSES_OK:
            continue

        listings.append({
            "id": listing_id,
            "title": title,
            "address": address,
            "rooms": rooms,
            "space": space_str,
            "price": price_str,
            "energy_class": energy_class or "N/A",
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
    subject = f"[Immowelt Nürnberg] {count} neue{'s' if count == 1 else ''} Haus zum Kauf!"

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
        f"Neue Häuser auf Immowelt "
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
    print(f"[{datetime.now().strftime('%d.%m.%Y %H:%M')}] Checking Immowelt (Nürnberg, Haus kaufen)...")

    listings = fetch_listings()
    print(f"Found {len(listings)} listing(s) matching filters")

    seen = load_seen()
    new_listings = [l for l in listings if l["id"] not in seen]

    if new_listings:
        print(f"NEW: {len(new_listings)} new listing(s)")
        for l in new_listings:
            print(f"  + [{l['id']}] {l['title']} — {l['price']}")
        send_email(new_listings)
        seen.update(l["id"] for l in new_listings)
        save_seen(seen)
    else:
        print("No new listings.")


if __name__ == "__main__":
    main()
