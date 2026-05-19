from bs4 import BeautifulSoup
import requests
import re
import time
import json
import os
from tqdm import tqdm
from dotenv import load_dotenv
import datetime
import math
import logging
from urllib.parse import urlparse

import os
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, 'cache')
os.makedirs(CACHE_DIR, exist_ok=True)

CACHE_FILE = os.path.join(CACHE_DIR, 'letterboxd_watchlist_cache.json')
TMDB_API_KEY = os.getenv('TMDB_API_KEY', '')  # Optional TMDB API key for better lookups

# Rate limiting configuration
RATE_LIMIT_DELAY = 2  # seconds between requests
MAX_RETRIES = 3
RETRY_DELAY = 30  # seconds to wait on rate limit

RATE_LIMIT_LOG = os.path.join(BASE_DIR, 'logs', 'letterboxd-rate-limit')
def log_rate_limit_event(message):
    with open(RATE_LIMIT_LOG, 'a') as f:
        f.write(f"{datetime.datetime.now().strftime('%I:%M%p %B %d, %Y')} {message}\n")

def load_cache():
    """
    Load the film cache from the CACHE_FILE (letterboxd_watchlist_cache.json).
    Returns a dictionary mapping film slugs or names to their cached data.
    """
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r') as f:
            try:
                return json.load(f)
            except Exception:
                return {}
    return {}

def save_cache(cache):
    """
    Save the given cache dictionary to the CACHE_FILE (letterboxd_watchlist_cache.json).
    """
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f, indent=2)

def get_tmdb_id_from_api(title, year=None):
    """
    Query the TMDB API for a movie by title and (optionally) year.
    Returns the TMDB ID as a string if found, otherwise None.
    Logs the search and result to the console.
    """
    if not TMDB_API_KEY:
        print("[TMDB] No API key provided. Skipping TMDB lookup.")
        return None
    
    try:
        # Clean title for search
        search_title = re.sub(r'[^\w\s]', '', title).strip()
        
        # Build search URL
        url = f"https://api.themoviedb.org/3/search/movie"
        params = {
            'api_key': TMDB_API_KEY,
            'query': search_title,
            'language': 'en-US',
            'page': 1,
            'include_adult': False
        }
        
        if year:
            params['year'] = year
        
        print(f"[TMDB] Searching for '{title}' ({year})...")
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        
        results = response.json().get('results', [])
        if results:
            # Return the first (most relevant) result
            print(f"[TMDB] Found TMDB ID {results[0]['id']} for '{title}' ({year})")
            return str(results[0]['id'])
        else:
            print(f"[TMDB] No results for '{title}' ({year})")
        
    except Exception as e:
        print(f"[TMDB] API error for '{title}': {e}")
    
    return None

def get_tmdb_id_from_film_page(film_slug):
    """Get TMDB ID by visiting the individual film page (fallback method)"""
    try:
        film_url = f"https://letterboxd.com/film/{film_slug}/"
        response = requests.get(film_url, timeout=30)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Look for TMDB links
        tmdb_links = soup.find_all("a", href=re.compile(r"https://www.themoviedb.org/movie/"))
        for link in tmdb_links:
            href = link.get('href')
            if href:
                tmdb_match = re.search(r'^https:\/\/www\.themoviedb\.org\/movie\/([0-9]+)\/', href)
                if tmdb_match:
                    return tmdb_match.group(1)
        
    except Exception as e:
        print(f"Error getting TMDB ID from film page for {film_slug}: {e}")
    
    return None

