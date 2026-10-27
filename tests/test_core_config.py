from __future__ import annotations

"""Settings parsing for list-valued fields.

The list fields are read straight from the environment, where an operator writes a
comma-separated string and the docs used to insist on JSON. Both must work; a
``SettingsError`` at import time is a server that never starts, which is how the CORS
documentation bug showed up.
"""

from app.core.config import Settings


def test_cors_origins_accepts_comma_separated_text():
    settings = Settings(cors_origins="http://localhost:3000, https://app.example.org")
    assert settings.cors_origins == ["http://localhost:3000", "https://app.example.org"]


def test_cors_origins_accepts_a_json_array():
    settings = Settings(cors_origins='["https://a.example", "https://b.example"]')
    assert settings.cors_origins == ["https://a.example", "https://b.example"]


def test_an_empty_list_field_is_an_empty_list():
    settings = Settings(cors_origins="")
    assert settings.cors_origins == []


def test_quoted_values_are_unwrapped():
    settings = Settings(cors_origins="'https://a.example',\"https://b.example\"")
    assert settings.cors_origins == ["https://a.example", "https://b.example"]


def test_overpass_mirrors_and_api_keys_use_the_same_rule():
    settings = Settings(overpass_mirrors="https://one.example/api,https://two.example/api", api_keys="k1,k2")
    assert settings.overpass_mirrors == ["https://one.example/api", "https://two.example/api"]
    assert settings.api_keys == ["k1", "k2"]
