from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def load_app_env() -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(env_path)
    load_dotenv()


load_app_env()


def get_env(name: str, default: str | None = None) -> str | None:
    return os.getenv(name, default)
