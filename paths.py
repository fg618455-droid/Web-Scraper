"""Zentrale Pfad-Auflösung für User-Daten (DB, Session-Cookies).

Frozen .exe: %APPDATA%\\DealScraper\\<file>  — überlebt App-Updates weil
das Verzeichnis NICHT im Install-Ordner liegt.

Dev (python main.py): Projekt-Verzeichnis wie bisher — kein Behavior-Wechsel
für die Entwicklung.

Migration: Beim ersten Lookup im neuen Pfad wird geschaut, ob am alten Ort
(direkt neben der .exe oder in _internal/) bereits Daten liegen. Wenn ja,
werden sie einmalig nach %APPDATA% kopiert.
"""
from __future__ import annotations

import os
import shutil
import sys


def _frozen() -> bool:
    return bool(getattr(sys, 'frozen', False))


def user_data_dir() -> str:
    """Wo persistente User-Daten landen.

    Frozen: %APPDATA%\\DealScraper\\
    Dev: Projekt-Wurzel (gleicher Ordner wie database.py)
    """
    if _frozen():
        base = os.environ.get('APPDATA') or os.path.expanduser('~')
        path = os.path.join(base, 'DealScraper')
        os.makedirs(path, exist_ok=True)
        return path
    return os.path.dirname(os.path.abspath(__file__))


def _legacy_candidates(filename: str) -> list[str]:
    """Frühere Speicherorte, von denen wir bei Bedarf migrieren."""
    if not _frozen():
        return []
    exe_dir = os.path.dirname(sys.executable)
    return [
        os.path.join(exe_dir, filename),
        os.path.join(exe_dir, '_internal', filename),
    ]


def resolve_user_file(filename: str) -> str:
    """Gibt den finalen Pfad für eine User-Datei zurück.

    Wenn die Datei noch nicht im neuen Verzeichnis existiert aber an einem
    Legacy-Ort gefunden wird, kopiert sie einmalig dorthin — so verlieren
    User aus älteren Versionen keine Daten beim Update.
    """
    target = os.path.join(user_data_dir(), filename)
    if os.path.exists(target):
        return target

    for legacy in _legacy_candidates(filename):
        if os.path.isfile(legacy):
            try:
                shutil.copy2(legacy, target)
            except Exception:
                pass
            break
    return target
