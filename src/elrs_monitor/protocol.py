# SPDX-License-Identifier: GPL-3.0-only
# Upstream provenance and attribution: see THIRD_PARTY_NOTICES.md.
"""ELRS OTA-v4 LoRa packet parsing, long interleaving, FEC, and CRC."""
import hashlib
import re

import numpy as np
from .profiles import DEFAULT


def hamming_encode(n):
    b = [(n>>i)&1 for i in range(4)]
    parity = [b[0]^b[1]^b[2],b[1]^b[2]^b[3],b[0]^b[1]^b[3],b[0]^b[2]^b[3]]
    value = 0
    for bit in b+parity:
        value = (value<<1)|bit
    return value


CODEBOOK = np.array([hamming_encode(i) for i in range(16)])
POPCOUNT = np.array([i.bit_count() for i in range(256)])
WHITENING = bytes.fromhex('ff fe fc f8 f0 e1 c2 85 0b 17 2f 5e bc')


def decode_long_interleaver(raw_bins, polarity=1, profile=DEFAULT):
    widths = profile.symbol_widths
    if len(raw_bins) != len(widths) or any(not 0 <= b < profile.bins for b in raw_bins):
        raise ValueError('Wrong modulation bins for configured LoRa profile')
    shifted = (polarity*np.array(raw_bins,dtype=int)-1)%profile.bins
    stream = []
    for symbol,width in zip(shifted,widths):
        reduction = profile.sf-width
        # SF7+ first eight symbols use two extra frequency-protection bits.
        binary = int(np.floor(symbol/(1<<reduction)+.5)) % (1<<width)
        gray = binary^(binary>>1)
        stream.extend((gray>>i)&1 for i in range(width))
    words = profile.packet_bytes*2
    bits = np.array(stream[:words*profile.code_bits]).reshape(profile.code_bits,words)
    codewords = (bits*(2**np.arange(profile.code_bits-1,-1,-1))[:,None]).sum(axis=0)
    book = CODEBOOK>>(8-profile.code_bits)
    distance = POPCOUNT[codewords[:,None]^book[None,:]]
    minima = distance.min(axis=1)
    # Punctured 4/6 has distance two: detect errors but do not claim correction.
    correctable = 1 if profile.code_bits == 8 else 0
    if np.any(minima > correctable) or np.any((distance == minima[:,None]).sum(axis=1) != 1):
        raise ValueError('Uncorrectable or ambiguous FEC codeword')
    nibbles = distance.argmin(axis=1)
    white = nibbles[::2] | (nibbles[1::2]<<4)
    return bytes(int(b)^w for b,w in zip(white,WHITENING)),int(minima.sum())


def crc(data, initializer, width, poly):
    mask = (1<<width)-1
    value = initializer&mask
    for byte in data:
        value ^= byte<<(width-8)
        for _ in range(8):
            value = ((value<<1)^(poly if value&(1<<(width-1)) else 0))&mask
    return value


def crc14(data, initializer):
    return crc(data,initializer,14,0x2e57)


def validate_crc(packet, initializer, nonce=0):
    if len(packet) == 8:
        observed = ((packet[0]>>2)<<8)|packet[7]
        data = bytes([packet[0]&3])+packet[1:7]
        return observed == crc14(data,initializer^nonce)
    if len(packet) == 13:
        return int.from_bytes(packet[11:13],'little') == crc(packet[:11],initializer^nonce,16,0x3d65)
    return False


def parse_uid(value):
    """Four link bytes or six UID bytes, as compact/colon/hyphen hex.

    Binding only reveals the last four bytes; zero-padding the unused prefix
    yields the same link CRC and hop seed, not the original full MAC/phrase UID.
    """
    if isinstance(value,bytes):
        raw = value
    elif isinstance(value,str) and re.fullmatch(
            r'(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{12}|'
            r'(?:[0-9a-fA-F]{2}:){3}[0-9a-fA-F]{2}|'
            r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}|'
            r'(?:[0-9a-fA-F]{2}-){3}[0-9a-fA-F]{2}|'
            r'(?:[0-9a-fA-F]{2}-){5}[0-9a-fA-F]{2})',value):
        raw = bytes.fromhex(value.replace(':','').replace('-',''))
    else:
        raise ValueError('UID must be 4 or 6 hexadecimal bytes, e.g. 12:34:56:78 or 00:00:12:34:56:78')
    if len(raw) not in (4,6):
        raise ValueError('UID must contain 4 or 6 bytes')
    if not any(raw[-4:]):
        raise ValueError('The last four UID bytes must not all be zero (unbound identity)')
    return raw.rjust(6,b'\x00')


