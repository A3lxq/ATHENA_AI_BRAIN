from __future__ import annotations

from pathlib import Path

import pytest

from athena.cli import _cmd_bench_index, _cmd_bench_retrieval, build_parser, main


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


def test_research_start_refuses_cleanly_at_the_daily_dispatch_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The dispatch-limit check happens before athena.worker is even imported
    # (cli._cmd_research_start defers that import until after the check
    # passes), so this test needs no ATHENA_HUEY_SECRET -- unlike
    # tests/test_worker.py's worker-backed tests, main() here never reaches
    # the code that would require one.
    monkeypatch.setenv("ATHENA_RESEARCH_MAX_DISPATCHES_PER_DAY", "0")

    migrate_exit_code = main(["migrate"])
    assert migrate_exit_code == 0
    capsys.readouterr()

    exit_code = main(
        ["research", "start", "--url", "https://a.example/", "--topic", "Ceiling Topic"]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "[FAIL]" in captured.out
    assert "daily dispatch limit" in captured.out


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


def test_bench_requires_a_subcommand() -> None:
    # Fails at argument-parsing time -- mirrors test_llm_requires_a_
    # subcommand/test_duplicates_requires_a_subcommand's own pattern.
    with pytest.raises(SystemExit):
        main(["bench"])


def test_bench_mutation_reports_mean_and_p95(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `bench mutation` builds its own throwaway git-repo vault internally
    # (see `_init_bench_git_repo`/`_cmd_bench_mutation` in athena.cli) -- it
    # needs no ATHENA_VAULT_DIR/ATHENA_HUEY_SECRET at all, only a writable
    # ATHENA_DATA_DIR (already set by the autouse `_isolated_data_dir`
    # fixture) and a real `git` binary on PATH.
    exit_code = main(["bench", "mutation", "--iterations", "2"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "=== Mutation Benchmark ===" in captured.out
    assert "mean=" in captured.out
    assert "p95=" in captured.out


def test_bench_mutation_rejects_non_positive_iterations(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["bench", "mutation", "--iterations", "0"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "[FAIL]" in captured.out


def test_bench_index_wired_into_parser() -> None:
    # A full real `bench index` run does real ingestion/indexing work
    # (Qdrant, embeddings) -- too slow/heavyweight for a unit test. This
    # only confirms the subcommand is wired into build_parser() correctly,
    # via parse_args() rather than main(): `athena.worker` (which
    # `_cmd_bench_index` imports lazily) constructs its module-level `huey`/
    # `_config` from the environment at first import and then keeps that
    # frozen for the rest of the process, so invoking `main(["bench",
    # "index"])` here would depend on whichever test file in the full suite
    # happened to import `athena.worker` first -- exactly the reason
    # test_duplicates_requires_a_subcommand's own comment gives for not
    # exercising worker-backed commands through `main()` in this file.
    # `_cmd_bench_index`'s clean-[FAIL]-without-a-vault behavior is instead
    # covered at the `athena.worker` level by tests/test_worker.py's
    # test_run_bootstrap_without_vault_configured_raises, via that module's
    # own fresh-isolated-import fixture.
    parser = build_parser()
    args = parser.parse_args(["bench", "index"])
    assert args.func is _cmd_bench_index


def test_bench_retrieval_accepts_corpus_argument(tmp_path: Path) -> None:
    # Same reasoning as test_bench_index_wired_into_parser_fails_cleanly_
    # without_vault: a real run needs the retrieval evaluation corpus and
    # embedding model, too heavy for a unit test. This only confirms
    # argument wiring (`--corpus`) is accepted without an argparse error --
    # build_parser() itself is exercised, `main()`'s dispatch is not.
    corpus_dir = tmp_path / "some-corpus"
    parser = build_parser()
    args = parser.parse_args(["bench", "retrieval", "--corpus", str(corpus_dir)])
    assert args.func is _cmd_bench_retrieval
    assert args.corpus == str(corpus_dir)
