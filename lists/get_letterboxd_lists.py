import os
import json
import re
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LIST_NAMES_FILE = os.path.join(os.path.dirname(BASE_DIR), 'cache', 'list_names.json')

load_dotenv()

LETTERBOXD_USERNAME = os.getenv('LETTERBOXD_USERNAME')
RATE_LIMIT_DELAY = int(os.getenv('LETTERBOXD_LIST_RATE_LIMIT_DELAY', '5'))
MAX_RETRIES = 3
RETRY_DELAY = 30

RATE_LIMIT_LOG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'logs', 'letterboxd-rate-limit'
)

DEFAULT_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}


def log_rate_limit_event(message):
    with open(RATE_LIMIT_LOG, 'a') as f:
        f.write(f"{datetime.now().strftime('%I:%M%p %B %d, %Y')} {message}\n")


def _letterboxd_block_status(response):
    return response is not None and response.status_code in (403, 429)


def _list_link_pattern(username):
    return re.compile(rf'^/{re.escape(username)}/list/([^/]+)/$')


def _parse_list_links_from_html(html, username):
    """
    Letterboxd changed the lists index markup; list links are still stable at
    /{user}/list/{slug}/.
    """
    soup = BeautifulSoup(html, 'html.parser')
    pattern = _list_link_pattern(username)
    lists_by_url = {}

    for anchor in soup.find_all('a', href=True):
        href = anchor['href'].split('?')[0]
        match = pattern.match(href)
        if not match:
            continue
        slug = match.group(1)
        if slug == 'edit':
            continue

        name = anchor.get_text(' ', strip=True)
        if not name or name.lower() == 'edit list':
            name = slug.replace('-', ' ').title()

        url = href if href.startswith('http') else f'https://letterboxd.com{href}'
        lists_by_url[url] = {'name': name, 'url': url}

    return list(lists_by_url.values())


def _extra_lists_from_env(username):
    """Optional manual list URLs/slugs when Letterboxd blocks pagination."""
    raw = os.getenv('LETTERBOXD_EXTRA_LIST_URLS', '').strip()
    if not raw:
        return []

    lists = []
    for entry in raw.split(','):
        entry = entry.strip()
        if not entry:
            continue
        if entry.startswith('http'):
            url = entry.rstrip('/') + '/'
            slug = url.rstrip('/').split('/')[-1]
        else:
            slug = entry.strip('/').split('/')[-1]
            url = f'https://letterboxd.com/{username}/list/{slug}/'
        lists.append({
            'name': slug.replace('-', ' ').title(),
            'url': url,
        })
    return lists


def _merge_lists(*list_groups):
    merged = {}
    for group in list_groups:
        for item in group:
            merged[item['url']] = item
    return list(merged.values())


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

    base_url = f'https://letterboxd.com/{LETTERBOXD_USERNAME}/lists/'
    print(f"[INFO] Scraping lists page: {base_url}")

    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)

    list_objs = []
    blocked = False
    complete = False
    page = 1
    expected_pages = None

    while True:
        page_url = base_url if page == 1 else f"{base_url}page/{page}/"
        print(f"[INFO] Fetching page {page}: {page_url}")

        page_ok = False
        pagination = None
        next_link = None

        for attempt in range(MAX_RETRIES):
            try:
                response = session.get(page_url, timeout=30)
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
                page_lists = _parse_list_links_from_html(response.text, LETTERBOXD_USERNAME)
                list_objs = _merge_lists(list_objs, page_lists)
                print(f"[INFO] Found {len(page_lists)} list(s) on page {page}")

                soup = BeautifulSoup(response.text, 'html.parser')
                pagination = soup.find('div', class_='pagination')
                next_link = pagination.find('a', class_='next') if pagination else None
                if pagination:
                    page_links = pagination.find_all('a', href=True)
                    page_numbers = []
                    for link in page_links:
                        text = link.get_text(strip=True)
                        if text.isdigit():
                            page_numbers.append(int(text))
                    if page_numbers:
                        expected_pages = max(page_numbers)

                page_ok = True
                break
            except requests.exceptions.HTTPError as e:
                if _letterboxd_block_status(e.response):
                    blocked = True
                    print(
                        f"[ERROR] Letterboxd blocked request ({e.response.status_code}) on page {page}"
                    )
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

        if expected_pages and page >= expected_pages:
            complete = True
            break

        page += 1
        time.sleep(RATE_LIMIT_DELAY)

    extra_lists = _extra_lists_from_env(LETTERBOXD_USERNAME)
    if extra_lists:
        print(f"[INFO] Merged {len(extra_lists)} list(s) from LETTERBOXD_EXTRA_LIST_URLS")
        list_objs = _merge_lists(list_objs, extra_lists)

    # If every page was fetched, or there is only one page, treat discovery as complete.
    if not complete and not blocked and page == 1 and not next_link:
        complete = True

    if complete:
        message = f"Discovered {len(list_objs)} Letterboxd list(s)"
        if save_to_cache:
            os.makedirs(os.path.dirname(LIST_NAMES_FILE), exist_ok=True)
            with open(LIST_NAMES_FILE, 'w', encoding='utf-8') as f:
                json.dump(list_objs, f, indent=2)
            print(f"[INFO] Saved list names and URLs to {LIST_NAMES_FILE}")
    elif blocked:
        message = (
            f"Letterboxd blocked list discovery on page {page} "
            f"({len(list_objs)} list(s) found so far). "
            "Add missing lists via LETTERBOXD_EXTRA_LIST_URLS or retry later."
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
