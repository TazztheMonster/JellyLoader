import logging
from flask import Flask, render_template, request, jsonify
import os
import threading
import requests
from urllib.parse import urlparse, parse_qs
import time
from datetime import datetime

app = Flask(__name__, static_folder="static")

# Basisverzeichnis festlegen
BASE_MEDIA_PATH = "/media/"

# Logging-Konfiguration
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),  # Ausgabe in die Konsole
        logging.FileHandler("jellyfin_downloader.log")  # Optional: Log-Datei
    ]
)

# Download variables
download_thread = None
download_status = {'is_paused': False, 'is_aborted': False, 'active': False}
current_progress = 0
total_progress = 100  # Placeholder for number of episodes
current_download_info = {'show_name': '', 'season': '', 'episode': ''}

# Zeitrahmen für erlaubte Downloads
START_HOUR = 23  # Startzeit (z.B. 02:00 Uhr)
END_HOUR = 5  # Endzeit (z.B. 05:00 Uhr)

API_TOKEN = "1a2b3c4d5e6f7g8h9i0j"
BASE_URL = "https://jellyfin.your.domain"

def is_within_allowed_time():
    """ Prüft, ob die aktuelle Zeit innerhalb des erlaubten Zeitraums liegt. """
    now = datetime.now()
    current_hour = now.hour

    if START_HOUR < END_HOUR:
        # Zeitspanne innerhalb desselben Tages (z.B. 02:00 bis 05:00)
        return START_HOUR <= current_hour < END_HOUR
    else:
        # Zeitspanne über Mitternacht hinaus (z.B. 23:00 bis 04:00)
        return current_hour >= START_HOUR or current_hour < END_HOUR

def manage_download_timing():
    """ Steuert automatisch das Pausieren/Fortsetzen des Downloads basierend auf der Zeit. """
    while download_status['active']:
        if is_within_allowed_time():
            # Wenn innerhalb des Zeitraums, fortsetzen
            if download_status['is_paused']:
                logging.info("INFO: Resuming download within allowed time range.")
                download_status['is_paused'] = False
        else:
            # Wenn außerhalb des Zeitraums, pausieren
            if not download_status['is_paused']:
                logging.info("INFO: Pausing download outside allowed time range.")
                download_status['is_paused'] = True
        time.sleep(60)  # Jede Minute prüfen

def list_subdirectories(base_path):
    """ Listet alle Unterverzeichnisse im angegebenen Basisverzeichnis auf. """
    try:
        return [f.name for f in os.scandir(base_path) if f.is_dir()]
    except Exception as e:
        logging.error(f"Failed to list directories in {base_path}: {e}")
        return []

