"""
Monitors Genossenschaft rental listings in Nürnberg & Fürth.

Sites covered:
  Static (requests + BS4):
    wbg Nürnberg           https://wbg.nuernberg.de/mieten/wohnungen/wohnungssuche/
    Bauverein Fürth        https://bauverein-fuerth.de/category/wohnungsangebote/
    Eigenes Heim Fürth     https://bgeh.immomio.online         (Immomio white-label)
    BGSN eG                https://www.bgsn-eg.de/wohnen/mietobjekte/
    WG NORIS               https://wgnoris.de/wohnangebote/
  Static (Immomio GraphQL API):
    WG Fürth·Oberasbach    https://www.wg-fue-oas.de/wohnungsangebote/
  Dynamic (Playwright):
    SWN Nürnberg           https://swnuernberg.de/vermietungsangebote/
    Volkswohl Fürth        https://www.volkswohl-fuerth.de/  (Immosolve iframe)

Filters applied: ≥3 Zimmer, ≥78 m², Energieklasse A+/A/B/C (or unknown), kein WBS
"""

import json
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
    # Skip listings that require a Wohnberechtigungsschein (social housing cert)
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
# wbg Nürnberg  (TYPO3 + OpenImmo, server-rendered)
# Card: div.row containing dt/dd pairs
# DTs: Straße, Nettokaltmiete, Verfügbar ab, Ort, Gesamtmiete, Anzahl Zimmer, Wohnfläche
# ---------------------------------------------------------------------------

