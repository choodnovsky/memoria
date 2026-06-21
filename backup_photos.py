#!/usr/bin/env python3
"""
Бекап новых фото из Photos.app (.photoslibrary/originals) на Яндекс.Диск.

Логика:
  1. Рекурсивно обходим SOURCE_DIR, фильтруем по расширениям из PHOTO_EXTENSIONS.
  2. Для каждого файла считаем SHA1.
  3. Если хеша нет в локальном манифесте (manifest.json) -> файл новый -> заливаем.
  4. Путь на Диске строится по дате модификации файла: /Backup/Photos/YYYY/MM/filename.ext
  5. После успешной заливки хеш + метаданные пишутся в манифест.

Настройка:
  Все ключи/токены и пути берутся из файла .env (см. .env.example рядом со скриптом).
  Скопируй .env.example в .env и заполни своими значениями.

Запуск:
  python3 backup_photos.py                    # обычный запуск
  python3 backup_photos.py --dry-run           # показать что было бы залито, ничего не трогая
  python3 backup_photos.py --rebuild-manifest  # пересобрать манифест по данным с Диска (если manifest.json потерян)
"""

import os
import sys
import json
import hashlib
import argparse
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from PIL import Image, ExifTags
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pillow_heif = None

# ──────────────────────────────────────────────────────────────────
# НАСТРОЙКИ — загружаются из .env (лежит рядом со скриптом)
# ──────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

TOKEN = os.environ.get("YADISK_TOKEN")
# Дефолт строится динамически от домашней папки текущего пользователя (~),
# вместо хардкода конкретного имени пользователя в пути.
_default_source_dir = os.path.join(
    os.path.expanduser("~"),
    "Pictures",
    "Медиатека Фото.photoslibrary",
    "originals",
)
SOURCE_DIR = os.path.expanduser(os.environ.get("SOURCE_DIR") or _default_source_dir)
DISK_BASE_PATH = os.environ.get("DISK_BASE_PATH")
MANIFEST_PATH = os.environ.get("MANIFEST_PATH")

_extensions_raw = os.environ.get("PHOTO_EXTENSIONS")
PHOTO_EXTENSIONS = {ext.strip().lower() for ext in _extensions_raw.split(",") if ext.strip()}

# Пауза между заливками файлов (секунды). Нужна, чтобы не упираться в rate limit
# Яндекс.Диска при заливке тысяч файлов подряд. Можно поставить 0, если не нужна.
UPLOAD_DELAY_SECONDS = float(os.environ.get("UPLOAD_DELAY_SECONDS", "0.5"))

# Читаемые имена вида 2020-10-15_14-32-08.jpeg на основе EXIF даты съёмки.
# Если в .env не задано — по умолчанию включено.
USE_EXIF_NAMES = os.environ.get("USE_EXIF_NAMES", "true").strip().lower() in ("1", "true", "yes")

API_BASE = "https://cloud-api.yandex.net/v1/disk"

# Find EXIF tag id for DateTimeOriginal once
_DATETIME_ORIGINAL_TAG = None
for _tag_id, _tag_name in ExifTags.TAGS.items():
    if _tag_name == "DateTimeOriginal":
        _DATETIME_ORIGINAL_TAG = _tag_id
        break

# ──────────────────────────────────────────────────────────────────


def get_headers():
    if not TOKEN:
        print("Ошибка: YADISK_TOKEN не задан. Проверь файл .env (см. .env.example).")
        sys.exit(1)
    return {"Authorization": f"OAuth {TOKEN}"}


