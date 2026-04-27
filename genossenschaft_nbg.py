import json
import json as _json
import os
import re
import smtplib
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup
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

# Load monitor config
_cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor_config.json")
_CFG = _json.load(open(_cfg_path)) if os.path.exists(_cfg_path) else {}

SEEN_FILE = "seen_genossenschaft.json"

# --- Filters ---
MIN_ROOMS = 3
MIN_SPACE_M2 = 78.0
# Only filter OUT listings where energy class is explicitly known AND bad.
# Unknown ("N/A") listings are included.
ENERGY_OK = {"A+", "A", "B", "C"}
ENERGY_BAD = {"D", "E", "F", "G", "H"}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
}


def _get(url, timeout=20):
    r = requests.get(url, headers=_HEADERS, timeout=timeout)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return BeautifulSoup(r.text, "html.parser")


_WBS_PATTERN = re.compile(
    r"wohnberechtigungsschein|wohnberechtigungs\s*schein|\bwbs\b", re.I
)


def _passes_filters(rooms, space_str, energy_class, title="", text=""):
    """Return True if listing meets minimum criteria."""
    if rooms is not None and rooms < MIN_ROOMS:
        return False
    space_val = _parse_m2(space_str)
    if space_val is not None and space_val < MIN_SPACE_M2:
        return False
    if energy_class and energy_class.upper() in ENERGY_BAD:
        return False
    if _WBS_PATTERN.search(title) or _WBS_PATTERN.search(text):
        return False
    return True


def _parse_m2(space_str):
    if not space_str or space_str == "N/A":
        return None
    m = re.search(r"([\d,\.]+)", space_str)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# S01
# ---------------------------------------------------------------------------

def check_s01():
    base_url = _CFG.get("s01", {}).get("url", "")
    if not base_url:
        return []
    BASE = base_url.rstrip("/").rsplit("/", 3)[0]
    listings = []
    seen_hrefs = set()

    for page in range(1, 4):
        if page == 1:
            url = base_url
        else:
            url = (
                base_url
                + f"?tx_openimmo_openimmo%5BcurrentPage%5D={page}"
            )
        try:
            soup = _get(url)
        except Exception as e:
            print(f"    s01 page {page}: {e}")
            break

        links = [
            a for a in soup.find_all(
                "a", href=re.compile(r"/mieten/wohnungen/wohnungssuche/\d+/\d+")
            )
            if "?" not in a.get("href", "")
        ]
        if not links:
            break

        for a in links:
            href = a.get("href", "").split("?")[0]
            if href in seen_hrefs:
                continue
            seen_hrefs.add(href)

            path_key = re.search(r"/wohnungssuche/(.+)", href)
            if not path_key:
                continue
            listing_id = "s01_" + path_key.group(1).strip("/").replace("/", "_")

            card = a
            found_card = None
            for _ in range(8):
                card = card.parent
                if not card:
                    break
                if card.name == "div" and card.find("dt"):
                    found_card = card
                    break

            if not found_card:
                continue

            details = {}
            for dt in found_card.find_all("dt"):
                label = dt.get_text(strip=True).rstrip(":")
                dd = dt.find_next_sibling("dd")
                if dd:
                    details[label] = dd.get_text(strip=True)

            title_el = found_card.find("h3") or found_card.find("h2")
            title = title_el.get_text(strip=True) if title_el else details.get("Straße", href)

            street = details.get("Straße", "")
            city = details.get("Ort", "Nürnberg")
            address = f"{street}, {city}".strip(", ")

            price_raw = details.get("Nettokaltmiete", "")
            price = price_raw if price_raw else "N/A"

            rooms_raw = details.get("Anzahl Zimmer", "")
            try:
                rooms = float(rooms_raw.replace(",", "."))
            except (ValueError, AttributeError):
                rooms = None

            space_raw = details.get("Wohnfläche", details.get("Bürofläche", ""))
            if space_raw:
                space = space_raw if "m²" in space_raw else f"{space_raw} m²"
            else:
                space = "N/A"

            available = details.get("Verfügbar ab", "N/A")

            if "Bürofläche" in details and "Wohnfläche" not in details:
                continue

            card_text = found_card.get_text(" ", strip=True)
            if not _passes_filters(rooms, space, "N/A", title=title, text=card_text):
                continue

            listings.append({
                "id": listing_id,
                "source": _CFG.get("s01", {}).get("label", "S01"),
                "title": title,
                "address": address,
                "rooms": rooms,
                "space": space,
                "price": price,
                "energy_class": "N/A",
                "available": available,
                "url": BASE + href,
            })

        time.sleep(1)

    return listings


# ---------------------------------------------------------------------------
# S02
# ---------------------------------------------------------------------------