def extract_films_from_page(soup):
    """
    Extract all film data from a single Letterboxd watchlist page.
    Updated to work with new Letterboxd HTML structure using griditem li elements
    with react-component divs containing film data.
    Returns a list of film data dictionaries (no TMDB lookup).
    """
    films = []
    
    # Look for the new structure: li elements with class 'griditem' containing react-component divs
    film_containers = soup.find_all('li', class_='griditem')
    if not film_containers:
        # Fallback to old structure for backward compatibility
        poster_list = soup.find('ul', class_='poster-list')
        if poster_list:
            film_containers = poster_list.find_all('li', class_='poster-container')
    
    now = datetime.datetime.now().strftime('%b %d %Y %I:%M%p').lower().replace('am', 'am').replace('pm', 'pm')
    
    for container in film_containers:
        try:
            # Look for react-component div with film data
            film_div = container.find('div', class_='react-component')
            if not film_div:
                # Fallback to old structure
                film_div = container.find('div', class_='film-poster')
                if not film_div:
                    continue
            
            # Extract film data from the new structure
            film_id = film_div.get('data-film-id')
            if not film_id:
                continue
                
            film_name = film_div.get('data-item-name', '').strip()
            film_slug = film_div.get('data-item-slug', '')
            film_link = film_div.get('data-item-link', '')
            poster_url = film_div.get('data-poster-url', '')
            
            # Extract year from film name or slug
            year = ''
            if film_name:
                # Try to extract year from film name (e.g., "Movie Name (2023)")
                year_match = re.search(r'\((\d{4})\)', film_name)
                if year_match:
                    year = year_match.group(1)
                else:
                    # Try to extract from slug if it ends with 4 digits
                    if len(film_slug) >= 4 and film_slug[-4:].isdigit():
                        year = film_slug[-4:]
            
            film_data = {
                'film_id': film_id,
                'film_slug': film_slug,
                'film_link': film_link,
                'poster_url': poster_url,
                'film_name': film_name,
                'year': year,
                'date_scraped': now
            }
            films.append(film_data)
            
        except Exception as e:
            print(f"Error extracting film data: {e}")
            continue
    
    return films

def get_total_film_count(soup):
    """
    Extract the total number of films in the watchlist from the page header.
    Returns the count as an integer, or None if not found.
    """
    try:
        count_element = soup.find('span', class_='js-watchlist-count')
        if count_element:
            count_text = count_element.get_text()
            # Extract number from "211 films" format
            match = re.search(r'(\d+)', str(count_text))
            if match:
                return int(match.group(1))
    except Exception as e:
        print(f"Error extracting film count: {e}")
    
    return None

def get_total_pages_from_pagination(soup):
    """
    Extract the total number of pages from the pagination links.
    This is more reliable than calculating based on film count.
    """
    try:
        # Find the pagination section
        pagination = soup.find('div', class_='pagination')
        if not pagination:
            return 1
        
        # Find all page links
        page_links = pagination.find_all('a', href=True)
        max_page = 1
        
        for link in page_links:
            href = link.get('href')
            if href and '/page/' in href:
                # Extract page number from href like "/larswan/watchlist/page/8/"
                page_match = re.search(r'/page/(\d+)/', href)
                if page_match:
                    page_num = int(page_match.group(1))
                    max_page = max(max_page, page_num)
        
        # Also check the current page span (page 1 is shown as span, not link)
        current_page_span = pagination.find('span')
        if current_page_span and current_page_span.get_text().strip().isdigit():
            current_page = int(current_page_span.get_text().strip())
            max_page = max(max_page, current_page)
        
        print(f"[DEBUG] Found {max_page} pages from pagination links")
        return max_page
        
    except Exception as e:
        print(f"[ERROR] Error extracting pages from pagination: {e}")
        return 1

def get_total_pages(soup, films_per_page=28):
    """
    Get total pages from pagination links (preferred) or calculate from film count (fallback).
    """
    # Try pagination first
    pages_from_pagination = get_total_pages_from_pagination(soup)
    if pages_from_pagination > 1:
        return pages_from_pagination
    
    # Fallback to calculation
    total_films = get_total_film_count(soup)
    if total_films:
        calculated_pages = math.ceil(total_films / films_per_page)
        print(f"[DEBUG] Calculated {calculated_pages} pages from film count ({total_films} films)")
        return calculated_pages
    
    return 1  # Default to 1 page if we can't determine

