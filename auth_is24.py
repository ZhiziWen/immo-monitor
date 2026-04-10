"""
Run this script once to authenticate with ImmobilienScout24.
A Chrome browser window will open — solve the robot challenge manually,
then press Enter in this terminal to save the session.
"""
import os
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

PROFILE_DIR = os.path.expanduser("~/.is24-browser-profile")
SEARCH_URL = (
    "https://www.immobilienscout24.de/Suche/de/bayern/nuernberg/haus-kaufen"
    "?numberofrooms=4.0-&constructionYear=2000-&energyefficiencyclass=A_PLUS,A,B,C,D"
)

def main():
    print(f"Browser profile will be saved to: {PROFILE_DIR}")
    print("A Chrome window will open. Complete any robot/cookie challenge, then press Enter here.")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,
            channel="chrome",
            locale="de-DE",
            viewport={"width": 1280, "height": 800},
        )
        page = context.pages[0] if context.pages else context.new_page()
        Stealth().apply_stealth_sync(page)

        # Navigate to IS24 homepage — less likely to trigger challenge immediately
        page.goto("https://www.immobilienscout24.de", wait_until="domcontentloaded", timeout=30000)
        page.bring_to_front()

        print("\n>>> Browser is open on IS24 homepage.")
        print(">>> 1. Browse around freely (accept cookies, click a few listings)")
        print(">>> 2. If a robot challenge appears, complete it")
        print(">>> 3. When you're satisfied the session is authenticated, come back here and press Enter")
        input("\n>>> Press Enter to save session and exit... ")

        context.close()

    print("Session saved. You can now run immoscout24.py normally.")

if __name__ == "__main__":
    main()