def check_s02():
    base_url = _CFG.get("s02", {}).get("url", "")
    if not base_url:
        return []
    BASE = base_url.rstrip("/").rsplit("/category", 1)[0]
    url = base_url
    listings = []

    try:
        soup = _get(url)
    except Exception as e:
        print(f"    s02: {e}")
        return []

    for article in soup.find_all("article"):
        title_el = article.find("h2") or article.find("h3")
        if not title_el:
            continue
        a = title_el.find("a", href=True)
        if not a:
            continue

        href = a.get("href", "")
        title = a.get_text(strip=True)
        if not title:
            continue

        slug = href.rstrip("/").split("/")[-1]
        listing_id = "s02_" + slug

        text = article.get_text(" ", strip=True)

        m_rooms = re.search(r"(\d+(?:[,\.]\d+)?)\s*-?\s*Zimmer", text, re.IGNORECASE)
        rooms = float(m_rooms.group(1).replace(",", ".")) if m_rooms else None

        m_space = re.search(r"([\d,\.]+)\s*m²", text)
        space = f"{m_space.group(1)} m²" if m_space else "N/A"

        m_price = re.search(r"([\d\.]+(?:,\d+)?)\s*€", text)
        price = f"{m_price.group(1)} €" if m_price else "N/A"

        m_addr = re.search(r"\d{5}\s+(?:Fürth|Nürnberg)[^<\n,]{0,40}", text)
        address = m_addr.group(0).strip() if m_addr else "Fürth"

        if not _passes_filters(rooms, space, "N/A", title=title, text=text):
            continue

        listings.append({
            "id": listing_id,
            "source": _CFG.get("s02", {}).get("label", "S02"),
            "title": title,
            "address": address,
            "rooms": rooms,
            "space": space,
            "price": price,
            "energy_class": "N/A",
            "available": "N/A",
            "url": href,
        })

    return listings


# ---------------------------------------------------------------------------
# S03
# ---------------------------------------------------------------------------

def check_s03():
    base_url = _CFG.get("s03", {}).get("url", "")
    if not base_url:
        return []
    BASE = base_url.rstrip("/")
    listings = []
    seen_ids = set()

    try:
        soup = _get(BASE + "/")
    except Exception as e:
        print(f"    s03: {e}")
        return []

    CARD_PATTERN = re.compile(
        r"Objekt ID:\s*([\w\.\-]+)"
        r".*?Zimmer:\s*([\d,]+)"
        r".*?Wohnfläche:\s*([\d,]+)\s*m²"
        r".*?Verfügbar ab:\s*([\d\.]+)"
        r".*?Kaltmiete:\s*([\d,\.]+)\s*EUR",
        re.DOTALL,
    )

    for tag in soup.find_all(string=re.compile(r"Objekt ID:")):
        card = tag
        for _ in range(6):
            card = card.parent
            if not card:
                break
            if card.name in ("div", "article") and card.find("a"):
                break

        if not card:
            continue

        text = card.get_text(" ", strip=True)
        m = CARD_PATTERN.search(text)
        if not m:
            continue

        obj_id, rooms_raw, space_raw, avail, price_raw = m.groups()
        if obj_id in seen_ids:
            continue
        seen_ids.add(obj_id)

        listing_id = "s03_" + obj_id

        try:
            rooms = float(rooms_raw.replace(",", "."))
        except ValueError:
            rooms = None

        space = f"{space_raw.replace(',', '.')} m²"
        price = f"{price_raw.replace(',', '.')} €"

        title_el = card.find("h3") or card.find("h2")
        title = title_el.get_text(strip=True) if title_el else text[:60]

        img = card.find("img", alt=re.compile(r"str\.|straße|weg|gasse", re.I))
        if img:
            address = img.get("alt", "Fürth")
        else:
            m_addr = re.search(
                r"[A-ZÄÖÜ][a-zäöüß\-]+(str\.|straße|weg|gasse|platz|allee)[^\n,]{0,30}",
                text, re.I
            )
            address = m_addr.group(0).strip() if m_addr else "Fürth"

        detail_link = card.find("a", string=re.compile(r"Details|Detail|Expose", re.I))
        if not detail_link:
            detail_link = card.find("a", href=re.compile(r"/expose|/details|/property", re.I))
        link = detail_link.get("href", "") if detail_link else ""
        if link and not link.startswith("http"):
            link = BASE + link

        if not _passes_filters(rooms, space, "N/A", title=title, text=text):
            continue

        listings.append({
            "id": listing_id,
            "source": _CFG.get("s03", {}).get("label", "S03"),
            "title": title,
            "address": address,
            "rooms": rooms,
            "space": space,
            "price": price,
            "energy_class": "N/A",
            "available": avail,
            "url": link or BASE,
        })

    return listings


# ---------------------------------------------------------------------------
# S04
# ---------------------------------------------------------------------------

