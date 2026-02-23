"""Tree manager: runtime management of compiled procedure trees.

Handles loading all YAML procedures, intent routing, and hot reload.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

import py_trees

from bt_engine.compiler import ProcedureCompiler

logger = logging.getLogger(__name__)


class TreeManager:
    """Manages compiled procedure trees at runtime.

    Scans a procedures directory, compiles all YAML files, and provides
    intent-based routing to tree factories.
    """

    def __init__(self, procedures_dir: str | Path = "procedures"):
        self.procedures_dir = Path(procedures_dir)
        self.compiler = ProcedureCompiler()

        # intent string -> procedure metadata
        self._intent_map: dict[str, dict] = {}
        # procedure id -> parsed procedure dict
        self._procedures: dict[str, dict] = {}
        # procedure id -> source yaml path
        self._sources: dict[str, Path] = {}

    def load_all(self) -> None:
        """Scan procedures directory and compile all YAML files."""
        if not self.procedures_dir.exists():
            logger.warning(f"Procedures directory not found: {self.procedures_dir}")
            return

        yaml_files = list(self.procedures_dir.glob("*.yaml")) + list(
            self.procedures_dir.glob("*.yml")
        )

        for path in yaml_files:
            try:
                self._load_file(path)
            except Exception as e:
                logger.error(f"Failed to load {path}: {e}")

        logger.info(
            f"TreeManager loaded {len(self._procedures)} procedures, "
            f"{len(self._intent_map)} intents"
        )

    def _load_file(self, path: Path) -> None:
        """Load and register a single YAML procedure file."""
        from bt_engine.compiler.parser import load_and_validate

        proc = load_and_validate(path)
        proc_id = proc["id"]

        self._procedures[proc_id] = proc
        self._sources[proc_id] = path

        # Register trigger intents
        for intent in proc.get("trigger_intents", []):
            # Normalize intent to a simple key
            intent_key = self._normalize_intent(intent)
            self._intent_map[intent_key] = {
                "proc_id": proc_id,
                "intent": intent,
                "path": path,
            }

    def get_tree_factory(self, intent: str) -> Callable[[], py_trees.trees.BehaviourTree] | None:
        """Get a tree factory for the given intent.

        Returns a callable that compiles a fresh tree each time (for clean state).
        Returns None if no procedure matches the intent.
        """
        intent_key = self._normalize_intent(intent)
        meta = self._intent_map.get(intent_key)
        if meta is None:
            return None

        proc = self._procedures.get(meta["proc_id"])
        if proc is None:
            return None

        # Return a factory that compiles a fresh tree each call
        def factory():
            return self.compiler.compile_from_dict(proc)

        return factory

    def get_all_intents(self) -> list[str]:
        """Return all registered trigger intent keys."""
        return list(self._intent_map.keys())

    def get_all_procedures(self) -> list[dict]:
        """Return metadata for all loaded procedures."""
        return [
            {
                "id": proc["id"],
                "name": proc["name"],
                "description": proc.get("description", ""),
                "trigger_intents": proc.get("trigger_intents", []),
            }
            for proc in self._procedures.values()
        ]

    def reload_file(self, path: str | Path) -> None:
        """Recompile a single YAML file (for hot reload).

        Removes old intent mappings for the procedure and re-registers.
        """
        path = Path(path)

        # Find and remove old mappings for this file
        old_proc_id = None
        for proc_id, source in self._sources.items():
            if source == path:
                old_proc_id = proc_id
                break

        if old_proc_id:
            # Remove old intent mappings
            self._intent_map = {
                k: v for k, v in self._intent_map.items()
                if v["proc_id"] != old_proc_id
            }
            del self._procedures[old_proc_id]
            del self._sources[old_proc_id]

        # Re-load
        self._load_file(path)
        logger.info(f"Reloaded procedure from {path}")

    def reload_all(self) -> None:
        """Clear all procedures and reload from disk."""
        self._intent_map.clear()
        self._procedures.clear()
        self._sources.clear()
        self.load_all()

    @staticmethod
    def _normalize_intent(intent: str) -> str:
        """Normalize an intent string for lookup.

        Maps various phrasings to canonical keys:
          'refund', 'return', 'money back' -> 'refund'
          'complaint', 'unhappy' -> 'complaint'
          'fraud alert', 'suspicious activity' -> 'fraud_alert'
        """
        intent = intent.strip().lower()

        # Direct canonical mappings
        canonical = {
            "refund": "refund",
            "return": "refund",
            "money back": "refund",
            "cancel order": "refund",
            "complaint": "complaint",
            "unhappy": "complaint",
            "dissatisfied": "complaint",
            "problem with": "complaint",
            "issue with": "complaint",
            "bad experience": "complaint",
            "fraud alert": "fraud_alert",
            "fraud_alert": "fraud_alert",
            "suspicious activity": "fraud_alert",
            "fraud investigation": "fraud_alert",
            "alert triage": "fraud_alert",
            "suspicious transaction": "fraud_alert",
        }

        return canonical.get(intent, intent.replace(" ", "_"))
