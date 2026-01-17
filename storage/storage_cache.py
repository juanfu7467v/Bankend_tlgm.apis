from .storage_client import init_firebase
from .storage_utils import generate_cache_key, build_storage_path
import tempfile
import os


def get_cached_file(command: str, params: str):
    try:
        bucket = init_firebase()
        cache_key = generate_cache_key(command, params)

        blobs = list(bucket.list_blobs(prefix=f"telegram-results/{command}/{cache_key}"))
        if not blobs:
            return None

        blob = blobs[0]
        temp_file = tempfile.NamedTemporaryFile(delete=False)
        blob.download_to_filename(temp_file.name)

        return {
            "path": temp_file.name,
            "content_type": blob.content_type,
            "filename": os.path.basename(blob.name)
        }

    except Exception:
        return None


def save_file_to_cache(command: str, params: str, file_path: str, content_type: str):
    try:
        bucket = init_firebase()
        cache_key = generate_cache_key(command, params)

        extension = file_path.split(".")[-1]
        storage_path = build_storage_path(command, cache_key, extension)

        blob = bucket.blob(storage_path)
        blob.upload_from_filename(file_path, content_type=content_type)

        return True
    except Exception:
        return False
