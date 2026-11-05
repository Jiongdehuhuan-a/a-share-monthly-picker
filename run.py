# -*- coding: utf-8 -*-
"""启动入口：python run.py serve [--port 8770] [--schedule]"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
