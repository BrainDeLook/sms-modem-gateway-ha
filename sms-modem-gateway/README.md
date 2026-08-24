# SMS Modem Gateway

Home Assistant add-on for receiving and sending SMS through a USB GSM modem.
Complete messages are persisted in `/data/messages.db` before their physical
SIM records are removed.

Current features:

- conservative multipart UDH assembly;
- durable SQLite delivery queue;
- crash-safe physical cleanup journal;
- incoming and outgoing SMS REST API;
- signal, network, SIM and modem status;
- HTTP Basic authentication;
- production Waitress HTTP server;
- no MQTT dependency.

Voice calls are handled directly by SMS Gammu Viewer through a separate modem
serial port and are intentionally not exposed by this SMS gateway.

See [LEAN_API.md](LEAN_API.md) for the REST endpoints. The API is intentionally
small and is consumed by [SMS Gammu Viewer](https://github.com/BrainDeLook/sms-gammu-viewer-ha).
