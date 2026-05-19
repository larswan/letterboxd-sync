import requests
import json
import os
import re
from collections import Counter
from datetime import datetime
from dotenv import load_dotenv
from logger import get_logger

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, 'cache')
os.makedirs(CACHE_DIR, exist_ok=True)

PLEX_CACHE = os.path.join(CACHE_DIR, 'plex_watchlist_cache.json')
TMDB_CACHE = os.path.join(CACHE_DIR, 'tmdb_watchlist_cache.json')
OVERSEERR_CACHE = os.path.join(CACHE_DIR, 'overseerr_watchlist_cache.json')

# Overseerr MediaStatus (server/constants/media.ts)
MEDIA_STATUS_UNKNOWN = 1
MEDIA_STATUS_PENDING = 2
MEDIA_STATUS_PROCESSING = 3
MEDIA_STATUS_PARTIALLY_AVAILABLE = 4
MEDIA_STATUS_AVAILABLE = 5

# Overseerr MediaRequest status
REQUEST_STATUS_PENDING = 1
REQUEST_STATUS_APPROVED = 2


def format_date(date=None):
    if not date:
        date = datetime.now()
    return date.strftime('%b %d %Y %I:%M%p').lower()


def _classify_overseerr_error(exc):
    """Map exceptions/responses to a short error category for recap logging."""
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
        if status == 403:
            return 'HTTP 403 Forbidden (API key permissions)'
        if status == 401:
            return 'HTTP 401 Unauthorized (invalid API key)'
        if status == 404:
            return 'HTTP 404 Not Found'
        return f'HTTP {status}'

    message = str(exc)
    status_match = re.search(r'(\d{3})\s+Client Error', message)
    if status_match:
        code = status_match.group(1)
        if code == '403':
            return 'HTTP 403 Forbidden (API key permissions)'
        if code == '401':
            return 'HTTP 401 Unauthorized (invalid API key)'
        return f'HTTP {code}'

    lowered = message.lower()
    if 'timed out' in lowered or 'timeout' in lowered:
        return 'Connection timeout'
    if 'connection' in lowered:
        return 'Connection error'
    return 'Unexpected error'


def _overseerr_headers(api_key):
    return {
        'X-Api-Key': api_key,
        'Content-Type': 'application/json',
    }