def check_s04():
    base_url = _CFG.get("s04", {}).get("url", "")
    if not base_url:
        return []
    BASE = base_url.rstrip("/").rsplit("/wohnen", 1)[0]
    url = base_url
    listings = []

    try:
        r = requests.get(url, headers=_HEADERS, timeout=30, stream=True)
        r.raise_for_status()
        content = b""
        for chunk in r.iter_content(chunk_size=32768):
            content += chunk
            if len(content) > 2 * 1024 * 1024:
                break
        soup = BeautifulSoup(content, "html.parser")
    except Exception as e:
        print(f"    s04: {e}")
        return []

    seen_ids = set()
    for card in soup.find_all("div", class_="immo_card"):
        if "immo_card_inner" in (card.get("class") or []):
            continue

        a = card.find("a", href=re.compile(r"/immobiliendetails"))
        if not a:
            continue
        href = a.get("href", "")
        imnr = re.search(r"imnr=([\w\-]+)", href)
        listing_id = "s04_" + (imnr.group(1) if imnr else href)
        if listing_id in seen_ids:
            continue
        seen_ids.add(listing_id)

        text = card.get_text(" ", strip=True)
        title_el = card.find("h3") or card.find("h2") or card.find("strong")
        title = title_el.get_text(strip=True) if title_el else text[:60]

        m_addr = re.search(r"(\d{5}\s+\w+(?:\s+\w+)?)", text)
        address = m_addr.group(1).strip() if m_addr else "Nürnberg"

        m_price = re.search(r"Kaltmiete:\s*([\d\.,]+)\s*€", text)
        price = f"{m_price.group(1)} €" if m_price else _extract_price(text)

        m_rooms = re.search(r"Zimmer:\s*([\d,\.]+)", text)
        try:
            rooms = float(m_rooms.group(1).replace(",", ".")) if m_rooms else None
        except ValueError:
            rooms = None

        m_space = re.search(r"Wohnfläche:\s*([\d,\.]+)\s*m²", text)
        space = f"{m_space.group(1)} m²" if m_space else "N/A"

        if not _passes_filters(rooms, space, "N/A", title=title, text=text):
            continue

        listings.append({
            "id": listing_id,
            "source": _CFG.get("s04", {}).get("label", "S04"),
            "title": title,
            "address": address,
            "rooms": rooms,
            "space": space,
            "price": price,
            "energy_class": "N/A",
            "available": "N/A",
            "url": BASE + href if href.startswith("/") else href,
        })

    return listings


# ---------------------------------------------------------------------------
# S05
# ---------------------------------------------------------------------------

def check_s05():
    url = _CFG.get("s05", {}).get("url", "")
    if not url:
        return []
    listings = []

    try:
        soup = _get(url)
    except Exception as e:
        print(f"    s05: {e}")
        return []

    for card in soup.find_all(
        ["article", "div"],
        class_=re.compile(r"listing|property|card|offer|angebot|expose", re.I),
    ):
        text = card.get_text(" ", strip=True)
        if not re.search(r"€|Zimmer|m²|Miete", text):
            continue

        a = card.find("a", href=True)
        href = a.get("href", "") if a else ""
        slug = href.rstrip("/").split("/")[-1] or re.sub(r"\W+", "_", text[:40])
        listing_id = "s05_" + slug

        title_el = card.find("h3") or card.find("h2") or card.find("h4")
        title = title_el.get_text(strip=True) if title_el else text[:60]

        rooms = _extract_rooms(text)
        space = _extract_space(text)

        if not _passes_filters(rooms, space, "N/A", title=title, text=text):
            continue

        listings.append({
            "id": listing_id,
            "source": _CFG.get("s05", {}).get("label", "S05"),
            "title": title,
            "address": _extract_address(text, "Nürnberg"),
            "rooms": rooms,
            "space": space,
            "price": _extract_price(text),
            "energy_class": "N/A",
            "available": "N/A",
            "url": href if href.startswith("http") else url.rstrip("/").rsplit("/", 1)[0] + href,
        })

    return listings


# ---------------------------------------------------------------------------
# S13
# ---------------------------------------------------------------------------

def check_s13():
    """S13 — AngularJS app, server-rendered after JS execution."""
    base_url = _CFG.get("s13", {}).get("url", "")
    if not base_url:
        return []
    BASE = base_url.rstrip("/").rsplit("/", 1)[0]
    url = base_url
    listings = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=_HEADERS["User-Agent"],
                locale="de-DE",
                viewport={"width": 1280, "height": 800},
            )
            page = ctx.new_page()
            Stealth().apply_stealth_sync(page)
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(5000)
            soup = BeautifulSoup(page.content(), "html.parser")
            browser.close()
    except Exception as e:
        print(f"    s13: {e}")
        return []

    seen_ids = set()
    for card in soup.find_all("div", class_="result"):
        classes = card.get("class") or []
        if "resultlist" in classes or "resultlist_objects" in classes:
            continue
        if "ng-scope" not in classes:
            continue

        text = card.get_text(" ", strip=True)
        if not re.search(r"€|Zimmer|m²", text):
            continue

        a = card.find("a", href=re.compile(r"/vermietungsexpose/"))
        if not a:
            continue
        href = a.get("href", "")
        oid = re.search(r"oid=(\d+)", href)
        listing_id = "s13_" + (oid.group(1) if oid else href)
        if listing_id in seen_ids:
            continue
        seen_ids.add(listing_id)

        title_el = card.find("h2") or card.find("h3") or card.find("strong")
        title = title_el.get_text(strip=True) if title_el else text[:60]

        m_addr = re.search(r"([A-ZÄÖÜ][^\n·]+·\s*\d{5}\s+\w+[^\n]*)", text)
        address = m_addr.group(1).strip() if m_addr else _extract_address(text, "Nürnberg")

        rooms = _extract_rooms(text)
        space_m = re.search(r"(\d+)\s*m²\s*Wohnfläche", text)
        space = f"{space_m.group(1)} m²" if space_m else _extract_space(text)

        price_m = re.search(r"([\d,\.]+)\s*€\s*Kaltmiete", text)
        price = f"{price_m.group(1)} €" if price_m else _extract_price(text)

        if not _passes_filters(rooms, space, "N/A", title=title, text=text):
            continue

        listings.append({
            "id": listing_id,
            "source": _CFG.get("s13", {}).get("label", "S13"),
            "title": title,
            "address": address,
            "rooms": rooms,
            "space": space,
            "price": price,
            "energy_class": "N/A",
            "available": "N/A",
            "url": href if href.startswith("http") else BASE + href,
        })

    return listings


