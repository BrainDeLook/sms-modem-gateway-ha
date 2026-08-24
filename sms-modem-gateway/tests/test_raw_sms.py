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
        self.assertEqual(result[0]["UDHType"], "")
        self.assertEqual(len(result[0]["Fingerprint"]), 64)

    def test_falls_back_to_raw_8bit_concat_ie_in_combined_udh(self) -> None:
        item = record(5, "part")
        item["UDH"] = {
            "Type": "UserUDH",
            "ID8bit": -1,
            "ID16bit": -1,
            "PartNumber": -1,
            "AllParts": -1,
            # Port addressing IE followed by concat IE: ref=42, 3 parts, part 2.
            "Text": bytes.fromhex("0b05040b8423f000032a0302"),
        }
        result = SUPPORT._normalize_raw_part(item)
        self.assertEqual(result["Reference"], 42)
        self.assertEqual(result["ReferenceBits"], 8)
        self.assertEqual(result["PartsExpected"], 3)
        self.assertEqual(result["PartNumber"], 2)
        self.assertEqual(result["UDHHex"], "0b05040b8423f000032a0302")

    def test_falls_back_to_raw_16bit_concat_ie(self) -> None:
        item = record(7, "part")
        item["UDH"] = {
            "Type": "UserUDH",
            "ID8bit": -1,
            "ID16bit": -1,
            "PartNumber": -1,
            "AllParts": -1,
            "Text": bytes.fromhex("06080412340403"),
        }
        result = SUPPORT._normalize_raw_part(item)
        self.assertEqual(result["Reference"], 0x1234)
        self.assertEqual(result["ReferenceBits"], 16)
        self.assertEqual(result["PartsExpected"], 4)
        self.assertEqual(result["PartNumber"], 3)

    def test_rejects_invalid_raw_concat_sequence(self) -> None:
        item = record(9, "part")
        item["UDH"] = {
            "Type": "UserUDH",
            "ID8bit": -1,
            "ID16bit": -1,
            "PartNumber": -1,
            "AllParts": -1,
            "Text": bytes.fromhex("0500032a0304"),
        }
        result = SUPPORT._normalize_raw_part(item)
        self.assertEqual(result["Reference"], 42)
        self.assertEqual(result["PartsExpected"], 3)
        self.assertEqual(result["PartNumber"], 4)

    def test_treats_service_udh_with_spurious_reference_as_single_sms(self) -> None:
        item = record(11, "single provider message")
        item["UDH"] = {
            "Type": "UserUDH",
            "ID8bit": 42,
            "ID16bit": -1,
            "PartNumber": -1,
            "AllParts": -1,
            # Application-port addressing IE; no concatenation IE is present.
            "Text": bytes.fromhex("050403158101"),
        }
        result = SUPPORT._normalize_raw_part(item)
        self.assertIsNone(result["Reference"])
        self.assertIsNone(result["ReferenceBits"])
        self.assertEqual(result["PartsExpected"], 1)
        self.assertEqual(result["PartNumber"], 1)

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

    def test_ack_ignores_unread_to_read_state_transition(self) -> None:
        item = record(5, "first")
        machine = FakeMachine([item])
        fingerprint = SUPPORT.retrieveRawSms(machine)[0]["Fingerprint"]
        item["State"] = "Read"

        result = SUPPORT.acknowledgeRawSms(machine, [
            {"Location": 5, "Fingerprint": fingerprint},
        ])

        self.assertEqual(machine.deleted, [5])
        self.assertEqual(result, {"Deleted": [5], "Mismatched": []})

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
