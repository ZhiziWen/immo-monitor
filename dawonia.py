import requests
from bs4 import BeautifulSoup
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os
from datetime import datetime

BASE_URL_TEMPLATE = (
    "https://www.dawonia.de/de/mieten"
    "?order-key=room&sorting=desc&items-per-page=20&items-page-count={page}"
    "&city=N%C3%BCrnberg&type=flat&roomNumber=3"
)
SEEN_FILE = "seen_dawonia.json"
BASE_URL = "https://www.dawonia.de"
MAX_PAGES = 5  # fetch up to 5 pages

# --- Filters (edit to customize) ---
# Only notify for listings in these cities (case-insensitive). Empty list = all cities.
FILTER_CITIES = ["Nürnberg", "Nuremberg"]
# Minimum number of rooms. Set to 0 to disable.
MIN_ROOMS = 3
# Max monthly rent in EUR (Kaltmiete). Set to 0 to disable.
MAX_PRICE = 0


def fetch_listings():
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    listings = []
    seen_ids = set()

    for page in range(1, MAX_PAGES + 1):
        url = BASE_URL_TEMPLATE.format(page=page)
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        items = soup.find_all("div", class_="teaser-item")
        if not items:
            break  # no more pages

        for item in items:
            obj = item.find("div", class_="teaser-object")
            if not obj:
                continue

            listing_id = obj.get("data-object-id")
            if not listing_id or listing_id in seen_ids:
                continue
            seen_ids.add(listing_id)

            title_el = obj.find("div", class_="teaser-object__headline")
            title = title_el.get_text(strip=True) if title_el else "N/A"

            text_div = obj.find("div", class_="teaser-object__text")
            address = "N/A"
            city = ""
            if text_div:
                first_p = text_div.find("p")
                if first_p:
                    address = first_p.get_text(separator=" ", strip=True)
                    city_span = first_p.find("span", class_="text-uppercase")
                    if city_span:
                        city = city_span.get_text(strip=True)

            price_el = obj.find("span", class_="text-color-cyan-01")
            price = price_el.get_text(strip=True) if price_el else "N/A"

            # Extract room count — accept integers and decimals like "3,5"
            rooms = None
            if text_div:
                for span in text_div.find_all("span", class_="text-bold"):
                    try:
                        rooms = float(span.get_text(strip=True).replace(",", "."))
                        break
                    except ValueError:
                        continue

            link = obj.find("a", href=True)
            if not link or "/real-estate/" not in link["href"]:
                continue  # skip ads and non-listing content
            listing_url = f"{BASE_URL}{link['href']}"

            # City filter
            if FILTER_CITIES:
                if not any(c.lower() == city.lower() for c in FILTER_CITIES):
                    continue

            # Rooms filter — skip only if rooms parsed successfully and is too low
            if MIN_ROOMS and rooms is not None and rooms < MIN_ROOMS:
                continue

            # Price filter
            if MAX_PRICE and price != "N/A":
                try:
                    amount = float(price.replace("Kaltmiete:", "").replace("€", "").replace(".", "").replace(",", ".").strip())
                    if amount > MAX_PRICE:
                        continue
                except ValueError:
                    pass

            listings.append({
                "id": listing_id,
                "title": title,
                "address": address,
                "rooms": rooms,
                "price": price,
                "url": listing_url,
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
    # Support multiple recipients separated by comma
    recipients = [r.strip() for r in os.environ.get("NOTIFY_EMAIL", sender).split(",")]

    count = len(new_listings)
    subject = f"[Dawonia] {count} neue Wohnung{'en' if count > 1 else ''} in Nuernberg!"

    sections = []
    for l in new_listings:
        sections.append(
            f"Titel:   {l['title']}\n"
            f"Adresse: {l['address']}\n"
            f"Zimmer:  {l['rooms']}\n"
            f"Miete:   {l['price']}\n"
            f"Link:    {l['url']}"
        )

    body = (
        f"Neue Wohnungen gefunden ({datetime.now().strftime('%d.%m.%Y %H:%M')}):\n\n"
        + "\n\n" + ("-" * 60) + "\n\n".join(sections)
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
    print(f"[{datetime.now().strftime('%d.%m.%Y %H:%M')}] Checking Dawonia listings...")

    listings = fetch_listings()
    print(f"Found {len(listings)} listing(s) on page")

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
