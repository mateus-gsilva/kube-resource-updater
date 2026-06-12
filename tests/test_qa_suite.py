"""Pytest entrypoint for the canonical QA suite.

The project's tests live in tools/qa_params.py — a single self-contained
script (fail-first asserts, full-run failure counter, zero test-framework
dependencies). This wrapper makes `pytest` work as a thin alias so CI and
contributors used to pytest get the same signal. See CONTRIBUTING.md
("Tests are the contract") for why the harness is custom.
"""

import pathlib
import subprocess
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_qa_suite_green() -> None:
    proc = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "tools" / "qa_params.py")],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        tail = "\n".join(proc.stdout.splitlines()[-40:])
        raise AssertionError(
            f"qa_params.py exited {proc.returncode}\n--- last 40 lines ---\n{tail}"
        )
