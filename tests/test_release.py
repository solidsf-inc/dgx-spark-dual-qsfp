from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_tool", ROOT / "scripts/release.py")
assert SPEC is not None and SPEC.loader is not None
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class PublicAuditTests(unittest.TestCase):
    def test_source_inventory_is_sorted_and_excludes_generated_files(self):
        paths = [path.relative_to(ROOT).as_posix() for path in release.collect_source_files()]
        self.assertEqual(paths, sorted(paths))
        self.assertIn("scripts/release.py", paths)
        self.assertIn("tests/test_release.py", paths)
        self.assertIn(".github/PULL_REQUEST_TEMPLATE.md", paths)
        self.assertIn("results/PROVENANCE.md", paths)
        self.assertIn("schemas/result-v3-runtime.schema.json", paths)
        self.assertIn("schemas/result-v3.schema.json", paths)
        self.assertIn("src/spark_four_hca/result_validation.py", paths)
        self.assertFalse(
            any(path == ".gitea" or path.startswith(".gitea/") for path in paths)
        )
        self.assertFalse(any("egg-info" in path for path in paths))

    def test_v3_release_surface_is_required_in_source_and_archives(self):
        required_v3 = {
            "schemas/result-v3-runtime.schema.json",
            "schemas/result-v3.schema.json",
            "src/spark_four_hca/result_validation.py",
        }
        self.assertLessEqual(required_v3, release.REQUIRED_SOURCE_PATHS)
        self.assertLessEqual(required_v3, release.REQUIRED_ARCHIVE_PATHS)

    def test_github_metadata_is_issue_only_and_has_no_workflow(self):
        metadata = ROOT / ".github"
        self.assertTrue((metadata / "PULL_REQUEST_TEMPLATE.md").is_file())
        self.assertFalse((metadata / "workflows").exists())
        for name in ("bug_report.yml", "reproduction.yml"):
            issue_form = (metadata / "ISSUE_TEMPLATE" / name).read_text(encoding="utf-8")
            self.assertIn("description:", issue_form)
            self.assertNotIn("\nabout:", issue_form)

    def test_local_release_tools_remain_pinned(self):
        instructions = (ROOT / "RELEASING.md").read_text(encoding="utf-8")
        for package, version in release.PINNED_BUILD_TOOLS.items():
            self.assertIn(f"{package}=={version}", instructions)

    def test_release_instructions_inspect_each_archive_and_verify_results(self):
        instructions = (ROOT / "RELEASING.md").read_text(encoding="utf-8")
        self.assertNotIn("tar -tf dist/*.tar.gz", instructions)
        self.assertIn("tar -tf dist/dgx-spark-four-hca-*-repo.tar.gz", instructions)
        self.assertIn("tar -tf dist/dgx_spark_four_hca-*.tar.gz", instructions)
        self.assertIn("python3 scripts/release.py verify-results", instructions)

    def test_gitea_metadata_tree_is_absent(self):
        legacy = ROOT / ".gitea"
        self.assertFalse(
            legacy.exists() and any(path.is_file() for path in legacy.rglob("*"))
        )

    def test_sensitive_content_is_rejected(self):
        samples = [
            "/Users" + "/alice/project/config.toml",
            "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
            "password" + "=not-a-placeholder",
            "https://operator:" + "credential@example.invalid/path",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                with self.assertRaises(release.ReleaseError):
                    release.scan_public_bytes("fixture.txt", sample.encode())

    def test_unsafe_archive_paths_are_rejected(self):
        for path in (
            "../escape",
            "/absolute",
            "a\\b",
            "a/../b",
            "a/./b",
            "a//b",
            "C:/absolute",
            "a\x00b",
        ):
            with self.subTest(path=path):
                with self.assertRaises(release.ReleaseError):
                    release.validate_archive_path(path)

    def test_runtime_smoke_environment_excludes_system_site_packages(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            destination = Path(temporary_name) / "runtime"
            with mock.patch.object(release.venv, "EnvBuilder") as builder:
                release._create_isolated_runtime(destination)
            builder.assert_called_once_with(
                with_pip=True, system_site_packages=False
            )
            builder.return_value.create.assert_called_once_with(destination)


class ChecksumTests(unittest.TestCase):
    def test_committed_result_checksums(self):
        results = release.verify_result_checksums()
        self.assertEqual(len(results), 3)

    def test_result_checksum_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            results = root / "results"
            results.mkdir()
            (results / "result.json").write_text("{}\n", encoding="utf-8")
            (results / "SHA256SUMS").write_text(
                f"{'0' * 64}  result.json\n", encoding="utf-8"
            )
            with self.assertRaises(release.ReleaseError):
                release.verify_result_checksums(root)


class CanonicalArchiveTests(unittest.TestCase):
    def test_source_archive_is_reproducible_and_normalized(self):
        with tempfile.TemporaryDirectory() as first_name:
            with tempfile.TemporaryDirectory() as second_name:
                first = release.build_source_archive(Path(first_name))
                second = release.build_source_archive(Path(second_name))
                self.assertEqual(first.read_bytes(), second.read_bytes())
                gzip_header = first.read_bytes()[:10]
                self.assertEqual(gzip_header[3], 0)
                self.assertEqual(int.from_bytes(gzip_header[4:8], "little"), 0)
                with tarfile.open(first, "r:gz") as archive:
                    members = archive.getmembers()
                names = [member.name.rstrip("/") for member in members]
                self.assertEqual(names, sorted(names))
                self.assertTrue(any("/.github/" in name for name in names))
                self.assertFalse(any("/.gitea/" in name for name in names))
                for required in release.REQUIRED_ARCHIVE_PATHS:
                    self.assertTrue(
                        any(name.endswith("/" + required) for name in names),
                        required,
                    )
                self.assertTrue(all(member.uid == member.gid == 0 for member in members))
                self.assertTrue(
                    all(member.uname == member.gname == "root" for member in members)
                )
                self.assertTrue(
                    all(member.mtime == release.DEFAULT_SOURCE_DATE_EPOCH for member in members)
                )
                launcher = next(member for member in members if member.name.endswith("/spark-four-hca"))
                self.assertEqual(launcher.mode, 0o755)

    def test_sdist_canonicalization_is_reproducible(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            temporary = Path(temporary_name)
            source = temporary / "input.tar.gz"
            payload = b"example\n"
            with source.open("wb") as raw:
                with gzip.GzipFile(filename="input.tar.gz", mode="wb", fileobj=raw) as zipped:
                    with tarfile.open(fileobj=zipped, mode="w") as archive:
                        info = tarfile.TarInfo("project-1.0/file.txt")
                        info.size = len(payload)
                        info.uid = 501
                        info.gid = 20
                        archive.addfile(info, io.BytesIO(payload))
            first = release.canonicalize_sdist(source, temporary / "first.tar.gz")
            second = release.canonicalize_sdist(source, temporary / "second.tar.gz")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(
                hashlib.sha256(first.read_bytes()).digest(),
                hashlib.sha256(second.read_bytes()).digest(),
            )
            release._verify_canonical_tar(first, release.DEFAULT_SOURCE_DATE_EPOCH)

    def test_full_candidate_comparison_covers_every_artifact(self):
        names = (
            "SHA256SUMS",
            "project-1.0-repo.tar.gz",
            "project-1.0-py3-none-any.whl",
            "project-1.0.tar.gz",
        )
        with tempfile.TemporaryDirectory() as temporary_name:
            temporary = Path(temporary_name)
            first = temporary / "first"
            second = temporary / "second"
            first.mkdir()
            second.mkdir()
            for index, name in enumerate(names):
                payload = f"artifact-{index}\n"
                (first / name).write_text(payload, encoding="utf-8")
                (second / name).write_text(payload, encoding="utf-8")
            digests = release._compare_release_candidates(first, second)
            self.assertEqual(set(digests), set(names))
            (second / names[-1]).write_text("changed\n", encoding="utf-8")
            with self.assertRaises(release.ReleaseError):
                release._compare_release_candidates(first, second)


class SmokeInstallTests(unittest.TestCase):
    def test_sdist_becomes_a_wheel_before_isolated_runtime_install(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            temporary = Path(temporary_name)
            output = temporary / "release"
            output.mkdir()
            (output / "candidate.whl").write_bytes(b"candidate-wheel")
            (output / "candidate.tar.gz").write_bytes(b"candidate-sdist")
            extracted = temporary / "extracted"
            extracted.mkdir()

            commands: list[list[str]] = []

            def fake_runtime(destination: Path) -> None:
                binaries = destination / "bin"
                binaries.mkdir(parents=True)
                (binaries / "python").touch()

            def fake_command(command, *, cwd, environment):
                rendered = list(command)
                commands.append(rendered)
                self.assertEqual(environment["PIP_NO_INDEX"], "1")
                if rendered[:4] == [
                    release.sys.executable,
                    "-m",
                    "build",
                    "--wheel",
                ]:
                    wheel_directory = Path(
                        rendered[rendered.index("--outdir") + 1]
                    )
                    (wheel_directory / "from-sdist.whl").write_bytes(
                        b"sdist-wheel"
                    )
                elif rendered[1:4] == ["-m", "pip", "install"]:
                    (Path(rendered[0]).parent / "spark-four-hca").touch()
                elif "summarize" in rendered:
                    destination = Path(rendered[rendered.index("--output") + 1])
                    destination.write_text("input SHA-256\n", encoding="utf-8")
                return "ok"

            with (
                mock.patch.object(release, "_check_build_tool_versions"),
                mock.patch.object(release, "_verify_canonical_tar"),
                mock.patch.object(
                    release, "_materialize_source_archive", return_value=extracted
                ),
                mock.patch.object(release, "_verify_wheel_legal_files"),
                mock.patch.object(
                    release, "_create_isolated_runtime", side_effect=fake_runtime
                ) as create_runtime,
                mock.patch.object(
                    release, "_run_smoke_command", side_effect=fake_command
                ),
            ):
                summaries = release._clean_install_smoke(output, ROOT)

            self.assertEqual(len(summaries), 2)
            self.assertEqual(create_runtime.call_count, 2)
            build_commands = [command for command in commands if "build" in command]
            self.assertEqual(len(build_commands), 1)
            self.assertIn("--no-isolation", build_commands[0])
            install_commands = [
                command
                for command in commands
                if command[1:4] == ["-m", "pip", "install"]
            ]
            self.assertEqual(len(install_commands), 2)
            for command in install_commands:
                self.assertIn("--no-index", command)
                self.assertIn("--no-deps", command)
                self.assertNotIn("--no-build-isolation", command)
                self.assertTrue(command[-1].endswith(".whl"))
            self.assertEqual(
                len([command for command in commands if "validate-result" in command]),
                2,
            )
            self.assertEqual(
                len([command for command in commands if "summarize" in command]),
                2,
            )


if __name__ == "__main__":
    unittest.main()