def sha1_of_file(path, chunk_size=1024 * 1024):
    """Считает SHA1 файла по чанкам, не загружая всё в память."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def load_manifest():
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"uploaded": {}}  # hash -> {"path": disk_path, "uploaded_at": iso_ts, "local_path": str}


def save_manifest(manifest):
    tmp_path = MANIFEST_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, MANIFEST_PATH)  # атомарная замена, чтобы не словить битый файл при обрыве


def scan_source_dir(source_dir):
    """Рекурсивно собирает список файлов-фото."""
    files = []
    for root, _dirs, filenames in os.walk(source_dir):
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if ext in PHOTO_EXTENSIONS:
                files.append(os.path.join(root, name))
    return files


def get_capture_datetime(path):
    """
    Пытается прочитать дату съёмки из EXIF (DateTimeOriginal).
    Если не получилось (нет EXIF, битый файл, неподдерживаемый формат) —
    возвращает None, вызывающий код должен откатиться на mtime.
    """
    if _DATETIME_ORIGINAL_TAG is None:
        return None
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return None
            raw = exif.get(_DATETIME_ORIGINAL_TAG)
            if not raw:
                # На некоторых HEIC/iOS-файлах дата лежит в IFD Exif, а не в основном блоке
                try:
                    exif_ifd = exif.get_ifd(0x8769)  # Exif IFD pointer
                    raw = exif_ifd.get(_DATETIME_ORIGINAL_TAG)
                except Exception:
                    raw = None
            if not raw:
                return None
            # формат EXIF: "2020:10:15 14:32:08"
            return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
    except Exception:
        return None


def get_file_datetime(path):
    """Дата для сортировки по папкам и для имени файла: EXIF, либо mtime как fallback."""
    if USE_EXIF_NAMES:
        dt = get_capture_datetime(path)
        if dt is not None:
            return dt, True  # True = дата из EXIF
    mtime = os.path.getmtime(path)
    return datetime.fromtimestamp(mtime), False  # False = дата из mtime (fallback)


def build_disk_path(local_path, taken_names):
    """
    Строит путь на Диске: /Backup/Photos/YYYY/MM/имя.ext

    Если USE_EXIF_NAMES включён и дата съёмки прочиталась — имя вида
    2020-10-15_14-32-08.jpeg. Если дата не прочиталась (fallback на mtime)
    или EXIF-имена выключены — используется оригинальное имя файла (UUID).

    taken_names — set уже занятых disk_path (из манифеста + файлов в этом
    запуске), чтобы при коллизии (несколько кадров в одну секунду) добавить
    суффикс _2, _3... и не затереть/не спутать файлы.
    """
    dt, is_exif = get_file_datetime(local_path)
    ext = os.path.splitext(local_path)[1]  # сохраняем регистр расширения как в оригинале

    if USE_EXIF_NAMES and is_exif:
        base_name = dt.strftime("%Y-%m-%d_%H-%M-%S")
        candidate = f"{DISK_BASE_PATH}/{dt.year:04d}/{dt.month:02d}/{base_name}{ext}"
        if candidate not in taken_names:
            taken_names.add(candidate)
            return candidate
        # коллизия — добавляем суффикс
        suffix = 2
        while True:
            candidate = f"{DISK_BASE_PATH}/{dt.year:04d}/{dt.month:02d}/{base_name}_{suffix}{ext}"
            if candidate not in taken_names:
                taken_names.add(candidate)
                return candidate
            suffix += 1
    else:
        # нет EXIF-даты или EXIF-имена выключены — используем оригинальное имя файла (UUID)
        filename = os.path.basename(local_path)
        candidate = f"{DISK_BASE_PATH}/{dt.year:04d}/{dt.month:02d}/{filename}"
        taken_names.add(candidate)
        return candidate


def ensure_disk_folder(path, cache=set(), max_retries=5):
    """
    Создаёт папку на Диске, если её ещё нет. Создаёт по частям, т.к. Яндекс
    не создаёт вложенные папки рекурсивно за один вызов.
    cache — чтобы не дёргать API повторно для папок, которые уже точно созданы в этом запуске.
    """
    if path in cache:
        return
    parts = path.strip("/").split("/")
    current = ""
    for part in parts:
        current += "/" + part
        if current in cache:
            continue

        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.put(
                    f"{API_BASE}/resources",
                    headers=get_headers(),
                    params={"path": current},
                    timeout=30,
                )
                break
            except requests.exceptions.RequestException as e:
                if attempt == max_retries:
                    raise RuntimeError(f"Не удалось создать папку {current} после {max_retries} попыток: {e}")
                wait = min(2 ** attempt, 60)
                print(f"\n  Сетевая ошибка при создании папки {current}: {e}, повтор через {wait}с...")
                time.sleep(wait)

        # 201 - создана, 409 - уже существует. Оба ок.
        if resp.status_code not in (201, 409):
            raise RuntimeError(f"Не удалось создать папку {current}: {resp.status_code} {resp.text}")
        cache.add(current)
    cache.add(path)


def upload_file(local_path, disk_path, max_retries=5):
    """Двухшаговая заливка: получить upload-ссылку -> PUT файла."""
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(
                f"{API_BASE}/resources/upload",
                headers=get_headers(),
                params={"path": disk_path, "overwrite": "false"},
                timeout=30,
            )
            if resp.status_code == 409:
                # Файл с таким именем уже есть на Диске (но не в манифесте) — не перезаписываем, просто отметим как залитый
                return "already_exists"
            resp.raise_for_status()
            upload_url = resp.json()["href"]

            print(f"  заливаю...", end=" ", flush=True)
            t0 = time.time()
            with open(local_path, "rb") as f:
                put_resp = requests.put(upload_url, data=f, timeout=120)
            elapsed = time.time() - t0
            print(f"{elapsed:.1f}с")

            if put_resp.status_code in (201, 202):
                return "ok"
            elif put_resp.status_code == 429 or put_resp.status_code >= 500:
                wait = min(2 ** attempt, 60)
                print(f"  Сервер занят ({put_resp.status_code}), жду {wait}с...")
                time.sleep(wait)
                continue
            else:
                raise RuntimeError(f"Ошибка заливки {put_resp.status_code}: {put_resp.text}")

        except requests.exceptions.Timeout:
            wait = min(2 ** attempt, 60)
            print(f"\n  Таймаут (>120с) на PUT, повтор через {wait}с...")
            time.sleep(wait)
        except requests.exceptions.RequestException as e:
            wait = min(2 ** attempt, 60)
            print(f"\n  Сетевая ошибка: {e}, повтор через {wait}с...")
            time.sleep(wait)

    raise RuntimeError(f"Не удалось залить {local_path} после {max_retries} попыток")


def rebuild_manifest_from_disk():
    """
    Пересобирает манифест, опрашивая Яндекс.Диск рекурсивно по DISK_BASE_PATH
    и забирая md5 каждого файла. Используется, если manifest.json потерян.
    Хеши на Диске — md5, поэтому при rebuild манифест хранит md5, а не sha1
    (это нормально, главное — сверка идёт по тому же алгоритму при следующих запусках,
    поэтому после rebuild лучше один раз прогнать обычный запуск, чтобы досчитать sha1 локально).
    """
    print("Пересборка манифеста по данным с Диска...")
    manifest = {"uploaded": {}}
    offset = 0
    limit = 200

    def walk_disk(path):
        nonlocal manifest
        offset = 0
        while True:
            resp = requests.get(
                f"{API_BASE}/resources",
                headers=get_headers(),
                params={"path": path, "limit": limit, "offset": offset, "fields": "_embedded.items.path,_embedded.items.type,_embedded.items.md5,_embedded.items.name"},
                timeout=30,
            )
            if resp.status_code == 404:
                return
            resp.raise_for_status()
            data = resp.json()
            items = data.get("_embedded", {}).get("items", [])
            if not items:
                break
            for item in items:
                if item["type"] == "dir":
                    walk_disk(item["path"].replace("disk:", ""))
                else:
                    md5 = item.get("md5")
                    if md5:
                        manifest["uploaded"][md5] = {
                            "path": item["path"],
                            "uploaded_at": None,
                            "local_path": None,
                            "hash_type": "md5",
                        }
            offset += limit
            if len(items) < limit:
                break

    walk_disk(DISK_BASE_PATH)
    save_manifest(manifest)
    print(f"Готово. В манифест добавлено {len(manifest['uploaded'])} файлов.")
    print("Хеши взяты как md5 (с Диска). При следующих запусках сверка будет идти по sha1 — "
          "первый прогон после rebuild может посчитать файлы 'новыми', если их sha1 ещё не в манифесте. "
          "Это нормально: дубликат не создастся, т.к. заливка с overwrite=false вернёт 409 и файл просто отметится залитым.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Показать что будет залито, ничего не отправлять")
    parser.add_argument("--rebuild-manifest", action="store_true", help="Пересобрать манифест по данным с Диска")
    args = parser.parse_args()

    if args.rebuild_manifest:
        rebuild_manifest_from_disk()
        return

    if not os.path.isdir(SOURCE_DIR):
        print(f"Папка не найдена: {SOURCE_DIR}")
        sys.exit(1)

    print(f"Сканирую {SOURCE_DIR} ...")
    all_files = scan_source_dir(SOURCE_DIR)
    print(f"Найдено файлов-фото: {len(all_files)}")

    manifest = load_manifest()
    uploaded_hashes = set(manifest["uploaded"].keys())
    # все disk_path, которые уже заняты согласно манифесту — нужно для защиты от коллизий имён
    taken_names = {entry["path"] for entry in manifest["uploaded"].values() if entry.get("path")}

    new_files = []
    print("Считаю хеши и сверяю с манифестом...")
    for i, path in enumerate(all_files, 1):
        if i % 200 == 0:
            print(f"  обработано {i}/{len(all_files)}")
        try:
            file_hash = sha1_of_file(path)
        except OSError as e:
            print(f"  не смог прочитать {path}: {e}")
            continue
        if file_hash not in uploaded_hashes:
            new_files.append((path, file_hash))

    print(f"\nНовых файлов к заливке: {len(new_files)}")

    if not new_files:
        print("Нечего заливать, манифест актуален.")
        return

    if args.dry_run:
        print("\n[dry-run] Файлы, которые были бы залиты:")
        # копия taken_names, чтобы dry-run не портил состояние для реального запуска
        preview_taken = set(taken_names)
        for path, _ in new_files:
            disk_path = build_disk_path(path, preview_taken)
            print(f"  {path}  ->  {disk_path}")
        return

    folder_cache = set()
    ok_count = 0
    fail_count = 0

    for idx, (path, file_hash) in enumerate(new_files, 1):
        disk_path = build_disk_path(path, taken_names)
        disk_folder = os.path.dirname(disk_path)
        print(f"[{idx}/{len(new_files)}] {os.path.basename(path)} -> {disk_path}", flush=True)
        try:
            ensure_disk_folder(disk_folder, cache=folder_cache)
            result = upload_file(path, disk_path)
            manifest["uploaded"][file_hash] = {
                "path": disk_path,
                "uploaded_at": datetime.now().isoformat(),
                "local_path": path,
                "hash_type": "sha1",
            }
            ok_count += 1
            # сохраняем манифест после каждого файла — если скрипт прервётся,
            # уже залитые файлы не попытаются залиться повторно
            save_manifest(manifest)
        except Exception as e:
            print(f"  ОШИБКА: {e}")
            fail_count += 1

        if UPLOAD_DELAY_SECONDS > 0 and idx < len(new_files):
            time.sleep(UPLOAD_DELAY_SECONDS)

    print(f"\nГотово. Залито: {ok_count}, ошибок: {fail_count}")


if __name__ == "__main__":
    main()