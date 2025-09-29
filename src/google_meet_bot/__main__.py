import os
import sys

# Позволяет запускать как модуль (python -m google_meet_bot) и как скрипт по пути файла
if __package__ is None or __package__ == "":
    # Запуск как скрипт: добавим каталог src в sys.path
    pkg_dir = os.path.dirname(__file__)              # .../src/google_meet_bot
    src_dir = os.path.dirname(pkg_dir)               # .../src
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    from google_meet_bot.cli import main
else:
    from .cli import main

if __name__ == "__main__":
    main()


