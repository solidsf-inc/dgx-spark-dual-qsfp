#!/usr/bin/env python3
"""Build and verify deterministic release artifacts.

The source archive is assembled from an explicit allowlist.  It never shells
out to git to discover files, so a standalone export and a monorepo checkout
produce the same bytes.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import importlib.metadata
import io
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from typing import Iterable, Sequence
import venv
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DATE_EPOCH = 1_786_579_200  # 2026-08-13T00:00:00Z
EXECUTABLE_SOURCE_PATHS = frozenset({"spark-four-hca"})

PINNED_BUILD_TOOLS = {
    "build": "1.3.0",
    "setuptools": "80.9.0",
    "wheel": "0.45.1",
}

REQUIRED_SOURCE_PATHS = frozenset(
    {
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/ISSUE_TEMPLATE/reproduction.yml",
        ".github/PULL_REQUEST_TEMPLATE.md",
        ".gitignore",
        "CHANGELOG.md",
        "CITATION.cff",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "MANIFEST.in",
        "Makefile",
        "MAINTAINERS.md",
        "NOTICE",
        "README.md",
        "RELEASING.md",
        "REVIEW_CHECKLIST.md",
        "SECURITY.md",
        "examples/pair.toml",
        "pyproject.toml",
        "results/PROVENANCE.md",
        "results/README.md",
        "results/SHA256SUMS",
        "schemas/result-v1.schema.json",
        "schemas/result-v2.schema.json",
        "schemas/result-v3-runtime.schema.json",
        "schemas/result-v3.schema.json",
        "scripts/release.py",
        "spark-four-hca",
        "src/spark_four_hca/__init__.py",
        "src/spark_four_hca/__main__.py",
        "src/spark_four_hca/cli.py",
        "src/spark_four_hca/result_validation.py",
    }
)

REQUIRED_ARCHIVE_PATHS = frozenset(
    {
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/ISSUE_TEMPLATE/reproduction.yml",
        ".github/PULL_REQUEST_TEMPLATE.md",
        "REVIEW_CHECKLIST.md",
        "results/PROVENANCE.md",
        "results/SHA256SUMS",
        "schemas/result-v1.schema.json",
        "schemas/result-v2.schema.json",
        "schemas/result-v3-runtime.schema.json",
        "schemas/result-v3.schema.json",
        "scripts/release.py",
        "src/spark_four_hca/result_validation.py",
    }
)

_TOP_LEVEL_SOURCE_FILES = frozenset(
    {
        ".gitignore",
        "CHANGELOG.md",
        "CITATION.cff",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "MANIFEST.in",
        "Makefile",
        "MAINTAINERS.md",
        "NOTICE",
        "README.md",
        "RELEASING.md",
        "SECURITY.md",
        "pyproject.toml",
        "spark-four-hca",
    }
)

_ALLOWED_DIRECTORY_SUFFIXES = {
    ".github": frozenset({".md", ".yaml", ".yml"}),
    "docs": frozenset({".md"}),
    "examples": frozenset({".toml"}),
    "schemas": frozenset({".json"}),
    "scripts": frozenset({".py"}),
    "src/spark_four_hca": frozenset({".py", ".typed"}),
    "tests": frozenset({".py", ".txt", ".json", ".toml"}),
}

_RESULTS_ALLOWED_NAMES = frozenset({"PROVENANCE.md", "README.md", "SHA256SUMS"})
_GENERATED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
    }
)


class ReleaseError(RuntimeError):
    """An actionable release validation failure."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_posix(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ReleaseError(f"path escapes project root: {path}") from exc
    value = relative.as_posix()
    validate_archive_path(value)
    return value


