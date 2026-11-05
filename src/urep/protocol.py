from __future__ import annotations

import os
import struct
from enum import IntEnum
from typing import Any

import cbor2

from .crypto import decrypt_frame, encrypt_frame

PROTO_VERSION = 1
MAX_PLAIN = 64 * 1024
MAX_SECURE = 1024 * 1024
NONCE_LEN = 12


class ProtocolError(ValueError):
    pass


class Msg(IntEnum):
    HELLO = 0x01
    HELLO_ACK = 0x02
    AUTH = 0x03

    PING = 0x10
    PONG = 0x11
    PEERS = 0x12

    MANIFEST_REQ = 0x20
    MANIFEST = 0x21

    FILE_PUT = 0x30
    FILE_CHUNK = 0x31
    FILE_DELETE = 0x32
    FILE_GET = 0x33
    FILE_DATA = 0x34

    ACK = 0x40
    NACK = 0x41


PLAIN_TYPES = {Msg.HELLO, Msg.HELLO_ACK, Msg.AUTH}


def _check_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ProtocolError("payload must be a mapping")
    return payload


def encode_plain(msg_type: int, payload: dict[str, Any]) -> bytes:
    body = cbor2.dumps(_check_payload(payload))
    if len(body) + 1 > MAX_PLAIN:
        raise ProtocolError("plain frame too large")
    return struct.pack(">IB", len(body) + 1, int(msg_type)) + body


def decode_plain(frame: bytes) -> tuple[int, dict[str, Any]]:
    if len(frame) < 5:
        raise ProtocolError("plain frame too short")
    length = struct.unpack(">I", frame[:4])[0]
    if length != len(frame) - 4 or length < 1:
        raise ProtocolError("invalid plain frame length")
    msg_type = frame[4]
    try:
        payload = cbor2.loads(frame[5:])
    except Exception as exc:
        raise ProtocolError("invalid CBOR payload") from exc
    return msg_type, _check_payload(payload)


def encode_secure(msg_type: int, payload: dict[str, Any], key: bytes) -> bytes:
    plaintext = cbor2.dumps(_check_payload(payload))
    nonce = os.urandom(NONCE_LEN)
    ciphertext = encrypt_frame(key, int(msg_type), nonce, plaintext)
    body = bytes([int(msg_type)]) + nonce + ciphertext
    if len(body) > MAX_SECURE:
        raise ProtocolError("secure frame too large")
    return struct.pack(">I", len(body)) + body


def decode_secure(frame: bytes, key: bytes) -> tuple[int, dict[str, Any]]:
    if len(frame) < 4 + 1 + NONCE_LEN + 16:
        raise ProtocolError("secure frame too short")
    length = struct.unpack(">I", frame[:4])[0]
    if length != len(frame) - 4 or length > MAX_SECURE:
        raise ProtocolError("invalid secure frame length")
    body = frame[4:]
    msg_type = body[0]
    nonce = body[1 : 1 + NONCE_LEN]
    ciphertext = body[1 + NONCE_LEN :]
    plaintext = decrypt_frame(key, msg_type, nonce, ciphertext)
    try:
        payload = cbor2.loads(plaintext)
    except Exception as exc:
        raise ProtocolError("invalid CBOR payload") from exc
    return msg_type, _check_payload(payload)


async def read_frame(reader, max_size: int) -> bytes:
    header = await reader.readexactly(4)
    length = struct.unpack(">I", header)[0]
    if length < 1 or length > max_size:
        raise ProtocolError(f"frame length out of range: {length}")
    body = await reader.readexactly(length)
    return header + body


async def write_frame(writer, frame: bytes) -> None:
    writer.write(frame)
    await writer.drain()


async def read_plain(reader) -> tuple[int, dict[str, Any]]:
    return decode_plain(await read_frame(reader, MAX_PLAIN))


async def write_plain(writer, msg_type: int, payload: dict[str, Any]) -> None:
    await write_frame(writer, encode_plain(msg_type, payload))


async def read_secure(reader, key: bytes) -> tuple[int, dict[str, Any]]:
    return decode_secure(await read_frame(reader, MAX_SECURE), key)


async def write_secure(writer, msg_type: int, payload: dict[str, Any], key: bytes) -> None:
    await write_frame(writer, encode_secure(msg_type, payload, key))


def msg_name(msg_type: int) -> str:
    try:
        return Msg(msg_type).name
    except ValueError:
        return f"0x{msg_type:02x}"
