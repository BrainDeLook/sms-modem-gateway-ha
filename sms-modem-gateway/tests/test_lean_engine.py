"""Tests for the minimal durable modem engine."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.modules.setdefault("gammu", types.ModuleType("gammu"))

from lean_assembler import assemble
from lean_engine import ModemEngine
from lean_store import MessageStore


def physical(location: int, sequence: int, total: int = 3) -> dict:
    return {
        "DateTime": "2026-08-16T20:00:00",
        "Number": "+70000000000",
        "SMSC": {"Number": "+79990000000"},
        "State": "UnRead",
        "Text": f"part-{sequence}",
        "Location": location,
        "UDH": {
            "Type": "ConcatenatedMessages",
            "Text": bytes([5, 0, 3, 42, total, sequence]),
            "ID8bit": 42,
            "ID16bit": -1,
            "PartNumber": sequence,
            "AllParts": total,
        },
    }


class FakeMachine:
    def __init__(self, records: list[dict], fail_location: int | None = None) -> None:
        self.records = records
        self.deleted: list[int] = []
        self.fail_location = fail_location

    def GetSMSStatus(self) -> dict:
        return {"SIMUsed": len(self.records), "PhoneUsed": 0, "TemplatesUsed": 0}

    def GetSignalQuality(self) -> dict:
        return {"SignalPercent": 75}

    def GetNetworkInfo(self) -> dict:
        return self.network

    def GetNextSMS(self, **kwargs) -> list[dict]:
        if kwargs.get("Start"):
            return self.records[:1]
        location = kwargs.get("Location")
        for index, item in enumerate(self.records):
            if item["Location"] == location:
                return self.records[index + 1:index + 2]
        return []

    def DeleteSMS(self, *, Folder: int, Location: int) -> None:
        if self.fail_location == Location:
            raise OSError("modem disconnected")
        self.deleted.append(Location)
        self.records = [
            item for item in self.records if item["Location"] != Location
        ]


class FailingStore:
    def cleanup_groups(self) -> dict:
        return {}

    def enqueue(self, message) -> bool:
        raise OSError("disk full")

    def cleanup(self, retention_days: int) -> int:
        return 0


class LeanAssemblerTests(unittest.TestCase):
    def test_waits_for_missing_part(self) -> None:
        from support import retrieveRawSms

        result = assemble(retrieveRawSms(FakeMachine([
            physical(1, 1), physical(2, 2),
        ])))
        self.assertEqual(result.complete, ())
        self.assertEqual(len(result.pending), 2)

    def test_orders_complete_message_by_udh_sequence(self) -> None:
        from support import _normalize_raw_part

        records = [
            _normalize_raw_part(physical(3, 3)),
            _normalize_raw_part(physical(1, 1)),
            _normalize_raw_part(physical(2, 2)),
        ]
        result = assemble(records)
        self.assertEqual(len(result.complete), 1)
        self.assertEqual(result.complete[0].text, "part-1part-2part-3")

    def test_conflicting_sequence_is_quarantined(self) -> None:
        from support import _normalize_raw_part

        first = _normalize_raw_part(physical(1, 1))
        conflict_record = physical(9, 1)
        conflict_record["Text"] = "different"
        conflict = _normalize_raw_part(conflict_record)
        result = assemble([first, conflict])
        self.assertEqual(result.complete, ())
        self.assertEqual(len(result.ambiguous), 2)


class LeanStoreAndEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = MessageStore(Path(self.tempdir.name) / "messages.db")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_poll_queues_then_deletes_complete_message(self) -> None:
        machine = FakeMachine([
            physical(1, 1), physical(2, 2), physical(3, 3),
        ])
        engine = ModemEngine(machine, self.store)
        result = engine.poll_once()

        self.assertEqual(result["queued"], 1)
        self.assertEqual(machine.deleted, [1, 2, 3])
        queued = self.store.pending()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["Text"], "part-1part-2part-3")

        self.assertTrue(self.store.acknowledge(queued[0]["ID"]))
        self.assertEqual(self.store.pending(), [])

    def test_database_failure_never_deletes_modem_parts(self) -> None:
        machine = FakeMachine([
            physical(1, 1), physical(2, 2), physical(3, 3),
        ])
        engine = ModemEngine(machine, FailingStore())
        with self.assertRaisesRegex(OSError, "disk full"):
            engine.poll_once()
        self.assertEqual(machine.deleted, [])

    def test_partial_modem_cleanup_resumes_from_sqlite_journal(self) -> None:
        machine = FakeMachine(
            [physical(1, 1), physical(2, 2), physical(3, 3)],
            fail_location=2,
        )
        engine = ModemEngine(machine, self.store)

        with self.assertRaisesRegex(OSError, "modem disconnected"):
            engine.poll_once()
        self.assertEqual(machine.deleted, [1])
        self.assertEqual(self.store.counts()["physical_cleanup"], 3)

        machine.fail_location = None
        result = engine.poll_once()
        self.assertEqual(machine.deleted, [1, 2, 3])
        self.assertEqual(result["physical"], 0)
        self.assertEqual(self.store.counts()["physical_cleanup"], 0)

    def test_status_resolves_missing_operator_from_gammu_networks(self) -> None:
        machine = FakeMachine([])
        machine.network = {"NetworkName": "", "NetworkCode": "250 99", "State": "HomeNetwork"}
        with patch.object(sys.modules["gammu"], "GSMNetworks", {"250 99": "Beeline"}, create=True):
            status = ModemEngine(machine, self.store).status()
        self.assertEqual(status["network"]["NetworkName"], "Beeline")
        self.assertEqual(status["network"]["State"], "HomeNetwork")
        self.assertEqual(machine.network["NetworkName"], "")

    def test_status_preserves_modem_name_and_unknown_code(self) -> None:
        machine = FakeMachine([])
        with patch.object(
            sys.modules["gammu"], "GSMNetworks",
            {"250 99": "Beeline", "250 01": "MTS"}, create=True,
        ):
            machine.network = {"NetworkName": "Operator from modem", "NetworkCode": "25099"}
            self.assertEqual(ModemEngine(machine, self.store).status()["network"], machine.network)
            machine.network = {"NetworkName": "", "NetworkCode": "25002"}
            self.assertEqual(ModemEngine(machine, self.store).status()["network"], machine.network)
            machine.network = {"NetworkName": "", "NetworkCode": "25001"}
            self.assertEqual(
                ModemEngine(machine, self.store).status()["network"]["NetworkName"],
                "MTS",
            )
            machine.network = {"NetworkName": None, "NetworkCode": "25099"}
            self.assertEqual(
                ModemEngine(machine, self.store).status()["network"]["NetworkName"],
                "Beeline",
            )


if __name__ == "__main__":
    unittest.main()
