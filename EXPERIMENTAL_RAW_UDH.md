# Raw UDH experiment

This branch is intentionally isolated from the fork's `main` branch and from
PavelVe's stable add-on.

Add this exact repository URL in Home Assistant's add-on store:

```text
https://github.com/BrainDeLook/home-assistant-addons-ru#experiment/raw-udh-api
```

Install **SMS Gammu Gateway (Raw UDH Experiment)**. It has a separate add-on
slug, so it does not overwrite the stable installation.

Before starting it:

1. Stop the stable SMS Gammu Gateway. Never let two processes open the same
   serial modem.
2. Copy the stable add-on configuration, including device, credentials and
   baud rate.
3. Disable SMS monitoring, automatic deletion of read SMS, call monitoring
   and MQTT consumption. These are disabled by default in `1.8.0-dev.2` but
   verify the copied configuration. SMS Gammu Viewer must be the only message
   consumer.
4. Keep API port `5000`, or update the integration host/port to match any
   custom port mapping.

Pair it with the integration branch:

```text
https://github.com/BrainDeLook/sms-gammu-viewer-ha/tree/experiment/raw-udh-assembler
```

The additional API is documented in the integration repository at
`docs/RAW_GATEWAY_API.md`.
