import requests
from bs4 import BeautifulSoup
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os
from datetime import datetime

URL = (
    "https://www.dawonia.de/de/mieten"
    "?order-key=room&sorting=desc&items-per-page=20&items-page-count=1"
    "&city=N%C3%BCrnberg&type=flat&roomNumber=3"
)
SEEN_FILE = "seen_listings.json"
BASE_URL = "https://www.dawonia.de"


def fetch_listings():
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    response = requests.get(URL, headers=headers, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    listings = []

    for item in soup.find_all("div", class_="teaser-item"):
        obj = item.find("div", class_="teaser-object")
        if not obj:
            continue

        listing_id = obj.get("data-object-id")
        if not listing_id:
            continue

        title_el = obj.find("div", class_="teaser-object__headline")
        title = title_el.get_text(strip=True) if title_el else "N/A"

        text_div = obj.find("div", class_="teaser-object__text")
        address = "N/A"
        if text_div:
            first_p = text_div.find("p")
            if first_p:
                address = first_p.get_text(separator=" ", strip=True)

        price_el = obj.find("span", class_="text-color-cyan-01")
        price = price_el.get_text(strip=True) if price_el else "N/A"

        link = obj.find("a", href=True)
        url = f"{BASE_URL}{link['href']}" if link else URL

        listings.append({
            "id": listing_id,
            "title": title,
            "address": address,
            "price": price,
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
    # Support multiple recipients separated by comma
    recipients = [r.strip() for r in os.environ.get("NOTIFY_EMAIL", sender).split(",")]

    count = len(new_listings)
    subject = f"[Dawonia] {count} neue Wohnung{'en' if count > 1 else ''} in Nuernberg!"

    sections = []
    for l in new_listings:
        sections.append(
            f"Titel:   {l['title']}\n"
            f"Adresse: {l['address']}\n"
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
