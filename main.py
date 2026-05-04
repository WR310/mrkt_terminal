import os
import sys
import logging
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

load_dotenv()

# === Настройка логирования: консоль + файл с ротацией ===
DATA_DIR = "data"
LOG_FILE = os.path.join(DATA_DIR, "terminal.log")
os.makedirs(DATA_DIR, exist_ok=True)


def _setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    if getattr(root, "_mrkt_configured", False):
        return

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=5 * 1024 * 1024,  # 5 МБ
        backupCount=2,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler(sys.__stdout__)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(fmt)

    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Зеркалим print() в logger, чтобы все модули писались в файл без правок
    class _PrintToLog:
        def __init__(self, level=logging.INFO):
            self._buf = ""
            self._level = level
            self._logger = logging.getLogger("stdout")

        def write(self, msg):
            if not msg:
                return
            self._buf += msg
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                line = line.rstrip()
                if line:
                    self._logger.log(self._level, line)

        def flush(self):
            if self._buf.strip():
                self._logger.log(self._level, self._buf.strip())
            self._buf = ""

    sys.stdout = _PrintToLog(logging.INFO)
    sys.stderr = _PrintToLog(logging.ERROR)

    root._mrkt_configured = True
    logging.getLogger("MRKT").info("Логирование инициализировано: %s", LOG_FILE)


_setup_logging()

# Импортируем GUI после настройки логов
from gui import main

if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.getLogger("MRKT").exception("Фатальная ошибка терминала")
        raise
