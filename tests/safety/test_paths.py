"""Tests for `athena.safety.paths` — the vault path safety boundary.

Real filesystem fixtures (via pytest's `tmp_path`) are used throughout,
including real symlinks, per `docs/design/vault-safety-boundary.md` §7:
these are real filesystem safety checks and must be tested against a real
filesystem, not a mocked one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athena.safety.paths import (
    InvalidPathError,
    PathEscapesVaultError,
    PathMode,
    PathNotFoundError,
    SafeVaultPath,
    SymlinkNotAllowedError,
    VaultRoot,
    VaultRootConfigError,
    resolve_vault_path,
    resolve_vault_pathspec,
)


@pytest.fixture
def vault_root(tmp_path: Path) -> VaultRoot:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    return VaultRoot.initialize(vault_dir)


class TestVaultRootInitialize:
    def test_initializes_on_real_directory(self, tmp_path: Path) -> None:
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        root = VaultRoot.initialize(vault_dir)
        assert root.path == vault_dir.resolve()

    def test_rejects_nonexistent_path(self, tmp_path: Path) -> None:
        with pytest.raises(VaultRootConfigError):
            VaultRoot.initialize(tmp_path / "does-not-exist")

    def test_rejects_file_as_root(self, tmp_path: Path) -> None:
        a_file = tmp_path / "not-a-dir.txt"
        a_file.write_text("hello")
        with pytest.raises(VaultRootConfigError):
            VaultRoot.initialize(a_file)

    def test_rejects_symlinked_root(self, tmp_path: Path) -> None:
        real_dir = tmp_path / "real-vault"
        real_dir.mkdir()
        link = tmp_path / "vault-link"
        link.symlink_to(real_dir, target_is_directory=True)
        with pytest.raises(VaultRootConfigError):
            VaultRoot.initialize(link)


class TestSafeVaultPathConstruction:
    def test_cannot_be_constructed_directly(self, vault_root: VaultRoot) -> None:
        with pytest.raises(TypeError):
            SafeVaultPath(path=vault_root.path, vault_root=vault_root, _token=object())  # type: ignore[call-arg]


class TestPathTraversal:
    def test_dotdot_traversal_escapes_vault(self, vault_root: VaultRoot) -> None:
        (vault_root.path.parent / "etc-outside").mkdir()
        with pytest.raises(PathEscapesVaultError):
            resolve_vault_path("../../etc/passwd", vault_root, PathMode.MAYBE_EXISTING)

    def test_deeply_nested_dotdot_chain_escapes_vault(self, vault_root: VaultRoot) -> None:
        with pytest.raises(PathEscapesVaultError):
            resolve_vault_path(
                "../../../../../../../root/.ssh/id_rsa",
                vault_root,
                PathMode.MAYBE_EXISTING,
            )

    def test_absolute_path_outside_vault_escapes(self, vault_root: VaultRoot) -> None:
        with pytest.raises(PathEscapesVaultError):
            resolve_vault_path("/etc/passwd", vault_root, PathMode.MAYBE_EXISTING)

    def test_sibling_prefix_confusion_is_not_treated_as_inside(
        self, tmp_path: Path, vault_root: VaultRoot
    ) -> None:
        # vault_root = tmp_path/vault ; sibling = tmp_path/vault-backup.
        # A naive string-prefix check on "tmp_path/vault" would treat
        # "tmp_path/vault-backup/..." as inside the vault; the real ancestor
        # check (via Path.parents) must not.
        sibling = tmp_path / "vault-backup"
        sibling.mkdir()
        (sibling / "secret.md").write_text("secret")
        with pytest.raises(PathEscapesVaultError):
            resolve_vault_path("../vault-backup/secret.md", vault_root, PathMode.EXISTING)

    def test_embedded_nul_byte_is_invalid(self, vault_root: VaultRoot) -> None:
        with pytest.raises(InvalidPathError):
            resolve_vault_path("foo\x00bar.md", vault_root, PathMode.MAYBE_EXISTING)

    def test_empty_string_is_invalid(self, vault_root: VaultRoot) -> None:
        with pytest.raises(InvalidPathError):
            resolve_vault_path("", vault_root, PathMode.MAYBE_EXISTING)

    def test_vault_root_itself_is_rejected(self, vault_root: VaultRoot) -> None:
        with pytest.raises(InvalidPathError):
            resolve_vault_path(".", vault_root, PathMode.EXISTING)


class TestSymlinks:
    def test_symlink_escaping_vault_is_rejected(
        self, tmp_path: Path, vault_root: VaultRoot
    ) -> None:
        outside_target = tmp_path / "outside.md"
        outside_target.write_text("outside content")
        link = vault_root.path / "escape-link.md"
        link.symlink_to(outside_target)
        with pytest.raises(SymlinkNotAllowedError):
            resolve_vault_path("escape-link.md", vault_root, PathMode.EXISTING)

    def test_symlink_to_another_in_vault_location_is_rejected(
        self, vault_root: VaultRoot
    ) -> None:
        real_note = vault_root.path / "real-note.md"
        real_note.write_text("real content")
        link = vault_root.path / "link-to-real-note.md"
        link.symlink_to(real_note)
        with pytest.raises(SymlinkNotAllowedError):
            resolve_vault_path("link-to-real-note.md", vault_root, PathMode.EXISTING)

    def test_symlinked_intermediate_directory_is_rejected(
        self, tmp_path: Path, vault_root: VaultRoot
    ) -> None:
        real_dir = vault_root.path / "real-dir"
        real_dir.mkdir()
        (real_dir / "note.md").write_text("content")
        linked_dir = vault_root.path / "linked-dir"
        linked_dir.symlink_to(real_dir, target_is_directory=True)
        with pytest.raises(SymlinkNotAllowedError):
            resolve_vault_path("linked-dir/note.md", vault_root, PathMode.EXISTING)

    def test_permission_error_on_an_inaccessible_ancestor_does_not_crash(
        self, monkeypatch: pytest.MonkeyPatch, vault_root: VaultRoot
    ) -> None:
        """`Path.is_symlink()` raises `PermissionError` for an inaccessible
        ancestor component on Python <=3.12 (fixed to silently return False
        on 3.13+'s `os.path.islink()`-based implementation) -- this real,
        Python-version-dependent difference was found because this
        project's own dev environment (3.14) masked it locally for four
        phases, only surfacing in CI (pinned to 3.12) right before the v1.0
        tag. `_check_no_symlinks_in_chain` must not let this propagate as
        an unhandled crash regardless of which Python version runs it; the
        downstream `resolve(strict=True)`-based escape checks handle the
        actual rejection correctly either way.
        """

        def _raise_permission_error(self: Path) -> bool:
            raise PermissionError(13, "Permission denied", str(self))

        monkeypatch.setattr(Path, "is_symlink", _raise_permission_error)

        with pytest.raises(PathEscapesVaultError):
            resolve_vault_path(
                "../../../../../../../root/.ssh/id_rsa",
                vault_root,
                PathMode.MAYBE_EXISTING,
            )


class TestExistingMode:
    def test_existing_file_resolves_successfully(self, vault_root: VaultRoot) -> None:
        note = vault_root.path / "note.md"
        note.write_text("hello")
        safe = resolve_vault_path("note.md", vault_root, PathMode.EXISTING)
        assert isinstance(safe, SafeVaultPath)
        assert safe.path == note.resolve()

    def test_nonexistent_target_raises_not_found(self, vault_root: VaultRoot) -> None:
        with pytest.raises(PathNotFoundError):
            resolve_vault_path("does-not-exist.md", vault_root, PathMode.EXISTING)


class TestCreateMode:
    def test_not_yet_existing_parent_that_is_safe_succeeds(
        self, vault_root: VaultRoot
    ) -> None:
        safe = resolve_vault_path("newdir/newfile.md", vault_root, PathMode.CREATE)
        assert isinstance(safe, SafeVaultPath)
        assert safe.path == vault_root.path / "newdir" / "newfile.md"
        assert not safe.path.exists()

    def test_not_yet_existing_target_directly_in_root_succeeds(
        self, vault_root: VaultRoot
    ) -> None:
        safe = resolve_vault_path("new-note.md", vault_root, PathMode.CREATE)
        assert safe.path == vault_root.path / "new-note.md"

    def test_ancestor_escaping_vault_fails(self, vault_root: VaultRoot) -> None:
        with pytest.raises(PathEscapesVaultError):
            resolve_vault_path("../outside/newfile.md", vault_root, PathMode.CREATE)

    def test_symlink_squatting_on_leaf_is_rejected(
        self, tmp_path: Path, vault_root: VaultRoot
    ) -> None:
        outside_target = tmp_path / "outside.md"
        outside_target.write_text("data")
        squat_link = vault_root.path / "target.md"
        squat_link.symlink_to(outside_target)
        with pytest.raises(SymlinkNotAllowedError):
            resolve_vault_path("target.md", vault_root, PathMode.CREATE)

    def test_existing_ancestor_deep_in_tree_succeeds(self, vault_root: VaultRoot) -> None:
        existing = vault_root.path / "a" / "b"
        existing.mkdir(parents=True)
        safe = resolve_vault_path("a/b/c/d/new.md", vault_root, PathMode.CREATE)
        assert safe.path == vault_root.path / "a" / "b" / "c" / "d" / "new.md"


class TestMaybeExistingMode:
    def test_existing_target_resolves_strictly(self, vault_root: VaultRoot) -> None:
        note = vault_root.path / "note.md"
        note.write_text("hello")
        safe = resolve_vault_path("note.md", vault_root, PathMode.MAYBE_EXISTING)
        assert safe.path == note.resolve()

    def test_nonexistent_target_succeeds_lexically(self, vault_root: VaultRoot) -> None:
        safe = resolve_vault_path(
            "deleted/gone-from-worktree.md", vault_root, PathMode.MAYBE_EXISTING
        )
        assert safe.path == vault_root.path / "deleted" / "gone-from-worktree.md"

    def test_escaping_target_still_rejected_even_if_nonexistent(
        self, vault_root: VaultRoot
    ) -> None:
        with pytest.raises(PathEscapesVaultError):
            resolve_vault_path("../../gone.md", vault_root, PathMode.MAYBE_EXISTING)


class TestResolveVaultPathspec:
    def test_returns_vault_relative_posix_string_for_deleted_path(
        self, vault_root: VaultRoot
    ) -> None:
        pathspec = resolve_vault_pathspec("CHAT_GPT/deleted-note.md", vault_root)
        assert pathspec == "CHAT_GPT/deleted-note.md"

    def test_returns_vault_relative_posix_string_for_existing_path(
        self, vault_root: VaultRoot
    ) -> None:
        subdir = vault_root.path / "CHAT_GPT"
        subdir.mkdir()
        (subdir / "note.md").write_text("hi")
        pathspec = resolve_vault_pathspec("CHAT_GPT/note.md", vault_root)
        assert pathspec == "CHAT_GPT/note.md"

    def test_escaping_pathspec_raises(self, vault_root: VaultRoot) -> None:
        with pytest.raises(PathEscapesVaultError):
            resolve_vault_pathspec("../../etc/passwd", vault_root)
