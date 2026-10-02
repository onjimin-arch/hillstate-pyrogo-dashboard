from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["poc"]["start"] = date.fromisoformat(str(cfg["poc"]["start"]))
    cfg["poc"]["end"] = date.fromisoformat(str(cfg["poc"]["end"]))
    return cfg


CFG = load_config()
