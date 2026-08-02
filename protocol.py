"""Compact, versioned ESP-NOW wire format for BadgeBlud."""

try:
    import ustruct as struct
except ImportError:  # Host CPython
    import struct

import math


MAGIC = b"BB"
VERSION = 2
MAX_PLAYERS = 4
NAME_LENGTH = 8
POSITION_SCALE = 256.0
ANGLE_SCALE = 65535.0 / (2.0 * math.pi)

BEACON = 1
JOIN = 2
ACCEPT = 3
REJECT = 4
START = 5
POSE = 6
SNAPSHOT = 7
FIRE = 8
HIT = 9
END = 10
LEAVE = 11

PHASE_LOBBY = 0
PHASE_MATCH = 1

FLAG_DEAD = 1
FLAG_PROTECTED = 2
FLAG_MAP_OPEN = 4

REJECT_FULL = 1
REJECT_STARTED = 2
REJECT_VERSION = 3

HEADER_FORMAT = ">2sBBHH"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
PLAYER_FORMAT = ">B6s8shhHBBBBBH"
PLAYER_SIZE = struct.calcsize(PLAYER_FORMAT)


class ProtocolError(ValueError):
    pass


def clean_name(value):
    if not isinstance(value, str):
        value = str(value)
    # MicroPython accepts codec arguments here but still emits UTF-8, and its
    # bytes type does not provide CPython's ljust(). Build ASCII explicitly.
    encoded = bytearray()
    for character in value:
        code = ord(character)
        if 32 <= code <= 126:
            encoded.append(code)
        if len(encoded) >= NAME_LENGTH:
            break
    encoded = bytes(encoded)
    return encoded + b" " * (NAME_LENGTH - len(encoded))


def decode_name(value):
    # Decode manually as well: this keeps malformed or non-ASCII peer names
    # from escaping the radio handler on either Python implementation.
    characters = []
    for item in value:
        code = item if isinstance(item, int) else ord(item)
        if code in (0, 32):
            continue
        characters.append(chr(code) if 32 <= code <= 126 else "?")
    return "".join(characters) or "badge"


def encode_position(value):
    return int(max(-127.0, min(127.0, value)) * POSITION_SCALE)


def decode_position(value):
    return value / POSITION_SCALE


def encode_angle(value):
    return int((value % (2.0 * math.pi)) * ANGLE_SCALE) & 0xFFFF


def decode_angle(value):
    return value / ANGLE_SCALE


def packet(message_type, session, sequence, payload=b""):
    return struct.pack(
        HEADER_FORMAT,
        MAGIC,
        VERSION,
        message_type,
        session & 0xFFFF,
        sequence & 0xFFFF,
    ) + payload


def parse(data):
    if not isinstance(data, (bytes, bytearray)) or len(data) < HEADER_SIZE:
        raise ProtocolError("truncated header")
    magic, version, message_type, session, sequence = struct.unpack(
        HEADER_FORMAT, data[:HEADER_SIZE]
    )
    if magic != MAGIC:
        raise ProtocolError("foreign packet")
    if version != VERSION:
        raise ProtocolError("protocol version")
    if message_type < BEACON or message_type > LEAVE:
        raise ProtocolError("message type")
    return message_type, session, sequence, bytes(data[HEADER_SIZE:])


def pack_beacon(session, sequence, phase, count, channel, name):
    payload = struct.pack(">BBB8s", phase, count, channel or 0, clean_name(name))
    return packet(BEACON, session, sequence, payload)


def unpack_beacon(payload):
    if len(payload) != struct.calcsize(">BBB8s"):
        raise ProtocolError("beacon size")
    phase, count, channel, name = struct.unpack(">BBB8s", payload)
    return phase, count, channel, decode_name(name)


def pack_join(session, sequence, name):
    return packet(JOIN, session, sequence, clean_name(name))


def unpack_join(payload):
    if len(payload) != NAME_LENGTH:
        raise ProtocolError("join size")
    return decode_name(payload)


