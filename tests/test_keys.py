import re

from app import keys


def test_generated_key_has_expected_format():
    key = keys.generate_key()

    assert re.fullmatch(r"mgd_[0-9A-Za-z]{43}_[0-9a-f]{6}", key)
    assert keys.is_well_formed(key)


def test_generated_keys_are_unique():
    assert len({keys.generate_key() for _ in range(1000)}) == 1000


def test_typo_in_key_fails_checksum():
    key = keys.generate_key()
    body_char = key[10]
    typo = key[:10] + ("A" if body_char != "A" else "B") + key[11:]

    assert not keys.is_well_formed(typo)


def test_malformed_keys_are_rejected():
    valid = keys.generate_key()
    for bad in ["", "mgd_", "sk-abc", valid.replace("mgd_", "xyz_"), valid + "0", valid[:-1], valid.upper()]:
        assert not keys.is_well_formed(bad), bad


def test_hash_is_deterministic_sha256_hex():
    key = keys.generate_key()

    assert keys.hash_key(key) == keys.hash_key(key)
    assert re.fullmatch(r"[0-9a-f]{64}", keys.hash_key(key))
    assert keys.hash_key(key) != keys.hash_key(keys.generate_key())


def test_display_prefix_is_short_and_part_of_the_key():
    key = keys.generate_key()

    assert keys.display_prefix(key) == key[:12]
    assert key.startswith(keys.display_prefix(key))
