"""
SMS Gammu Gateway - Support functions
Gammu integration functions for SMS operations and state machine management

Based on: https://github.com/pajikos/sms-gammu-gateway
Licensed under Apache License 2.0
"""

import sys
import os
import logging
import hashlib
import json
import gammu


def init_state_machine(pin, device_path='/dev/ttyUSB0', baud_rate='auto'):
    """Initialize gammu state machine with HA add-on config.

    baud_rate: 'auto' ponechá gammu auto-detekci rychlosti (connection = at),
    konkrétní hodnota (např. '115200') rychlost zafixuje (connection = at115200).
    Auto-detekce zasekává některé moduly (SIM800C) → default je fixní rychlost.
    """
    sm = gammu.StateMachine()

    # Rychlost: 'auto' -> at (gammu hádá), jinak at<rychlost> (fixní)
    if baud_rate and str(baud_rate) != 'auto':
        connection = f"at{baud_rate}"
    else:
        connection = "at"

    # Create gammu config dynamically
    config_content = f"""[gammu]
device = {device_path}
connection = {connection}
commtimeout = 40
"""

    # Write config to temporary file
    config_file = '/tmp/gammu.config'
    with open(config_file, 'w') as f:
        f.write(config_content)

    sm.ReadConfig(Filename=config_file)
    
    try:
        sm.Init()
        logging.info(f"Successfully initialized gammu with device: {device_path}")

        # Try to check security status
        try:
            security_status = sm.GetSecurityStatus()
            logging.info(f"SIM security status: {security_status}")

            if security_status == 'PIN':
                if pin is None or pin == '':
                    logging.error("PIN is required but not provided.")
                    sys.exit(1)
                else:
                    sm.EnterSecurityCode('PIN', pin)
                    logging.info("PIN entered successfully")

        except Exception as e:
            logging.warning(f"Could not check SIM security status: {e}")

    except gammu.ERR_NOSIM:
        logging.warning("SIM card not accessible, but device is connected")
    except Exception as e:
        logging.error(f"Error initializing device: {e}")
        try:
            devices = [d for d in os.listdir('/dev/') if d.startswith('tty')]
            logging.info(f"Available devices: {', '.join([f'/dev/{d}' for d in sorted(devices)[:10]])}")
        except:
            pass
        raise
        
    return sm


def _physical_sms(machine):
    """Read physical modem records without calling gammu.LinkSMS."""
    status = machine.GetSMSStatus()
    expected = status['SIMUsed'] + status['PhoneUsed'] + status['TemplatesUsed']
    records = []
    start = True
    cursor = None
    seen_locations = set()

    while len(records) < expected:
        if start:
            batch = machine.GetNextSMS(Start=True, Folder=0)
            start = False
        else:
            batch = machine.GetNextSMS(Location=cursor, Folder=0)
        if not batch:
            break
        cursor = batch[0]['Location']
        new_records = [
            part for part in batch
            if part.get('Location') not in seen_locations
        ]
        if not new_records:
            break
        records.extend(new_records)
        seen_locations.update(part.get('Location') for part in new_records)
    return records


def _safe_text(value):
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return str(value or '')


def _raw_udh_bytes(udh):
    value = (udh or {}).get('Text', b'')
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, (list, tuple)):
        try:
            return bytes(value)
        except (TypeError, ValueError):
            return b''
    return b''


def _concat_from_raw_udh(raw):
    """Parse GSM/3GPP concat IEI 0x00 or 0x08 from arbitrary UDH."""
    if not raw:
        return None
    # First octet is UDHL: number of following UDH octets.
    end = min(len(raw), 1 + raw[0])
    offset = 1
    while offset + 2 <= end:
        iei = raw[offset]
        length = raw[offset + 1]
        data_start = offset + 2
        data_end = data_start + length
        if data_end > end:
            return None
        data = raw[data_start:data_end]
        if iei == 0x00 and length == 3:
            reference, bits, total, sequence = data[0], 8, data[1], data[2]
        elif iei == 0x08 and length == 4:
            reference = (data[0] << 8) | data[1]
            bits, total, sequence = 16, data[2], data[3]
        else:
            offset = data_end
            continue
        # Return even malformed values. The assembler must quarantine a
        # recognized concat IE instead of misclassifying its fragment as a
        # standalone SMS.
        return reference, bits, total, sequence
    return None


