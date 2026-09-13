"""Platform options from installed defaults, saved settings and cached source data."""
import json
from .models import BOSS_CITY_CODES
from .settings import APPLY_POLICY
from .task_store import KINDS


def option_catalog(config, rows=()):
    from .collectors.boss import FILTER_CODES
    cities = dict(BOSS_CITY_CODES)
    for row in rows:
        name = row.get("city") or ""
        if not name or name in cities:
            continue
        try:
            raw = json.loads(row.get("raw_json") or "{}")
            code = str(raw.get("city") or raw.get("cityCode") or "")
        except (ValueError, AttributeError):
            continue
        if code.isascii() and code.isdigit() and len(code) == 9:
            cities[name] = code
    for city in config.cities:
        if city.boss_code:
            cities[city.name] = city.boss_code
    filters = {key: [{"label": label, "value": value} for label, value in entries.items()]
               for key, entries in FILTER_CODES.items()}
    filters.setdefault("industry", [])
    return {"cities": [{"name": name, "boss_code": code} for name, code in cities.items()],
            "filters": filters, "task_kinds": [{"value": kind, "label": label} for kind, label in KINDS.items()],
            "apply_policy": APPLY_POLICY}
