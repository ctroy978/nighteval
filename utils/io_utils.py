"""Helpers for reading and writing project artifacts."""

import json
from pathlib import Path
from typing import Any


def read_json_file(path: str) -> Any:
    """Load JSON content from disk."""

    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")

    with target.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_text(path: Path, content: str) -> None:
    """Persist plain text to disk."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(content)


def write_json(path: Path, payload: Any) -> None:
    """Persist JSON to disk with indentation for readability."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
