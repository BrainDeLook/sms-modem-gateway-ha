# SMS Modem Gateway for Home Assistant

Reliable minimal add-on for USB GSM modems. It uses `python-gammu` only as the
modem driver and implements multipart UDH assembly, durable delivery and the
REST contract in its own code.

## Installation

Add this repository to **Settings → Add-ons → Add-on Store → Repositories**:

```text
https://github.com/BrainDeLook/sms-modem-gateway-ha
```

Install **SMS Modem Gateway**. Do not run another gateway
against the same serial device at the same time.

Then install [SMS Gammu Viewer](https://github.com/BrainDeLook/sms-gammu-viewer-ha)
through HACS and configure it to use the add-on REST API on port `5000`.

## Reliability model

1. Read physical records without `gammu.LinkSMS`.
2. Parse standard 8-bit and 16-bit concatenation UDH.
3. Wait for an exact, conflict-free set of parts.
4. Commit the logical SMS and its physical cleanup journal to SQLite.
5. Delete only modem records whose location fingerprint still matches.
6. Deliver the logical message through a durable ID-based REST queue.

MQTT is intentionally not included. Voice calls are handled directly by the
integration through a separate modem serial port.