def _normalize_raw_part(part):
    """Build a JSON-safe record and a stable acknowledge fingerprint."""
    udh = part.get('UDH') or {}
    raw_udh = _raw_udh_bytes(udh)
    id_16 = udh.get('ID16bit', -1)
    id_8 = udh.get('ID8bit', -1)
    total = int(udh.get('AllParts', -1) or -1)
    sequence = int(udh.get('PartNumber', -1) or -1)
    if isinstance(id_16, int) and id_16 >= 0:
        reference, reference_bits = id_16, 16
    elif isinstance(id_8, int) and id_8 >= 0:
        reference, reference_bits = id_8, 8
    else:
        reference, reference_bits = None, None

    # Combined/unknown UDH can be reported as UserUDH with the convenience
    # fields left at -1. The standards-defined IE is still present in Text.
    if reference is None or total < 2 or not (1 <= sequence <= total):
        parsed_concat = _concat_from_raw_udh(raw_udh)
        if parsed_concat is not None:
            reference, reference_bits, total, sequence = parsed_concat

    if reference is None:
        total, sequence = 1, 1

    smsc = part.get('SMSC') or {}
    result = {
        'Date': str(part.get('DateTime') or ''),
        'Number': _safe_text(part.get('Number')),
        'SMSC': _safe_text(smsc.get('Number') if isinstance(smsc, dict) else smsc),
        'State': _safe_text(part.get('State')),
        'Text': _safe_text(part.get('Text')),
        'Location': int(part.get('Location', -1)),
        'Reference': reference,
        'ReferenceBits': reference_bits,
        'PartNumber': sequence,
        'PartsExpected': total,
        'UDHType': _safe_text(udh.get('Type')),
        'UDHHex': raw_udh.hex(),
    }
    fingerprint_payload = json.dumps(
        result, sort_keys=True, ensure_ascii=False, separators=(',', ':')
    ).encode('utf-8')
    result['Fingerprint'] = hashlib.sha256(fingerprint_payload).hexdigest()
    return result


def retrieveRawSms(machine):
    """Return every physical record with its UDH concatenation identity."""
    return [_normalize_raw_part(part) for part in _physical_sms(machine)]


def acknowledgeRawSms(machine, requested_parts):
    """Delete only records whose current location fingerprint still matches."""
    current = {part['Location']: part for part in retrieveRawSms(machine)}
    deleted = []
    mismatched = []
    seen = set()

    for requested in requested_parts or []:
        try:
            location = int(requested.get('Location'))
        except (AttributeError, TypeError, ValueError):
            mismatched.append({'Location': None, 'Reason': 'invalid_request'})
            continue
        if location in seen:
            mismatched.append({'Location': location, 'Reason': 'duplicate_request'})
            continue
        seen.add(location)
        expected_fingerprint = str(requested.get('Fingerprint') or '')
        actual = current.get(location)
        if actual is None:
            mismatched.append({'Location': location, 'Reason': 'not_found'})
            continue
        if actual['Fingerprint'] != expected_fingerprint:
            mismatched.append({'Location': location, 'Reason': 'fingerprint_changed'})
            continue
        machine.DeleteSMS(Folder=0, Location=location)
        deleted.append(location)

    return {'Deleted': deleted, 'Mismatched': mismatched}


