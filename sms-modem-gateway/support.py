"""Minimal Gammu transport helpers used by the lean modem engine."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import gammu


def init_state_machine(
    pin: str, device_path: str = "/dev/ttyUSB0", baud_rate: str = "auto"
):
    machine = gammu.StateMachine()
    connection = "at" if not baud_rate or baud_rate == "auto" else f"at{baud_rate}"
    config_path = Path("/tmp/sms-modem-gateway-gammu.conf")
    config_path.write_text(
        "[gammu]\n"
        f"device = {device_path}\n"
        f"connection = {connection}\n"
        "commtimeout = 40\n",
        encoding="utf-8",
    )
    machine.ReadConfig(Filename=str(config_path))
    machine.Init()
    logging.info("Initialized modem: %s", device_path)

    try:
        security = machine.GetSecurityStatus()
        if security == "PIN":
            if not pin:
                raise RuntimeError("SIM PIN is required but not configured")
            machine.EnterSecurityCode("PIN", pin)
            logging.info("SIM PIN accepted")
    except RuntimeError:
        raise
    except Exception as error:
        logging.warning("Could not read SIM security status: %s", error)
    return machine


def _physical_sms(machine) -> list[dict[str, Any]]:
    status = machine.GetSMSStatus()
    expected = sum(
        int(status.get(key, 0) or 0)
        for key in ("SIMUsed", "PhoneUsed", "TemplatesUsed")
    )
    records: list[dict[str, Any]] = []
    cursor: int | None = None
    seen: set[int] = set()
    while len(records) < expected:
        batch = (
            machine.GetNextSMS(Start=True, Folder=0)
            if cursor is None
            else machine.GetNextSMS(Location=cursor, Folder=0)
        )
        if not batch:
            break
        cursor = int(batch[0]["Location"])
        added = 0
        for part in batch:
            location = int(part.get("Location", -1))
            if location in seen:
                continue
            records.append(part)
            seen.add(location)
            added += 1
        if added == 0:
            break
    return records


def _safe_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _raw_udh_bytes(udh: dict[str, Any]) -> bytes:
    value = (udh or {}).get("Text", b"")
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, (list, tuple)):
        try:
            return bytes(value)
        except (TypeError, ValueError):
            return b""
    return b""


def _concat_from_raw_udh(raw: bytes) -> tuple[int, int, int, int] | None:
    """Parse standard 8-bit (IEI 00) or 16-bit (IEI 08) concatenation."""
    if not raw:
        return None
    end = min(len(raw), 1 + raw[0])
    offset = 1
    while offset + 2 <= end:
        iei, length = raw[offset], raw[offset + 1]
        start = offset + 2
        finish = start + length
        if finish > end:
            return None
        data = raw[start:finish]
        if iei == 0x00 and length == 3:
            return data[0], 8, data[1], data[2]
        if iei == 0x08 and length == 4:
            return (data[0] << 8) | data[1], 16, data[2], data[3]
        offset = finish
    return None


def _normalize_raw_part(part: dict[str, Any]) -> dict[str, Any]:
    udh = part.get("UDH") or {}
    raw_udh = _raw_udh_bytes(udh)
    id_16, id_8 = udh.get("ID16bit", -1), udh.get("ID8bit", -1)
    total = int(udh.get("AllParts", -1) or -1)
    sequence = int(udh.get("PartNumber", -1) or -1)
    if isinstance(id_16, int) and id_16 >= 0:
        reference, bits = id_16, 16
    elif isinstance(id_8, int) and id_8 >= 0:
        reference, bits = id_8, 8
    else:
        reference, bits = None, None

    # Gammu иногда заполняет ID8bit даже у одиночной SMS со служебным UDH
    # (например, application-port addressing). Такой ID нельзя считать
    # concat Reference без валидных PartsExpected/PartNumber. Сырой UDH,
    # наоборот, является источником истины и может исправить неполное поле
    # UDH, которое вернул драйвер.
    metadata_is_concat = (
        reference is not None
        and bits in (8, 16)
        and total >= 2
        and 1 <= sequence <= total
    )
    parsed = _concat_from_raw_udh(raw_udh)
    if parsed is not None:
        reference, bits, total, sequence = parsed
    elif not metadata_is_concat:
        # Это обычная одиночная SMS. Сбрасываем фиктивный Reference ID,
        # чтобы сборщик не отправлял её в ambiguous/quarantine.
        reference, bits = None, None
        total, sequence = 1, 1

    smsc = part.get("SMSC") or {}
    result = {
        "Date": str(part.get("DateTime") or ""),
        "Number": _safe_text(part.get("Number")),
        "SMSC": _safe_text(smsc.get("Number") if isinstance(smsc, dict) else smsc),
        "State": _safe_text(part.get("State")),
        "Text": _safe_text(part.get("Text")),
        "Location": int(part.get("Location", -1)),
        "Reference": reference,
        "ReferenceBits": bits,
        "PartNumber": sequence,
        "PartsExpected": total,
        "UDHType": _safe_text(udh.get("Type")),
        "UDHHex": raw_udh.hex(),
    }
    # Modems commonly change UnRead to Read during fingerprint verification.
    immutable = {key: value for key, value in result.items() if key != "State"}
    payload = json.dumps(
        immutable, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    result["Fingerprint"] = hashlib.sha256(payload).hexdigest()
    return result


def retrieveRawSms(machine) -> list[dict[str, Any]]:
    """Compatibility name for the physical-record reader."""
    return [_normalize_raw_part(part) for part in _physical_sms(machine)]


def acknowledgeRawSms(machine, requested_parts) -> dict[str, list[Any]]:
    """Delete only locations whose immutable physical identity still matches."""
    current = {part["Location"]: part for part in retrieveRawSms(machine)}
    deleted: list[int] = []
    mismatched: list[dict[str, Any]] = []
    seen: set[int] = set()
    for requested in requested_parts or []:
        try:
            location = int(requested.get("Location"))
        except (AttributeError, TypeError, ValueError):
            mismatched.append({"Location": None, "Reason": "invalid_request"})
            continue
        if location in seen:
            mismatched.append({"Location": location, "Reason": "duplicate_request"})
            continue
        seen.add(location)
        actual = current.get(location)
        if actual is None:
            mismatched.append({"Location": location, "Reason": "not_found"})
        elif actual["Fingerprint"] != str(requested.get("Fingerprint") or ""):
            mismatched.append({"Location": location, "Reason": "fingerprint_changed"})
        else:
            machine.DeleteSMS(Folder=0, Location=location)
            deleted.append(location)
    return {"Deleted": deleted, "Mismatched": mismatched}
