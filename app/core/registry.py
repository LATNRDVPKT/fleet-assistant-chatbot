"""Loads the YAML config files (vehicles, glossary, manifest)."""
import yaml

from app.core.config import get_settings

_vehicles: list[dict] | None = None
_glossary: dict | None = None


def _load(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def vehicles() -> list[dict]:
    """The vehicle registry (config/vehicles.yaml), read once at first use."""
    global _vehicles
    if _vehicles is None:
        _vehicles = _load(get_settings().vehicles_path)["vehicles"]
    return _vehicles


def glossary() -> dict:
    """Synonyms, vocabulary and safety patterns (config/glossary.yaml), read once at first use."""
    global _glossary
    if _glossary is None:
        _glossary = _load(get_settings().glossary_path)
    return _glossary


def manifest() -> dict:
    """The ingestion manifest is re-read every time, so edits are picked up immediately."""
    return _load(get_settings().manifest_path)


def vehicle_models() -> list[str]:
    return [v["model"] for v in vehicles()]


def find_vehicle(model: str | None) -> dict | None:
    """'hx400', 'HX-400', 'hexa' → the HX-400 registry entry (or None)."""
    if not model:
        return None
    m = model.strip().lower()
    for v in vehicles():
        if m == v["model"].lower() or m in v["aliases"]:
            return v
    return None
