"""Run the full ABAC unit-test suite in a disposable, offline container.

From the ``paladin/`` directory, run:

    python scripts/run_isolated_abac_tests.py

The suite mixes two test frameworks, each executed by its own runner:

* pytest-style files (``test_abac_policy``, ``test_abac_vector_store``,
  ``test_ingest_manifest``, ``test_seed_users``) use ``pytest.mark.parametrize``, ``pytest.raises``
  and the ``tmp_path`` fixture, so they are executed with pytest.
* unittest-style files (``test_abac_secondary_denial``,
  ``test_ask_http_503_live``) are ``unittest.TestCase`` suites executed with
  ``python -m unittest``.

pytest is deliberately NOT part of the production image and is never baked
into it: the image is built unchanged from ``gateway/Dockerfile`` under a
unique temporary tag. Instead the runner downloads the pure-python pytest
wheels on the HOST into a temporary wheelhouse, mounts it read-only into the
disposable container, and installs pytest from those wheels with
``pip --no-index`` into a tmpfs. The container runs with ``--network none``,
so it cannot reach any package index or service at any point.

Isolation guarantees:
  * unique temporary image tag, force-removed afterward (best effort)
  * no ``docker compose`` commands are used; no running service is rebuilt,
    restarted or replaced
  * the host ``tests/`` directory and the wheelhouse are mounted read-only
  * the container is network-disabled; no persistent volumes are mounted
  * no real JWT keys: the key paths point at ``/dev/null`` and the suites
    override authentication entirely
  * the disposable container removes itself (``--rm``) and the temporary
    wheelhouse is deleted in the runner's cleanup block

False-positive guard: pytest exit code 5 ("no tests collected") and a
"Ran 0 tests" unittest summary are reported as failures, never as success.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = PROJECT_ROOT / "tests"
# pytest-style suites: parametrize / pytest.raises / tmp_path fixtures.
PYTEST_MODULES = (
    "tests/test_abac_policy.py",
    "tests/test_abac_vector_store.py",
    "tests/test_ingest_manifest.py",
    "tests/test_seed_users.py",
)
# unittest-style suites: TestCase classes with skipUnless guards.
UNITTEST_MODULES = (
    "tests.test_abac_secondary_denial",
    "tests.test_ask_http_503_live",
)

# The suites override authentication and never sign or decode tokens.
# /dev/null satisfies jwt_utils' import-time file reads without mounting
# Paladin's real signing keys into the test container.
ENV = {
    "DATABASE_URL": "sqlite://",
    "JWT_PRIVATE_KEY_PATH": "/dev/null",
    "JWT_PUBLIC_KEY_PATH": "/dev/null",
}

PYTEST_NO_TESTS_COLLECTED_EXIT = 5


def run(command: list[str]) -> int:
    """Run a Docker CLI command from the project root and return its exit code."""
    print("+", subprocess.list2cmdline(command), flush=True)
    return subprocess.run(command, cwd=PROJECT_ROOT, check=False).returncode


def run_capture(command: list[str]) -> tuple[int, str]:
    """Run a command, print its combined output, and return (exit_code, output)."""
    print("+", subprocess.list2cmdline(command), flush=True)
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    print(completed.stdout, end="", flush=True)
    return completed.returncode, completed.stdout


def download_pytest_wheels(destination: Path) -> bool:
    """Download pytest and its dependencies as wheels on the HOST.

    The disposable container is network-disabled; this read-only wheelhouse is
    the only route by which pytest reaches it, so the production image never
    needs a pytest dependency and the container never needs an index.
    """
    return (
        run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--only-binary",
                ":all:",
                "--dest",
                str(destination),
                "pytest",
            ]
        )
        == 0
    )


def container_run_command(image_tag: str, mounts: list[str], inner: str) -> list[str]:
    """Assemble a docker run for the disposable, fully isolated test container."""
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        # The only writable paths: pytest's tmp_path fixture and the runtime
        # --target install of pytest from the mounted wheelhouse.
        "--tmpfs",
        "/tmp:rw,exec,size=256m",
        "--no-healthcheck",
    ]
    for mount in mounts:
        command += ["--mount", mount]
    for key, value in ENV.items():
        command += ["--env", f"{key}={value}"]
    command += ["--env", "PYTHONPATH=/app", image_tag, "sh", "-c", inner]
    return command


def main() -> int:
    image_tag = f"paladin-api-abac-test:{uuid.uuid4().hex[:12]}"
    wheelhouse = Path(tempfile.mkdtemp(prefix="paladin-pytest-wheels-"))
    build_attempted = False
    try:
        print(f"Downloading pytest wheels on the host into {wheelhouse}", flush=True)
        if not download_pytest_wheels(wheelhouse):
            print(
                "ERROR: the host-side pytest wheel download failed. Running the "
                "pytest-style suites would require adding pytest to the "
                "production image or enabling networking inside the test "
                "container; both are forbidden by the isolation constraints, "
                "so aborting instead of relaxing them.",
                file=sys.stderr,
                flush=True,
            )
            return 2

        build_attempted = True
        build_status = run(
            ["docker", "build", "--file", "gateway/Dockerfile", "--tag", image_tag, "."]
        )
        if build_status:
            return build_status

        failures = 0
        tests_mount = f"type=bind,source={TESTS_DIR.resolve()},target=/app/tests,readonly"

        # ---- Runner 1: pytest-style suites ------------------------------------
        pytest_inner = (
            "/opt/venv/bin/python -m pip install --no-index --no-cache-dir "
            "--no-compile --find-links /mnt/wheels --target /tmp/pylibs -q pytest"
            " && PYTHONPATH=/app:/tmp/pylibs /opt/venv/bin/python -m pytest "
            + " ".join(PYTEST_MODULES)
            + " -v -p no:cacheprovider"
        )
        pytest_rc, pytest_output = run_capture(
            container_run_command(
                image_tag,
                mounts=[
                    tests_mount,
                    f"type=bind,source={wheelhouse},target=/mnt/wheels,readonly",
                ],
                inner=pytest_inner,
            )
        )
        if pytest_rc == PYTEST_NO_TESTS_COLLECTED_EXIT:
            print(
                "FAIL: pytest collected zero tests (exit code 5); refusing to "
                "report success.",
                flush=True,
            )
            failures += 1
        elif pytest_rc:
            print(f"FAIL: the pytest runner exited with {pytest_rc}.", flush=True)
            failures += 1
        else:
            collected = re.search(r"collected (\d+) item", pytest_output)
            if not collected or int(collected.group(1)) == 0:
                print(
                    "FAIL: pytest reported zero collected items despite exit "
                    "code 0; refusing to report success.",
                    flush=True,
                )
                failures += 1

        # ---- Runner 2: unittest-style suites ----------------------------------
        unittest_inner = " ".join(
            ["/opt/venv/bin/python", "-m", "unittest", *UNITTEST_MODULES, "-v"]
        )
        unittest_rc, unittest_output = run_capture(
            container_run_command(image_tag, mounts=[tests_mount], inner=unittest_inner)
        )
        ran = re.search(r"Ran (\d+) tests?", unittest_output)
        if not ran or int(ran.group(1)) == 0:
            print(
                "FAIL: unittest collected/ran zero tests; refusing to report "
                "success.",
                flush=True,
            )
            failures += 1
        elif unittest_rc:
            print(f"FAIL: the unittest runner exited with {unittest_rc}.", flush=True)
            failures += 1

        if failures:
            return 1
        print("All isolated ABAC suites passed.", flush=True)
        return 0
    except FileNotFoundError as exc:
        print(f"Required executable not found: {exc.filename}", file=sys.stderr)
        return 127
    finally:
        # The unique tag and the wheelhouse are owned by this invocation.
        # Cleanup is best-effort and cannot hide a build or test failure.
        shutil.rmtree(wheelhouse, ignore_errors=True)
        if build_attempted:
            try:
                cleanup = subprocess.run(
                    ["docker", "image", "rm", "--force", image_tag],
                    cwd=PROJECT_ROOT,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if cleanup.returncode == 0:
                    print(f"Removed temporary image {image_tag}", flush=True)
            except OSError:
                # For example, Docker was not installed. Preserve the original
                # build/test error rather than replacing it with cleanup noise.
                pass


if __name__ == "__main__":
    raise SystemExit(main())