# ---------------------------------------------------------------------------
# S14
# ---------------------------------------------------------------------------

def check_s14():
    """S14 — listings loaded via iframe intercepted API."""
    iframe_url = _CFG.get("s14", {}).get("url", "")
    detail_base = _CFG.get("s14", {}).get("detail_url", "")
    if not iframe_url:
        return []
    listings = []

    api_data = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=_HEADERS["User-Agent"],
                locale="de-DE",
                viewport={"width": 1280, "height": 800},
            )
            page = ctx.new_page()

            def on_response(resp):
                if "estates/list" in resp.url:
                    try:
                        api_data["estates"] = resp.json()
                    except Exception:
                        pass

            page.on("response", on_response)
            page.goto(iframe_url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(3000)
            browser.close()
    except Exception as e:
        print(f"    s14: {e}")
        return []

    data = api_data.get("estates", {})
    for obj in data.get("immoObjects", []):
        lbl = obj.get("labels", {})
        listing_id = "s14_" + str(obj.get("id", ""))

        title = lbl.get("titel", "").strip()
        address = f"{lbl.get('strasse','')} {lbl.get('hausnummer','')}, {lbl.get('plz','')} {lbl.get('ort','')}".strip()

        rooms_raw = lbl.get("anzahlZimmer", "")
        try:
            rooms = float(rooms_raw.replace(",", "."))
        except (ValueError, AttributeError):
            rooms = None

        space_raw = lbl.get("wohnflaeche", "")
        space = f"{str(space_raw).replace('.', ',')} m²" if space_raw else "N/A"

        price_raw = lbl.get("monatlGesamtkosten", "")
        price = f"{price_raw} €" if price_raw else "N/A"

        available = lbl.get("availableStart", "N/A")

        if not _passes_filters(rooms, space, "N/A", title=title):
            continue

        listings.append({
            "id": listing_id,
            "source": _CFG.get("s14", {}).get("label", "S14"),
            "title": title,
            "address": address,
            "rooms": rooms,
            "space": space,
            "price": price,
            "energy_class": "N/A",
            "available": available,
            "url": detail_base or iframe_url,
        })

    return listings


# ---------------------------------------------------------------------------
# S06
# ---------------------------------------------------------------------------

def check_s06():
    base_url = _CFG.get("s06", {}).get("url", "")
    if not base_url:
        return []
    BASE = base_url.rstrip("/").rsplit("/exposes", 1)[0]
    url = base_url
    listings = []
    seen_ids = set()

    try:
        soup = _get(url)
    except Exception as e:
        print(f"    s06: {e}")
        return []

    for card in soup.find_all("div", class_=lambda c: c and "card-border-round" in c):
        if card.find_parent("div", class_=lambda c: c and "card-border-round" in c):
            continue

        text = card.get_text(" ", strip=True)
        if not re.search(r"Wo\.Nr\.", text):
            continue

        m_id = re.search(r"Wo\.Nr\.\s*(\d+)", text)
        if not m_id:
            continue
        wo_nr = m_id.group(1)
        if wo_nr in seen_ids:
            continue
        seen_ids.add(wo_nr)
        listing_id = "s06_" + wo_nr

        link_el = card.find("a", href=re.compile(r"/wohnungen/"))
        url_detail = (BASE + link_el["href"] if link_el and not link_el["href"].startswith("http")
                      else (link_el["href"] if link_el else BASE + "/exposes/"))

        m_price = re.search(r"€\s*([\d\.,]+)", text)
        price = f"{m_price.group(1)} €" if m_price else "N/A"

        m_space = re.search(r"([\d,]+)\s*m²", text)
        space = f"{m_space.group(1)} m²" if m_space else "N/A"

        m_rooms = re.search(r"(\d+(?:[,\.]\d+)?)\s*[-–]\s*Zimmer", text, re.I)
        if not m_rooms:
            m_rooms = re.search(r"(\d+(?:[,\.]\d+)?)\s*Zimmer", text, re.I)
        try:
            rooms = float(m_rooms.group(1).replace(",", ".")) if m_rooms else None
        except ValueError:
            rooms = None

        m_addr = re.search(r"([A-ZÄÖÜ][a-zäöüß\-]+(?:str\.|straße|weg|gasse|platz|allee)[^,\n]{0,25},\s*\d{5}\s+\w+)", text, re.I)
        if not m_addr:
            m_addr = re.search(r"(\d{5}\s+[\w\-]+)", text)
        address = m_addr.group(1).strip() if m_addr else "Erlangen"

        m_avail = re.search(r"frei ab\s+([\d\.]+)", text, re.I)
        available = m_avail.group(1) if m_avail else "N/A"

        m_title = re.search(r"€[\d\.,]+\s+(.+?)\s+Wo\.Nr\.", text)
        title = m_title.group(1).strip() if m_title else text[:60]

        if not _passes_filters(rooms, space, "N/A", title=title, text=text):
            continue

        listings.append({
            "id": listing_id,
            "source": _CFG.get("s06", {}).get("label", "S06"),
            "title": title,
            "address": address,
            "rooms": rooms,
            "space": space,
            "price": price,
            "energy_class": "N/A",
            "available": available,
            "url": url_detail,
        })

    return listings


# ---------------------------------------------------------------------------
# S07
# ---------------------------------------------------------------------------

def check_s07():
    base_url = _CFG.get("s07", {}).get("url", "")
    if not base_url:
        return []
    BASE = base_url.rstrip("/").rsplit("/wohnungen", 1)[0]
    listings = []

    try:
        soup = _get(base_url)
    except Exception as e:
        print(f"    s07: {e}")
        return []

    seen_ids = set()
    for card in soup.find_all(class_="type-mietangebote"):
        classes = card.get("class", [])
        if "category-wohnungen" not in classes and "category-reihenhaus" not in classes:
            continue

        post_id = next((c.replace("post-", "") for c in classes if c.startswith("post-") and c != "post-type-archive"), None)
        if not post_id or post_id in seen_ids:
            continue
        seen_ids.add(post_id)
        listing_id = "s07_" + post_id

        text = card.get_text(" ", strip=True)

        section = card.find("section", attrs={"data-ha-element-link": True})
        if section:
            import json as _json2
            try:
                link_data = _json2.loads(section["data-ha-element-link"])
                url_detail = link_data.get("url", base_url)
            except Exception:
                url_detail = base_url
        else:
            url_detail = base_url

        title_el = card.find(["h1", "h2", "h3", "h4"])
        title = title_el.get_text(strip=True) if title_el else text[:60]

        m_rooms = re.search(r"(\d+(?:[,\.]\d+)?)\s*[-–]?\s*Zimmer", title, re.I)
        try:
            rooms = float(m_rooms.group(1).replace(",", ".")) if m_rooms else None
        except ValueError:
            rooms = None

        m_space = re.search(r"ca\.\s*([\d,\.]+)\s*(?:qm|m²)", text, re.I)
        if not m_space:
            m_space = re.search(r"([\d,\.]+)\s*(?:qm|m²)", text)
        space = f"{m_space.group(1).replace(',','.')} m²" if m_space else "N/A"

        m_price = re.search(r"Gesamtmiete:\s*([\d\.,]+)\s*€", text)
        price = f"{m_price.group(1)} €" if m_price else "N/A"

        m_addr = re.search(r"([A-ZÄÖÜ][a-zäöüß\-]+(?:str\.|straße|weg|gasse|platz|allee)[^,\n]{0,20},\s*\d{5}\s+\w+)", text, re.I)
        if not m_addr:
            m_addr = re.search(r"(\d{5}\s+\w+)", text)
        address = m_addr.group(1).strip() if m_addr else "Nürnberg"

        if not _passes_filters(rooms, space, "N/A", title=title, text=text):
            continue

        listings.append({
            "id": listing_id,
            "source": _CFG.get("s07", {}).get("label", "S07"),
            "title": title,
            "address": address,
            "rooms": rooms,
            "space": space,
            "price": price,
            "energy_class": "N/A",
            "available": "N/A",
            "url": url_detail,
        })

    return listings


# ---------------------------------------------------------------------------
# S08  (Immomio GraphQL API)
# ---------------------------------------------------------------------------

_S08_GQL_QUERY = """
query propertyList($input: HomepagePropertySearchRequest!) {
  propertyList(input: $input) {
    page { totalElements hasNext page }
    nodes {
      name totalRooms size totalRentGross externalId objectId wbs
      applicationLink status marketingType
      availableFrom { dateAvailable stringAvailable }
      address { city street houseNumber zipCode district }
    }
  }
}
"""


def check_s08():
    """S08 — GraphQL API, no Playwright needed."""
    gql_url = _CFG.get("s08", {}).get("gql", "")
    token = _CFG.get("s08", {}).get("token", "")
    fallback_url = _CFG.get("s08", {}).get("url", "")
    if not gql_url or not token:
        return []
    listings = []
    page_num = 0
    seen_ids = set()

    while True:
        payload = {
            "operationName": "propertyList",
            "variables": {
                "input": {
                    "page": page_num, "size": 50,
                    "token": token,
                    "propertyType": None, "wbs": None,
                    "barrierFree": None, "balconyOrTerrace": None,
                    "roomNumber": {"from": None, "to": None},
                    "floor": {"from": None, "to": None},
                    "totalRentGross": {"from": None, "to": None},
                    "salesPrice": {"from": None, "to": None},
                    "propertySize": {"from": None, "to": None},
                    "externalId": None,
                    "sort": ["created,desc"],
                    "area": None, "marketingType": None,
                }
            },
            "query": _S08_GQL_QUERY,
        }

        try:
            r = requests.post(
                gql_url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Origin": _CFG.get("s08", {}).get("gql_origin", ""),
                    "Referer": _CFG.get("s08", {}).get("gql_origin", "") + "/",
                },
                timeout=20,
            )
            r.raise_for_status()
            data = r.json().get("data", {}).get("propertyList", {})
        except Exception as e:
            print(f"    s08: {e}")
            break

        nodes = data.get("nodes", [])
        page_info = data.get("page", {})

        for obj in nodes:
            obj_id = str(obj.get("objectId") or obj.get("externalId") or "")
            if not obj_id or obj_id in seen_ids:
                continue
            seen_ids.add(obj_id)
            listing_id = "s08_" + obj_id

            title = (obj.get("name") or "").strip()
            addr = obj.get("address") or {}
            address = (
                f"{addr.get('street','')} {addr.get('houseNumber','')}, "
                f"{addr.get('zipCode','')} {addr.get('city','')}"
            ).strip(", ")

            try:
                rooms = float(obj.get("totalRooms") or 0) or None
            except (ValueError, TypeError):
                rooms = None

            size_val = obj.get("size")
            space = f"{size_val} m²" if size_val else "N/A"

            rent = obj.get("totalRentGross")
            price = f"{rent} €" if rent else "N/A"

            avail_raw = obj.get("availableFrom") or {}
            available = avail_raw.get("dateAvailable") or avail_raw.get("stringAvailable") or "N/A"

            wbs_flag = obj.get("wbs") or False
            link = obj.get("applicationLink") or fallback_url

            if wbs_flag:
                continue
            if not _passes_filters(rooms, space, "N/A", title=title):
                continue

            listings.append({
                "id": listing_id,
                "source": _CFG.get("s08", {}).get("label", "S08"),
                "title": title or address,
                "address": address,
                "rooms": rooms,
                "space": space,
                "price": price,
                "energy_class": "N/A",
                "available": available,
                "url": link,
            })

        if not page_info.get("hasNext"):
            break
        page_num += 1
        time.sleep(0.5)

    return listings


