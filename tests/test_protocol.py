from __future__ import annotations

import pytest

from urep.crypto import CryptoError
from urep.protocol import (
    Msg,
    ProtocolError,
    decode_plain,
    decode_secure,
    encode_plain,
    encode_secure,
    msg_name,
)


def test_plain_roundtrip() -> None:
    frame = encode_plain(
        Msg.HELLO,
        {"proto": 1, "node_id": "node", "tags": ["a"], "role": "peer", "nonce": b"x" * 32},
    )
    msg_type, payload = decode_plain(frame)
    assert msg_type == Msg.HELLO
    assert payload["node_id"] == "node"
    assert payload["nonce"] == b"x" * 32


def test_secure_roundtrip() -> None:
    key = b"k" * 32
    frame = encode_secure(Msg.PING, {"ts": 123}, key)
    msg_type, payload = decode_secure(frame, key)
    assert msg_type == Msg.PING
    assert payload["ts"] == 123


def test_secure_wrong_key_fails() -> None:
    frame = encode_secure(Msg.PING, {"ts": 1}, b"a" * 32)
    with pytest.raises(CryptoError):
        decode_secure(frame, b"b" * 32)


def test_plain_truncated_fails() -> None:
    with pytest.raises(ProtocolError):
        decode_plain(b"\x00\x00")


def test_plain_length_mismatch_fails() -> None:
    frame = encode_plain(Msg.PING, {"ts": 1})
    broken = b"\x00\x00\xff\xff" + frame[4:]
    with pytest.raises(ProtocolError):
        decode_plain(broken)


def test_payload_must_be_mapping() -> None:
    with pytest.raises(ProtocolError):
        encode_plain(Msg.PING, [1, 2, 3])  # type: ignore[arg-type]


def test_oversized_plain_frame_rejected() -> None:
    with pytest.raises(ProtocolError):
        encode_plain(Msg.PING, {"blob": "x" * (70 * 1024)})


def test_msg_name() -> None:
    assert msg_name(Msg.PING) == "PING"
    assert msg_name(0xFE).startswith("0x")
