"""Conservative assembly of physical SMS records into logical messages."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any


@dataclass(frozen=True)
class LogicalMessage:
    id: str
    number: str
    text: str
    date: str
    parts: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class AssemblyResult:
    complete: tuple[LogicalMessage, ...]
    pending: tuple[dict[str, Any], ...]
    ambiguous: tuple[dict[str, Any], ...]


def _message_id(parts: list[dict[str, Any]]) -> str:
    identity = {
        "number": parts[0]["Number"],
        "date": min(str(part.get("Date") or "") for part in parts),
        "reference": parts[0].get("Reference"),
        "reference_bits": parts[0].get("ReferenceBits"),
        "fingerprints": sorted(part["Fingerprint"] for part in parts),
    }
    encoded = json.dumps(
        identity, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _logical(parts: list[dict[str, Any]]) -> LogicalMessage:
    ordered = sorted(parts, key=lambda part: int(part["PartNumber"]))
    return LogicalMessage(
        id=_message_id(ordered),
        number=str(ordered[0]["Number"]),
        text="".join(str(part.get("Text") or "") for part in ordered),
        date=min(str(part.get("Date") or "") for part in ordered),
        parts=tuple(ordered),
    )


def assemble(records: list[dict[str, Any]]) -> AssemblyResult:
    """Assemble only complete, unambiguous UDH groups.

    Incomplete and conflicting records remain on the modem. This intentionally
    favors delayed delivery over guessing and corrupting message text.
    """
    complete: list[LogicalMessage] = []
    pending: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}

    for record in records:
        required = ("Number", "Location", "Fingerprint", "PartNumber", "PartsExpected")
        if any(record.get(field) in (None, "") for field in required):
            ambiguous.append(record)
            continue
        reference = record.get("Reference")
        if reference is None:
            if int(record.get("PartNumber", 0)) == 1 and int(record.get("PartsExpected", 0)) == 1:
                complete.append(_logical([record]))
            else:
                ambiguous.append(record)
            continue
        total = int(record.get("PartsExpected", 0))
        sequence = int(record.get("PartNumber", 0))
        bits = int(record.get("ReferenceBits", 0))
        if total < 2 or sequence < 1 or sequence > total or bits not in (8, 16):
            ambiguous.append(record)
            continue
        key = (str(record["Number"]), bits, int(reference), total)
        groups.setdefault(key, []).append(record)

    for parts in groups.values():
        total = int(parts[0]["PartsExpected"])
        by_sequence: dict[int, dict[str, Any]] = {}
        conflict = False
        for part in parts:
            sequence = int(part["PartNumber"])
            previous = by_sequence.get(sequence)
            if previous is not None and previous["Fingerprint"] != part["Fingerprint"]:
                conflict = True
            by_sequence[sequence] = part
        if conflict or len(parts) > total:
            ambiguous.extend(parts)
        elif set(by_sequence) == set(range(1, total + 1)):
            complete.append(_logical(list(by_sequence.values())))
        else:
            pending.extend(parts)

    return AssemblyResult(tuple(complete), tuple(pending), tuple(ambiguous))
