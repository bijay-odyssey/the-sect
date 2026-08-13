"""The .env parser.

It sits directly in the credential path -- this is what reads the database password off
disk -- so the edge cases get tests rather than trust.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sect.env import find_env_file, load_dotenv, parse_env


def test_parses_plain_assignments() -> None:
    parsed = parse_env("FOO=bar\nBAZ=qux\n")
    assert parsed == {"FOO": "bar", "BAZ": "qux"}


def test_ignores_blank_lines_and_comments() -> None:
    parsed = parse_env("# a comment\n\nFOO=bar\n   \n# another\n")
    assert parsed == {"FOO": "bar"}


def test_accepts_an_export_prefix() -> None:
    assert parse_env("export FOO=bar") == {"FOO": "bar"}


def test_quotes_are_stripped_and_preserve_spacing() -> None:
    parsed = parse_env("A=\"  padded  \"\nB='  single  '")
    assert parsed == {"A": "  padded  ", "B": "  single  "}


def test_escapes_are_decoded_only_inside_double_quotes() -> None:
    parsed = parse_env(r'A="one\ntwo"' + "\n" + r"B='one\ntwo'")
    assert parsed["A"] == "one\ntwo"
    assert parsed["B"] == r"one\ntwo"


def test_a_hash_in_an_unquoted_value_is_not_a_comment() -> None:
    """The case that matters: a database password containing '#'.

    Stripping inline comments would silently truncate the connection string and produce
    a baffling authentication failure, so the parser does not do it.
    """
    url = "postgresql://user:pa#ssw0rd@host.neon.tech/db?sslmode=require"
    assert parse_env(f"DATABASE_URL={url}")["DATABASE_URL"] == url


def test_an_equals_sign_in_a_value_survives() -> None:
    parsed = parse_env("URL=postgres://h/db?opts=a=b&c=d")
    assert parsed["URL"] == "postgres://h/db?opts=a=b&c=d"


def test_lines_without_an_equals_sign_are_skipped() -> None:
    assert parse_env("NOT_AN_ASSIGNMENT\nFOO=bar") == {"FOO": "bar"}


def test_the_real_environment_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Render sets real variables. A stray .env in an image must not override them."""
    env_file = tmp_path / ".env"
    env_file.write_text("SECT_TEST_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("SECT_TEST_KEY", "from-environment")
    monkeypatch.delenv("SECT_SKIP_DOTENV", raising=False)

    applied = load_dotenv(env_file)

    assert applied == {}
    assert os.environ["SECT_TEST_KEY"] == "from-environment"


def test_override_is_available_when_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SECT_TEST_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("SECT_TEST_KEY", "from-environment")
    monkeypatch.delenv("SECT_SKIP_DOTENV", raising=False)

    assert load_dotenv(env_file, override=True) == {"SECT_TEST_KEY": "from-file"}
    assert os.environ["SECT_TEST_KEY"] == "from-file"


def test_skip_variable_disables_loading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What keeps this very test suite off a developer's real database."""
    env_file = tmp_path / ".env"
    env_file.write_text("SECT_TEST_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("SECT_SKIP_DOTENV", "1")

    assert load_dotenv(env_file) == {}
    assert "SECT_TEST_KEY" not in os.environ


def test_a_missing_file_is_not_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECT_SKIP_DOTENV", raising=False)
    assert load_dotenv(tmp_path / "nothing-here") == {}


def test_the_search_walks_upwards(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("FOO=bar\n", encoding="utf-8")
    nested = tmp_path / "examples" / "disciple-scribe"
    nested.mkdir(parents=True)

    assert find_env_file(nested) == (tmp_path / ".env").resolve()


def test_the_search_gives_up_cleanly(tmp_path: Path) -> None:
    assert find_env_file(tmp_path, filename=".env-that-does-not-exist") is None
