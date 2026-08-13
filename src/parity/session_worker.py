"""Private persistent isolated-execution worker.

The parent sends only opaque call-directory names over stdin.  Single frames and
ordered/named frame bundles continue to travel through private Arrow files;
outputs use private Arrow/JSON files, and stdout/stderr are never part of the
protocol.  A protocol failure terminates the worker so the parent can fail the
session closed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from parity.worker import run_request

_CALL_TOKEN = re.compile(r"^call-[0-9]{8}-[0-9a-f]{32}$")


def run_session(root: Path) -> None:
    """Process serialized calls beneath one parent-created private directory."""

    root = root.resolve(strict=True)
    for raw_token in sys.stdin.buffer:
        token = raw_token.rstrip(b"\r\n").decode("ascii")
        if not _CALL_TOKEN.fullmatch(token):
            raise ValueError("invalid worker-session call token")
        call_root = root / token
        # The fixed filenames and validated opaque token keep all protocol I/O
        # beneath the session root.  User values never appear in the pipe.
        run_request(call_root / "request.json", call_root / "response.json")


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        return 2
    try:
        run_session(Path(arguments[0]))
    except BaseException:
        # Exceptions may include paths, source, or input data.  Do not print
        # them; the parent reports a data-safe WorkerSessionError instead.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
