from __future__ import annotations

import pytest

from urep.crypto import (
    CryptoError,
    client_proof,
    decrypt_frame,
    derive_mac_key,
    derive_session_key,
    encrypt_frame,
    server_proof,
    verify,
)


def test_keys_are_bound_to_token() -> None:
    assert derive_mac_key("token-a") != derive_mac_key("token-b")
    nonce_a = b"a" * 32
    nonce_b = b"b" * 32
    assert derive_session_key("token", nonce_a, nonce_b) != derive_session_key(
        "token", nonce_b, nonce_a
    )


def test_proofs_bind_kind_nonce_and_node() -> None:
    client_nonce = b"c" * 32
    server_nonce = b"s" * 32
    proof = server_proof("token", client_nonce, server_nonce, "srv")
    assert verify(proof, server_proof("token", client_nonce, server_nonce, "srv"))
    assert not verify(proof, server_proof("wrong", client_nonce, server_nonce, "srv"))
    assert not verify(proof, server_proof("token", client_nonce, server_nonce, "other"))
    assert not verify(
        proof, client_proof("token", client_nonce, server_nonce, "srv")
    )


def test_frame_roundtrip_and_tamper_detection() -> None:
    key = derive_session_key("token", b"c" * 32, b"s" * 32)
    nonce = b"n" * 12
    ciphertext = encrypt_frame(key, 0x10, nonce, b"hello")
    assert decrypt_frame(key, 0x10, nonce, ciphertext) == b"hello"

    with pytest.raises(CryptoError):
        decrypt_frame(key, 0x11, nonce, ciphertext)

    other_key = derive_session_key("other", b"c" * 32, b"s" * 32)
    with pytest.raises(CryptoError):
        decrypt_frame(other_key, 0x10, nonce, ciphertext)


def test_nonce_length_is_enforced() -> None:
    with pytest.raises(CryptoError):
        derive_session_key("token", b"short", b"x" * 32)
