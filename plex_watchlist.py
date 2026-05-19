import os
import json
import random
import re
import time
from datetime import datetime

import requests
from plexapi.server import PlexServer
from dotenv import load_dotenv
from logger import get_logger

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, 'cache')
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

TMDB_CACHE = os.path.join(CACHE_DIR, 'tmdb_watchlist_cache.json')
PLEX_CACHE = os.path.join(CACHE_DIR, 'plex_watchlist_cache.json')
PLEX_LIST_DELTA_FILE = os.path.join(LOG_DIR, 'plex_list_delta.json')
PLAYLIST_NAME = 'Letterboxd Watchlist'
DISCOVER_BASE = 'https://discover.provider.plex.tv'

load_dotenv()

PLEX_PRODUCT = os.getenv('PLEX_PRODUCT', 'Letterboxd Sync')
PLEX_PLATFORM = os.getenv('PLEX_PLATFORM', 'Python')


def _plex_token():
    return os.getenv('PLEX_TOKEN', '').strip().strip("'\"")


def _plex_client_headers(token=None):
    client_id = os.getenv('CLIENT_ID', '').strip()
    headers = {
        'Accept': 'application/json',
        'X-Plex-Product': PLEX_PRODUCT,
        'X-Plex-Platform': PLEX_PLATFORM,
        'X-Plex-Token': token or _plex_token(),
    }
    if client_id:
        headers['X-Plex-Client-Identifier'] = client_id
    return headers


def _discover_session():
    session = requests.Session()
    session.headers.update(_plex_client_headers())
    return session


def format_date(date=None):
    if not date:
        date = datetime.now()
    return date.strftime('%b %d %Y %I:%M%p').lower()


def tmdb_id_from_guid(guid):
    if not guid:
        return None
    match = re.search(r'tmdb://(\d+)', str(guid))
    return match.group(1) if match else None


def tmdb_id_from_metadata(meta):
    for entry in meta.get('Guid') or []:
        gid = entry.get('id') if isinstance(entry, dict) else entry
        tmdb_id = tmdb_id_from_guid(gid)
        if tmdb_id:
            return tmdb_id
    return None


def _rating_key_from_guid(guid):
    if not guid:
        return None
    return str(guid).rsplit('/', 1)[-1]


def _watchlist_item_record(meta):
    return {
        'title': meta.get('title'),
        'year': meta.get('year'),
        'tmdb_id': tmdb_id_from_metadata(meta),
        'guid': meta.get('guid'),
        'rating_key': meta.get('ratingKey') or _rating_key_from_guid(meta.get('guid')),
    }


def fetch_plex_watchlist_movies(session=None, maxresults=5000):
    """Fetch the Plex account watchlist from discover.provider.plex.tv."""
    session = session or _discover_session()
    headers = dict(session.headers)
    movies = []
    offset = 0
    page_size = 100

    while len(movies) < maxresults:
        params = {
            'includeCollections': 1,
            'includeExternalMedia': 1,
            'type': 18,
            'X-Plex-Container-Start': offset,
            'X-Plex-Container-Size': page_size,
        }
        response = session.get(
            f'{DISCOVER_BASE}/library/sections/watchlist/all',
            headers=headers,
            params=params,
            timeout=60,
        )
        response.raise_for_status()
        container = response.json().get('MediaContainer', {})
        batch = container.get('Metadata') or []
        movies.extend(batch)
        total = container.get('totalSize', len(movies))
        offset += len(batch)
        if not batch or offset >= total:
            break

    return movies[:maxresults]