def fetch_page_with_retry(url, page_num=None, max_retries=MAX_RETRIES):
    """
    Fetch a page with retry logic for rate limiting.
    Returns the response object or None if all retries fail.
    """
    # Headers to mimic a real browser request
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
    }
    
    for attempt in range(max_retries):
        try:
            if page_num and page_num > 1:
                page_url = f"{url}page/{page_num}/"
            else:
                page_url = url
            
            print(f"[DEBUG] Fetching page {page_num or 1}: {page_url}")
            response = requests.get(page_url, headers=headers, timeout=30)
            
            # Check for rate limiting (429 status code)
            if response.status_code == 429:
                log_rate_limit_event(f"Hit rate limit on attempt {attempt + 1}. Waiting {RETRY_DELAY} seconds...")
                time.sleep(RETRY_DELAY)
                continue
            
            response.raise_for_status()
            return response
            
        except requests.exceptions.RequestException as e:
            print(f"[ERROR] Request failed on attempt {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                print(f"[RETRY] Waiting {RATE_LIMIT_DELAY} seconds before retry...")
                time.sleep(RATE_LIMIT_DELAY)
            else:
                print(f"[FATAL] All {max_retries} attempts failed for page {page_num or 1}")
                return None
    
    return None

def _build_letterboxd_list_radarr_url(list_url: str) -> str:
    """
    Convert a Letterboxd URL (e.g. https://letterboxd.com/<user>/watchlist/)
    into the letterboxd-list-radarr endpoint URL.
    """
    base_url = os.getenv(
        'LETTERBOXD_LIST_RADARR_BASE_URL',
        'https://letterboxd-list-radarr.onrender.com'
    ).rstrip('/')

    parsed = urlparse(list_url)
    # Keep the Letterboxd path portion only (e.g. "<user>/watchlist/").
    letterboxd_path = parsed.path.lstrip('/')
    return f"{base_url}/{letterboxd_path}"

def _normalize_letterboxd_film_slug(clean_title: str) -> str:
    """
    letterboxd-list-radarr returns `clean_title` in `transform.ts` as `movie.slug`.
    Depending on upstream changes, that slug may be a bare slug or include path
    components like `film/<slug>` or `/film/<slug>/`.
    """
    if not clean_title:
        return ''

    s = str(clean_title).strip()
    s = s.strip('/')
    if s.startswith('film/'):
        s = s[len('film/'):]
    # If any path components remain, keep the last segment.
    return s.split('/')[-1]

def scrape_letterboxd_watchlist_external(list_url):
    """
    Scrape a watchlist via the hosted/self-hosted letterboxd-list-radarr service.
    The service returns a streamed JSON array of Radarr-like movie objects:
    { id, title, release_year, clean_title, ... }.
    """
    backend = 'external'
    print(f"Scraping {list_url} using {backend} backend...")

    all_films = []
    try:
        now = datetime.datetime.now().strftime('%b %d %Y %I:%M%p').lower().replace('am', 'am').replace('pm', 'pm')
        external_url = _build_letterboxd_list_radarr_url(list_url)

        # Optional query parameters
        params = {}
        limit = os.getenv('LETTERBOXD_LIST_RADARR_LIMIT', '').strip()
        if limit:
            params['limit'] = limit

        error_on_empty = os.getenv('LETTERBOXD_LIST_RADARR_ERROR_ON_EMPTY', 'true').strip().lower()
        if error_on_empty in {'true', 'false'}:
            # letterboxd-list-radarr expects `errorOnEmpty=false|true`
            params['errorOnEmpty'] = error_on_empty

        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'application/json,text/plain,*/*'
        }

        print(f"[INFO] Fetching external list data: {external_url}")
        response = requests.get(external_url, params=params, headers=headers, timeout=120)
        response.raise_for_status()

        movies = response.json()
        if not isinstance(movies, list):
            print(f"[ERROR] External scraper returned unexpected payload type: {type(movies)}")
            return None

        for movie in movies:
            if not isinstance(movie, dict):
                continue

            tmdb_id = movie.get('id', None)
            title = (movie.get('title') or '').strip()
            release_year = movie.get('release_year', None)
            clean_title = movie.get('clean_title') or ''
            film_slug = _normalize_letterboxd_film_slug(clean_title)

            # letterboxd-list-radarr only includes entries that have a tmdb id.
            if tmdb_id is None:
                continue

            film_link = f"https://letterboxd.com/film/{film_slug}/" if film_slug else ''

            all_films.append({
                # Keep native schema keys for compatibility with your pipeline.
                'film_id': str(tmdb_id),
                'film_slug': film_slug,
                'film_link': film_link,
                'poster_url': '',
                'film_name': title,
                'year': str(release_year) if release_year else '',
                'date_scraped': now,
                # New: allow tmdb lookup step to skip redundant API calls.
                'tmdb_id': str(tmdb_id),
            })

        print(f"[INFO] Saving {len(all_films)} total films to {CACHE_FILE}")
        with open(CACHE_FILE, 'w') as f:
            json.dump(all_films, f, indent=2)

        print(f"[SUCCESS] External scraping completed! Found {len(all_films)} films")
        return all_films

    except Exception as e:
        print(f"[ERROR] External scraping failed: {e}")
        return None

