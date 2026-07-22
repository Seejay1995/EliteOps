"""Tiny EDSM bodies client — "is this system already charted?"

Used by the Navigation Assistant so it never tells you to spend 10 minutes FSSing a
system the galaxy already knows. If EDSM has the system's bodies, we can judge what's
worth mapping (or skip) without a full FSS. Standard library only; any network error
returns None and the assistant falls back to its normal FSS advice.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_BASE = "https://www.edsm.net/api-system-v1/bodies"
_USER_AGENT = "EliteOps/1.0 (+exploration copilot)"

# subtypes worth a DSS map (mirrors the local heuristic)
_VALUABLE_SUBTYPES = {"Earth-like world", "Water world", "Ammonia world"}


def system_bodies(name: str, timeout: float = 12.0) -> dict[str, Any] | None:
    """Return {name, bodyCount, known, charted, bodies, worth_mapping} for a system,
    or None if EDSM can't be reached. `charted` is True only when EDSM knows every body."""
    name = (name or "").strip()
    if not name:
        return None
    url = _BASE + "?" + urllib.parse.urlencode({"systemName": name})
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    bodies = data.get("bodies") or []
    count = data.get("bodyCount")
    known = len(bodies)
    charted = bool(known and (count is None or known >= count))
    worth = []
    for b in bodies:
        sub = b.get("subType") or ""
        terra = (b.get("terraformingState") or "") in ("Candidate for terraforming", "Terraformable")
        if sub in _VALUABLE_SUBTYPES or terra:
            worth.append({"name": b.get("name"), "subType": sub, "terraformable": terra,
                          "isMapped": b.get("isMapped", False)})
    return {"name": data.get("name") or name, "bodyCount": count, "known": known,
            "charted": charted, "bodies": bodies, "worth_mapping": worth}