def search_discover_movie(session, film_name, year=None, tmdb_id=None):
    """
    Search Plex Discover for a movie. Returns metadata dict with rating_key, title, guid.
    """
    query = film_name
    if year:
        query = f'{film_name} {year}'

    params = {
        'query': query,
        'limit': 10,
        'searchTypes': 'movies',
        'searchProviders': 'discover',
        'includeMetadata': 1,
    }
    response = session.get(
        f'{DISCOVER_BASE}/library/search',
        headers=session.headers,
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    search_results = response.json().get('MediaContainer', {}).get('SearchResults', [])
    candidates = []
    for block in search_results:
        for item in block.get('SearchResult', []):
            metadata = item.get('Metadata')
            if metadata:
                candidates.append(metadata)

    if tmdb_id:
        for metadata in candidates:
            if tmdb_id_from_metadata(metadata) == str(tmdb_id):
                return metadata

    target_year = str(year) if year else None
    normalized_name = (film_name or '').strip().lower()
    for metadata in candidates:
        if (metadata.get('title') or '').strip().lower() != normalized_name:
            continue
        if target_year and str(metadata.get('year', '')) != target_year:
            continue
        return metadata

    return candidates[0] if candidates else None


def add_to_watchlist(session, rating_key):
    response = session.put(
        f'{DISCOVER_BASE}/actions/addToWatchlist',
        params={'ratingKey': rating_key},
        timeout=30,
    )
    response.raise_for_status()


def remove_from_watchlist(session, rating_key):
    response = session.put(
        f'{DISCOVER_BASE}/actions/removeFromWatchlist',
        params={'ratingKey': rating_key},
        timeout=30,
    )
    response.raise_for_status()


def write_plex_list_delta(removed_items):
    payload = {
        'logged_at': datetime.now().isoformat(),
        'description': 'Movies on Plex watchlist that were not on the Letterboxd watchlist before sync',
        'count': len(removed_items),
        'movies': removed_items,
    }
    with open(PLEX_LIST_DELTA_FILE, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2)
    return PLEX_LIST_DELTA_FILE


def sync_plex_native_watchlist(films):
    """
    Overwrite the Plex account watchlist to match Letterboxd (by Discover ratingKey).
    Logs Plex-only titles to logs/plex_list_delta.json before removing them.
    """
    logger = get_logger()
    session = _discover_session()

    letterboxd_targets = {}
    unresolved = []
    for film in films:
        tmdb_id = film.get('tmdb_id')
        if not tmdb_id:
            continue
        try:
            metadata = search_discover_movie(
                session,
                film.get('film_name'),
                year=film.get('year'),
                tmdb_id=tmdb_id,
            )
        except requests.RequestException as e:
            unresolved.append({**film, 'error': str(e)})
            continue

        if not metadata:
            unresolved.append(film)
            continue

        rating_key = metadata.get('ratingKey') or _rating_key_from_guid(metadata.get('guid'))
        if not rating_key:
            unresolved.append(film)
            continue

        letterboxd_targets[rating_key] = {
            'film': film,
            'metadata': metadata,
        }

    logger.info('Fetching current Plex watchlist...')
    try:
        current_watchlist = fetch_plex_watchlist_movies(session=session)
    except requests.RequestException as e:
        logger.error(f'Failed to fetch Plex watchlist: {e}')
        return False

    logger.info(f'Found {len(current_watchlist)} movies on Plex watchlist')

    current_by_key = {}
    for meta in current_watchlist:
        rating_key = meta.get('ratingKey') or _rating_key_from_guid(meta.get('guid'))
        if rating_key:
            current_by_key[rating_key] = meta

    target_keys = set(letterboxd_targets.keys())
    to_remove_keys = [key for key in current_by_key if key not in target_keys]
    delta_records = [_watchlist_item_record(current_by_key[key]) for key in to_remove_keys]

    delta_path = write_plex_list_delta(delta_records)
    logger.info(
        f'Logged {len(delta_records)} Plex-only watchlist movies to {delta_path} '
        f'(will remove before sync)'
    )

    removed_count = 0
    for rating_key in to_remove_keys:
        try:
            remove_from_watchlist(session, rating_key)
            removed_count += 1
            title = current_by_key[rating_key].get('title')
            logger.debug(f'Removed from Plex watchlist: {title}')
        except requests.RequestException as e:
            logger.debug(
                f"Could not remove '{current_by_key[rating_key].get('title', rating_key)}': {e}"
            )

    logger.info(f'Removed {removed_count} movies from Plex watchlist')

    added_count = 0
    skipped_existing = len(target_keys & set(current_by_key.keys()))
    added_keys = set()

    for rating_key, payload in letterboxd_targets.items():
        if rating_key in current_by_key:
            continue
        try:
            add_to_watchlist(session, rating_key)
            added_count += 1
            added_keys.add(rating_key)
            logger.debug(
                f"Added to Plex watchlist: {payload['metadata'].get('title')}"
            )
        except requests.RequestException as e:
            film = payload['film']
            unresolved.append({**film, 'error': str(e)})
            logger.debug(f"Could not add '{film.get('film_name')}': {e}")
        time.sleep(0.15)

    logger.info(
        f'Plex watchlist sync recap: {added_count} added, {removed_count} removed, '
        f'{skipped_existing} already matched, {len(unresolved)} unresolved'
    )

    plex_results = []
    for film in films:
        tmdb_id = film.get('tmdb_id')
        film_unresolved = any(
            u.get('tmdb_id') == tmdb_id or u.get('film_name') == film.get('film_name')
            for u in unresolved
        )
        matched_key = None
        for rating_key, payload in letterboxd_targets.items():
            if payload['film'].get('tmdb_id') == tmdb_id:
                matched_key = rating_key
                break

        if not tmdb_id:
            status = 'Skipped (no TMDB ID)'
        elif film_unresolved:
            status = 'Could not add to Plex watchlist'
        elif matched_key in added_keys:
            status = 'Added to Plex watchlist'
        elif matched_key in current_by_key or matched_key in target_keys:
            status = 'Already on Plex watchlist'
        else:
            status = 'Not on Plex watchlist'

        plex_results.append({
            'film_name': film.get('film_name'),
            'tmdb_id': tmdb_id,
            'year': film.get('year'),
            'date_added': format_date(),
            'availability': status,
        })

    with open(PLEX_CACHE, 'w') as f:
        json.dump(plex_results, f, indent=2)

    if unresolved:
        logger.warning(
            f'{len(unresolved)} Letterboxd films could not be added to Plex watchlist '
            f'(see debug logs or expand search matching)'
        )

    return True


def sync_plex_playlist(server, films):
    """Legacy behavior: maintain a server playlist of library matches."""
    logger = get_logger()
    movies_section = server.library.section('Movies')

    plex_results = []
    items_to_add = []
    for film in films:
        name = film.get('film_name')
        tmdb_id = film.get('tmdb_id')
        year = film.get('year')
        logger.debug(f"Processing film: {name} (TMDB ID: {tmdb_id}, Year: {year})")
        if not tmdb_id:
            plex_results.append({
                'film_name': name,
                'tmdb_id': tmdb_id,
                'date_added': '',
                'availability': 'Not Found',
            })
            continue
        try:
            if year:
                results = movies_section.search(title=name, year=year)
            else:
                results = movies_section.search(title=name)
            if results:
                items_to_add.append(results[0])
                plex_results.append({
                    'film_name': name,
                    'tmdb_id': tmdb_id,
                    'date_added': format_date(),
                    'availability': 'Available in Library',
                })
            else:
                plex_results.append({
                    'film_name': name,
                    'tmdb_id': tmdb_id,
                    'date_added': '',
                    'availability': 'Not in Library',
                })
        except Exception as e:
            plex_results.append({
                'film_name': name,
                'tmdb_id': tmdb_id,
                'date_added': '',
                'availability': f'Error: {e}',
            })

    playlist = None
    for pl in server.playlists():
        if pl.title == PLAYLIST_NAME:
            playlist = pl
            break

    if items_to_add:
        random.shuffle(items_to_add)
        if not playlist:
            playlist = server.createPlaylist(PLAYLIST_NAME, items=items_to_add)
        else:
            try:
                lead_movie = random.choice(items_to_add)
                other_movies = [movie for movie in items_to_add if movie != lead_movie]
                random.shuffle(other_movies)
                playlist.reload()
                current_items = list(playlist.items())
                items_to_remove = []
                for item in current_items:
                    if hasattr(item, 'guid') and hasattr(lead_movie, 'guid') and item.guid == lead_movie.guid:
                        continue
                    items_to_remove.append(item)
                if items_to_remove:
                    playlist.removeItems(items_to_remove)
                if other_movies:
                    playlist.addItems(other_movies)
            except Exception as e:
                logger.warning(f'Playlist update failed, recreating playlist: {e}')
                playlist.delete()
                server.createPlaylist(PLAYLIST_NAME, items=items_to_add)
    elif playlist:
        playlist.delete()

    with open(PLEX_CACHE, 'w') as f:
        json.dump(plex_results, f, indent=2)

    logger.info(
        f'Plex playlist sync recap: {len(items_to_add)} library matches of {len(films)} films'
    )
    return True


def _verify_plex_account_token(session):
    response = session.get('https://plex.tv/api/v2/user', timeout=30)
    if response.status_code == 401:
        raise ValueError('Invalid Plex account token (401 Unauthorized)')
    response.raise_for_status()


def main():
    plex_host = os.getenv('PLEX_HOST')
    plex_token = _plex_token()
    sync_mode = os.getenv('PLEX_SYNC_MODE', 'watchlist').strip().lower()
    logger = get_logger()

    if not os.path.exists(TMDB_CACHE):
        logger.error(f"TMDB cache file '{TMDB_CACHE}' not found.")
        return False

    with open(TMDB_CACHE, 'r') as f:
        films = json.load(f)

    if not plex_token:
        logger.error('PLEX_TOKEN environment variable is required')
        return False

    if sync_mode == 'playlist':
        if not plex_host:
            logger.error('PLEX_HOST is required for playlist sync mode')
            return False
        logger.info('Plex sync mode: playlist (library playlist only)')
        try:
            session = requests.Session()
            session.headers.update(_plex_client_headers(plex_token))
            server = PlexServer(plex_host, plex_token, session=session)
            logger.info(f'Connected to Plex server: {server.friendlyName}')
        except Exception as e:
            logger.error(f'Failed to connect to Plex server: {e}')
            return False
        return sync_plex_playlist(server, films)

    logger.info('Plex sync mode: native watchlist (overwrite from Letterboxd)')
    try:
        session = _discover_session()
        _verify_plex_account_token(session)
        user = session.get('https://plex.tv/api/v2/user', timeout=30).json()
        username = user.get('username') or user.get('title')
        logger.info(f'Plex account: {username}')
    except Exception as e:
        logger.error(
            f'Plex account token check failed: {e}. '
            'Run python3 plex_auth.py start && poll to get a valid account token.'
        )
        return False

    return sync_plex_native_watchlist(films)


if __name__ == '__main__':
    main()
