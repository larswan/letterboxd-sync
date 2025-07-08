from bs4 import BeautifulSoup
import requests
import re
import time
import json
import os
from tqdm import tqdm

CACHE_FILE = 'film_cache.json'

def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r') as f:
            try:
                return json.load(f)
            except Exception:
                return {}
    return {}

def save_cache(cache):
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f)

def scrape_list(list_link):
    print("Scraping "+list_link)
    watchlist = []
    cache = load_cache()
    updated = False
    try:
        while True:
            list_page = requests.get(list_link, timeout=30)
            list_page.raise_for_status()
            
            soup = BeautifulSoup(list_page.content, 'html.parser')
            
            table = soup.find('ul', class_='poster-list')
            if table is None:
                print("No poster-list found on page. The page structure may have changed.")
                return None
            
            films = table.find_all('li')
            
            for film in tqdm(films, desc="Processing films"):
                try:
                    film_div = film.find('div')
                    if not film_div:
                        continue
                    film_card = film_div.get('data-target-link')
                    if not film_card:
                        continue
                    film_page = 'https://letterboxd.com/' + film_card
                    # Use the film_card as a unique key for caching
                    if film_card in cache:
                        tmdb_id = cache[film_card]
                        if tmdb_id and tmdb_id not in watchlist:
                            watchlist.append(tmdb_id)
                        continue
                    # Add delay to prevent rate limiting
                    time.sleep(5)  # Wait 5 seconds between requests
                    filmget = requests.get(film_page, timeout=30)
                    filmget.raise_for_status()
                    film_soup = BeautifulSoup(filmget.content, 'html.parser')
                    # Parse TMDB URLs to get the TMDB ID
                    tmdb_urls = film_soup.find_all("a", href=re.compile(r"https://www.themoviedb.org/movie/"))
                    found_tmdb = False
                    for tmdb_url in tmdb_urls:
                        href = tmdb_url.get('href')
                        if href:
                            tmdb_id_match = re.search(r'^https:\/\/www\.themoviedb\.org\/movie\/([0-9]+)\/', href)
                            if tmdb_id_match:
                                tmdb_id = tmdb_id_match.group(1)
                                cache[film_card] = tmdb_id
                                updated = True
                                if tmdb_id not in watchlist:
                                    watchlist.append(tmdb_id)
                                found_tmdb = True
                                break
                    if not found_tmdb:
                        cache[film_card] = None
                        updated = True
                except requests.exceptions.HTTPError as e:
                    if e.response.status_code == 429:
                        print(f"\nRate limited by Letterboxd. Waiting 60 seconds...")
                        time.sleep(60)  # Wait 60 seconds on rate limit
                        continue
                    else:
                        print(f"Error processing film: {e}")
                        continue
                except Exception as e:
                    print(f"Error processing film: {e}")
                    continue
            next_link = soup.find('a', class_='next')
            if next_link is None:
                break
            else:
                href = next_link.get('href')
                if href:
                    list_link = 'https://letterboxd.com/' + href
                    # Add delay between pages
                    time.sleep(2)
        if updated:
            save_cache(cache)
        return watchlist
    except requests.RequestException as e:
        print(f"Error fetching data from Letterboxd: {e}")
        if updated:
            save_cache(cache)
        return None
    except Exception as e:
        print(f"Unexpected error during scraping: {e}")
        if updated:
            save_cache(cache)
        return None