# ---------------------------------------------------------------------------
# Shared helper: Immowelt HomepageModul  (S09 + S10)
# ---------------------------------------------------------------------------

def _check_hm_widget(url, source_label, id_prefix):
    if not url:
        return []
    listings = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=_HEADERS["User-Agent"],
                locale="de-DE",
                viewport={"width": 1280, "height": 800},
            )
            page = ctx.new_page()
            Stealth().apply_stealth_sync(page)
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(5000)
            soup = BeautifulSoup(page.content(), "html.parser")
            browser.close()
    except Exception as e:
        print(f"    {id_prefix}: {e}")
        return []

    seen_ids = set()
    for card in soup.find_all("div", class_="hm_listbox"):
        text = card.get_text(" ", strip=True)

        if "Wohnfläche" not in text and re.search(r"Verkaufs|Büro|Gewerbe|Lager|Nutz", text, re.I):
            continue

        m_uuid = re.search(r'ToExpose\("([A-F0-9\-]{36})"\)', str(card), re.I)
        if not m_uuid:
            m_uuid = re.search(r'ToExpose\([\"\']([^"\']+)[\"\']', str(card))
        if m_uuid:
            uid = m_uuid.group(1).upper().replace("-", "")
        else:
            uid = re.sub(r"\W+", "_", text[:40])
        if uid in seen_ids:
            continue
        seen_ids.add(uid)
        listing_id = f"{id_prefix}_{uid}"

        uuid_clean = uid if "-" not in uid else uid.replace("-", "")
        url_detail = f"https://www.immowelt.de/expose/{uuid_clean}"

        m_title = re.match(r"^(.+?)\s+[\d\.,]+\s+€", text)
        title = m_title.group(1).strip() if m_title else text[:80]

        m_price = re.search(r"([\d\.]+(?:,\d+)?)\s*€", text)
        price = f"{m_price.group(1)} €" if m_price else "N/A"

        m_space = re.search(r"([\d,\.]+)\s*m²\s*Wohnfläche", text)
        if not m_space:
            m_space = re.search(r"([\d,\.]+)\s*m²", text)
        space = f"{m_space.group(1)} m²" if m_space else "N/A"

        m_rooms = re.search(r"(\d+(?:[,\.]\d+)?)\s+Zimmer", text)
        try:
            rooms = float(m_rooms.group(1).replace(",", ".")) if m_rooms else None
        except ValueError:
            rooms = None

        m_addr = re.search(r"(\d{5}\s+\w[\w\s\-]*\(\w+\)|\d{5}\s+\w[\w\s\-]+)", text)
        address = m_addr.group(1).strip() if m_addr else source_label

        if not _passes_filters(rooms, space, "N/A", title=title, text=text):
            continue

        listings.append({
            "id": listing_id,
            "source": source_label,
            "title": title,
            "address": address,
            "rooms": rooms,
            "space": space,
            "price": price,
            "energy_class": "N/A",
            "available": "N/A",
            "url": url_detail,
        })

    return listings


