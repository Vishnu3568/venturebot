"""Tests for the application-boundary environment configuration loader (Step 30B)."""

import os
from pathlib import Path

import pytest

from venturebot.env import load_env_file


def test_load_env_file_missing_path(tmp_path: Path):
    """Verify that load_env_file returns False gracefully when file does not exist."""
    missing = tmp_path / "does_not_exist.env"
    result = load_env_file(missing)
    assert result is False


def test_load_env_file_loads_harmless_variable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify a harmless test variable is parsed and loaded into os.environ."""
    test_key = "VENTUREBOT_HARMLESS_TEST_VAR"
    monkeypatch.delenv(test_key, raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text(f"{test_key}=venturebot_alpha_123\n", encoding="utf-8")

    result = load_env_file(env_file)
    assert result is True
    assert os.environ.get(test_key) == "venturebot_alpha_123"


def test_load_env_file_preserves_existing_os_environ_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify that existing os.environ variables take precedence over .env definitions."""
    test_key = "VENTUREBOT_PRECEDENCE_TEST_VAR"
    monkeypatch.setenv(test_key, "existing_os_value")

    env_file = tmp_path / ".env"
    env_file.write_text(f"{test_key}=env_file_value\n", encoding="utf-8")

    result = load_env_file(env_file)
    assert result is True
    assert os.environ.get(test_key) == "existing_os_value"


def test_load_env_file_ignores_comments_and_blank_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify blank lines, comment lines, and malformed lines without equals are ignored."""
    test_key = "VENTUREBOT_VALID_VAR"
    monkeypatch.delenv(test_key, raising=False)

    lines = [
        "# This is a comment at line start",
        "   # This is an indented comment",
        "",
        "   ",
        "MALFORMED_LINE_WITHOUT_EQUALS",
        "=VALUE_WITHOUT_KEY",
        f"{test_key}=active_value",
    ]
    env_file = tmp_path / ".env"
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = load_env_file(env_file)
    assert result is True
    assert os.environ.get(test_key) == "active_value"
    assert "MALFORMED_LINE_WITHOUT_EQUALS" not in os.environ


def test_load_env_file_handles_quoted_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify single and double surrounding quotes are stripped, and inner equals preserved."""
    keys = [
        "VENTUREBOT_DOUBLE_QUOTED",
        "VENTUREBOT_SINGLE_QUOTED",
        "VENTUREBOT_UNQUOTED",
        "VENTUREBOT_EMPTY_DOUBLE",
        "VENTUREBOT_EMPTY_SINGLE",
        "VENTUREBOT_NESTED_QUOTES",
        "VENTUREBOT_INNER_EQUALS",
    ]
    for k in keys:
        monkeypatch.delenv(k, raising=False)

    content = (
        'VENTUREBOT_DOUBLE_QUOTED="double quoted value"\n'
        "VENTUREBOT_SINGLE_QUOTED='single quoted value'\n"
        "VENTUREBOT_UNQUOTED=unquoted_value\n"
        'VENTUREBOT_EMPTY_DOUBLE=""\n'
        "VENTUREBOT_EMPTY_SINGLE=''\n"
        'VENTUREBOT_NESTED_QUOTES="value with \'inner\' quotes"\n'
        "VENTUREBOT_INNER_EQUALS=part1=part2=part3\n"
    )
    env_file = tmp_path / ".env"
    env_file.write_text(content, encoding="utf-8")

    result = load_env_file(env_file)
    assert result is True
    assert os.environ.get("VENTUREBOT_DOUBLE_QUOTED") == "double quoted value"
    assert os.environ.get("VENTUREBOT_SINGLE_QUOTED") == "single quoted value"
    assert os.environ.get("VENTUREBOT_UNQUOTED") == "unquoted_value"
    assert os.environ.get("VENTUREBOT_EMPTY_DOUBLE") == ""
    assert os.environ.get("VENTUREBOT_EMPTY_SINGLE") == ""
    assert os.environ.get("VENTUREBOT_NESTED_QUOTES") == "value with 'inner' quotes"
    assert os.environ.get("VENTUREBOT_INNER_EQUALS") == "part1=part2=part3"
