from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CatalogMatch:
    name: str
    genus: str
    base_value: int
    colony_range_m: int | None


def normalize_organic_name(value: str) -> str:
    text = str(value or "").strip().casefold()
    text = text.replace("$", "").replace(";", "")
    text = re.sub(r"^codex_ent_", "", text)
    text = re.sub(r"_(name|genus_name)$", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


class ExobiologyCatalog:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else Path(__file__).resolve().parent / "Data" / "exobiology_catalog.json"
        self.version = "unknown"
        self.first_logged_multiplier = 5
        self.source = ""
        self._species: dict[str, CatalogMatch] = {}
        self._ranges: dict[str, int] = {}
        self._genus_values: dict[str, list[int]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return
        self.version = str(raw.get("catalogVersion") or "unknown")
        self.source = str(raw.get("source") or "")
        self.first_logged_multiplier = max(1, int(raw.get("firstLoggedMultiplier") or 5))
        for genus, distance in dict(raw.get("genusColonyRangesMeters") or {}).items():
            try:
                self._ranges[normalize_organic_name(genus)] = int(distance)
            except (TypeError, ValueError):
                continue
        for item in list(raw.get("species") or []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            try:
                value = int(item.get("baseValue") or 0)
            except (TypeError, ValueError):
                continue
            genus = str(item.get("genus") or name.split(" ", 1)[0]).strip()
            colony_range = item.get("colonyRangeMeters")
            if colony_range is None:
                colony_range = self._ranges.get(normalize_organic_name(genus))
            try:
                colony_range = int(colony_range) if colony_range is not None else None
            except (TypeError, ValueError):
                colony_range = None
            match = CatalogMatch(name=name, genus=genus, base_value=value, colony_range_m=colony_range)
            self._species[normalize_organic_name(name)] = match
            self._genus_values.setdefault(normalize_organic_name(genus), []).append(value)
            for alias in item.get("aliases", []) or []:
                self._species[normalize_organic_name(str(alias))] = match

    def resolve(self, *names: str) -> CatalogMatch | None:
        normalized = [normalize_organic_name(name) for name in names if name]
        for name in normalized:
            if name in self._species:
                return self._species[name]
        # Variant labels often append a colour/morph after the species name.
        for name in normalized:
            candidates = [match for key, match in self._species.items() if key and (name.startswith(key + " ") or key.startswith(name + " "))]
            unique = {match.name: match for match in candidates}
            if len(unique) == 1:
                return next(iter(unique.values()))
        return None

    def resolve_contained(self, *names: str) -> CatalogMatch | None:
        candidates: dict[str, CatalogMatch] = {}
        for raw in names:
            text = normalize_organic_name(raw)
            if not text:
                continue
            padded = f" {text} "
            for key, match in self._species.items():
                if key and f" {key} " in padded:
                    candidates[match.name] = match
        return next(iter(candidates.values())) if len(candidates) == 1 else None

    def colony_range_for_genus(self, genus: str) -> int | None:
        return self._ranges.get(normalize_organic_name(genus))


    def value_range_for_genus(self, genus: str) -> tuple[int | None, int | None]:
        values = self._genus_values.get(normalize_organic_name(genus), [])
        if not values:
            return None, None
        return min(values), max(values)

    def species_for_genus(self, genus: str) -> list[CatalogMatch]:
        key = normalize_organic_name(genus)
        unique: dict[str, CatalogMatch] = {}
        for match in self._species.values():
            if normalize_organic_name(match.genus) == key:
                unique[match.name] = match
        return sorted(unique.values(), key=lambda item: item.name.casefold())

    def enrich(self, metadata: dict[str, Any], genus: str = "", species: str = "", variant: str = "") -> dict[str, Any]:
        result = dict(metadata)
        match = self.resolve(variant, species)
        if match:
            result.setdefault("baseValue", match.base_value)
            result.setdefault("estimatedValue", match.base_value)
            if match.colony_range_m is not None:
                result.setdefault("colonyRangeMeters", match.colony_range_m)
        elif genus:
            colony_range = self.colony_range_for_genus(genus)
            if colony_range is not None:
                result.setdefault("colonyRangeMeters", colony_range)
        return result


DEFAULT_CATALOG = ExobiologyCatalog()