def check_s09():
    return _check_hm_widget(
        _CFG.get("s09", {}).get("url", ""),
        _CFG.get("s09", {}).get("label", "S09"),
        "s09",
    )


def check_s10():
    return _check_hm_widget(
        _CFG.get("s10", {}).get("url", ""),
        _CFG.get("s10", {}).get("label", "S10"),
        "s10",
    )


# ---------------------------------------------------------------------------
# S11
# ---------------------------------------------------------------------------

def check_s11():
    url = _CFG.get("s11", {}).get("url", "")
    if not url:
        return []
    listings = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=_HEADERS["User-Agent"], locale="de-DE")
            page = ctx.new_page()
            page.goto(url, wait_until="networkidle", timeout=25000)
            page.wait_for_timeout(3000)
            soup = BeautifulSoup(page.content(), "html.parser")
            browser.close()
    except Exception as e:
        print(f"    s11: {e}")
        return []

    seen_ids = set()
    for div in soup.find_all("div", class_="et_pb_text_inner"):
        text = div.get_text(" ", strip=True)
        if not re.search(r"m²|qm|Zimmer|Wohnfläche|€", text):
            continue

        for block in re.split(r"\n{2,}|(?=\d[\-–]\s*Zimmer)", text):
            block = block.strip()
            if not block or not re.search(r"m²|Zimmer|€", block):
                continue

            m_rooms = re.search(r"(\d+(?:[,\.]\d+)?)\s*[-–]?\s*Zimmer", block, re.I)
            m_space = re.search(r"([\d,\.]+)\s*(?:m²|qm)", block)
            m_price = re.search(r"([\d\.]+(?:,\d+)?)\s*€", block)

            try:
                rooms = float(m_rooms.group(1).replace(",", ".")) if m_rooms else None
            except ValueError:
                rooms = None

            space = f"{m_space.group(1)} m²" if m_space else "N/A"
            price = f"{m_price.group(1)} €" if m_price else "N/A"

            uid = re.sub(r"\W+", "_", block[:50])
            if uid in seen_ids:
                continue
            seen_ids.add(uid)
            listing_id = "s11_" + uid

            if not _passes_filters(rooms, space, "N/A", title=block, text=block):
                continue

            listings.append({
                "id": listing_id,
                "source": _CFG.get("s11", {}).get("label", "S11"),
                "title": block[:80],
                "address": _extract_address(block, "Nürnberg"),
                "rooms": rooms,
                "space": space,
                "price": price,
                "energy_class": "N/A",
                "available": "N/A",
                "url": url,
            })

    return listings