def pack_accept(session, sequence, slot):
    return packet(ACCEPT, session, sequence, struct.pack(">B", slot))


def unpack_accept(payload):
    if len(payload) != 1:
        raise ProtocolError("accept size")
    return struct.unpack(">B", payload)[0]


def pack_reject(session, sequence, reason):
    return packet(REJECT, session, sequence, struct.pack(">B", reason))


def unpack_reject(payload):
    if len(payload) != 1:
        raise ProtocolError("reject size")
    return struct.unpack(">B", payload)[0]


def pack_start(session, sequence, countdown_ms=3000):
    return packet(START, session, sequence, struct.pack(">H", countdown_ms))


def unpack_start(payload):
    if len(payload) != 2:
        raise ProtocolError("start size")
    return struct.unpack(">H", payload)[0]


def pack_pose(session, sequence, x, y, heading, flags):
    payload = struct.pack(
        ">hhHB", encode_position(x), encode_position(y), encode_angle(heading), flags
    )
    return packet(POSE, session, sequence, payload)


def unpack_pose(payload):
    if len(payload) != struct.calcsize(">hhHB"):
        raise ProtocolError("pose size")
    x, y, heading, flags = struct.unpack(">hhHB", payload)
    return decode_position(x), decode_position(y), decode_angle(heading), flags


def pack_fire(session, sequence, heading):
    return packet(FIRE, session, sequence, struct.pack(">H", encode_angle(heading)))


def unpack_fire(payload):
    if len(payload) != 2:
        raise ProtocolError("fire size")
    return decode_angle(struct.unpack(">H", payload)[0])


def pack_hit(session, sequence, attacker, victim, health):
    return packet(HIT, session, sequence, struct.pack(">BBB", attacker, victim, health))


def unpack_hit(payload):
    if len(payload) != 3:
        raise ProtocolError("hit size")
    return struct.unpack(">BBB", payload)


def pack_end(session, sequence, winner):
    return packet(END, session, sequence, struct.pack(">B", winner))


def unpack_end(payload):
    if len(payload) != 1:
        raise ProtocolError("end size")
    return struct.unpack(">B", payload)[0]


def pack_leave(session, sequence):
    return packet(LEAVE, session, sequence)


def pack_snapshot(session, sequence, remaining_ms, players):
    payload = struct.pack(">IB", max(0, min(0xFFFFFFFF, remaining_ms)), len(players))
    for player in players:
        payload += struct.pack(
            PLAYER_FORMAT,
            player["slot"],
            player["mac"],
            clean_name(player["name"]),
            encode_position(player["x"]),
            encode_position(player["y"]),
            encode_angle(player["heading"]),
            player["health"],
            player["kills"],
            player["deaths"],
            player["streak"],
            player["flags"],
            max(0, min(65535, player.get("respawn_ms", 0))),
        )
    return packet(SNAPSHOT, session, sequence, payload)


def unpack_snapshot(payload):
    if len(payload) < 5:
        raise ProtocolError("snapshot size")
    remaining_ms, count = struct.unpack(">IB", payload[:5])
    if count > MAX_PLAYERS or len(payload) != 5 + count * PLAYER_SIZE:
        raise ProtocolError("snapshot players")
    players = []
    offset = 5
    for _ in range(count):
        values = struct.unpack(PLAYER_FORMAT, payload[offset : offset + PLAYER_SIZE])
        players.append(
            {
                "slot": values[0],
                "mac": values[1],
                "name": decode_name(values[2]),
                "x": decode_position(values[3]),
                "y": decode_position(values[4]),
                "heading": decode_angle(values[5]),
                "health": values[6],
                "kills": values[7],
                "deaths": values[8],
                "streak": values[9],
                "flags": values[10],
                "respawn_ms": values[11],
            }
        )
        offset += PLAYER_SIZE
    return remaining_ms, players
