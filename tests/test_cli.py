from __future__ import annotations

from pathlib import Path

import pytest

from athena.cli import main


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATHENA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ATHENA_VAULT_DIR", raising=False)
    monkeypatch.delenv("ATHENA_HUEY_SECRET", raising=False)


def test_version_command(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["version"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "athena" in captured.out


def test_doctor_command_runs_and_prints_report(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["doctor"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "vault_root" in captured.out
    assert "Overall:" in captured.out


def test_doctor_command_exit_code_reflects_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATHENA_VAULT_DIR", str(tmp_path / "nonexistent-vault"))
    exit_code = main(["doctor"])
    assert exit_code == 1


def test_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit):
        main([])


def test_duplicates_requires_a_subcommand() -> None:
    # Fails at argument-parsing time, before athena.worker's lazy import --
    # see tests/test_worker.py's module docstring for why worker-backed
    # commands are exercised there (via a fresh, isolated import per test)
    # rather than through `main()` here, where athena.worker's module-level
    # `huey`/`_config` would be whatever they were on this process's first
    # import, not necessarily this test's monkeypatched environment.
    with pytest.raises(SystemExit):
        main(["duplicates"])


def test_lifecycle_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit):
        main(["lifecycle"])


def test_duplicates_resolve_requires_confirm_or_reject() -> None:
    with pytest.raises(SystemExit):
        main(["duplicates", "resolve", "1"])


def test_duplicates_merge_requires_keep_flag() -> None:
    with pytest.raises(SystemExit):
        main(["duplicates", "merge", "1"])


def test_llm_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit):
        main(["llm"])


def test_llm_summarize_without_a_configured_vault(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["llm", "summarize", "a.md"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "ATHENA_VAULT_DIR is not set" in captured.out


def test_llm_summarize_missing_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    monkeypatch.setenv("ATHENA_VAULT_DIR", str(vault_dir))

    exit_code = main(["llm", "summarize", "does-not-exist.md"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "cannot read vault note" in captured.out


def test_llm_summarize_disabled_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "a.md").write_text("some note content\n", encoding="utf-8")
    monkeypatch.setenv("ATHENA_VAULT_DIR", str(vault_dir))

    exit_code = main(["llm", "summarize", "a.md"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "disabled" in captured.out.lower()