def retrieveAllSms(machine):
    """Retrieve all SMS messages from SIM/device memory"""
    try:
        status = machine.GetSMSStatus()
        allMultiPartSmsCount = status['SIMUsed'] + status['PhoneUsed'] + status['TemplatesUsed']

        allMultiPartSms = []
        start = True

        while len(allMultiPartSms) < allMultiPartSmsCount:
            if start:
                currentMultiPartSms = machine.GetNextSMS(Start=True, Folder=0)
                start = False
            else:
                currentMultiPartSms = machine.GetNextSMS(Location=currentMultiPartSms[0]['Location'], Folder=0)
            allMultiPartSms.append(currentMultiPartSms)

        allSms = gammu.LinkSMS(allMultiPartSms)

        results = []
        for sms in allSms:
            smsPart = sms[0]

            # Multipart completeness check: u dlouhých (concatenated) SMS nese
            # každá část v UDH údaj "AllParts" = kolik částí zpráva celkem má.
            # Pokud ještě nedorazily všechny části, nesmíme zprávu publikovat ani
            # mazat - jinak uživatel dostane useknutý text a smazáním první části
            # znemožníme pozdější složení zbytku.
            all_parts = 0
            for part in sms:
                udh = part.get('UDH') or {}
                part_total = udh.get('AllParts', 0) or 0
                if part_total > all_parts:
                    all_parts = part_total
            complete = (all_parts <= 1) or (len(sms) >= all_parts)

            result = {
                "Date": str(smsPart['DateTime']),
                "Number": smsPart['Number'],
                "State": smsPart['State'],
                "Locations": [smsPart['Location'] for smsPart in sms],
                "Complete": complete,
                "PartsReceived": len(sms),
                "PartsExpected": all_parts if all_parts > 1 else 1,
            }

            # Try to decode SMS - this may fail for MMS notifications or corrupted messages
            try:
                decodedSms = gammu.DecodeSMS(sms)
                if decodedSms == None:
                    # DecodeSMS returned None - use raw text from SMS part
                    result["Text"] = smsPart.get('Text', '')
                else:
                    # Successfully decoded - concatenate all text entries
                    text = ""
                    for entry in decodedSms['Entries']:
                        if entry.get('Buffer') is not None:
                            text += entry['Buffer']
                    result["Text"] = text if text else smsPart.get('Text', '')

            except UnicodeDecodeError as e:
                # MMS notification or binary message that can't be decoded as UTF-8
                logging.warning(f"Cannot decode SMS as UTF-8 (probably MMS notification): {e}")
                # Try to get raw text, but handle potential binary data safely
                try:
                    raw_text = smsPart.get('Text', '')
                    # If Text is bytes, try to decode with error handling
                    if isinstance(raw_text, bytes):
                        result["Text"] = raw_text.decode('utf-8', errors='replace')
                    else:
                        result["Text"] = str(raw_text) if raw_text else '[MMS or binary message]'
                except Exception:
                    result["Text"] = '[MMS or binary message - cannot display]'

            except Exception as e:
                # Any other decoding error (corrupted SMS, unknown format, etc.)
                logging.warning(f"Error decoding SMS: {e}")
                # Fallback to raw text with safe handling
                try:
                    raw_text = smsPart.get('Text', '')
                    if isinstance(raw_text, bytes):
                        result["Text"] = raw_text.decode('utf-8', errors='replace')
                    else:
                        result["Text"] = str(raw_text) if raw_text else '[Decoding error]'
                except Exception:
                    result["Text"] = '[Message decoding failed]'

            results.append(result)

        return results

    except Exception as e:
        logging.error(f"Error retrieving SMS: {e}")
        raise  # Re-raise exception so track_gammu_operation can detect failure


def deleteSms(machine, sms):
    """Delete SMS by location"""
    try:
        list(map(lambda location: machine.DeleteSMS(Folder=0, Location=location), sms["Locations"]))
    except Exception as e:
        logging.error(f"Error deleting SMS: {e}")


def encodeSms(smsinfo):
    """Encode SMS for sending"""
    return gammu.EncodeSMS(smsinfo)


def setupCallbacks(machine, unified_callback):
    """
    Nastaví callback pro příchozí hovory a SMS.
    Využívá Gammu SetIncomingCall, SetIncomingSMS a jeden společný SetIncomingCallback.

    Args:
        machine: Gammu state machine
        unified_callback: Callback funkce pro všechny události (sm, event_type, data)
                         event_type může být 'Call' nebo 'SMS'

    Returns: {'calls': bool, 'sms': bool} - co se podařilo nastavit
    """
    result = {'calls': False, 'sms': False}

    # Nastav společný callback pro všechny události
    try:
        machine.SetIncomingCallback(unified_callback)
        logging.info("📱 Unified callback: SetIncomingCallback registered")
    except Exception as e:
        logging.error(f"📱 SetIncomingCallback failed: {type(e).__name__}: {e}")
        return result

    # Povol Call notifikace (bez parametru podle dokumentace)
    try:
        machine.SetIncomingCall()
        result['calls'] = True
        logging.info("📞 Call notifications: ENABLED")
    except gammu.ERR_NOTSUPPORTED:
        logging.warning("📞 SetIncomingCall: Not supported by this modem")
    except Exception as e:
        logging.error(f"📞 SetIncomingCall failed: {type(e).__name__}: {e}")

    # Povol SMS notifikace (bez parametru podle dokumentace)
    try:
        machine.SetIncomingSMS()
        result['sms'] = True
        logging.info("📨 SMS notifications: ENABLED")
    except gammu.ERR_NOTSUPPORTED:
        logging.warning("📨 SetIncomingSMS: Not supported by this modem")
    except Exception as e:
        logging.error(f"📨 SetIncomingSMS failed: {type(e).__name__}: {e}")

    return result