def format_uid(uid):
    return parse_uid(uid).hex(':')


def uid_from_phrase(phrase):
    return hashlib.md5(('-DMY_BINDING_PHRASE="'+phrase+'"').encode()).digest()[:6]


def decode_sync(packet):
    return dict(fhss_index=packet[1],nonce=packet[2],rate_enum=packet[3],
                hybrid=bool(packet[4]&1) if len(packet)==8 else None,switch_encoding=packet[4]&1,
                telemetry_ratio_code=(packet[4]>>1)&7,gemini=bool(packet[4]&16),
                ota_protocol=(packet[4]>>5)&3,
                link_mode={0:'normal',1:'MAVLink'}.get((packet[4]>>5)&3,'reserved'),
                uid4=packet[5],model_matched_uid5=packet[6])


def unpack_four(data):
    packed = int.from_bytes(data,'little')
    return [(packed>>(10*i))&1023 for i in range(4)]


def update(channel,value,bits,kind='analog'):
    return dict(channel=channel,value=value,bits=bits,kind=kind)


def decode_rc(packet, mode='Hybrid', nonce=None):
    if len(packet) == 13:
        if mode not in ('8ch','12ch','16ch'):
            raise ValueError('Full-resolution packet requires 8ch, 12ch, or 16ch mode')
        high = bool(packet[0]&64)
        if mode == '8ch' and high:
            raise ValueError('High channel group in configured 8ch mode; check switch mode')
        low_base = 9 if mode == '16ch' and high else 1
        high_base = (13 if high else 5) if mode == '16ch' else (9 if high else 5)
        updates = [update(low_base+i,v,10) for i,v in enumerate(unpack_four(packet[1:6]))]
        updates += [update(high_base+i,v,10) for i,v in enumerate(unpack_four(packet[6:11]))]
        return dict(channel_updates=updates,armed=bool(packet[0]&128),
                    is_high_aux=high,stubborn_ack=bool(packet[0]&4),
                    uplink_power_code=((packet[0]>>3)&7)+1,switch_mode=mode)
    if len(packet) != 8 or mode not in ('Hybrid','Wide'):
        raise ValueError('Standard packet requires Hybrid or Wide mode')
    channels = unpack_four(packet[1:6])
    armed = bool(packet[6]&128)
    updates = [update(i+1,v,10) for i,v in enumerate(channels)]+[update(5,int(armed),1,'armed')]
    result = dict(channels_raw10=channels,armed=armed,stubborn_ack=bool(packet[6]&64),
                  channel_updates=updates,switch_mode=mode)
    if mode == 'Wide':
        if nonce is None:
            raise ValueError('Wide auxiliary slot requires a validated nonce')
        index = ((nonce&7)+((nonce>>3)&1))%8
        value = packet[6]&63
        if index == 7:
            result['uplink_power_code'] = value
            return result
        aux,bits = index+2,6
    else:
        index = (packet[6]>>3)&7
        aux = 8 if index >= 6 else index+2
        bits = 4 if index >= 6 else 3
        value = packet[6]&((1<<bits)-1)
    result.update(auxiliary_channel=aux,auxiliary_value=value,auxiliary_value_bits=bits)
    updates.append(update(aux+4,value,bits,'switch'))
    return result


def decode_data(packet):
    if len(packet) == 13:
        return dict(package_index=packet[0]>>3,stubborn_ack=bool(packet[0]&4),payload_hex=packet[1:11].hex())
    return dict(package_index=packet[1]&127,stubborn_ack=bool(packet[1]&128),payload_hex=packet[2:7].hex())
