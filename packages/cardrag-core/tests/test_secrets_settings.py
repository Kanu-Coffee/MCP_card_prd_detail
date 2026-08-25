from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from cardrag_core import SecretResolutionError, WebDAVSettings, resolve_env_secret


def test_env_and_file_secret_are_mutually_exclusive(tmp_path: Path) -> None:
    secret_file = tmp_path / "password"
    secret_file.write_text("from-file\n", encoding="utf-8")
    assert resolve_env_secret("TOKEN", environ={"TOKEN": "direct"}) == "direct"
    assert resolve_env_secret("TOKEN", environ={"TOKEN_FILE": str(secret_file)}) == "from-file"
    with pytest.raises(SecretResolutionError, match="only one"):
        resolve_env_secret(
            "TOKEN",
            environ={"TOKEN": "direct", "TOKEN_FILE": str(secret_file)},
        )
    with pytest.raises(SecretResolutionError, match="required"):
        resolve_env_secret("TOKEN", environ={})
    with pytest.raises(SecretResolutionError, match="absolute"):
        resolve_env_secret("TOKEN", environ={"TOKEN_FILE": "relative-secret"})


def test_webdav_settings_use_exact_environment_contract(tmp_path: Path) -> None:
    username_file = tmp_path / "username"
    password_file = tmp_path / "password"
    ca_file = tmp_path / "ca.pem"
    username_file.write_text("dav-user\n", encoding="utf-8")
    password_file.write_text("dav-password\n", encoding="utf-8")
    ca_file.write_text("test-ca", encoding="utf-8")
    settings = WebDAVSettings.from_env(
        environ={
            "CARDRAG_ENVIRONMENT": "production",
            "CARDRAG_WEBDAV_BASE_URL": "https://dav.example.test/root",
            "CARDRAG_WEBDAV_USERNAME_FILE": str(username_file),
            "CARDRAG_WEBDAV_PASSWORD_FILE": str(password_file),
            "CARDRAG_WEBDAV_CA_FILE": str(ca_file),
        }
    )
    assert settings.base_url == "https://dav.example.test/root/"
    assert settings.username == "dav-user"
    assert settings.password.get_secret_value() == "dav-password"
    assert settings.connect_timeout_seconds == 10
    assert settings.transfer_timeout_seconds == 600
    assert settings.httpx_verify == str(ca_file)
    assert "dav-password" not in repr(settings)


def test_webdav_settings_reject_unsafe_urls_and_production_http() -> None:
    common = {"username": "user", "password": SecretStr("password")}
    with pytest.raises(ValidationError, match="must use HTTPS"):
        WebDAVSettings(base_url="http://dav.example.test/root", **common)
    with pytest.raises(ValidationError, match="credentials are forbidden"):
        WebDAVSettings(base_url="https://user:pass@dav.example.test/root", **common)
    with pytest.raises(ValidationError, match="query or fragment"):
        WebDAVSettings(base_url="https://dav.example.test/root?q=1", **common)
    with pytest.raises(ValidationError, match="traversal"):
        WebDAVSettings(base_url="https://dav.example.test/root/%2e%2e/private", **common)

    test_settings = WebDAVSettings(
        environment="test",
        base_url="http://127.0.0.1:8080/dav",
        allow_insecure_http=True,
        **common,
    )
    assert test_settings.base_url == "http://127.0.0.1:8080/dav/"


def test_username_and_password_file_conflicts_fail_closed(tmp_path: Path) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text("value", encoding="utf-8")
    with pytest.raises(SecretResolutionError, match="USERNAME"):
        WebDAVSettings.from_env(
            environ={
                "CARDRAG_WEBDAV_BASE_URL": "https://dav.example.test/",
                "CARDRAG_WEBDAV_USERNAME": "user",
                "CARDRAG_WEBDAV_USERNAME_FILE": str(secret_file),
                "CARDRAG_WEBDAV_PASSWORD": "password",
            }
        )
