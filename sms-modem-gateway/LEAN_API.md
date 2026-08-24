# SMS Modem Gateway REST API

This branch is intentionally independent from the stable add-on. Never run
both add-ons against the same serial device.

The gateway owns the modem, assembles physical UDH parts, commits a complete
logical message to `/data/messages.db`, and only then deletes the physical SIM
records. Home Assistant consumes the durable queue by immutable message ID.

## Endpoints

All `/v1` endpoints use HTTP Basic authentication.

- `GET /health` — process health without modem I/O.
- `GET /v1/messages` — pending logical SMS messages.
- `POST /v1/messages/<id>/ack` — acknowledge durable delivery.
- `POST /v1/sms` — send an SMS (`number`, `text`, optional `unicode`, `flash`, `smsc`).
- `GET /v1/status` — modem, network, signal, capacity and queue state.
- `GET /v1/modem` — modem identity.
- `POST /v1/poll` — diagnostic immediate poll.

Voice calls are intentionally outside this add-on: SMS Gammu Viewer controls
them directly through the modem's separate serial voice port.
