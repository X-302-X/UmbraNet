import json
import os
import time

from config_utils import apply_config_migrations, sanitize_config
from rollback_paths import rollback_subdir

BACKUP_DIR = str(rollback_subdir("config"))



def ensure_backup_dir():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    return BACKUP_DIR



def create_config_backup(config: dict) -> str:
    ensure_backup_dir()
    sanitized, _warnings = sanitize_config(config)
    stamp = time.strftime("%Y-%m-%d-%H-%M-%S")
    path = os.path.join(BACKUP_DIR, f"config-{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sanitized, f, indent=2, ensure_ascii=False)
    return path



def list_config_backups():
    ensure_backup_dir()
    items = []
    for name in os.listdir(BACKUP_DIR):
        if not name.lower().endswith(".json"):
            continue
        path = os.path.join(BACKUP_DIR, name)
        if not os.path.isfile(path):
            continue
        stat = os.stat(path)
        items.append(
            {
                "name": name,
                "path": path,
                "mtime": stat.st_mtime,
                "size": stat.st_size,
            }
        )
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items



def load_config_backup(path: str):
    """Читает бэкап настроек и поднимает его до текущей версии схемы.

    Бэкап мог быть снят прошлой версией программы: без миграции восстановление
    вернуло бы старые грабли (например, выключатель DPI, которого больше нет).
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    migrated, _report = apply_config_migrations(raw)
    sanitized, _warnings = sanitize_config(migrated)
    return sanitized
