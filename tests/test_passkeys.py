"""Tests for swarm.auth.passkeys — PasskeyStore."""

from __future__ import annotations

import time

from swarm.auth.passkeys import PasskeyStore, StoredCredential


def _make_cred(name: str = "test", cred_id: bytes = b"cred1") -> StoredCredential:
    return StoredCredential(
        credential_id=cred_id,
        public_key=b"pubkey-data",
        sign_count=0,
        device_name=name,
        registered_at=time.time(),
    )


class TestPasskeyStore:
    def test_empty_store(self, tmp_path) -> None:
        store = PasskeyStore(tmp_path / "passkeys.json")
        assert store.get_all() == []
        assert store.has_any() is False

    def test_add_and_load(self, tmp_path) -> None:
        path = tmp_path / "passkeys.json"
        store = PasskeyStore(path)
        cred = _make_cred("laptop")
        store.add(cred)

        assert store.has_any() is True
        loaded = store.get_all()
        assert len(loaded) == 1
        assert loaded[0].device_name == "laptop"
        assert loaded[0].credential_id == b"cred1"

    def test_remove(self, tmp_path) -> None:
        path = tmp_path / "passkeys.json"
        store = PasskeyStore(path)
        store.add(_make_cred("a", b"id-a"))
        store.add(_make_cred("b", b"id-b"))
        assert len(store.get_all()) == 2

        store.remove(b"id-a")
        remaining = store.get_all()
        assert len(remaining) == 1
        assert remaining[0].device_name == "b"

    def test_update_sign_count(self, tmp_path) -> None:
        path = tmp_path / "passkeys.json"
        store = PasskeyStore(path)
        store.add(_make_cred("laptop", b"cred-x"))

        store.update_sign_count(b"cred-x", 42)
        loaded = store.get_all()
        assert loaded[0].sign_count == 42

    def test_roundtrip_serialization(self, tmp_path) -> None:
        path = tmp_path / "passkeys.json"
        store = PasskeyStore(path)
        cred = _make_cred("phone", b"\x00\x01\x02\xff")
        store.add(cred)

        # Load from fresh instance
        store2 = PasskeyStore(path)
        loaded = store2.get_all()
        assert len(loaded) == 1
        assert loaded[0].credential_id == b"\x00\x01\x02\xff"
        assert loaded[0].device_name == "phone"

    def test_corrupted_file(self, tmp_path) -> None:
        path = tmp_path / "passkeys.json"
        path.write_text("not json")
        store = PasskeyStore(path)
        assert store.get_all() == []

    def test_file_permissions(self, tmp_path) -> None:
        import os
        import stat

        path = tmp_path / "passkeys.json"
        store = PasskeyStore(path)
        store.add(_make_cred())
        mode = os.stat(path).st_mode
        assert not (mode & stat.S_IROTH)
        assert not (mode & stat.S_IWOTH)
