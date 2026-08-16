# SMS Modem Gateway (Lean Experiment)

Minimal Home Assistant add-on for receiving and sending SMS through a USB GSM
modem. Complete messages are persisted in `/data/messages.db` before their
physical SIM records are removed.

Current features:

- conservative multipart UDH assembly;
- durable SQLite delivery queue;
- crash-safe physical cleanup journal;
- incoming and outgoing SMS REST API;
- signal, network, SIM and modem status;
- outgoing dial and hangup REST endpoints;
- HTTP Basic authentication;
- production Waitress HTTP server;
- no MQTT dependency.

See [LEAN_API.md](LEAN_API.md) for the REST endpoints.
