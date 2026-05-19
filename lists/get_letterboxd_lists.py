import os
import json
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from dotenv import load_dotenv

# Setup paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LISTS_DIR = BASE_DIR
os.makedirs(LISTS_DIR, exist_ok=True)

LIST_NAMES_FILE = os.path.join(os.path.dirname(BASE_DIR), 'cache', 'list_names.json')

load_dotenv()

LETTERBOXD_USERNAME = os.getenv('LETTERBOXD_USERNAME')

RATE_LIMIT_DELAY = 2
MAX_RETRIES = 3
RETRY_DELAY = 30

RATE_LIMIT_LOG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'logs', 'letterboxd-rate-limit'
)


def log_rate_limit_event(message):
    with open(RATE_LIMIT_LOG, 'a') as f:
        f.write(f"{datetime.now().strftime('%I:%M%p %B %d, %Y')} {message}\n")


def _letterboxd_block_status(response):
    """Return True if Letterboxd blocked or rate-limited this request."""
    return response is not None and response.status_code in (403, 429)


def discover_letterboxd_lists(save_to_cache=True):
    """
    Scrape all pages of the user's Letterboxd lists index.

    Returns dict with keys:
      - lists: list of {name, url}
      - complete: True only if every index page was fetched (not blocked mid-run)
      - blocked: True if a 403/429 stopped discovery
      - message: human-readable status

    list_names.json is written only when complete=True.
    """
    if not LETTERBOXD_USERNAME:
        return {
            'lists': [],
            'complete': False,
            'blocked': False,
            'message': 'LETTERBOXD_USERNAME not set in .env',
        }

    base_url = f"https://letterboxd.com/{LETTERBOXD_USERNAME}/lists/"
    print(f"[INFO] Scraping lists page: {base_url}")

    list_objs = []
    blocked = False
    complete = False
    page = 1

    while True:
        page_url = base_url if page == 1 else f"{base_url}page/{page}/"
        print(f"[INFO] Fetching page {page}: {page_url}")

        page_ok = False
        pagination = None
        next_link = None

        for attempt in range(MAX_RETRIES):
            try:
                response = requests.get(page_url, timeout=30)
                if response.status_code == 429:
                    blocked = True
                    log_rate_limit_event(
                        f"Rate limited on lists index page {page}, attempt {attempt + 1}"
                    )
                    print(f"[ERROR] Letterboxd rate limit (429) on page {page}")
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(RETRY_DELAY)
                    continue
                if response.status_code == 403:
                    blocked = True
                    print(f"[ERROR] Letterboxd blocked request (403) on page {page}")
                    break

                response.raise_for_status()
                soup = BeautifulSoup(response.text, 'html.parser')
                for section in soup.find_all('section', class_='list'):
                    h2 = section.find('h2', class_='title-2')
                    if h2:
                        a = h2.find('a', href=True)
                        if a:
                            name = a.get_text(strip=True)
                            url = a['href']
                            if url.startswith('/'):
                                url = f"https://letterboxd.com{url}"
                            list_objs.append({'name': name, 'url': url})

                pagination = soup.find('div', class_='pagination')
                next_link = pagination.find('a', class_='next') if pagination else None
                page_ok = True
                break
            except requests.exceptions.HTTPError as e:
                if _letterboxd_block_status(e.response):
                    blocked = True
                    print(f"[ERROR] Letterboxd blocked request ({e.response.status_code}) on page {page}")
                    break
                print(f"[ERROR] HTTP error on attempt {attempt + 1}: {e}")
                if attempt < MAX_RETRIES - 1:
                    print(f"[RETRY] Waiting {RATE_LIMIT_DELAY} seconds before retry...")
                    time.sleep(RATE_LIMIT_DELAY)
                else:
                    print(f"[FATAL] All {MAX_RETRIES} attempts failed for page {page}")
            except requests.exceptions.RequestException as e:
                print(f"[ERROR] Request failed on attempt {attempt + 1}: {e}")
                if attempt < MAX_RETRIES - 1:
                    print(f"[RETRY] Waiting {RATE_LIMIT_DELAY} seconds before retry...")
                    time.sleep(RATE_LIMIT_DELAY)
                else:
                    print(f"[FATAL] All {MAX_RETRIES} attempts failed for page {page}")

        if blocked or not page_ok:
            if not page_ok and not blocked:
                print("[ERROR] Stopping list discovery due to repeated request failures.")
            break

        if not next_link:
            complete = True
            break

        page += 1
        time.sleep(RATE_LIMIT_DELAY)

    if complete:
        message = f"Discovered {len(list_objs)} Letterboxd list(s) across {page} page(s)"
        if save_to_cache:
            os.makedirs(os.path.dirname(LIST_NAMES_FILE), exist_ok=True)
            with open(LIST_NAMES_FILE, 'w', encoding='utf-8') as f:
                json.dump(list_objs, f, indent=2)
            print(f"[INFO] Saved list names and URLs to {LIST_NAMES_FILE}")
    elif blocked:
        message = (
            f"Letterboxd blocked list discovery (stopped on page {page}); "
            "Plex playlists will not be updated"
        )
        print(f"[WARN] {message}")
    else:
        message = (
            f"Letterboxd list discovery incomplete (stopped on page {page}); "
            "Plex playlists will not be updated"
        )
        print(f"[WARN] {message}")

    return {
        'lists': list_objs,
        'complete': complete,
        'blocked': blocked,
        'message': message,
    }


def test_scrape_lists_page_to_json():
    """Backward-compatible wrapper used by older callers."""
    return discover_letterboxd_lists(save_to_cache=True)