def download_file(url, local_filename):
    """ Lädt die Datei herunter und berechnet den Fortschritt """
    with requests.get(url, stream=True, headers={"X-Emby-Token": API_TOKEN}) as r:
        try:
            r.raise_for_status()
            total_size = int(r.headers.get('Content-Length', 0))  # Gesamte Dateigröße
            downloaded_size = 0  # Bisher heruntergeladene Größe

            with open(local_filename, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if download_status['is_aborted']:
                        logging.info("Download aborted.")
                        return False
                    while download_status['is_paused']:
                        time.sleep(1)  # Pause loop
                    f.write(chunk)
                    downloaded_size += len(chunk)  # Fortschritt erhöhen
                    download_status['progress'] = (downloaded_size / total_size) * 100  # Prozentsatz berechnen

            return True
        except Exception as e:
            logging.error(f"Failed to download file from {url}: {e}")
            return False

def create_directory_structure(base_path, relative_path):
    """ Erstellt die Verzeichnisstruktur für den Download """
    full_path = os.path.join(base_path, relative_path)
    try:
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
    except Exception as e:
        logging.error(f"Failed to create directories for {full_path}: {e}")
    return full_path

def get_first_user_id():
    url = f"{BASE_URL}/emby/Users"
    try:
        response = requests.get(url, headers={"X-Emby-Token": API_TOKEN})
        response.raise_for_status()
        users = response.json()
        if users:
            return users[0]['Id']
        else:
            logging.error("No users found.")
    except Exception as e:
        logging.error(f"Failed to fetch users: {e}")
        return None

def get_items(parent_id, user_id):
    """ Ruft die Items (Staffeln, Episoden) der Jellyfin-API ab """
    url = f"{BASE_URL}/emby/Users/{user_id}/Items?ParentId={parent_id}"
    try:
        response = requests.get(url, headers={"X-Emby-Token": API_TOKEN})
        response.raise_for_status()
        items = response.json().get('Items', [])
        
        # Debugging: Logge die vollständigen Items, um die Struktur zu verstehen
        if not items:
            logging.error(f"No items found for Parent ID {parent_id}. Full Response: {response.json()}")
        return items
    except Exception as e:
        logging.error(f"Failed to fetch items from {url}: {e}")
        return []

def get_original_filename(episode_id):
    """ Abrufen des Originaldateinamens aus den MediaSources """
    url = f"{BASE_URL}/emby/Items/{episode_id}?api_key={API_TOKEN}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        media_sources = response.json().get('MediaSources', [])
        if media_sources:
            file_path = media_sources[0].get('Path', '')
            if file_path:
                return os.path.basename(file_path)
            else:
                logging.error(f"No file path found for episode {episode_id}")
                return None
        else:
            logging.error(f"No media sources found for episode {episode_id}")
            return None
    except Exception as e:
        logging.error(f"Failed to fetch media sources for episode {episode_id}: {e}")
        return None

def download_episode(episode_id, episode_name, relative_path, base_path):
    global current_download_info
    original_filename = get_original_filename(episode_id)
    if original_filename:
        local_filename = os.path.join(relative_path, original_filename)
    else:
        local_filename = os.path.join(relative_path, f"{episode_name}.mp4")

    local_path = create_directory_structure(base_path, local_filename)
    download_url = f"{BASE_URL}/emby/Items/{episode_id}/Download"
    current_download_info['episode'] = episode_name
    logging.info(f"Downloading episode {episode_name}")
    return download_file(download_url, local_path)

def download_series(jellyfin_url, base_path):
    global current_progress, total_progress, current_download_info, download_status

    user_id = get_first_user_id()
    if not user_id:
        return

    parsed_url = urlparse(jellyfin_url)
    fragment = parsed_url.fragment.lstrip('/')
    cleaned_fragment = fragment.replace('details?', '')
    fragment_params = parse_qs(cleaned_fragment)
    parent_id = fragment_params.get('id', [None])[0]

    if not parent_id:
        logging.error("Failed to extract media/season ID from the URL.")
        return

    # Abrufen der Items (Staffeln oder Serie)
    seasons = get_items(parent_id, user_id)
    if not seasons:
        logging.error("No seasons found.")
        return

    # Versuchen, den Seriennamen korrekt zu extrahieren
    if 'SeriesName' in seasons[0]:
        show_name = seasons[0]['SeriesName']
    elif 'Name' in seasons[0]:
        show_name = seasons[0]['Name']
    else:
        show_name = 'Unknown_Show'

    current_download_info['show_name'] = show_name
    logging.info(f"Detected series: {show_name}")

    total_progress = sum(len(get_items(season['Id'], user_id)) for season in seasons if season['Type'] == 'Season')

    current_progress = 0
    download_status['active'] = True

    timing_thread = threading.Thread(target=manage_download_timing)
    timing_thread.start()

    # Loop durch die Staffeln und laden der Episoden
    for season in seasons:
        if season['Type'] == 'Season':
            season_id = season['Id']
            season_number = season.get('IndexNumber', 'Unknown_Season')
            current_download_info['season'] = f"Season {season_number}"
            episodes = get_items(season_id, user_id)
            for episode in episodes:
                if episode['Type'] == 'Episode':
                    current_progress += 1
                    if download_episode(episode['Id'], episode['Name'], os.path.join(show_name, f"Season_{season_number}"), base_path):
                        logging.info(f"Episode {current_progress}/{total_progress} downloaded.")
                    else:
                        download_status['active'] = False
                        return
    download_status['active'] = False


@app.route('/')
def index():
    subdirectories = list_subdirectories(BASE_MEDIA_PATH)
    return render_template('index.html', subdirectories=subdirectories)


@app.route('/start_download', methods=['POST'])
def start_download():
    global download_thread, download_status

    jellyfin_url = request.form['jellyfin_url']
    selected_subdirectory = request.form['selected_subdirectory']

    download_dir = os.path.join(BASE_MEDIA_PATH, selected_subdirectory)

    # Set initial status based on current time
    download_status = {'is_paused': not is_within_allowed_time(), 'is_aborted': False, 'active': True}

    # Start download in a new thread
    download_thread = threading.Thread(target=download_series, args=(jellyfin_url, download_dir))
    download_thread.start()

    return jsonify({'status': 'Download started' if not download_status['is_paused'] else 'Download paused (outside allowed time)', 'directory': download_dir})


@app.route('/pause_download', methods=['POST'])
def pause_download():
    global download_status
    download_status['is_paused'] = True
    return jsonify({'status': 'Download paused'})


@app.route('/resume_download', methods=['POST'])
def resume_download():
    global download_status
    download_status['is_paused'] = False
    return jsonify({'status': 'Download resumed'})


@app.route('/abort_download', methods=['POST'])
def abort_download():
    global download_status
    download_status['is_aborted'] = True
    download_status['active'] = False
    return jsonify({'status': 'Download aborted'})


@app.route('/progress', methods=['GET'])
def progress():
    return jsonify({
        'current': current_progress,
        'total': total_progress,
        'download_info': current_download_info,
        'status': download_status,
        'file_progress': download_status.get('progress', 0)  # Prozentsatz des aktuellen Dateidownloads
    })



if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
