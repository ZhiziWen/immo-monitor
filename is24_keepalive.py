"""
IS24 keep-alive: visits IS24 pages twice daily with human-like behavior
to keep session cookies fresh and prevent DataDome expiry.

Key changes vs naive keep-alive:
- Runs on-screen (not off-screen) — off-screen is a major DataDome tell
- Scrolls through pages and hovers over listings
- Random timing jitter ± 30 min around scheduled time
- Twice daily (morning + evening via two launchd plists)
"""
import os
import random
import smtplib
import time
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

# Pages to visit — mix of different page types like a real user would
PAGES = [
    "https://www.immobilienscout24.de",
    "https://www.immobilienscout24.de/Suche/de/bayern/schweinfurt-kreis/haus-mieten",
    "https://www.immobilienscout24.de/Suche/radius/haus-mieten"
    "?centerofsearchaddress=Grettstadt;97508;;;;&geocoordinates=49.9476;10.4731;10.0",
    "https://www.immobilienscout24.de/Suche/de/bayern/nuernberg/haus-kaufen",
]


def _human_scroll(page):
    """Simulate human scrolling: slow, irregular, with pauses."""
    total_height = page.evaluate("document.body.scrollHeight") or 3000
    pos = 0
    while pos < min(total_height * 0.7, 2500):
        step = random.randint(150, 400)
        pos += step
        page.evaluate(f"window.scrollTo(0, {pos})")
        time.sleep(random.uniform(0.3, 1.1))
    # Scroll back up a bit — real users often do this
    page.evaluate(f"window.scrollTo(0, {max(0, pos - random.randint(200, 500))})")
    time.sleep(random.uniform(0.5, 1.5))


def _hover_listing(page):
    """Hover over first visible listing card if present."""
    try:
        card = page.query_selector("div.listing-card")
        if card:
            card.hover()
            time.sleep(random.uniform(0.5, 1.5))
    except Exception:
        pass


def send_alert(message):
    project_dir = os.path.dirname(os.path.abspath(__file__))

    # 1. macOS notification
    try:
        os.system(
            "osascript -e 'display notification "
            "\"IS24 Session abgelaufen — Terminal wird geöffnet\" "
            "with title \"Immo Monitor\"'"
        )
    except Exception:
        pass

    # 2. Open Terminal with auth_is24.py
    try:
        launcher = "/tmp/is24_auth_launcher.sh"
        with open(launcher, "w") as f:
            f.write("#!/bin/bash\n")
            f.write(f"cd '{project_dir}' && /opt/anaconda3/bin/python3 auth_is24.py\n")
        os.chmod(launcher, 0o755)
        os.system(f'open -a Terminal "{launcher}"')
        print("Opened Terminal with auth_is24.py")
    except Exception as e:
        print(f"Failed to open Terminal: {e}")

    # 3. Email backup
    try:
        sender = os.environ["GMAIL_USER"]
        password = os.environ["GMAIL_APP_PASSWORD"]
        recipient = "wenzhizi@foxmail.com"
        msg = MIMEMultipart()
        msg["From"] = sender
        msg["To"] = recipient
        msg["Subject"] = "[Immo-Monitor] IS24 Session abgelaufen – Terminal wurde geöffnet"
        msg.attach(MIMEText(message, "plain", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(sender, password)
            smtp.sendmail(sender, [recipient], msg.as_string())
        print(f"Alert sent to {recipient}")
    except Exception as e:
        print(f"Failed to send alert: {e}")


def main():
    # Random jitter ±30 min so we don't hit IS24 at the exact same second every day
    jitter = random.randint(-1800, 1800)
    if jitter > 0:
        print(f"[{datetime.now().strftime('%d.%m.%Y %H:%M')}] Jitter: waiting {jitter}s...")
        time.sleep(jitter)

    print(f"[{datetime.now().strftime('%d.%m.%Y %H:%M')}] IS24 keep-alive running...")

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                PROFILE_DIR,
                headless=False,
                channel="chrome",
                locale="de-DE",
                viewport={"width": 1280, "height": 800},
                # NO off-screen positioning — that's a DataDome tell
                # Window appears briefly but that's fine for a twice-daily script
            )
            page = context.new_page()
            Stealth().apply_stealth_sync(page)

            for url in PAGES:
                print(f"  Visiting {url[:70]}...")
                page.goto(url, wait_until="domcontentloaded", timeout=30000)

                # Wait for page to settle, then act human
                wait = random.randint(3000, 6000)
                page.wait_for_timeout(wait)

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

                _human_scroll(page)
                _hover_listing(page)

                # Random inter-page pause
                inter_wait = random.uniform(4, 10)
                time.sleep(inter_wait)

            context.close()
        print(f"[{datetime.now().strftime('%d.%m.%Y %H:%M')}] Keep-alive done, session refreshed.")

    except Exception as e:
        print(f"Keep-alive error: {e}")


if __name__ == "__main__":
    main()
