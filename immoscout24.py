from bs4 import BeautifulSoup
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os
from datetime import datetime
from playwright.sync_api import sync_playwright

# --- Configuration ---
SEARCH_URL = (
    "https://www.immobilienscout24.de/Suche/de/bayern/nuernberg/haus-kaufen"
    "?numberofrooms=4.0-&constructionYear=2000-&energyefficiencyclass=A_PLUS,A,B,C,D"
)
SEEN_FILE = "seen_immoscout24.json"
BASE_URL = "https://www.immobilienscout24.de"

# --- Filters (applied after URL filters as a safety net) ---
MIN_ROOMS = 4
MIN_CONSTRUCTION_YEAR = 2000
# Energy classes D or better (A+ is best, H is worst)
ENERGY_CLASSES_OK = {"A_PLUS", "A", "B", "C", "D"}


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
        )
        page = context.new_page()
        page.goto(SEARCH_URL, wait_until="networkidle", timeout=60000)
        content = page.content()
        browser.close()

    soup = BeautifulSoup(content, "html.parser")

    # IS24 is a Next.js app — search results are embedded as JSON in __NEXT_DATA__
    next_data_tag = soup.find("script", id="__NEXT_DATA__")
    if next_data_tag and next_data_tag.string:
        listings = _parse_next_data(next_data_tag.string)
        if listings is not None:
            return listings

    # Fallback: parse HTML result list directly
    print("Warning: __NEXT_DATA__ not found or empty, falling back to HTML parsing")
    return _parse_html(soup)


def _parse_next_data(json_str):
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        print(f"Warning: Could not parse __NEXT_DATA__ JSON: {e}")
        return None

    listings = []
    try:
        search_response = data["props"]["pageProps"].get("searchResponse", {})
        result_entries = (
            search_response
            .get("resultlist.resultlist", {})
            .get("resultlistEntries", [{}])
        )
        if not result_entries:
            return None

        result_list = result_entries[0].get("resultlistEntry", [])
        if not result_list:
            print("No entries found in __NEXT_DATA__")
            return []

        for entry in result_list:
            listing_id = str(entry.get("@id", entry.get("id", ""))).strip()
            if not listing_id:
                continue

            re_data = entry.get("resultlist.realEstate", {})

            # Build expose URL
            urls = re_data.get("urls", [])
            url = urls[0].get("@href", "") if urls else ""
            if not url:
                url = f"{BASE_URL}/expose/{listing_id}"

            title = re_data.get("title", "N/A")

            # Price
            price_obj = re_data.get("price", {})
            price_val = price_obj.get("value")
            price_str = f"{int(price_val):,} €".replace(",", ".") if price_val else "N/A"

            # Address
            addr = re_data.get("address", {})
            street = addr.get("street", "")
            house_number = addr.get("houseNumber", "")
            city = addr.get("city", "")
            quarter = addr.get("quarter", "")
            address_parts = [f"{street} {house_number}".strip(), quarter, city]
            address = ", ".join(p for p in address_parts if p)

            # Rooms, space, year, energy
            rooms = re_data.get("numberOfRooms")
            living_space = re_data.get("livingSpace")
            space_str = f"{living_space:.0f} m²" if living_space else "N/A"
            construction_year = re_data.get("constructionYear")
            energy_class = re_data.get("energyEfficiencyClass", "")

            # Apply filters (safety net — URL already filters, but data may vary)
            if rooms is not None and rooms < MIN_ROOMS:
                continue
            if construction_year is not None and construction_year < MIN_CONSTRUCTION_YEAR:
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
                "construction_year": construction_year or "N/A",
                "url": url,
            })

    except (KeyError, IndexError, TypeError) as e:
        print(f"Warning: Unexpected __NEXT_DATA__ structure: {e}")
        return None

    return listings


def _parse_html(soup):
    """Fallback HTML parser for IS24 result list items."""
    listings = []

    for item in soup.select("li[data-id]"):
        listing_id = item.get("data-id", "").strip()
        if not listing_id:
            continue

        # Skip promoted showcase entries (no real ID or different URL pattern)
        if item.get("data-is-ad") or "showcase" in " ".join(item.get("class", [])):
            continue

        title_el = item.select_one("h5, h2, .result-list-entry__brand-title")
        title = title_el.get_text(strip=True) if title_el else "N/A"

        address_el = item.select_one(".result-list-entry__address, address")
        address = address_el.get_text(strip=True) if address_el else "N/A"

        price_el = item.select_one(
            ".result-list-entry__primary-criterion dd, "
            "[data-testid='price'] span"
        )
        price_str = price_el.get_text(strip=True) if price_el else "N/A"

        url = f"{BASE_URL}/expose/{listing_id}"

        listings.append({
            "id": listing_id,
            "title": title,
            "address": address,
            "rooms": None,
            "space": "N/A",
            "price": price_str,
            "energy_class": "N/A",
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
    subject = (
        f"[IS24 Nürnberg] {count} neue{'s' if count == 1 else ''} Haus zum Kauf!"
    )

    sections = []
    for l in new_listings:
        sections.append(
            f"Titel:    {l['title']}\n"
            f"Adresse:  {l['address']}\n"
            f"Zimmer:   {l['rooms']}\n"
            f"Fläche:   {l['space']}\n"
            f"Preis:    {l['price']}\n"
            f"Baujahr:  {l['construction_year']}\n"
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
    print(f"[{datetime.now().strftime('%d.%m.%Y %H:%M')}] Checking ImmobilienScout24 (Nürnberg, Haus kaufen)...")

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