# ---------------------------------------------------------------------------
# S12
# ---------------------------------------------------------------------------

def check_s12():
    url = _CFG.get("s12", {}).get("url", "")
    if not url:
        return []
    listings = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=_HEADERS["User-Agent"], locale="de-DE")
            page = ctx.new_page()
            page.goto(url, wait_until="networkidle", timeout=25000)
            page.wait_for_timeout(3000)
            soup = BeautifulSoup(page.content(), "html.parser")
            browser.close()
    except Exception as e:
        print(f"    s12: {e}")
        return []

    realtor_div = None
    for section in soup.find_all("section", class_="widget_wgn_html_widget"):
        if "realtor" in str(section):
            realtor_div = section.find("div", class_="wgn-realtor")
            break

    if not realtor_div:
        return []

    text = realtor_div.get_text(" ", strip=True)
    if re.search(r"keine Angebote|nicht verfügbar|derzeit keine", text, re.I):
        if not re.search(r"m²|Zimmer|€", text):
            return []

    seen_ids = set()
    for card in realtor_div.find_all(["div", "article", "tr", "li"]):
        card_text = card.get_text(" ", strip=True)
        if not re.search(r"m²|Zimmer|€", card_text):
            continue
        if len(card_text) < 20:
            continue

        m_rooms = re.search(r"(\d+(?:[,\.]\d+)?)\s*[-–]?\s*Zimmer", card_text, re.I)
        m_space = re.search(r"([\d,\.]+)\s*m²", card_text)
        m_price = re.search(r"([\d\.]+(?:,\d+)?)\s*€", card_text)
        try:
            rooms = float(m_rooms.group(1).replace(",", ".")) if m_rooms else None
        except ValueError:
            rooms = None
        space = f"{m_space.group(1)} m²" if m_space else "N/A"
        price = f"{m_price.group(1)} €" if m_price else "N/A"

        uid = re.sub(r"\W+", "_", card_text[:50])
        if uid in seen_ids:
            continue
        seen_ids.add(uid)

        link = card.find("a", href=True)
        url_detail = link["href"] if link else url

        if not _passes_filters(rooms, space, "N/A", title=card_text, text=card_text):
            continue

        listings.append({
            "id": "s12_" + uid,
            "source": _CFG.get("s12", {}).get("label", "S12"),
            "title": card_text[:80],
            "address": _extract_address(card_text, "Nürnberg"),
            "rooms": rooms,
            "space": space,
            "price": price,
            "energy_class": "N/A",
            "available": "N/A",
            "url": url_detail,
        })

    return listings


