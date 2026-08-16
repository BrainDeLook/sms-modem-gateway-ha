"""Single-owner modem engine for the lean REST gateway."""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import threading
from typing import Any

import gammu

from lean_assembler import assemble
from lean_store import MessageStore
from support import acknowledgeRawSms, retrieveRawSms


class ModemEngine:
    def __init__(
        self,
        machine: Any,
        store: MessageStore,
        poll_interval: int = 10,
        retention_days: int = 30,
    ) -> None:
        self.machine = machine
        self.store = store
        self.poll_interval = max(5, int(poll_interval))
        self.retention_days = max(1, int(retention_days))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_poll: str | None = None
        self.last_error: str | None = None
        self.pending_parts = 0
        self.ambiguous_parts = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._poll_loop, name="modem-poll", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        with self._lock:
            try:
                self.machine.Terminate()
            except Exception:
                pass

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as error:
                self.last_error = str(error)
                logging.exception("Modem poll failed")
            self._stop.wait(self.poll_interval)

    def poll_once(self) -> dict[str, int]:
        """Persist complete messages before deleting exact modem records."""
        with self._lock:
            deleted = self._drain_physical_cleanup()
            records = retrieveRawSms(self.machine)
            result = assemble(records)
            self.pending_parts = len(result.pending)
            self.ambiguous_parts = len(result.ambiguous)
            if result.pending:
                logging.info(
                    "Waiting for multipart SMS: %d physical part(s)",
                    len(result.pending),
                )
            if result.ambiguous:
                logging.error(
                    "Quarantined %d ambiguous SMS part(s)", len(result.ambiguous)
                )

            stored = 0
            for message in result.complete:
                if self.store.enqueue(message):
                    stored += 1
                    logging.info(
                        "Queued SMS id=%s sender=%s parts=%d",
                        message.id[:12], message.number, len(message.parts),
                    )
            deleted += self._drain_physical_cleanup()

            self.store.cleanup(self.retention_days)
            self.last_poll = datetime.now(timezone.utc).isoformat()
            self.last_error = None
            logging.info(
                "Poll complete: physical=%d queued=%d deleted=%d pending=%d",
                len(records), stored, deleted, len(result.pending),
            )
            return {
                "physical": len(records),
                "queued": stored,
                "deleted": deleted,
                "pending": len(result.pending),
                "ambiguous": len(result.ambiguous),
            }

    def _drain_physical_cleanup(self) -> int:
        deleted_count = 0
        for message_id, requested in self.store.cleanup_groups().items():
            acknowledgement = acknowledgeRawSms(self.machine, requested)
            deleted = [int(item) for item in acknowledgement["Deleted"]]
            mismatched = [
                int(item["Location"])
                for item in acknowledgement["Mismatched"]
                if item.get("Location") is not None
            ]
            # A mismatch means this exact physical identity no longer exists.
            # It must be resolved, never retried against a reused SIM slot.
            self.store.resolve_cleanup(message_id, deleted + mismatched)
            deleted_count += len(deleted)
            if acknowledgement["Mismatched"]:
                logging.warning(
                    "Physical SMS cleanup mismatch id=%s details=%s",
                    message_id[:12], acknowledgement["Mismatched"],
                )
        return deleted_count

    def send_sms(
        self,
        number: str,
        text: str,
        *,
        unicode: bool | None = None,
        flash: bool = False,
        smsc: str | None = None,
    ) -> list[Any]:
        if unicode is None:
            unicode = any(ord(character) > 127 for character in text)
        sms_info = {
            "Class": 0 if flash else -1,
            "Unicode": bool(unicode),
            "Entries": [{"ID": "ConcatenatedTextLong", "Buffer": text}],
        }
        encoded = gammu.EncodeSMS(sms_info)
        with self._lock:
            results = []
            for message in encoded:
                message["SMSC"] = {"Number": smsc} if smsc else {"Location": 1}
                message["Number"] = number
                results.append(self.machine.SendSMS(message))
            return results

    def status(self) -> dict[str, Any]:
        with self._lock:
            signal = self.machine.GetSignalQuality()
            network = self.machine.GetNetworkInfo()
            capacity = self.machine.GetSMSStatus()
        return {
            "online": True,
            "last_poll": self.last_poll,
            "last_error": self.last_error,
            "pending_parts": self.pending_parts,
            "ambiguous_parts": self.ambiguous_parts,
            "signal": signal,
            "network": network,
            "capacity": capacity,
            "queue": self.store.counts(),
        }

    def modem_info(self) -> dict[str, Any]:
        with self._lock:
            return {
                "imei": self.machine.GetIMEI(),
                "manufacturer": self.machine.GetManufacturer(),
                "model": self.machine.GetModel(),
                "imsi": self.machine.GetSIMIMSI(),
            }

    def dial(self, number: str) -> None:
        with self._lock:
            self.machine.DialVoice(number)

    def hangup(self) -> None:
        with self._lock:
            self.machine.CancelCall(0, True)