def fetch_wbg():
    BASE = "https://wbg.nuernberg.de"
    listings = []
    seen_hrefs = set()  # persists across pages to prevent cross-page duplicates

    for page in range(1, 4):
        if page == 1:
            url = f"{BASE}/mieten/wohnungen/wohnungssuche/"
        else:
            url = (
                f"{BASE}/mieten/wohnungen/wohnungssuche/"
                f"?tx_openimmo_openimmo%5BcurrentPage%5D={page}"
            )
        try:
            soup = _get(url)
        except Exception as e:
            print(f"    wbg page {page}: {e}")
            break

        # Only clean detail links (no query params — PDF / cache links have ?)
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
            listing_id = "wbg_" + path_key.group(1).strip("/").replace("/", "_")

            # Find nearest ancestor div.row that has <dt> elements
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

            # Skip commercial listings (Bürofläche but no Wohnfläche → commercial)
            if "Bürofläche" in details and "Wohnfläche" not in details:
                continue

            card_text = found_card.get_text(" ", strip=True)
            if not _passes_filters(rooms, space, "N/A", title=title, text=card_text):
                continue

            listings.append({
                "id": listing_id,
                "source": "wbg Nürnberg",
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
# Bauverein Fürth  (WordPress — one blog post per listing)
# Card: article > h2.entry-title > a
# ---------------------------------------------------------------------------

def fetch_bauverein_fuerth():
    BASE = "https://bauverein-fuerth.de"
    url = f"{BASE}/category/wohnungsangebote/"
    listings = []

    try:
        soup = _get(url)
    except Exception as e:
        print(f"    Bauverein Fürth: {e}")
        return []

    for article in soup.find_all("article"):
        # Title link is in h2.entry-title > a (second <a> has non-empty text)
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
        listing_id = "bauverein_" + slug

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
            "source": "Bauverein Fürth",
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
# Eigenes Heim Fürth via Immomio  (server-rendered)
# Card text: "Objekt ID: NNN Zimmer: N Wohnfläche: XX,XX m² Verfügbar ab: DD.MM.YYYY Kaltmiete: XXX,XX EUR"
# ---------------------------------------------------------------------------

def fetch_eigenes_heim():
    BASE = "https://bgeh.immomio.online"
    listings = []
    seen_ids = set()

    try:
        soup = _get(BASE + "/")
    except Exception as e:
        print(f"    Eigenes Heim Fürth: {e}")
        return []

    # Cards that contain a valid Objekt ID
    CARD_PATTERN = re.compile(
        r"Objekt ID:\s*([\w\.\-]+)"
        r".*?Zimmer:\s*([\d,]+)"
        r".*?Wohnfläche:\s*([\d,]+)\s*m²"
        r".*?Verfügbar ab:\s*([\d\.]+)"
        r".*?Kaltmiete:\s*([\d,\.]+)\s*EUR",
        re.DOTALL,
    )

    for tag in soup.find_all(string=re.compile(r"Objekt ID:")):
        # Walk up to a card container (has both text and links)
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

        listing_id = "bgeh_" + obj_id

        try:
            rooms = float(rooms_raw.replace(",", "."))
        except ValueError:
            rooms = None

        space = f"{space_raw.replace(',', '.')} m²"
        price = f"{price_raw.replace(',', '.')} €"

        # Title from h3/h2
        title_el = card.find("h3") or card.find("h2")
        title = title_el.get_text(strip=True) if title_el else text[:60]

        # Address from image alt or street pattern
        img = card.find("img", alt=re.compile(r"str\.|straße|weg|gasse", re.I))
        if img:
            address = img.get("alt", "Fürth")
        else:
            m_addr = re.search(
                r"[A-ZÄÖÜ][a-zäöüß\-]+(str\.|straße|weg|gasse|platz|allee)[^\n,]{0,30}",
                text, re.I
            )
            address = m_addr.group(0).strip() if m_addr else "Fürth"

        # Detail link
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
            "source": "Eigenes Heim Fürth",
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
# BGSN eG  (requests + BS4, large page — stream first 2 MB)
# Structure mirrors wbg: div.row with dt/dd, links /wohnen/mietobjekte/<slug>/
# ---------------------------------------------------------------------------

def fetch_bgsn():
    BASE = "https://www.bgsn-eg.de"
    url = f"{BASE}/wohnen/mietobjekte/"
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
        print(f"    BGSN: {e}")
        return []

    # Cards use class "immo_card" (avoid "immo_card_inner" which duplicates)
    seen_ids = set()
    for card in soup.find_all("div", class_="immo_card"):
        # Skip inner wrapper duplicates
        if "immo_card_inner" in (card.get("class") or []):
            continue

        a = card.find("a", href=re.compile(r"/immobiliendetails"))
        if not a:
            continue
        href = a.get("href", "")
        imnr = re.search(r"imnr=([\w\-]+)", href)
        listing_id = "bgsn_" + (imnr.group(1) if imnr else href)
        if listing_id in seen_ids:
            continue
        seen_ids.add(listing_id)

        text = card.get_text(" ", strip=True)
        # Text format: "Title PostalCode City, Type Kaltmiete: X € Zimmer: N Wohnfläche: XX.XX m²"
        title_el = card.find("h3") or card.find("h2") or card.find("strong")
        title = title_el.get_text(strip=True) if title_el else text[:60]

        # Address: postcode + city (before the comma+type)
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
            "source": "BGSN eG",
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
# WG NORIS  (currently no public listings — monitor for future)
# ---------------------------------------------------------------------------

def fetch_wg_noris():
    url = "https://wgnoris.de/wohnangebote/"
    listings = []

    try:
        soup = _get(url)
    except Exception as e:
        print(f"    WG NORIS: {e}")
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
        listing_id = "noris_" + slug

        title_el = card.find("h3") or card.find("h2") or card.find("h4")
        title = title_el.get_text(strip=True) if title_el else text[:60]

        rooms = _extract_rooms(text)
        space = _extract_space(text)

        if not _passes_filters(rooms, space, "N/A", title=title, text=text):
            continue

        listings.append({
            "id": listing_id,
            "source": "WG NORIS",
            "title": title,
            "address": _extract_address(text, "Nürnberg"),
            "rooms": rooms,
            "space": space,
            "price": _extract_price(text),
            "energy_class": "N/A",
            "available": "N/A",
            "url": href if href.startswith("http") else "https://wgnoris.de" + href,
        })

    return listings


# ---------------------------------------------------------------------------
# SWN Nürnberg  (JavaScript-rendered — Playwright)
# ---------------------------------------------------------------------------

def fetch_swn():
    """SWN Nürnberg — AngularJS app, server-rendered after JS execution.
    Listing cards: div.result.ng-scope, links: /vermietungsexpose/?oid=N"""
    BASE = "https://swnuernberg.de"
    url = f"{BASE}/vermietungsangebote/"
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
        print(f"    SWN: {e}")
        return []

    # Individual listing cards: class="result ng-scope" (not the container "resultlist")
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
        listing_id = "swn_" + (oid.group(1) if oid else href)
        if listing_id in seen_ids:
            continue
        seen_ids.add(listing_id)

        title_el = card.find("h2") or card.find("h3") or card.find("strong")
        title = title_el.get_text(strip=True) if title_el else text[:60]

        # Address: "Straße · Postcode City (District)"
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
            "source": "SWN Nürnberg",
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
# Volkswohl Fürth  (immosolve CMS — Playwright, currently shows empty when no vacancies)
# ---------------------------------------------------------------------------

def fetch_volkswohl():
    """Volkswohl Fürth — listings live in an Immosolve iframe.
    Load the iframe URL directly and intercept the REST API response."""
    IFRAME_URL = "https://2907330.hpm.immosolve.eu/?startRoute=result-list&objectIdentifier=2"
    DETAIL_BASE = "https://www.volkswohl-fuerth.de/shu-cms/wohnungsangebote/immosolve/"
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
            page.goto(IFRAME_URL, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(3000)
            browser.close()
    except Exception as e:
        print(f"    Volkswohl Fürth: {e}")
        return []

    data = api_data.get("estates", {})
    for obj in data.get("immoObjects", []):
        lbl = obj.get("labels", {})
        listing_id = "volkswohl_" + str(obj.get("id", ""))

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
            "source": "Volkswohl Fürth",
            "title": title,
            "address": address,
            "rooms": rooms,
            "space": space,
            "price": price,
            "energy_class": "N/A",
            "available": available,
            "url": DETAIL_BASE,
        })

    return listings


# ---------------------------------------------------------------------------
# WG Fürth·Oberasbach  (Immomio "homepage widget" — direct GraphQL API)
# API: https://gql-hp.immomio.com/homepage/graphql
# Token extracted from the Borlabs-blocked iframe on their WordPress page.
# ---------------------------------------------------------------------------

_WG_FUERTH_OAS_TOKEN = (
    "eyJhbGciOiJIUzI1NiJ9"
    ".eyJjdXN0b21lcklkIjo2MjE5NTI5NTksImlkIjo2MjkzMzU5NDcsImNyZWF0ZWQiOjE3MDEwNzc5MzM1MjJ9"
    ".D_sdvXPOq0TUMZ7OFV8anJzJK5XYF3065nxxcxsUA6U"
)

_IMMOMIO_GQL_QUERY = """
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


def fetch_wg_fuerth_oberasbach():
    """WG Fürth·Oberasbach — Immomio GraphQL API, no Playwright needed."""
    listings = []
    page_num = 0
    seen_ids = set()

    while True:
        payload = {
            "operationName": "propertyList",
            "variables": {
                "input": {
                    "page": page_num, "size": 50,
                    "token": _WG_FUERTH_OAS_TOKEN,
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
            "query": _IMMOMIO_GQL_QUERY,
        }

        try:
            r = requests.post(
                "https://gql-hp.immomio.com/homepage/graphql",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Origin": "https://homepage.immomio.com",
                    "Referer": "https://homepage.immomio.com/",
                },
                timeout=20,
            )
            r.raise_for_status()
            data = r.json().get("data", {}).get("propertyList", {})
        except Exception as e:
            print(f"    WG Fürth·Oberasbach: {e}")
            break

        nodes = data.get("nodes", [])
        page_info = data.get("page", {})

        for obj in nodes:
            obj_id = str(obj.get("objectId") or obj.get("externalId") or "")
            if not obj_id or obj_id in seen_ids:
                continue
            seen_ids.add(obj_id)
            listing_id = "wg_fue_oas_" + obj_id

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
            link = obj.get("applicationLink") or "https://www.wg-fue-oas.de/wohnungsangebote/"

            if wbs_flag:
                continue
            if not _passes_filters(rooms, space, "N/A", title=title):
                continue

            listings.append({
                "id": listing_id,
                "source": "WG Fürth·Oberasbach",
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
        f"[Genossenschaft NBG/FÜ] "
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
        f"Neue Mietangebote bei Genossenschaften in Nürnberg & Fürth\n"
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
    ("wbg Nürnberg",          fetch_wbg),
    ("Bauverein Fürth",       fetch_bauverein_fuerth),
    ("Eigenes Heim Fürth",    fetch_eigenes_heim),
    ("BGSN eG",               fetch_bgsn),
    ("WG NORIS",              fetch_wg_noris),
    ("WG Fürth·Oberasbach",   fetch_wg_fuerth_oberasbach),
    ("SWN Nürnberg",          fetch_swn),
    ("Volkswohl Fürth",       fetch_volkswohl),
]


def main():
    import random
    # 0–25 min jitter so hourly runs don't always hit at :00
    delay = random.randint(0, 1500)
    print(f"[{datetime.now().strftime('%d.%m.%Y %H:%M')}] Waiting {delay}s before checking...")
    time.sleep(delay)
    print(f"[{datetime.now().strftime('%d.%m.%Y %H:%M')}] Checking Genossenschaft listings (NBG & FÜ)...")

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
