"""Small, deliberately constrained SSH subprocess wrapper."""

from __future__ import annotations

from dataclasses import dataclass
import os
import shlex
import subprocess
from typing import Iterable, Mapping, TextIO


DEFAULT_SSH_OPTIONS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=2",
)


@dataclass(frozen=True)
class CommandResult:
    host: str
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    def check(self) -> "CommandResult":
        if self.returncode != 0:
            rendered = shlex.join(self.argv)
            detail = self.stderr.strip() or self.stdout.strip() or "no diagnostic output"
            raise RemoteCommandError(
                f"remote command failed on {self.host} ({self.returncode}): "
                f"{rendered}\n{detail}"
            )
        return self


class RemoteCommandError(RuntimeError):
    """Raised when an SSH command fails."""


def _remote_command(argv: Iterable[str], env: Mapping[str, str] | None = None) -> str:
    args = tuple(str(item) for item in argv)
    if not args:
        raise ValueError("remote command cannot be empty")
    prefix: list[str] = []
    if env:
        for key, value in sorted(env.items()):
            if not key.replace("_", "").isalnum() or key[0].isdigit():
                raise ValueError(f"invalid environment variable name: {key!r}")
            prefix.append(f"{key}={value}")
    return shlex.join((*prefix, *args))


class SSHRunner:
    """Execute commands through preconfigured SSH aliases without a shell locally."""

    def __init__(self, options: tuple[str, ...] = DEFAULT_SSH_OPTIONS):
        self.options = options

    def command(self, host: str, argv: Iterable[str], env: Mapping[str, str] | None = None) -> list[str]:
        return ["ssh", *self.options, host, _remote_command(argv, env)]

    def run(
        self,
        host: str,
        argv: Iterable[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout: float = 30,
        check: bool = False,
    ) -> CommandResult:
        normalized = tuple(str(item) for item in argv)
        completed = subprocess.run(
            self.command(host, normalized, env),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C"},
        )
        result = CommandResult(
            host=host,
            argv=normalized,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        return result.check() if check else result

    def popen(
        self,
        host: str,
        argv: Iterable[str],
        *,
        env: Mapping[str, str] | None = None,
        stdout: int | TextIO = subprocess.PIPE,
        stderr: int | TextIO = subprocess.PIPE,
    ) -> subprocess.Popen[str]:
        return subprocess.Popen(
            self.command(host, argv, env),
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
        )
