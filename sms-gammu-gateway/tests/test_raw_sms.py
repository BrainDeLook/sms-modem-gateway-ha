"""Tests for the experimental physical SMS API helpers."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest

sys.modules.setdefault("gammu", types.ModuleType("gammu"))
MODULE_PATH = Path(__file__).parents[1] / "support.py"
SPEC = importlib.util.spec_from_file_location("gateway_support_under_test", MODULE_PATH)
assert SPEC and SPEC.loader
SUPPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SUPPORT)


def record(location: int, text: str, *, sequence: int = 1) -> dict:
    return {
        "DateTime": "2026-08-16T12:00:00",
        "Number": "+70000000000",
        "SMSC": {"Number": "+79990000000"},
        "State": "UnRead",
        "Text": text,
        "Location": location,
        "UDH": {
            "ID8bit": 42,
            "ID16bit": -1,
            "PartNumber": sequence,
            "AllParts": 2,
        },
    }


class FakeMachine:
    def __init__(self, records: list[dict]) -> None:
        self.records = records
        self.deleted: list[int] = []

    def GetSMSStatus(self) -> dict:
        return {
            "SIMUsed": len(self.records),
            "PhoneUsed": 0,
            "TemplatesUsed": 0,
        }

    def GetNextSMS(self, **kwargs) -> list[dict]:
        if kwargs.get("Start"):
            return self.records[:1]
        location = kwargs.get("Location")
        for index, item in enumerate(self.records):
            if item["Location"] == location:
                return self.records[index + 1:index + 2]
        return []

    def DeleteSMS(self, *, Folder: int, Location: int) -> None:
        self.deleted.append(Location)


class RawSmsTests(unittest.TestCase):
    def test_raw_records_preserve_udh_identity_and_location(self) -> None:
        machine = FakeMachine([
            record(5, "first", sequence=1),
            record(8, "second", sequence=2),
        ])
        result = SUPPORT.retrieveRawSms(machine)
        self.assertEqual([item["Location"] for item in result], [5, 8])
        self.assertEqual([item["PartNumber"] for item in result], [1, 2])
        self.assertEqual(result[0]["Reference"], 42)
        self.assertEqual(result[0]["ReferenceBits"], 8)
        self.assertEqual(len(result[0]["Fingerprint"]), 64)

    def test_ack_deletes_only_matching_fingerprints(self) -> None:
        machine = FakeMachine([record(5, "first"), record(8, "second")])
        current = SUPPORT.retrieveRawSms(machine)
        result = SUPPORT.acknowledgeRawSms(machine, [
            {"Location": 5, "Fingerprint": current[0]["Fingerprint"]},
            {"Location": 8, "Fingerprint": "stale"},
        ])
        self.assertEqual(machine.deleted, [5])
        self.assertEqual(result["Deleted"], [5])
        self.assertEqual(result["Mismatched"][0]["Location"], 8)

    def test_ack_rejects_duplicate_location(self) -> None:
        machine = FakeMachine([record(5, "first")])
        fingerprint = SUPPORT.retrieveRawSms(machine)[0]["Fingerprint"]
        result = SUPPORT.acknowledgeRawSms(machine, [
            {"Location": 5, "Fingerprint": fingerprint},
            {"Location": 5, "Fingerprint": fingerprint},
        ])
        self.assertEqual(machine.deleted, [5])
        self.assertEqual(result["Mismatched"][0]["Reason"], "duplicate_request")


if __name__ == "__main__":
    unittest.main()
