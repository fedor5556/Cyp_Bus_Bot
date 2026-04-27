import os
import zipfile
import requests
import sys
import hashlib
from datetime import datetime

# Add parent directory to path so we can import config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config
from db.models import get_session, ScheduleVersion

def get_file_hash(filepath):
    hasher = hashlib.md5()
    with open(filepath, 'rb') as f:
        buf = f.read()
        hasher.update(buf)
    return hasher.hexdigest()

def download_static_gtfs():
    Config.ensure_directories()
    session = get_session()
    
    for city, siri_code in Config.CITIES.items():
        url = Config.STATIC_GTFS_URL_TEMPLATE.format(siri_code=siri_code)
        print(f"Downloading static GTFS for {city} from {url}...")
        
        city_dir = os.path.join(Config.STATIC_DATA_DIR, city)
        os.makedirs(city_dir, exist_ok=True)
        
        zip_path = os.path.join(city_dir, f"{city}_static_gtfs.zip")
        
        try:
            response = requests.get(url, stream=True, timeout=15)
            response.raise_for_status()
            
            with open(zip_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            new_hash = get_file_hash(zip_path)
            
            # Check if this hash is already the latest for this city
            latest_version = session.query(ScheduleVersion).filter(
                ScheduleVersion.city == city
            ).order_by(ScheduleVersion.download_time.desc()).first()
            
            if latest_version and latest_version.file_hash == new_hash:
                print(f"[{city}] Hash unchanged ({new_hash}). Skipping extraction.")
                os.remove(zip_path)
                continue
                
            print(f"[{city}] New schedule version detected! Extracting...")
            
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(city_dir)
                
            print(f"[{city}] Extraction complete to {city_dir}.")
            
            # Save the new version record
            new_ver = ScheduleVersion(city=city, file_hash=new_hash)
            session.add(new_ver)
            session.commit()
            
            # Clean up the zip file
            os.remove(zip_path)
            
        except requests.exceptions.RequestException as e:
            print(f"Network error downloading static GTFS for {city}: {e}")
        except zipfile.BadZipFile:
            print(f"Error: Downloaded file for {city} is not a valid zip file. URL might be incorrect or expired.")
            os.remove(zip_path) if os.path.exists(zip_path) else None
        except Exception as e:
            print(f"Unexpected error for {city}: {e}")
            
    session.close()

if __name__ == "__main__":
    download_static_gtfs()
