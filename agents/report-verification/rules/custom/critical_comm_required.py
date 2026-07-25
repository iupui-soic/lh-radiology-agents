"""Custom rule: a critical finding requires DOCUMENTED result communication.

Replaces the v1 YAML rule of the same id, which fired on every non-empty criticalFlags
regardless of what the report said -- so a signed report whose NOTIFICATION section already
documented the call still FAILed and parked at the sign-off gate (M4 run-book arc 2 signs and
pages immediately; the gate is arc 3's beat). The check the rule always described: FAIL only
when the impression carries a critical flag AND the report body does not document that the
result was communicated. A body that cannot be fetched still fails -- an unverifiable
communication is an undocumented one. Owner: Saptarshi.
"""
from __future__ import annotations

import re

# Documentation-of-communication phrasing, incl. the MIMIC-CXR notification idioms ("NOTIFICATION:",
# "findings were discussed with/relayed to Dr ___ by telephone/phone", "the team was notified/paged").
_COMM_EVIDENCE = re.compile(
    r"\bnotification\b|\bcommunicat\w*\b|\brelayed\b|\bnotified\b|\bpaged\b"
    r"|\bdiscussed with\b|\binformed\b|\btelephone\b|\bby phone\b",
    re.IGNORECASE,
)


def check(ctx: dict) -> dict | None:
    if not (ctx.get("impression") or {}).get("criticalFlags"):
        return None
    body = (ctx.get("report") or {}).get("body") or {}
    if body.get("present") and _COMM_EVIDENCE.search(body.get("text") or ""):
        return None  # communication is documented in the signed narrative
    return {
        "ruleId": "critical-comm-required",
        "severity": "FAIL",
        "message": "Critical finding present; ensure result communication is documented.",
        "location": "impression.criticalFlags",
    }
