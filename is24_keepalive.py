"""
IS24 keep-alive: visits IS24 homepage once daily to refresh session cookies.
Keeps the persistent browser profile active so the main scrapers don't hit
robot challenges as often.
"""
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# Load .env
_env_file = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_file):
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

PROFILE_DIR = os.path.expanduser("~/.is24-browser-profile")

# A few pages to visit to simulate a real user session
PAGES = [
    "https://www.immobilienscout24.de",
    "https://www.immobilienscout24.de/Suche/de/bayern/schweinfurt-kreis/haus-mieten",
    "https://www.immobilienscout24.de/Suche/de/bayern/nuernberg/haus-kaufen",
]


def send_alert(message):
    try:
        sender = os.environ["GMAIL_USER"]
        password = os.environ["GMAIL_APP_PASSWORD"]
        recipient = "wenzhizi@foxmail.com"
        msg = MIMEMultipart()
        msg["From"] = sender
        msg["To"] = recipient
        msg["Subject"] = "[Immo-Monitor] IS24 Session abgelaufen – bitte neu authentifizieren"
        msg.attach(MIMEText(message, "plain", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(sender, password)
            smtp.sendmail(sender, [recipient], msg.as_string())
        print(f"Alert sent to {recipient}")
    except Exception as e:
        print(f"Failed to send alert: {e}")


def main():
    print(f"[{datetime.now().strftime('%d.%m.%Y %H:%M')}] IS24 keep-alive running...")

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                PROFILE_DIR,
                headless=False,
                channel="chrome",
                locale="de-DE",
                viewport={"width": 1280, "height": 800},
                args=["--window-position=-2000,0"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            Stealth().apply_stealth_sync(page)

            for url in PAGES:
                print(f"  Visiting {url}")
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3000)

                title = page.title()
                if "Roboter" in title:
                    context.close()
                    send_alert(
                        f"Keep-alive hat eine Robot-Challenge erkannt.\n\n"
                        f"Bitte führe folgenden Befehl aus:\n\n"
                        f"  cd '/Users/zhiziwen/Documents/vibe coding项目/immo-monitor' && "
                        f"/opt/anaconda3/bin/python3 auth_is24.py\n\n"
                        f"Zeitpunkt: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
                    )
                    print("Robot challenge detected — alert sent.")
                    return

            context.close()
        print(f"[{datetime.now().strftime('%d.%m.%Y %H:%M')}] Keep-alive done, session refreshed.")

    except Exception as e:
        print(f"Keep-alive error: {e}")


if __name__ == "__main__":
    main()
