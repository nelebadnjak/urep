from __future__ import annotations

import hashlib
import hmac
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAC_SALT = b"urep-auth-v1"
SESSION_SALT = b"urep-session-v1"
DISCOVERY_SALT = b"urep-discovery-v1"
DISCOVERY_AAD = b"urep-discovery-v1"
NONCE_LEN = 12
PROOF_SERVER = b"S"
PROOF_CLIENT = b"C"


class CryptoError(ValueError):
    pass


def _hkdf(token: str, salt: bytes, info: bytes, length: int = 32) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(
        token.encode("utf-8")
    )


def derive_mac_key(token: str) -> bytes:
    return _hkdf(token, MAC_SALT, b"mac")


def derive_session_key(token: str, client_nonce: bytes, server_nonce: bytes) -> bytes:
    if len(client_nonce) != 32 or len(server_nonce) != 32:
        raise CryptoError("handshake nonces must be 32 bytes")
    return _hkdf(token, SESSION_SALT + client_nonce + server_nonce, b"frame-key")


def _proof(
    token: str,
    kind: bytes,
    client_nonce: bytes,
    server_nonce: bytes,
    node_id: str,
) -> bytes:
    if len(client_nonce) != 32 or len(server_nonce) != 32:
        raise CryptoError("handshake nonces must be 32 bytes")
    encoded = node_id.encode("utf-8")
    message = (
        kind
        + len(encoded).to_bytes(2, "big")
        + encoded
        + client_nonce
        + server_nonce
    )
    return hmac.new(derive_mac_key(token), message, hashlib.sha256).digest()


def server_proof(
    token: str, client_nonce: bytes, server_nonce: bytes, node_id: str
) -> bytes:
    return _proof(token, PROOF_SERVER, client_nonce, server_nonce, node_id)


def client_proof(
    token: str, client_nonce: bytes, server_nonce: bytes, node_id: str
) -> bytes:
    return _proof(token, PROOF_CLIENT, client_nonce, server_nonce, node_id)


def verify(expected: bytes, received: bytes) -> bool:
    return hmac.compare_digest(expected, received)


def encrypt_frame(key: bytes, msg_type: int, nonce: bytes, plaintext: bytes) -> bytes:
    aead = ChaCha20Poly1305(key)
    return aead.encrypt(nonce, plaintext, bytes([msg_type]))


def decrypt_frame(key: bytes, msg_type: int, nonce: bytes, ciphertext: bytes) -> bytes:
    try:
        aead = ChaCha20Poly1305(key)
        return aead.decrypt(nonce, ciphertext, bytes([msg_type]))
    except Exception as exc:  # cryptography raises InvalidTag
        raise CryptoError("frame authentication failed") from exc


def derive_discovery_key(token: str) -> bytes:
    return _hkdf(token, DISCOVERY_SALT, b"announce")


def encrypt_discovery(token: str, payload: bytes) -> bytes:
    nonce = os.urandom(NONCE_LEN)
    key = derive_discovery_key(token)
    return nonce + ChaCha20Poly1305(key).encrypt(nonce, payload, DISCOVERY_AAD)


def decrypt_discovery(token: str, packet: bytes) -> bytes:
    if len(packet) < NONCE_LEN + 16:
        raise CryptoError("discovery packet too short")
    nonce = packet[:NONCE_LEN]
    ciphertext = packet[NONCE_LEN:]
    try:
        key = derive_discovery_key(token)
        return ChaCha20Poly1305(key).decrypt(nonce, ciphertext, DISCOVERY_AAD)
    except Exception as exc:
        raise CryptoError("discovery authentication failed") from exc
