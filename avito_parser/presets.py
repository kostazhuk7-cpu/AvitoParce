"""
Presets management for saved search configurations.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

PRESETS_FILE = "presets.json"


class PresetManager:
    """Manages saved search presets."""

    def __init__(self, presets_path: str = PRESETS_FILE) -> None:
        self._path = Path(presets_path)
        self._presets: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        """Load presets from file."""
        if not self._path.exists():
            logger.debug(f"Presets file not found: {self._path}")
            return

        try:
            with open(self._path, "r", encoding="utf-8") as f:
                self._presets = json.load(f)
            logger.info(f"Loaded {len(self._presets)} presets")
        except Exception as e:
            logger.error(f"Failed to load presets: {e}")

    def _save(self) -> None:
        """Save presets to file."""
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._presets, f, ensure_ascii=False, indent=2)
            logger.info(f"Saved {len(self._presets)} presets")
        except Exception as e:
            logger.error(f"Failed to save presets: {e}")

    def list_presets(self) -> List[Dict[str, Any]]:
        """List all presets with basic info."""
        result = []
        for key, preset in self._presets.items():
            result.append({
                "key": key,
                "name": preset.get("name", key),
                "description": preset.get("description", ""),
                "city": preset.get("city", "moskva"),
                "query": preset.get("query", ""),
            })
        return result

    def get_preset(self, key: str) -> Optional[Dict[str, Any]]:
        """Get preset by key."""
        return self._presets.get(key)

    def add_preset(self, key: str, config: Dict[str, Any]) -> None:
        """Add or update a preset."""
        self._presets[key] = config
        self._save()
        logger.info(f"Preset saved: {key}")

    def delete_preset(self, key: str) -> bool:
        """Delete a preset by key."""
        if key in self._presets:
            del self._presets[key]
            self._save()
            logger.info(f"Preset deleted: {key}")
            return True
        return False

    def print_presets_table(self) -> None:
        """Print formatted table of presets."""
        presets = self.list_presets()
        if not presets:
            print("Пресеты не найдены. Создайте presets.json или добавьте через GUI.")
            return

        print("\n=== ДОСТУПНЫЕ ПРЕСЕТЫ ===\n")
        print(f"{'Ключ':<20} {'Название':<25} {'Город':<15} {'Запрос'}")
        print("-" * 80)
        for p in presets:
            print(f"{p['key']:<20} {p['name']:<25} {p['city']:<15} {p['query']}")
        print()
