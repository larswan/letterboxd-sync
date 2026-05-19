#!/usr/bin/env python3
"""
Plex PIN authentication for a long-lived X-Plex-Token.

Usage:
  python3 plex_auth.py start     # create PIN + print approval URL
  python3 plex_auth.py poll      # poll PIN until approved, save PLEX_TOKEN to .env
  python3 plex_auth.py test      # verify token can access Plex watchlist API
"""
import json
import os
import re
import sys
import time

import requests
from dotenv import load_dotenv, set_key

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, '.env')
PIN_STATE_FILE = os.path.join(BASE_DIR, 'cache', 'plex_pin_state.json')

PLEX_PRODUCT = os.getenv('PLEX_PRODUCT', 'Letterboxd Sync')
PLEX_PLATFORM = os.getenv('PLEX_PLATFORM', 'Python')


def _headers(client_id):
    return {
        'Accept': 'application/json',
        'X-Plex-Product': PLEX_PRODUCT,
        'X-Plex-Platform': PLEX_PLATFORM,
        'X-Plex-Client-Identifier': client_id,
    }


def _load_client_id():
    load_dotenv(ENV_FILE)
    client_id = os.getenv('CLIENT_ID', '').strip()
    if not client_id:
        print('ERROR: Set CLIENT_ID in .env first (a permanent UUID).')
        sys.exit(1)
    return client_id


def start_pin_flow():
    client_id = _load_client_id()
    os.makedirs(os.path.dirname(PIN_STATE_FILE), exist_ok=True)

    response = requests.post(
        'https://plex.tv/api/v2/pins?strong=true',
        headers=_headers(client_id),
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()

    pin_id = data['id']
    pin_code = data['code']

    with open(PIN_STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump({'id': pin_id, 'code': pin_code, 'client_id': client_id}, f, indent=2)

    auth_url = (
        'https://app.plex.tv/auth#'
        f'?clientID={client_id}'
        f'&code={pin_code}'
        f'&context%5Bdevice%5D%5Bproduct%5D={requests.utils.quote(PLEX_PRODUCT)}'
    )

    print('Plex PIN created.')
    print(f'PIN ID: {pin_id}')
    print(f'PIN code: {pin_code}')
    print()
    print('1) Open this URL in your browser and approve access:')
    print(auth_url)
    print()
    print('2) After approving, run:')
    print('   python3 plex_auth.py poll')
    return pin_id


def poll_pin_flow(timeout_seconds=300):
    client_id = _load_client_id()

    if not os.path.exists(PIN_STATE_FILE):
        print('No PIN state found. Run: python3 plex_auth.py start')
        sys.exit(1)

    with open(PIN_STATE_FILE, 'r', encoding='utf-8') as f:
        state = json.load(f)

    pin_id = state['id']
    if state.get('client_id') != client_id:
        print('WARNING: CLIENT_ID in .env does not match the PIN state file.')

    print(f'Waiting for Plex approval on PIN {pin_id} (up to {timeout_seconds}s)...')

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        response = requests.get(
            f'https://plex.tv/api/v2/pins/{pin_id}',
            headers=_headers(client_id),
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        token = data.get('authToken')
        if token:
            set_key(ENV_FILE, 'PLEX_TOKEN', token)
            print('SUCCESS: Saved new PLEX_TOKEN to .env')
            print('Run: python3 plex_auth.py test')
            return token

        time.sleep(2)

    print('Timed out waiting for approval. Open the URL from `python3 plex_auth.py start` and approve, then poll again.')
    sys.exit(1)


def test_token():
    load_dotenv(ENV_FILE)
    client_id = _load_client_id()
    token = os.getenv('PLEX_TOKEN', '').strip()
    if not token:
        print('ERROR: PLEX_TOKEN missing in .env')
        sys.exit(1)

    user = requests.get(
        'https://plex.tv/api/v2/user',
        headers={**_headers(client_id), 'X-Plex-Token': token},
        timeout=30,
    )
    print(f'plex.tv user API: {user.status_code}')
    if user.status_code != 200:
        print(user.text[:300])
        sys.exit(1)

    user_data = user.json()
    print(f"Plex user: {user_data.get('username') or user_data.get('title')}")

    watchlist = requests.get(
        'https://discover.provider.plex.tv/library/sections/watchlist/all',
        headers={**_headers(client_id), 'X-Plex-Token': token},
        params={'type': 18, 'includeCollections': 1, 'includeExternalMedia': 1},
        timeout=30,
    )
    print(f'watchlist API: {watchlist.status_code}')
    if watchlist.status_code != 200:
        print(watchlist.text[:300])
        sys.exit(1)

    print('Token works for Plex account + watchlist APIs.')


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1].lower()
    if command == 'start':
        start_pin_flow()
    elif command == 'poll':
        poll_pin_flow()
    elif command == 'test':
        test_token()
    else:
        print(f'Unknown command: {command}')
        sys.exit(1)


if __name__ == '__main__':
    main()
