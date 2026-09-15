"""Vault path safety boundary.

See `docs/design/vault-safety-boundary.md` §3.1/§5 for the full design this
module implements. This module answers exactly one question: "is this
filesystem path safe to touch, given the operation the caller intends?" It
performs no business/authorization logic (see the design's §1 scope note).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

__all__ = [
    "VaultRoot",
    "PathMode",
    "SafeVaultPath",
    "VaultPathError",
    "VaultRootConfigError",
    "PathEscapesVaultError",
    "SymlinkNotAllowedError",
    "PathNotFoundError",
    "InvalidPathError",
    "resolve_vault_path",
    "resolve_vault_pathspec",
]


class VaultPathError(Exception):
    """Base class for every vault path-safety error this module raises."""


class VaultRootConfigError(VaultPathError):
    """Raised by `VaultRoot.initialize` when the configured root is invalid."""


class PathEscapesVaultError(VaultPathError):
    """Raised when a resolved path is not a descendant of the vault root."""


class SymlinkNotAllowedError(VaultPathError):
    """Raised when a symlink is encountered anywhere along a candidate path.

    Distinct from `PathEscapesVaultError` so logs/tests can distinguish
    "an attacker is trying to climb out" from "someone placed an in-vault
    symlink we refuse to follow" (design §5).
    """


class PathNotFoundError(VaultPathError):
    """Raised when `mode=EXISTING` (or a MAYBE_EXISTING strict resolve) finds
    nothing at the target path."""


class InvalidPathError(VaultPathError):
    """Raised for structurally invalid input: embedded NUL, empty string, or
    a path equal to the vault root itself."""


@dataclass(frozen=True)
class VaultRoot:
    """The vault root, canonicalized exactly once at process startup.

    Immutable. Pass this instance by dependency injection into every
    consumer — never re-read from global config mid-run (design §2).
    """

    path: Path

    @classmethod
    def initialize(cls, configured_path: str | Path) -> VaultRoot:
        """Canonicalize `configured_path` into a `VaultRoot`.

        Raises `VaultRootConfigError` if the configured path does not exist,
        is not a directory, or is itself a symlink (a symlinked vault root
        is refused for the same reason in-vault symlinks are refused —
        design §5).
        """
        raw = Path(configured_path)
        if raw.is_symlink():
            raise VaultRootConfigError(
                f"configured vault root must not itself be a symlink: {raw}"
            )
        try:
            resolved = raw.resolve(strict=True)
        except OSError as exc:
            raise VaultRootConfigError(
                f"configured vault root does not exist: {raw}"
            ) from exc
        if not resolved.is_dir():
            raise VaultRootConfigError(
                f"configured vault root is not a directory: {resolved}"
            )
        return cls(path=resolved)


class PathMode(Enum):
    """The existence semantics the caller expects for a given operation."""

    EXISTING = "existing"
    """note_read, note_update, note_delete, move-source: target must exist."""

    CREATE = "create"
    """note_create, move-destination when absent: target must NOT be
    resolved via strict filesystem resolution, since it legitimately does
    not exist yet."""

    MAYBE_EXISTING = "maybe_existing"
    """git pathspecs, reconciliation enumeration, move-destination in the
    general case: existence is unknown and irrelevant to safety."""


# Only this module may construct a `SafeVaultPath`. `_CREATION_TOKEN` is a
# module-private sentinel (not exported in `__all__`); `SafeVaultPath`'s
# generated `__init__` refuses to build an instance unless it receives this
# exact object, which code outside this module has no legitimate way to
# obtain. This is the "type-level friction" mechanism design §3.3 #1 calls
# for: a caller that tries to construct one directly gets a `TypeError`, not
# a runtime surprise. It is intentionally not airtight against a determined
# importer of the private name — see the module-3.3 discussion — but it does
# make bypassing the validator a visible, deliberate act rather than an
# accident.
class _CreationToken:
    __slots__ = ()


_CREATION_TOKEN = _CreationToken()


@dataclass(frozen=True)
class SafeVaultPath:
    """Opaque wrapper around a path verified safe by `resolve_vault_path`.

    Constructible only by this module — see the `_CreationToken` mechanism
    just above. Business-logic functions that touch the filesystem should
    declare their path parameter as `SafeVaultPath`, never `str`/`Path`.
    """

    path: Path
    vault_root: VaultRoot
    _token: _CreationToken = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _CREATION_TOKEN:
            raise TypeError(
                "SafeVaultPath cannot be constructed directly; "
                "obtain one from resolve_vault_path()/resolve_vault_pathspec()."
            )


def _make_safe_vault_path(path: Path, vault_root: VaultRoot) -> SafeVaultPath:
    return SafeVaultPath(path=path, vault_root=vault_root, _token=_CREATION_TOKEN)


def _check_no_symlinks_in_chain(candidate: Path, vault_root: VaultRoot) -> None:
    """Reject if any component from `vault_root` down to `candidate` is a
    symlink — whether or not that symlink would ultimately escape the vault.

    This must walk component-by-component using the literal (not
    symlink-resolved) path rather than relying on `Path.resolve()`: a full
    resolve() follows symlinks transparently and only reveals the *final*
    target, silently losing the fact that a symlink sat partway through the
    chain — which matters even when that symlink's target is still in-vault
    (design §5's "reject in-vault symlinks too" policy). Building the path
    one literal component at a time and calling `is_symlink()` (an `lstat`
    on the accumulated literal path) at each step reproduces exactly what
    the kernel would do for that literal path, so an earlier symlink is
    caught before we ever act on whatever it points to.

    This single pass also implements the CREATE-mode "does something already
    occupy the exact leaf as a symlink" pre-creation check (design §5): the
    leaf is simply the last component walked here.
    """
    try:
        rel_parts = candidate.relative_to(vault_root.path).parts
    except ValueError:
        return  # Lexically outside vault_root already; ancestor checks handle this.

    current = vault_root.path
    for part in rel_parts:
        current = current / part
        try:
            is_symlink = current.is_symlink()
        except OSError:
            # An inaccessible ancestor (e.g. a traversal target under
            # another user's home directory, like /root/.ssh on a
            # non-root process) cannot be vouched for here either way --
            # same reasoning as the `resolve(strict=True)` catches below.
            # Do not raise: this module's `PathMode.EXISTING`/
            # `MAYBE_EXISTING` branches' own `resolve(strict=True)` calls
            # will independently hit the same inaccessible component and
            # correctly refuse the path (as "not found" or via the
            # CREATE-mode escape check), so silently continuing here is
            # safe, not a bypass. Confirmed empirically: Python 3.12's
            # `Path.is_symlink()` raises `PermissionError` for this case,
            # while 3.13+'s `os.path.islink()`-based implementation
            # already swallows it and returns False -- this except clause
            # makes the module's actual behavior version-independent
            # rather than accidentally depending on which Python runs it.
            is_symlink = False
        if is_symlink:
            raise SymlinkNotAllowedError(
                f"symlink encountered in vault path chain: {current}"
            )


def _reject_if_root(path: Path, vault_root: VaultRoot) -> None:
    if path == vault_root.path:
        raise InvalidPathError("path resolves to the vault root itself")


def _ensure_ancestor(resolved: Path, vault_root: VaultRoot) -> None:
    _reject_if_root(resolved, vault_root)
    if vault_root.path not in resolved.parents:
        raise PathEscapesVaultError(f"path escapes vault root: {resolved}")


def _validate_raw_path(raw_path: str | os.PathLike[str]) -> str:
    try:
        raw_str = os.fspath(raw_path)
    except TypeError as exc:
        raise InvalidPathError(f"path is not a valid path-like object: {raw_path!r}") from exc
    if raw_str == "":
        raise InvalidPathError("path must not be empty")
    if "\x00" in raw_str:
        raise InvalidPathError("path must not contain an embedded NUL byte")
    return raw_str


def _build_candidate(raw_str: str, vault_root: VaultRoot) -> Path:
    # `Path.__truediv__` discards the left operand entirely when the right
    # operand is absolute, so this single join naturally covers both
    # vault-relative and attacker-supplied-absolute inputs.
    return vault_root.path / raw_str


def _resolve_create_mode(candidate: Path, vault_root: VaultRoot) -> SafeVaultPath:
    """Two-phase CREATE-mode validation (design §5).

    Phase 1 (here): walk upward to the nearest existing ancestor, strictly
    resolve and ancestor-check *that*, then lexically validate the
    not-yet-existing remainder. Phase 2 (the caller's job, NOT this
    function's): the actual file must be created with
    `os.open(path, O_CREAT | O_EXCL | O_NOFOLLOW, ...)`. That is the real
    TOCTOU closure — if something is planted at the target between this
    validation returning and the caller's write, `O_EXCL | O_NOFOLLOW` makes
    the open atomically fail rather than silently following or clobbering
    it. This function cannot provide that guarantee by itself; callers that
    skip the `O_EXCL | O_NOFOLLOW` open are reintroducing the TOCTOU gap
    this design deliberately leaves as a documented, reviewed obligation
    (design §6).
    """
    _reject_if_root(candidate, vault_root)

    try:
        rel_parts = candidate.relative_to(vault_root.path).parts
    except ValueError as exc:
        raise PathEscapesVaultError(f"path escapes vault root: {candidate}") from exc

    # A literal '..' segment is a traversal attempt, not merely malformed
    # syntax — raised as PathEscapesVaultError for consistency with the
    # EXISTING/MAYBE_EXISTING modes, where the identical attempt is caught
    # by the post-resolve ancestor check and raises the same error type.
    if any(part == ".." for part in rel_parts):
        raise PathEscapesVaultError(f"path contains a '..' traversal segment: {candidate}")
    if any(part == "" for part in rel_parts):
        raise InvalidPathError("path must not contain empty segments")

    existing_ancestor = vault_root.path
    accumulated = vault_root.path
    found_index = 0
    for idx, part in enumerate(rel_parts):
        accumulated = accumulated / part
        if accumulated.exists():
            existing_ancestor = accumulated
            found_index = idx + 1
        else:
            break
    remainder_parts = rel_parts[found_index:]

    try:
        resolved_ancestor = existing_ancestor.resolve(strict=True)
    except OSError as exc:
        raise PathNotFoundError(
            f"could not resolve existing ancestor of {candidate}"
        ) from exc

    if resolved_ancestor != vault_root.path and vault_root.path not in resolved_ancestor.parents:
        raise PathEscapesVaultError(f"path escapes vault root: {resolved_ancestor}")

    if remainder_parts:
        final_path = resolved_ancestor.joinpath(*remainder_parts)
    else:
        final_path = resolved_ancestor
    return _make_safe_vault_path(final_path, vault_root)


def resolve_vault_path(
    raw_path: str | os.PathLike[str],
    vault_root: VaultRoot,
    mode: PathMode,
) -> SafeVaultPath:
    """Validate `raw_path` against `vault_root` under the given `mode`.

    Raises:
      InvalidPathError       — embedded NUL, empty string, or path equal to
                                vault_root itself.
      SymlinkNotAllowedError — a symlink was encountered anywhere along the
                                path, whether it escapes the vault or not.
      PathEscapesVaultError  — resolved path is not a descendant of
                                vault_root (covers `../` traversal and
                                absolute-path escapes; symlink-caused
                                escapes are raised as
                                SymlinkNotAllowedError instead, per this
                                module's uniform any-symlink-is-rejected
                                policy).
      PathNotFoundError      — mode=EXISTING and the target does not exist.

    mode=CREATE and the MAYBE_EXISTING fallback return a `SafeVaultPath`
    that is validated but, for the not-yet-existing remainder, NOT
    filesystem-resolved (it cannot be). Closing the residual TOCTOU gap for
    CREATE-mode operations is the caller's responsibility: the actual create
    must use `os.open(..., os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW)`. See
    `_resolve_create_mode`'s docstring for the full rationale — do not skip
    that obligation when wiring up `note_create`/move-destination handling.
    """
    raw_str = _validate_raw_path(raw_path)
    candidate = _build_candidate(raw_str, vault_root)

    _check_no_symlinks_in_chain(candidate, vault_root)

    if mode is PathMode.EXISTING:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            # Covers FileNotFoundError/NotADirectoryError (genuinely absent)
            # and PermissionError (an inaccessible ancestor, e.g. a
            # traversal target under another user's home directory) alike —
            # in every such case this module cannot vouch for the path, so
            # it is treated as "not found" rather than crashing the caller.
            raise PathNotFoundError(f"path does not exist: {raw_str}") from exc
        _ensure_ancestor(resolved, vault_root)
        return _make_safe_vault_path(resolved, vault_root)

    if mode is PathMode.CREATE:
        return _resolve_create_mode(candidate, vault_root)

    if mode is PathMode.MAYBE_EXISTING:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return _resolve_create_mode(candidate, vault_root)
        _ensure_ancestor(resolved, vault_root)
        return _make_safe_vault_path(resolved, vault_root)

    raise AssertionError(f"unhandled PathMode: {mode!r}")  # pragma: no cover


def resolve_vault_pathspec(raw_pathspec: str, vault_root: VaultRoot) -> str:
    """Resolve `raw_pathspec` and return it as a vault-relative POSIX string.

    Thin wrapper for the Git Automation Module (ADR-0005): calls
    `resolve_vault_path(..., mode=MAYBE_EXISTING)` and returns the
    vault-relative POSIX-style string ADR-0005's argv builder expects —
    never the resolved absolute path. Raises the same exceptions as
    `resolve_vault_path`.
    """
    safe_path = resolve_vault_path(raw_pathspec, vault_root, PathMode.MAYBE_EXISTING)
    return safe_path.path.relative_to(vault_root.path).as_posix()