# ---------------------------------------------------------------------------
# Shared text-extraction helpers
# ---------------------------------------------------------------------------

def _extract_price(text):
    m = re.search(r"([\d\.]+(?:,\d+)?)\s*€", text)
    return f"{m.group(1)} €" if m else "N/A"


def _extract_rooms(text):
    m = re.search(r"(\d+(?:[,\.]\d+)?)\s*Zimmer", text, re.I)
    if not m:
        m = re.search(r"(\d+(?:[,\.]\d+)?)\s*Zi\b", text, re.I)
    try:
        return float(m.group(1).replace(",", ".")) if m else None
    except (ValueError, AttributeError):
        return None


def _extract_space(text):
    m = re.search(r"([\d,\.]+)\s*m²", text)
    return f"{m.group(1)} m²" if m else "N/A"


def _extract_address(text, fallback_city="Nürnberg"):
    m = re.search(
        r"[A-ZÄÖÜ][a-zäöüß\-]+(str\.|straße|weg|gasse|platz|allee)[^\n,]{0,30}",
        text, re.I
    )
    if m:
        return m.group(0).strip()
    m2 = re.search(r"\d{5}\s+\w+", text)
    return m2.group(0).strip() if m2 else fallback_city


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
    subject = (
        f"[Monitor A] "
        f"{count} neue{'s' if count == 1 else ''} Mietangebot{'' if count == 1 else 'e'}!"
    )

    sections = []
    for l in new_listings:
        sections.append(
            f"Quelle:       {l['source']}\n"
            f"Titel:        {l['title']}\n"
            f"Adresse:      {l['address']}\n"
            f"Zimmer:       {l.get('rooms', 'N/A')}\n"
            f"Fläche:       {l['space']}\n"
            f"Kaltmiete:    {l['price']}\n"
            f"Energieklasse:{l.get('energy_class', 'N/A')}\n"
            f"Frei ab:      {l['available']}\n"
            f"Link:         {l['url']}"
        )

    body = (
        f"Neue Mietangebote\n"
        f"(≥{MIN_ROOMS} Zi, ≥{MIN_SPACE_M2:.0f} m², Energieklasse ≤C)\n"
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

FETCHERS = [
    ("S01", check_s01),
    ("S02", check_s02),
    ("S03", check_s03),
    ("S04", check_s04),
    ("S05", check_s05),
    ("S06", check_s06),
    ("S07", check_s07),
    ("S08", check_s08),
    ("S09", check_s09),
    ("S10", check_s10),
    ("S11", check_s11),
    ("S12", check_s12),
    ("S13", check_s13),
    ("S14", check_s14),
]


def main():
    print(f"[{datetime.now().strftime('%d.%m.%Y %H:%M')}] Checking listings...")

    seen = load_seen()
    all_listings = []

    for name, fetcher in FETCHERS:
        try:
            results = fetcher()
            print(f"  {name}: {len(results)} listing(s) after filters")
            all_listings.extend(results)
        except Exception as e:
            print(f"  {name} error: {e}")

    print(f"Total: {len(all_listings)} listing(s)")

    new_listings = [l for l in all_listings if l["id"] not in seen]

    if new_listings:
        print(f"NEW: {len(new_listings)} new listing(s)")
        for l in new_listings:
            print(f"  + [{l['source']}] {l['title'][:55]} | {l['rooms']}Zi | {l['space']} | {l['price']}")
        send_email(new_listings)
        seen.update(l["id"] for l in new_listings)
        save_seen(seen)
        os.system(
            "cd '/Users/zhiziwen/Documents/vibe coding项目/immo-monitor' && "
            "git add seen_genossenschaft.json && "
            "git diff --staged --quiet || ("
            "git commit -m 'chore: update seen listings [skip ci]' && "
            "git stash && git pull --rebase && git stash pop && git push)"
        )
    else:
        print("No new listings.")


if __name__ == "__main__":
    main()