def scrape_letterboxd_watchlist(list_url):
    """
    Scrape all pages of the Letterboxd watchlist with pagination and rate limiting.
    Returns a list of all films or None if scraping fails.
    """
    backend = os.getenv('LETTERBOXD_SCRAPER_BACKEND', 'external').lower().strip()
    if backend == 'external':
        films = scrape_letterboxd_watchlist_external(list_url)
        if films is not None:
            return films

        strict = os.getenv('LETTERBOXD_EXTERNAL_SCRAPER_STRICT', '0').strip().lower() in {'1', 'true', 'yes'}
        if strict:
            return None
        print("[WARN] External scraper failed; falling back to native scraper.")

    print(f"Scraping {list_url} with pagination and rate limiting...")
    all_films = []
    
    try:
        # First, get the first page to determine total pages
        print("[INFO] Fetching first page to determine total pages...")
        response = fetch_page_with_retry(list_url)
        if not response:
            print("[ERROR] Failed to fetch first page")
            return None
        
        soup = BeautifulSoup(response.text, 'html.parser')
        total_pages = get_total_pages(soup)
        total_films = get_total_film_count(soup)
        
        print(f"[INFO] Detected {total_films} films across {total_pages} pages")
        
        # Extract films from first page
        films = extract_films_from_page(soup)
        all_films.extend(films)
        print(f"[INFO] Extracted {len(films)} films from page 1")
        
        # Scrape remaining pages if any
        if total_pages > 1:
            print(f"[INFO] Scraping {total_pages - 1} additional pages...")
            
            for page_num in range(2, total_pages + 1):
                print(f"[INFO] Processing page {page_num}/{total_pages}")
                
                # Rate limiting delay between pages
                if page_num > 2:  # Don't delay before first page
                    print(f"[RATE LIMIT] Waiting {RATE_LIMIT_DELAY} seconds between pages...")
                    time.sleep(RATE_LIMIT_DELAY)
                
                response = fetch_page_with_retry(list_url, page_num)
                if not response:
                    print(f"[ERROR] Failed to fetch page {page_num}. Stopping.")
                    break
                
                soup = BeautifulSoup(response.text, 'html.parser')
                films = extract_films_from_page(soup)
                all_films.extend(films)
                print(f"[INFO] Extracted {len(films)} films from page {page_num}")
        
        # Save all films to cache
        print(f"[INFO] Saving {len(all_films)} total films to {CACHE_FILE}")
        with open(CACHE_FILE, 'w') as f:
            json.dump(all_films, f, indent=2)
        
        print(f"[SUCCESS] Scraping completed! Found {len(all_films)} films across {total_pages} pages")
        return all_films
        
    except Exception as e:
        print(f"[ERROR] Unexpected error during scraping: {e}")
        return None

def scrape_list_with_debug(list_url):
    """
    Debug version that prints HTML for troubleshooting.
    Use this if you need to see the raw HTML for debugging.
    """
    print(f"Scraping {list_url} with debug output...")
    all_films = []
    
    try:
        response = fetch_page_with_retry(list_url)
        if not response:
            print("[ERROR] Failed to fetch page")
            return None
        
        print("\n--- BEGIN RAW HTML ---\n")
        print(response.text)
        print("\n--- END RAW HTML ---\n")
        
        soup = BeautifulSoup(response.text, 'html.parser')
        films = extract_films_from_page(soup)
        all_films.extend(films)
        
        with open(CACHE_FILE, 'w') as f:
            json.dump(all_films, f, indent=2)
        print(f"Saved {len(all_films)} films to {CACHE_FILE}")
        return all_films
        
    except Exception as e:
        print(f"Error fetching data from Letterboxd: {e}")
        return None