def fetch_overseerr_movie(overseerr_host, api_key, tmdb_id):
    """Fetch movie details from Overseerr (includes mediaInfo when known)."""
    response = requests.get(
        f"{overseerr_host.rstrip('/')}/api/v1/movie/{tmdb_id}",
        headers=_overseerr_headers(api_key),
        timeout=30,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def movie_needs_request(movie_data):
    """
    Decide whether a watchlist movie should be requested in Overseerr.
    Returns (needs_request: bool, reason: str).
    """
    if not movie_data:
        return True, 'Not tracked in Overseerr yet'

    media_info = movie_data.get('mediaInfo')
    if not media_info:
        return True, 'No media info in Overseerr'

    status = media_info.get('status')
    if status == MEDIA_STATUS_AVAILABLE:
        return False, 'Available in library'
    if status in (MEDIA_STATUS_PENDING, MEDIA_STATUS_PROCESSING):
        return False, 'Already pending/processing in Overseerr'
    if status == MEDIA_STATUS_PARTIALLY_AVAILABLE:
        return False, 'Partially available in library'

    for req in media_info.get('requests') or []:
        req_status = req.get('status')
        if req_status in (REQUEST_STATUS_PENDING, REQUEST_STATUS_APPROVED):
            return False, 'Already requested in Overseerr'

    return True, 'Missing from library'


def submit_overseerr_request(overseerr_host, api_key, tmdb_id, is_4k=False):
    """Submit a movie request. Returns (status_label, updated)."""
    request_data = {
        'mediaType': 'movie',
        'mediaId': int(tmdb_id),
        'is4k': is_4k,
    }
    response = requests.post(
        f"{overseerr_host.rstrip('/')}/api/v1/request",
        headers=_overseerr_headers(api_key),
        json=request_data,
        timeout=30,
    )

    if response.status_code == 201:
        return f"Requested on [{format_date()}]", True
    if response.status_code == 409:
        return f"Already Requested [{format_date()}]", True
    if response.status_code == 404:
        return f"Unable To Find on Overseerr [{format_date()}]", True

    error_msg = response.text or f"HTTP {response.status_code}"
    return f"Error: {error_msg} [{format_date()}]", True


def overseerr_monitor_add_from_tmdb_cache(
    tmdb_cache=TMDB_CACHE,
    overseerr_cache=OVERSEERR_CACHE,
):
    """
    Read tmdb_watchlist_cache.json, check each title in Overseerr, and request
    movies that are not already available or requested.
    """
    load_dotenv()
    logger = get_logger()

    overseerr_host = os.getenv('OVERSEERR_HOST')
    overseerr_api_key = os.getenv('OVERSEERR_API_KEY')

    if not overseerr_host or not overseerr_api_key:
        logger.error('OVERSEERR_HOST and OVERSEERR_API_KEY must be set in environment variables')
        return False

    if not os.path.exists(tmdb_cache):
        logger.error(f"TMDB cache file '{tmdb_cache}' not found.")
        return False

    with open(tmdb_cache, 'r') as f:
        tmdb_results = json.load(f)

    logger.info(f"Loaded {len(tmdb_results)} films from {tmdb_cache}")

    overseerr_results = []
    requested_count = 0
    skipped_count = 0
    error_count = 0
    error_types = Counter()
    skip_reasons = Counter()

    for film in tmdb_results:
        name = film.get('film_name')
        tmdb_id = film.get('tmdb_id')

        entry = {
            'film_name': name,
            'tmdb_id': tmdb_id,
            'year': film.get('year'),
            'status': '',
            'date_checked': format_date(),
        }

        if not tmdb_id:
            entry['status'] = 'Skipped (no TMDB ID)'
            skipped_count += 1
            skip_reasons['No TMDB ID'] += 1
            overseerr_results.append(entry)
            continue

        try:
            movie_data = fetch_overseerr_movie(overseerr_host, overseerr_api_key, tmdb_id)
            needs_request, reason = movie_needs_request(movie_data)

            if not needs_request:
                entry['status'] = reason
                skipped_count += 1
                skip_reasons[reason] += 1
                logger.debug(f"Skipping '{name}': {reason}")
            else:
                status_label, _ = submit_overseerr_request(
                    overseerr_host, overseerr_api_key, tmdb_id
                )
                entry['status'] = status_label
                if status_label.startswith('Requested on'):
                    requested_count += 1
                    logger.debug(f"Requested '{name}' in Overseerr")
                elif status_label.startswith('Already Requested'):
                    skipped_count += 1
                    skip_reasons['Already requested'] += 1
                    logger.debug(f"'{name}' already requested in Overseerr")
                elif status_label.startswith('Unable To Find'):
                    error_count += 1
                    error_types['Not found in Overseerr'] += 1
                    logger.debug(f"Overseerr could not find '{name}'")
                elif status_label.startswith('Error'):
                    error_count += 1
                    error_types['Request failed'] += 1
                    logger.debug(f"Overseerr request error for '{name}': {status_label}")
                else:
                    error_count += 1
                    error_types['Unknown response'] += 1
                    logger.debug(f"Overseerr unexpected status for '{name}': {status_label}")

        except requests.RequestException as e:
            entry['status'] = f"Error: {str(e)} [{format_date()}]"
            error_count += 1
            error_types[_classify_overseerr_error(e)] += 1
            logger.debug(f"Overseerr API error for '{name}': {e}")
        except Exception as e:
            entry['status'] = f"Error: {str(e)} [{format_date()}]"
            error_count += 1
            error_types[_classify_overseerr_error(e)] += 1
            logger.debug(f"Unexpected Overseerr error for '{name}': {e}")

        overseerr_results.append(entry)

    with open(overseerr_cache, 'w') as f:
        json.dump(overseerr_results, f, indent=2)

    logger.info(
        f"Overseerr recap: {requested_count} requested, "
        f"{skipped_count} skipped, {error_count} errors "
        f"(of {len(tmdb_results)} films)"
    )
    if skip_reasons and skipped_count > 0:
        top_skip = skip_reasons.most_common(3)
        logger.info(
            "Overseerr skip reasons: "
            + ", ".join(f"{reason} ({count})" for reason, count in top_skip)
        )
    if error_types:
        top_error, top_count = error_types.most_common(1)[0]
        breakdown = ", ".join(f"{reason} ({count})" for reason, count in error_types.most_common(5))
        logger.error(f"Overseerr primary error: {top_error} ({top_count}x). Breakdown: {breakdown}")
    logger.info(f"Saved Overseerr results to {overseerr_cache}")

    if error_count > 0 and requested_count == 0 and skipped_count == 0:
        return False

    return True


def overseerr_monitor_add_from_plex_cache():
    """
    Legacy path: read plex_watchlist_cache.json and request movies marked
    as "Not in Library".
    """
    load_dotenv()
    logger = get_logger()

    overseerr_host = os.getenv('OVERSEERR_HOST')
    overseerr_api_key = os.getenv('OVERSEERR_API_KEY')

    if not overseerr_host or not overseerr_api_key:
        logger.error('OVERSEERR_HOST and OVERSEERR_API_KEY must be set in environment variables')
        return False

    if not os.path.exists(PLEX_CACHE):
        logger.error(f"Plex cache file '{PLEX_CACHE}' not found.")
        return False

    with open(PLEX_CACHE, 'r') as f:
        plex_results = json.load(f)

    movies_to_request = [
        film for film in plex_results if film.get('availability') == 'Not in Library'
    ]
    logger.info(f"Found {len(movies_to_request)} movies to request from Plex cache.")

    updated_count = 0
    requested_count = 0
    error_count = 0
    error_types = Counter()

    for film in movies_to_request:
        name = film.get('film_name')
        tmdb_id = film.get('tmdb_id')
        if not tmdb_id:
            continue

        try:
            status_label, updated = submit_overseerr_request(
                overseerr_host, overseerr_api_key, tmdb_id
            )
            film['availability'] = status_label
            if updated:
                updated_count += 1
                if status_label.startswith('Requested on'):
                    requested_count += 1
                elif status_label.startswith('Error') or status_label.startswith('Unable'):
                    error_count += 1
                    error_types['Request failed'] += 1
                logger.debug(f"Overseerr update for '{name}': {status_label}")
        except Exception as e:
            film['availability'] = f"Error: {str(e)} [{format_date()}]"
            updated_count += 1
            error_count += 1
            error_types[_classify_overseerr_error(e)] += 1
            logger.debug(f"Error requesting '{name}': {e}")

    if updated_count > 0:
        with open(PLEX_CACHE, 'w') as f:
            json.dump(plex_results, f, indent=2)

    logger.info(
        f"Overseerr recap (Plex cache): {requested_count} requested, "
        f"{error_count} errors (of {len(movies_to_request)} candidates)"
    )
    if error_types:
        top_error, top_count = error_types.most_common(1)[0]
        breakdown = ", ".join(f"{reason} ({count})" for reason, count in error_types.most_common(5))
        logger.error(f"Overseerr primary error: {top_error} ({top_count}x). Breakdown: {breakdown}")

    return True


if __name__ == '__main__':
    overseerr_monitor_add_from_tmdb_cache()