def validate_archive_path(value: str) -> PurePosixPath:
    """Reject paths that could escape or be interpreted inconsistently."""

    if not value or "\\" in value or "\x00" in value:
        raise ReleaseError(f"unsafe path: {value!r}")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ReleaseError(f"unsafe path: {value!r}")
    if re.match(r"^[A-Za-z]:", value):
        raise ReleaseError(f"unsafe path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise ReleaseError(f"unsafe path: {value!r}")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ReleaseError(f"control character in path: {value!r}")
    return path


def _is_generated_path(relative: str) -> bool:
    path = PurePosixPath(relative)
    if any(
        part in _GENERATED_DIRECTORY_NAMES or part.endswith(".egg-info")
        for part in path.parts
    ):
        return True
    first = path.parts[0]
    if first.startswith("raw-logs"):
        return True
    name = path.name
    return (
        name == ".DS_Store"
        or name.endswith((".pyc", ".pyo", "-client.log", "-server.log"))
        or (name.startswith("private-") and name.endswith(".toml"))
    )


def _is_allowed_source(relative: str) -> bool:
    path = PurePosixPath(relative)
    if len(path.parts) == 1:
        return relative in _TOP_LEVEL_SOURCE_FILES or bool(
            re.fullmatch(r"[A-Z][A-Z0-9_-]*\.md", relative)
        )
    if path.parts[0] == "results":
        return path.name in _RESULTS_ALLOWED_NAMES or path.suffix == ".json"
    for directory, suffixes in _ALLOWED_DIRECTORY_SUFFIXES.items():
        directory_path = PurePosixPath(directory)
        try:
            path.relative_to(directory_path)
        except ValueError:
            continue
        return path.suffix in suffixes
    return False


def collect_source_files(root: Path = PROJECT_ROOT) -> list[Path]:
    """Return the sorted, allowlisted public source set."""

    root = root.resolve()
    if not root.is_dir():
        raise ReleaseError(f"project root is not a directory: {root}")

    included: list[tuple[str, Path]] = []
    unknown: list[str] = []
    for path in root.rglob("*"):
        relative = _relative_posix(path, root)
        if _is_generated_path(relative):
            continue
        if path.is_symlink():
            raise ReleaseError(f"symlinks are not allowed in public source: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ReleaseError(f"special file is not allowed in public source: {relative}")
        if _is_allowed_source(relative):
            included.append((relative, path))
        else:
            unknown.append(relative)

    if unknown:
        rendered = "\n  ".join(sorted(unknown))
        raise ReleaseError(f"unallowlisted source files:\n  {rendered}")

    included.sort(key=lambda item: item[0])
    found = {relative for relative, _ in included}
    missing = sorted(REQUIRED_SOURCE_PATHS - found)
    if missing:
        raise ReleaseError(f"required source files missing: {', '.join(missing)}")
    return [path for _, path in included]


def _secret_rules() -> list[tuple[str, re.Pattern[str]]]:
    private_key_marker = "-----BEGIN " + "(?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
    token_prefixes = "(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,})"
    return [
        ("private-key material", re.compile(private_key_marker)),
        ("credential token", re.compile(token_prefixes)),
        (
            "absolute user-home path",
            re.compile(r"(?<![A-Za-z0-9_])/(?:Users|home)/[A-Za-z0-9._-]+(?:/|\b)"),
        ),
        (
            "absolute root-home path",
            re.compile(r"(?<![A-Za-z0-9_])/" + r"root(?:/|\b)"),
        ),
        (
            "Windows user-home path",
            re.compile(r"(?i)(?<![A-Za-z0-9_])[A-Z]:\\Users\\[A-Za-z0-9._-]+\\"),
        ),
        (
            "credential assignment",
            re.compile(
                r"(?im)^\s*(?:password|passwd|api[_-]?key|access[_-]?token|"
                r"client[_-]?secret)\s*[:=]\s*['\"]?[^\s'\"#]{4,}"
            ),
        ),
        (
            "credential-bearing URL",
            re.compile(r"(?i)\b(?:https?|ssh)://[^\s/:@]+:[^\s/@]+@"),
        ),
    ]


def scan_public_bytes(relative: str, data: bytes) -> None:
    if b"\x00" in data:
        raise ReleaseError(f"binary/NUL content is not allowed: {relative}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseError(f"source must be UTF-8 text: {relative}") from exc
    for label, pattern in _secret_rules():
        match = pattern.search(text)
        if match:
            line = text.count("\n", 0, match.start()) + 1
            raise ReleaseError(f"{label} found in {relative}:{line}")


def _git_ignored_included_paths(root: Path, relative_paths: Sequence[str]) -> list[str]:
    """Ask git when available; an exported tree without .git is still valid."""

    try:
        repository = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError:
        return []
    if repository.returncode != 0:
        return []
    check = subprocess.run(
        [
            "git",
            "-c",
            "core.excludesFile=/dev/null",
            "check-ignore",
            "--no-index",
            "--stdin",
        ],
        cwd=root,
        input="\n".join(relative_paths) + "\n",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check.returncode not in {0, 1}:
        raise ReleaseError(f"git check-ignore failed: {check.stderr.strip()}")
    return sorted(line for line in check.stdout.splitlines() if line)


def audit_source(root: Path = PROJECT_ROOT) -> list[Path]:
    files = collect_source_files(root)
    root = root.resolve()
    relative_paths = [_relative_posix(path, root) for path in files]
    ignored = _git_ignored_included_paths(root, relative_paths)
    if ignored:
        raise ReleaseError(
            "intended public files are ignored by git: " + ", ".join(ignored)
        )
    for relative, path in zip(relative_paths, files, strict=True):
        scan_public_bytes(relative, path.read_bytes())
    return files


_CHECKSUM_LINE = re.compile(r"^([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._-]*)$")


def verify_result_checksums(root: Path = PROJECT_ROOT) -> list[str]:
    results = root / "results"
    checksum_path = results / "SHA256SUMS"
    try:
        lines = checksum_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReleaseError(f"cannot read {checksum_path}: {exc}") from exc

    recorded: dict[str, str] = {}
    for number, line in enumerate(lines, start=1):
        match = _CHECKSUM_LINE.fullmatch(line)
        if not match:
            raise ReleaseError(f"invalid results/SHA256SUMS line {number}")
        digest, filename = match.groups()
        if filename in recorded:
            raise ReleaseError(f"duplicate results checksum: {filename}")
        recorded[filename] = digest

    actual = sorted(path.name for path in results.glob("*.json") if path.is_file())
    if sorted(recorded) != actual:
        raise ReleaseError(
            "results checksum inventory mismatch: "
            f"recorded={sorted(recorded)!r}, actual={actual!r}"
        )
    for filename in actual:
        digest = _sha256(results / filename)
        if digest != recorded[filename]:
            raise ReleaseError(
                f"results checksum mismatch for {filename}: "
                f"expected {recorded[filename]}, got {digest}"
            )
    return actual


def _project_identity(root: Path) -> tuple[str, str]:
    with (root / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    name = str(project["name"])
    if "version" in project:
        version = str(project["version"])
    else:
        version_source = root / "src/spark_four_hca/_version.py"
        try:
            tree = ast.parse(version_source.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as exc:
            raise ReleaseError(f"cannot read dynamic version: {exc}") from exc
        values = [
            node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "VERSION"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ]
        if len(values) != 1:
            raise ReleaseError("dynamic VERSION must be one string literal")
        version = values[0]
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", name):
        raise ReleaseError(f"unsafe project name: {name!r}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", version):
        raise ReleaseError(f"unsafe project version: {version!r}")
    return name, version


def source_date_epoch() -> int:
    raw = os.environ.get("SOURCE_DATE_EPOCH", str(DEFAULT_SOURCE_DATE_EPOCH))
    try:
        epoch = int(raw, 10)
    except ValueError as exc:
        raise ReleaseError("SOURCE_DATE_EPOCH must be an integer") from exc
    if epoch < 315_532_800:  # ZIP/tar tooling cannot represent pre-1980 reliably.
        raise ReleaseError("SOURCE_DATE_EPOCH must be at least 1980-01-01T00:00:00Z")
    return epoch


def _normalized_tar_info(name: str, *, directory: bool, epoch: int) -> tarfile.TarInfo:
    validate_archive_path(name)
    info = tarfile.TarInfo(name)
    info.mtime = epoch
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    if directory:
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        info.size = 0
    else:
        info.type = tarfile.REGTYPE
        relative = PurePosixPath(name)
        info.mode = 0o755 if relative.name == "spark-four-hca" else 0o644
    return info


def _write_canonical_tar_gz(
    output: Path,
    *,
    directories: Iterable[str],
    files: Iterable[tuple[str, bytes]],
    epoch: int,
) -> None:
    entries: list[tuple[str, bytes | None]] = [
        (name, None) for name in set(directories)
    ]
    entries.extend(files)
    entries.sort(key=lambda item: item[0])
    names = [name for name, _ in entries]
    if len(names) != len(set(names)):
        raise ReleaseError("duplicate archive member")

    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".release-", dir=output.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as raw_stream:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=9,
                fileobj=raw_stream,
                # Equivalent to gzip -n: no original filename and no wrapper
                # timestamp. SOURCE_DATE_EPOCH still normalizes every tar entry.
                mtime=0,
            ) as gzip_stream:
                with tarfile.open(
                    fileobj=gzip_stream,
                    mode="w",
                    format=tarfile.USTAR_FORMAT,
                ) as archive:
                    for name, data in entries:
                        info = _normalized_tar_info(
                            name, directory=data is None, epoch=epoch
                        )
                        if data is None:
                            archive.addfile(info)
                        else:
                            info.size = len(data)
                            archive.addfile(info, io.BytesIO(data))
        os.chmod(temporary, 0o644)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def build_source_archive(
    output_directory: Path,
    root: Path = PROJECT_ROOT,
    *,
    epoch: int | None = None,
) -> Path:
    root = root.resolve()
    files = audit_source(root)
    verify_result_checksums(root)
    name, version = _project_identity(root)
    archive_root = f"{name}-{version}"
    validate_archive_path(archive_root)
    effective_epoch = source_date_epoch() if epoch is None else epoch

    directories = {archive_root}
    archive_files: list[tuple[str, bytes]] = []
    for path in files:
        relative = _relative_posix(path, root)
        archive_name = f"{archive_root}/{relative}"
        archive_files.append((archive_name, path.read_bytes()))
        parent = PurePosixPath(archive_name).parent
        while str(parent) != ".":
            directories.add(parent.as_posix())
            if parent.as_posix() == archive_root:
                break
            parent = parent.parent

    output = output_directory / f"{archive_root}-repo.tar.gz"
    _write_canonical_tar_gz(
        output,
        directories=directories,
        files=archive_files,
        epoch=effective_epoch,
    )
    return output


def _read_safe_tar_members(source: Path) -> tuple[set[str], list[tuple[str, bytes]]]:
    directories: set[str] = set()
    files: list[tuple[str, bytes]] = []
    roots: set[str] = set()
    seen: set[str] = set()
    try:
        with tarfile.open(source, "r:gz") as archive:
            for member in archive.getmembers():
                path = validate_archive_path(member.name.rstrip("/"))
                name = path.as_posix()
                if name in seen:
                    raise ReleaseError(f"duplicate sdist member: {name}")
                seen.add(name)
                roots.add(path.parts[0])
                if member.isdir():
                    directories.add(name)
                elif member.isfile():
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ReleaseError(f"cannot read sdist member: {name}")
                    files.append((name, stream.read()))
                else:
                    raise ReleaseError(f"unsafe sdist member type: {name}")
    except (OSError, tarfile.TarError) as exc:
        raise ReleaseError(f"cannot read sdist {source}: {exc}") from exc
    if len(roots) != 1:
        raise ReleaseError(f"sdist must have exactly one top-level directory: {roots!r}")
    if not files:
        raise ReleaseError("sdist contains no files")
    for name, _ in files:
        parent = PurePosixPath(name).parent
        while str(parent) != ".":
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories, files


def canonicalize_sdist(source: Path, output: Path, *, epoch: int | None = None) -> Path:
    directories, files = _read_safe_tar_members(source)
    _write_canonical_tar_gz(
        output,
        directories=directories,
        files=files,
        epoch=source_date_epoch() if epoch is None else epoch,
    )
    return output


def _check_build_tool_versions() -> None:
    mismatches: list[str] = []
    for package, expected in PINNED_BUILD_TOOLS.items():
        try:
            actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            actual = "not installed"
        if actual != expected:
            mismatches.append(f"{package}=={expected} (found {actual})")
    if mismatches:
        install = " ".join(f"{name}=={version}" for name, version in PINNED_BUILD_TOOLS.items())
        raise ReleaseError(
            "pinned build tooling is required: "
            + ", ".join(mismatches)
            + f"; install with: python -m pip install {install}"
        )


def _materialize_source_archive(archive_path: Path, destination: Path) -> Path:
    """Expand a validated canonical archive without using tar extraction APIs."""

    directories, files = _read_safe_tar_members(archive_path)
    roots = {PurePosixPath(name).parts[0] for name in directories}
    roots.update(PurePosixPath(name).parts[0] for name, _ in files)
    if len(roots) != 1:
        raise ReleaseError(f"source archive has multiple roots: {roots!r}")
    destination.mkdir(parents=True, exist_ok=False)
    for name in sorted(directories):
        relative = validate_archive_path(name)
        target = destination.joinpath(*relative.parts)
        target.mkdir(parents=True, exist_ok=True)
        os.chmod(target, 0o755)
    for name, data in sorted(files, key=lambda item: item[0]):
        relative = validate_archive_path(name)
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
        os.chmod(target, 0o755 if relative.name == "spark-four-hca" else 0o644)
    return destination / next(iter(roots))


def _require_empty_output_directory(output: Path) -> None:
    if output.is_symlink():
        raise ReleaseError(f"release output cannot be a symlink: {output}")
    if output.exists() and not output.is_dir():
        raise ReleaseError(f"release output is not a directory: {output}")
    if output.exists() and any(output.iterdir()):
        raise ReleaseError(
            f"release output must be empty to avoid mixing artifacts: {output}"
        )
    output.mkdir(parents=True, exist_ok=True)


def _write_release_checksums(output: Path) -> Path:
    artifacts = sorted(
        path
        for path in output.iterdir()
        if path.is_file() and path.name != "SHA256SUMS"
    )
    if not artifacts:
        raise ReleaseError("no release artifacts to checksum")
    checksum = output / "SHA256SUMS"
    contents = "".join(f"{_sha256(path)}  {path.name}\n" for path in artifacts)
    checksum.write_text(contents, encoding="utf-8", newline="\n")
    os.chmod(checksum, 0o644)
    return checksum


def _build_release_candidate(output: Path, root: Path, *, epoch: int) -> list[Path]:
    """Build one candidate from a clean expansion of the canonical export."""

    _require_empty_output_directory(output)
    with tempfile.TemporaryDirectory(prefix="spark-four-hca-build-work-") as name:
        temporary = Path(name)
        raw_dist = temporary / "raw-dist"
        raw_dist.mkdir()
        repository = build_source_archive(output, root, epoch=epoch)
        exported_root = _materialize_source_archive(repository, temporary / "source")
        environment = dict(os.environ)
        environment["SOURCE_DATE_EPOCH"] = str(epoch)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--no-isolation",
                "--outdir",
                str(raw_dist),
            ],
            cwd=exported_root,
            env=environment,
            check=True,
        )
        wheels = sorted(raw_dist.glob("*.whl"))
        sdists = sorted(raw_dist.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise ReleaseError(
                f"expected one wheel and one sdist, got {wheels!r} and {sdists!r}"
            )

        shutil.copyfile(wheels[0], output / wheels[0].name)
        os.chmod(output / wheels[0].name, 0o644)
        canonicalize_sdist(sdists[0], output / sdists[0].name, epoch=epoch)
        _write_release_checksums(output)
    return verify_release(output, epoch=epoch, root=root)


def _copy_release_candidate(source: Path, output: Path) -> None:
    _require_empty_output_directory(output)
    for artifact in sorted(source.iterdir(), key=lambda path: path.name):
        if artifact.is_symlink() or not artifact.is_file():
            raise ReleaseError(f"unexpected release output entry: {artifact.name}")
        shutil.copyfile(artifact, output / artifact.name)
        os.chmod(output / artifact.name, 0o644)


def prepare_release(output: Path, root: Path = PROJECT_ROOT) -> list[Path]:
    root = root.resolve()
    audit_source(root)
    verify_result_checksums(root)
    _check_build_tool_versions()
    _require_empty_output_directory(output)
    epoch = source_date_epoch()

    with tempfile.TemporaryDirectory(prefix="spark-four-hca-candidate-") as name:
        staged_dist = Path(name) / "release"
        _build_release_candidate(staged_dist, root, epoch=epoch)
        _copy_release_candidate(staged_dist, output)

    return verify_release(output, epoch=epoch, root=root)


def _parse_checksum_file(path: Path) -> dict[str, str]:
    recorded: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        match = _CHECKSUM_LINE.fullmatch(line)
        if not match:
            raise ReleaseError(f"invalid release SHA256SUMS line {number}")
        digest, filename = match.groups()
        if filename in recorded:
            raise ReleaseError(f"duplicate release checksum: {filename}")
        recorded[filename] = digest
    return recorded


def _verify_gzip_header(path: Path) -> None:
    header = path.read_bytes()[:10]
    if len(header) != 10 or header[:3] != b"\x1f\x8b\x08":
        raise ReleaseError(f"not a gzip stream: {path.name}")
    flags = header[3]
    if flags != 0:
        raise ReleaseError(f"gzip has non-canonical optional header fields: {path.name}")
    recorded_epoch = int.from_bytes(header[4:8], "little")
    if recorded_epoch != 0:
        raise ReleaseError(f"gzip wrapper timestamp is not zero: {path.name}")


def _verify_canonical_tar(
    path: Path,
    epoch: int,
    *,
    required_suffixes: Iterable[str] = (),
) -> None:
    _verify_gzip_header(path)
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        names = [member.name.rstrip("/") for member in members]
        if names != sorted(names) or len(names) != len(set(names)):
            raise ReleaseError(f"tar members are not sorted and unique: {path.name}")
        roots: set[str] = set()
        for member, name in zip(members, names, strict=True):
            parsed = validate_archive_path(name)
            roots.add(parsed.parts[0])
            if member.uid != 0 or member.gid != 0:
                raise ReleaseError(f"nonzero owner in {path.name}: {name}")
            if member.uname != "root" or member.gname != "root":
                raise ReleaseError(f"nonnormalized owner name in {path.name}: {name}")
            if member.mtime != epoch:
                raise ReleaseError(f"nonnormalized mtime in {path.name}: {name}")
            expected_mode = 0o755 if member.isdir() or parsed.name == "spark-four-hca" else 0o644
            if member.mode != expected_mode:
                raise ReleaseError(
                    f"mode mismatch in {path.name}: {name} is {member.mode:o}, "
                    f"expected {expected_mode:o}"
                )
            if not (member.isdir() or member.isfile()):
                raise ReleaseError(f"unsafe tar member type in {path.name}: {name}")
        if len(roots) != 1:
            raise ReleaseError(f"archive has multiple roots: {path.name}")
        for suffix in required_suffixes:
            if not any(name.endswith("/" + suffix) for name in names):
                raise ReleaseError(f"archive is missing {suffix}: {path.name}")


def _verify_wheel_legal_files(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            for name in names:
                validate_archive_path(name.rstrip("/"))
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReleaseError(f"cannot read wheel {path}: {exc}") from exc
    basenames = {PurePosixPath(name).name for name in names}
    missing = {"LICENSE", "NOTICE"} - basenames
    if missing:
        raise ReleaseError(
            f"wheel is missing legal files {sorted(missing)!r}: {path.name}"
        )


def verify_release(
    output: Path,
    *,
    epoch: int | None = None,
    root: Path = PROJECT_ROOT,
) -> list[Path]:
    effective_epoch = source_date_epoch() if epoch is None else epoch
    if not output.is_dir():
        raise ReleaseError(f"release directory does not exist: {output}")
    checksum = output / "SHA256SUMS"
    if not checksum.is_file():
        raise ReleaseError("release SHA256SUMS is missing")
    recorded = _parse_checksum_file(checksum)
    actual = sorted(
        path.name
        for path in output.iterdir()
        if path.is_file() and path.name != "SHA256SUMS"
    )
    if sorted(recorded) != actual:
        raise ReleaseError(
            f"release checksum inventory mismatch: recorded={sorted(recorded)!r}, "
            f"actual={actual!r}"
        )
    for filename in actual:
        digest = _sha256(output / filename)
        if digest != recorded[filename]:
            raise ReleaseError(f"release checksum mismatch: {filename}")

    wheels = [output / name for name in actual if name.endswith(".whl")]
    repositories = [output / name for name in actual if name.endswith("-repo.tar.gz")]
    sdists = [
        output / name
        for name in actual
        if name.endswith(".tar.gz") and not name.endswith("-repo.tar.gz")
    ]
    if len(wheels) != 1 or len(repositories) != 1 or len(sdists) != 1:
        raise ReleaseError("release must contain one wheel, one sdist, and one repo archive")
    _verify_wheel_legal_files(wheels[0])
    _verify_canonical_tar(
        repositories[0], effective_epoch, required_suffixes=REQUIRED_ARCHIVE_PATHS
    )
    _verify_canonical_tar(
        sdists[0], effective_epoch, required_suffixes=REQUIRED_ARCHIVE_PATHS
    )
    with tempfile.TemporaryDirectory(prefix="spark-four-hca-verify-source-") as name:
        expected_repository = build_source_archive(
            Path(name), root, epoch=effective_epoch
        )
        if repositories[0].read_bytes() != expected_repository.read_bytes():
            raise ReleaseError(
                "repository archive does not match the current audited source tree: "
                f"artifact={_sha256(repositories[0])}, "
                f"current={_sha256(expected_repository)}"
            )
    return [output / name for name in actual] + [checksum]


def _candidate_artifacts(directory: Path) -> list[Path]:
    artifacts: list[Path] = []
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            raise ReleaseError(f"unexpected candidate entry: {path.name}")
        artifacts.append(path)
    return sorted(artifacts, key=lambda path: path.name)


def _compare_release_candidates(first: Path, second: Path) -> dict[str, str]:
    first_files = _candidate_artifacts(first)
    second_files = _candidate_artifacts(second)
    first_names = [path.name for path in first_files]
    second_names = [path.name for path in second_files]
    if first_names != second_names:
        raise ReleaseError(
            f"release inventories differ: {first_names!r} != {second_names!r}"
        )
    digests: dict[str, str] = {}
    for first_path, second_path in zip(first_files, second_files, strict=True):
        first_digest = _sha256(first_path)
        second_digest = _sha256(second_path)
        if first_path.read_bytes() != second_path.read_bytes():
            raise ReleaseError(
                f"two clean builds differ for {first_path.name}: "
                f"{first_digest} != {second_digest}"
            )
        digests[first_path.name] = first_digest
    return digests


def _run_smoke_command(
    command: Sequence[str], *, cwd: Path, environment: dict[str, str]
) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.returncode != 0:
        raise ReleaseError(
            f"clean-install command failed ({' '.join(command)}):\n{completed.stdout}"
        )
    return completed.stdout.strip()


def _create_isolated_runtime(destination: Path) -> None:
    """Create a runtime environment that cannot inherit controller packages."""

    venv.EnvBuilder(with_pip=True, system_site_packages=False).create(destination)


def _clean_install_smoke(output: Path, root: Path) -> list[str]:
    wheels = sorted(output.glob("*.whl"))
    sdists = sorted(
        path
        for path in output.glob("*.tar.gz")
        if not path.name.endswith("-repo.tar.gz")
    )
    if len(wheels) != 1 or len(sdists) != 1:
        raise ReleaseError("clean-install smoke requires one wheel and one sdist")
    _check_build_tool_versions()

    summaries: list[str] = []
    with tempfile.TemporaryDirectory(prefix="spark-four-hca-install-") as name:
        temporary = Path(name)
        environment = dict(os.environ)
        environment.pop("PYTHONHOME", None)
        environment.pop("PYTHONPATH", None)
        environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
        environment["PIP_NO_INPUT"] = "1"
        environment["PIP_NO_INDEX"] = "1"
        environment["PYTHONNOUSERSITE"] = "1"
        example = root.resolve() / "examples/pair.toml"
        result = root.resolve() / "results/reference-interleaved-2026-08-13.json"

        # Verify and materialize the canonical sdist without tar extraction
        # APIs, then build its wheel with this controller's already-validated,
        # pinned build environment. The runtime venv never receives build
        # tooling and never installs the sdist directly.
        _verify_canonical_tar(sdists[0], source_date_epoch())
        sdist_source = _materialize_source_archive(
            sdists[0], temporary / "sdist-source"
        )
        sdist_wheel_directory = temporary / "sdist-built-wheel"
        sdist_wheel_directory.mkdir()
        _run_smoke_command(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(sdist_wheel_directory),
            ],
            cwd=sdist_source,
            environment=environment,
        )
        sdist_wheels = sorted(sdist_wheel_directory.glob("*.whl"))
        if len(sdist_wheels) != 1:
            raise ReleaseError(
                "sdist smoke build must produce exactly one wheel: "
                f"{sdist_wheels!r}"
            )
        _verify_wheel_legal_files(sdist_wheels[0])

        for label, artifact in (
            ("wheel", wheels[0]),
            ("sdist-built-wheel", sdist_wheels[0]),
        ):
            environment_root = temporary / f"{label}-env"
            _create_isolated_runtime(environment_root)
            python = environment_root / "bin/python"
            cli = environment_root / "bin/spark-four-hca"
            _run_smoke_command(
                [
                    str(python),
                    "-c",
                    "import importlib.util,sys; "
                    "sys.exit(1 if importlib.util.find_spec('spark_four_hca') else 0)",
                ],
                cwd=temporary,
                environment=environment,
            )
            install = [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-deps",
                "--no-index",
            ]
            install.append(str(artifact.resolve()))
            _run_smoke_command(install, cwd=temporary, environment=environment)
            if not cli.is_file():
                raise ReleaseError(f"installed {label} did not provide spark-four-hca")
            _run_smoke_command(
                [str(cli), "--help"], cwd=temporary, environment=environment
            )
            version = _run_smoke_command(
                [str(cli), "--version"], cwd=temporary, environment=environment
            )
            theory = _run_smoke_command(
                [str(cli), "theory"], cwd=temporary, environment=environment
            )
            validation = _run_smoke_command(
                [str(cli), "validate", "--config", str(example)],
                cwd=temporary,
                environment=environment,
            )
            semantic = _run_smoke_command(
                [str(cli), "validate-result", "--input", str(result)],
                cwd=temporary,
                environment=environment,
            )
            summary_path = temporary / f"{label}-summary.md"
            _run_smoke_command(
                [
                    str(cli),
                    "summarize",
                    "--input",
                    str(result),
                    "--output",
                    str(summary_path),
                ],
                cwd=temporary,
                environment=environment,
            )
            if not summary_path.is_file() or "input SHA-256" not in summary_path.read_text(
                encoding="utf-8"
            ):
                raise ReleaseError(f"installed {label} did not create a provenance-bound summary")
            summaries.append(
                f"{label}: {version}; {theory}; {validation}; {semantic}"
            )
    return summaries


def reproducibility_check(root: Path = PROJECT_ROOT) -> dict[str, str]:
    root = root.resolve()
    audit_source(root)
    verify_result_checksums(root)
    _check_build_tool_versions()
    epoch = source_date_epoch()
    with tempfile.TemporaryDirectory(prefix="spark-four-hca-repro-") as name:
        temporary = Path(name)
        first = temporary / "first"
        second = temporary / "second"
        _build_release_candidate(first, root, epoch=epoch)
        _build_release_candidate(second, root, epoch=epoch)
        return _compare_release_candidates(first, second)


def verify_release_workflow(
    output: Path, root: Path = PROJECT_ROOT
) -> tuple[dict[str, str], list[str]]:
    """Verify a candidate, independently rebuild it, and clean-install it."""

    root = root.resolve()
    audit_source(root)
    verify_result_checksums(root)
    _check_build_tool_versions()
    epoch = source_date_epoch()
    verify_release(output, epoch=epoch, root=root)
    with tempfile.TemporaryDirectory(prefix="spark-four-hca-repeat-") as name:
        repeated = Path(name) / "release"
        _build_release_candidate(repeated, root, epoch=epoch)
        digests = _compare_release_candidates(output, repeated)
    smoke = _clean_install_smoke(output, root)
    return digests, smoke


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("audit", help="audit the allowlisted public source tree")
    subparsers.add_parser("verify-results", help="verify committed result checksums")

    archive = subparsers.add_parser(
        "source-archive", help="build the canonical standalone repository archive"
    )
    archive.add_argument("--output-dir", type=Path, required=True)

    canonicalize = subparsers.add_parser(
        "canonicalize-sdist", help="normalize an already-built setuptools sdist"
    )
    canonicalize.add_argument("source", type=Path)
    canonicalize.add_argument("output", type=Path)

    prepare = subparsers.add_parser(
        "prepare", help="build all local review artifacts; never upload or publish"
    )
    prepare.add_argument("--output-dir", type=Path, default=Path("dist"))

    verify = subparsers.add_parser(
        "verify-release",
        help="verify, independently rebuild, compare, and clean-install artifacts",
    )
    verify.add_argument("--output-dir", type=Path, default=Path("dist"))
    subparsers.add_parser(
        "reproducibility",
        help="build the full artifact set twice and compare every byte",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "audit":
            files = audit_source()
            print(f"public audit passed: {len(files)} allowlisted source files")
        elif args.command == "verify-results":
            results = verify_result_checksums()
            print(f"result checksums passed: {len(results)} files")
        elif args.command == "source-archive":
            output = build_source_archive(args.output_dir)
            print(f"wrote {output}  sha256={_sha256(output)}")
        elif args.command == "canonicalize-sdist":
            output = canonicalize_sdist(args.source, args.output)
            print(f"wrote {output}  sha256={_sha256(output)}")
        elif args.command == "prepare":
            outputs = prepare_release(args.output_dir)
            for output in outputs:
                print(output)
        elif args.command == "verify-release":
            digests, smoke = verify_release_workflow(args.output_dir)
            print("independent full-artifact rebuild matched:")
            for name, digest in sorted(digests.items()):
                print(f"  {digest}  {name}")
            for summary in smoke:
                print(f"clean-install smoke passed: {summary}")
        elif args.command == "reproducibility":
            digests = reproducibility_check()
            print("two-build full-artifact reproducibility passed:")
            for name, digest in sorted(digests.items()):
                print(f"  {digest}  {name}")
        else:  # pragma: no cover - argparse enforces the command set.
            raise AssertionError(args.command)
    except (ReleaseError, subprocess.CalledProcessError) as exc:
        print(f"release error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
