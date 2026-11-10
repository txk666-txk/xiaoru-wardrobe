from pathlib import Path
import sys
import os

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_API_BASE = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1")
KEY_OWNER = ""


DEBUG = False

MAX_CHUNK_SIZE = 4

LLM_VERS = "gpt-4o-mini"


def _resolve_base_dir():
    if getattr(sys, "frozen", False):
        if hasattr(sys, "_MEIPASS"):
            return Path(sys._MEIPASS).resolve()
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = str(_resolve_base_dir())

POPULATIONS_DIR = os.path.join(BASE_DIR, "agent_bank", "populations")
LLM_PROMPT_DIR = os.path.join(BASE_DIR, "simulation_engine", "prompt_template")
