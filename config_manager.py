import os
import json
import threading

DATA_DIR = "data"
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

_LOCK = threading.Lock()

DEFAULTS: dict = {
    "engine_discount": 15.0,
    "offers_discount": 15.0,
    "sniper_discount": 20.0,
    "calc_discount": 15.0,
    "calc_undercut_ton": 0.001,
    "engine_backdrops": {
        "Любой": True,
        "Black": False,
        "Gold": False,
        "Satin Gold": False,
    },
}


def _ensure_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def load_config() -> dict:
    """Грузит config.json. Если файла нет/битый — возвращает копию DEFAULTS."""
    _ensure_dir()
    if not os.path.exists(CONFIG_FILE):
        return json.loads(json.dumps(DEFAULTS))

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        merged = json.loads(json.dumps(DEFAULTS))
        for k, v in data.items():
            if k == "engine_backdrops" and isinstance(v, dict):
                merged["engine_backdrops"].update(v)
            else:
                merged[k] = v
        return merged
    except Exception as e:
        print(f"[CONFIG] Ошибка чтения {CONFIG_FILE}: {e}. Использую дефолты.")
        return json.loads(json.dumps(DEFAULTS))


def save_config(cfg: dict) -> None:
    """Атомарно пишет config.json."""
    _ensure_dir()
    with _LOCK:
        tmp = CONFIG_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_FILE)
        except Exception as e:
            print(f"[CONFIG] Ошибка записи: {e}")


def update_value(key: str, value) -> None:
    """Обновляет один ключ верхнего уровня и сразу сохраняет."""
    cfg = load_config()
    cfg[key] = value
    save_config(cfg)


def update_backdrop(name: str, value: bool) -> None:
    """Обновляет состояние одного чекбокса фона и сохраняет."""
    cfg = load_config()
    cfg.setdefault("engine_backdrops", {})[name] = bool(value)
    save_config(cfg)
