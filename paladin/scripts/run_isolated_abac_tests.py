"""Run the full ABAC unit-test suite in a disposable, offline container.

From the ``paladin/`` directory, run:

    python scripts/run_isolated_abac_tests.py

The suite mixes two test frameworks, each executed by its own runner:

* pytest-style files (``test_abac_policy``, ``test_abac_vector_store``,
  ``test_ingest_manifest``, ``test_generation_pointer``,
  ``test_generation_cutover``, ``test_seed_users``) use ``pytest.mark.parametrize``,
  ``pytest.raises`` and the ``tmp_path`` fixture, so they are executed with
  pytest.
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

Parser contract (fail closed):
  * ``parse_test_counts`` separately counts passed / skipped / failed tests.
  * A recognised result summary is parsed exactly: "OK" summaries,
    "OK (skipped=N)", "FAILED (failures=N, errors=N, skipped=N)".
  * An absent or unrecognised summary (missing "OK"/"FAILED" line, truncated
    or malformed output) is classified as FAILED: the run produced no
    certified result, so it is never reported as success and the process
    exit status is non-zero (exit 1 for failed, exit 3 for incomplete
    verification when tests ran but the outcome was not reported).
  * Missing or unparseable summary: every test that ran counts as failed and
    the returned dict carries ``unparseable: 1``.
"""

from __future__ import annotations

import re
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = PROJECT_ROOT / "tests"
# pytest-style suites: parametrize / pytest.raises / tmp_path fixtures.
PYTEST_MODULES = (
    "tests/test_abac_policy.py",
    "tests/test_abac_vector_store.py",
    "tests/test_ingest_manifest.py",
    "tests/test_generation_pointer.py",
    "tests/test_generation_cutover.py",
    "tests/test_seed_users.py",
    "tests/test_runner_reporting.py",
    "tests/test_live_credentials.py",
)
# unittest-style suites: TestCase classes with skipUnless guards.
UNITTEST_MODULES = (
    "tests.test_abac_secondary_denial",
    "tests.test_ask_http_503_live",
    "tests.test_ask_legacy_pointer_fallback_pg",
)

# The suites override authentication and never sign or decode tokens.
# /dev/null satisfies jwt_utils' import-time file reads without mounting
# Paladin's real signing keys into the test container.
ENV = {
    "DATABASE_URL": "sqlite://",
    "JWT_PRIVATE_KEY_PATH": "/dev/null",
    "JWT_PUBLIC_KEY_PATH": "/dev/null",
}
SCRATCH_TEST_ENV = (
    "PALADIN_PG_CONCURRENCY_URL",
    "PALADIN_PG_FALLBACK_URL",
    "PALADIN_CHROMA_FALLBACK_HOST",
)

PYTEST_NO_TESTS_COLLECTED_EXIT = 5
# ---------------------------------------------------------------------------
# Fail-closed parser: shapes recognised by the parser.
# ---------------------------------------------------------------------------

# "Ran 3 tests" / "Ran 3 tests in 0.001s"
_UNITTEST_RAN_RE = re.compile(r"^Ran (\d+) tests?\b", re.MULTILINE)
# "OK" or "OK (skipped=2)"
_UNITTEST_OK_RE = re.compile(r"^OK(?: \(skipped=(\d+)\))?$", re.MULTILINE)
# "FAILED (failures=1)" / "FAILED (failures=1, errors=2)" /
# "FAILED (failures=1, errors=2, skipped=1)"
_UNITTEST_FAILED_RE = re.compile(
    r"^FAILED \(failures=(\d+)(?:, errors=(\d+))?(?:, skipped=(\d+))?\)$",
    re.MULTILINE,
)



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


def _parse_pytest_counts(output: str) -> dict[str, int]:
    """Parse a pytest terminal summary into separately counted totals.

    Recognised shapes: ``48 passed in 3.5s``, ``2 failed, 3 skipped, 45
    passed in 4.1s``, ``1 failed, 2 errors, 47 passed in 4.1s`` (errors are
    folded into failed). Any absent or unrecognised summary (truncated
    output, crash without a summary, plain text) is fail-closed: no count is
    fabricated, so the result can never certify success.
    """
    counts = {"passed": 0, "skipped": 0, "failed": 0, "unparseable": 0}
    matched = False

    for kind in ("passed", "skipped", "failed"):
        match = re.search(rf"(\d+) {kind}", output)
        if match:
            counts[kind] = int(match.group(1))
            matched = True

    # "1 failed, 2 errors, 47 passed in 4.1s" -> errors fold into failed.
    error_match = re.search(r"(\d+) error", output)
    if error_match:
        counts["failed"] += int(error_match.group(1))
        matched = True

    if not matched:
        counts["unparseable"] = 1
    return counts


def _parse_unittest_counts(output: str) -> dict[str, int]:
    """Parse a unittest summary into separately counted totals.

    Recognised shapes: ``Ran 3 tests`` + ``OK``, ``Ran 4 tests`` + ``OK
    (skipped=2)``, ``Ran 6 tests`` + ``FAILED (failures=1, errors=2,
    skipped=1)``. Anything else -- missing summary, truncated summary,
    malformed summary -- is fail-closed: the outcome is unclassified and the
    run is reported as failed (non-zero exit), never as a pass.
    """
    counts = {"passed": 0, "skipped": 0, "failed": 0, "unparseable": 0}

    ran = _UNITTEST_RAN_RE.search(output)
    if not ran:
        # Zero tests were ever reported. The verdict layer treats this as a
        # collector failure (exit 1); nothing further to parse here.
        return counts

    total = int(ran.group(1))
    ok = _UNITTEST_OK_RE.search(output)
    failed_summary = _UNITTEST_FAILED_RE.search(output)

    if ok:
        skipped = int(ok.group(1)) if ok.group(1) else 0
        counts["passed"] = max(total - skipped, 0)
        counts["skipped"] = skipped
        return counts

    if failed_summary:
        failures = int(failed_summary.group(1))
        errors = int(failed_summary.group(2)) if failed_summary.group(2) else 0
        skipped = int(failed_summary.group(3)) if failed_summary.group(3) else 0
        counts["passed"] = max(total - failures - errors - skipped, 0)
        counts["skipped"] = skipped
        counts["failed"] = failures + errors
        return counts

    # "Ran N tests" with no OK/FAILED line (missing, truncated or malformed
    # summary): every test ran but none of their outcomes were reported, so
    # the results are unclassified. Fail-closed: they count as failed and the
    # unparseable flag records why.
    counts["unparseable"] = 1
    counts["failed"] = total
    return counts


def parse_test_counts(output: str, framework: str) -> dict[str, int]:
    """Extract passed/skipped/failed counts from a runner's summary line.

    pytest's terminal summary looks like ``48 passed, 2 skipped in 3.5s``
    (or ``1 failed, 2 errors, 47 passed``); unittest's is ``Ran 3 tests``
    plus ``OK`` / ``OK (skipped=1)`` / ``FAILED (failures=1, errors=2,
    skipped=1)``. Fail closed: an absent or unrecognised result summary is
    classified as failed and flagged ``unparseable: 1`` -- an unparseable
    result is never counted as a pass, and the process exit status is
    non-zero. The parser separately counts passed, skipped and failed tests.
    """
    counts = {"passed": 0, "skipped": 0, "failed": 0, "unparseable": 0}
    if framework == "pytest":
        return _parse_pytest_counts(output)
    return _parse_unittest_counts(output)


# Per-runner verdicts. A run is a PASS only when every collected test
# executed and passed; INCOMPLETE when tests executed but some were skipped
# (a skipped suite is an unverified suite, so the run must not claim complete
# verification); FAILED for a failure/error or zero collected tests.
RUN_PASS = "pass"
RUN_INCOMPLETE = "incomplete-verification"
RUN_FAILED = "failed"


def _as_match_value(value, default=0):
    """Normalize the ``collected`` arg to an integer item count.

    ``main()`` passes a compiled regex match; unit tests may pass either the
    raw summary string or the match object.
    """
    if value is None:
        return default
    try:
        return int(value.group(1))
    except AttributeError:
        match = re.search(r"(\d+)", str(value))
        return int(match.group(1)) if match else default


def classify_pytest(counts, exit_code, collected):
    """Classify a pytest run: ``(status, message)``."""
    if exit_code == PYTEST_NO_TESTS_COLLECTED_EXIT:
        return RUN_FAILED, (
            "FAIL: pytest collected zero tests (exit code 5); refusing to "
            "report success."
        )
    if exit_code:
        return RUN_FAILED, f"FAIL: the pytest runner exited with {exit_code}."
    if not collected or _as_match_value(collected) == 0:
        return RUN_FAILED, (
            "FAIL: pytest reported zero collected items despite exit code 0; "
            "refusing to report success."
        )
    if counts["failed"]:
        return RUN_FAILED, (
            f"FAIL: {counts['failed']} pytest test(s) failed or errored."
        )
    if counts["skipped"]:
        if counts["passed"] == 0:
            return RUN_INCOMPLETE, (
                "INCOMPLETE VERIFICATION: every pytest test was skipped "
                f"({counts['skipped']} skipped, 0 executed); nothing was "
                "verified, so this run is not a pass (exit 3)."
            )
        return RUN_INCOMPLETE, (
            f"INCOMPLETE VERIFICATION: {counts['skipped']} pytest test(s) were "
            f"skipped ({counts['passed']} passed); a skipped suite is an "
            "unverified suite, so this run cannot claim complete "
            "verification (exit 3)."
        )
    if counts["passed"] == 0:
        return RUN_FAILED, (
            "FAIL: pytest reported no executed tests; refusing to report "
            "success."
        )
    return RUN_PASS, f"pytest: {counts['passed']} passed, 0 skipped, 0 failed."


def classify_unittest(counts, exit_code, ran):
    """Classify a unittest run: ``(status, message)``."""
    if not ran or _as_match_value(ran) == 0:
        return RUN_FAILED, (
            "FAIL: unittest collected/ran zero tests; refusing to report "
            "success."
        )
    if exit_code:
        return RUN_FAILED, f"FAIL: the unittest runner exited with {exit_code}."
    if counts["failed"]:
        return RUN_FAILED, (
            f"FAIL: {counts['failed']} unittest test(s) failed or errored."
        )
    if counts["skipped"]:
        if counts["passed"] == 0:
            return RUN_INCOMPLETE, (
                "INCOMPLETE VERIFICATION: every unittest test was skipped "
                f"({counts['skipped']} skipped, 0 executed); nothing was "
                "verified, so this run is not a pass (exit 3)."
            )
        return RUN_INCOMPLETE, (
            f"INCOMPLETE VERIFICATION: {counts['skipped']} unittest test(s) "
            f"were skipped ({counts['passed']} passed); a skipped suite is an "
            "unverified suite, so this run cannot claim complete "
            "verification (exit 3)."
        )
    if counts["passed"] == 0:
        return RUN_FAILED, (
            "FAIL: unittest reported no executed tests; refusing to report "
            "success."
        )
    return RUN_PASS, f"unittest: {counts['passed']} passed, 0 skipped, 0 failed."


def runner_exit_status(statuses):
    """Map per-runner verdicts to the process exit status.

    0 — every runner fully passed (zero skips, zero failures).
    1 — at least one runner failed (or collected nothing).
    3 — INCOMPLETE VERIFICATION: no failures, but at least one runner had
        skipped tests. The distinct code keeps a padded run from being
        mistaken for a pass (0) or a failure (1).
    """
    if RUN_FAILED in statuses:
        return 1
    if RUN_INCOMPLETE in statuses:
        return 3
    return 0


def container_run_command(
    image_tag: str,
    mounts: list[str],
    inner: str,
    network: str | None = None,
    passthrough_env: tuple[str, ...] = (),
) -> list[str]:
    """Assemble a docker run for the disposable, fully isolated test container."""
    network_args = ["--network", network] if network else ["--network", "none"]
    command = [
        "docker",
        "run",
        "--rm",
        *network_args,
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
    if network:
        command += [
            "--env", "CHROMADB_HOST=gate2-fallback-chroma",
            "--env", "CHROMADB_PORT=8000",
        ]
    # Bare --env KEY copies that value from this host process without putting
    # the credential-bearing PostgreSQL URL in the printed Docker command.
    for key in passthrough_env:
        command += ["--env", key]
    command += ["--env", "PYTHONPATH=/app", image_tag, "sh", "-c", inner]
    return command


def scratch_test_configuration() -> tuple[str | None, tuple[str, ...]]:
    """Validate the optional internal-only scratch network and its endpoints.

    The default runner remains network-disabled. Scratch mode accepts only an
    internal network with the expected disposable PostgreSQL and Chroma
    containers attached; service credentials are passed by variable name so
    they are not printed in runner output.
    """
    network = os.environ.get("PALADIN_ISOLATED_TEST_NETWORK")
    if not network:
        return None, ()
    if not re.fullmatch(r"paladin-abac-scratch-[a-z0-9-]+", network):
        raise ValueError("scratch network name must use paladin-abac-scratch-<id>")

    inspect = subprocess.run(
        ["docker", "network", "inspect", "--format", "{{.Internal}}", network],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if inspect.returncode or inspect.stdout.strip().lower() != "true":
        raise ValueError("scratch test network must exist and be Docker-internal")

    attached = subprocess.run(
        [
            "docker", "network", "inspect", "--format",
            "{{range .Containers}}{{.Name}} {{end}}", network,
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    names = attached.stdout.split()
    has_pg = any(name.startswith("paladin-abac-pg-") for name in names)
    has_chroma = any(name.startswith("paladin-abac-chroma-") for name in names)
    only_scratch = all(
        name.startswith(("paladin-abac-pg-", "paladin-abac-chroma-"))
        for name in names
    )
    if attached.returncode or not (has_pg and has_chroma and only_scratch):
        raise ValueError(
            "scratch network must contain only the disposable paladin-abac PostgreSQL and Chroma services"
        )

    required = (
        "PALADIN_PG_CONCURRENCY_URL",
        "PALADIN_PG_FALLBACK_URL",
        "PALADIN_CHROMA_FALLBACK_HOST",
    )
    for variable in required:
        if not os.environ.get(variable):
            raise ValueError(f"scratch mode requires {variable}")
    for variable in required[:2]:
        parsed = urlparse(os.environ[variable])
        if parsed.hostname != "gate2-fallback-pg" or parsed.path != "/paladin_test":
            raise ValueError(
                f"{variable} must target the disposable gate2-fallback-pg/paladin_test service"
            )
    if os.environ["PALADIN_CHROMA_FALLBACK_HOST"] != "gate2-fallback-chroma":
        raise ValueError(
            "PALADIN_CHROMA_FALLBACK_HOST must target the disposable gate2-fallback-chroma service"
        )
    return network, required


def main() -> int:
    image_tag = f"paladin-api-abac-test:{uuid.uuid4().hex[:12]}"
    wheelhouse: Path | None = None
    pytest_source: Path | None = None
    build_attempted = False
    try:
        network, passthrough_env = scratch_test_configuration()
        configured_source = os.environ.get("PALADIN_ISOLATED_PYTEST_SOURCE")
        if configured_source:
            pytest_source = Path(configured_source).resolve()
            if not (pytest_source / "pytest" / "__init__.py").is_file() or not (
                pytest_source / "_pytest" / "__init__.py"
            ).is_file():
                print(
                    "ERROR: PALADIN_ISOLATED_PYTEST_SOURCE must contain the pure-Python pytest and _pytest packages.",
                    file=sys.stderr,
                    flush=True,
                )
                return 2
            print("Using the mounted pure-Python pytest source for offline tests.", flush=True)
        else:
            wheelhouse = Path(tempfile.mkdtemp(prefix="paladin-pytest-wheels-"))
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
        if wheelhouse is not None:
            pytest_inner = (
                "/opt/venv/bin/python -m pip install --no-index --no-cache-dir "
                "--no-compile --find-links /mnt/wheels --target /tmp/pylibs -q pytest"
                " && PYTHONPATH=/app:/tmp/pylibs /opt/venv/bin/python -m pytest "
                + " ".join(PYTEST_MODULES)
                + " -v -p no:cacheprovider"
            )
            pytest_mount = f"type=bind,source={wheelhouse},target=/mnt/wheels,readonly"
        else:
            pytest_inner = (
                "PYTHONPATH=/app:/mnt/pytest-source /opt/venv/bin/python -m pytest "
                + " ".join(PYTEST_MODULES)
                + " -v -p no:cacheprovider"
            )
            pytest_mount = (
                f"type=bind,source={pytest_source},target=/mnt/pytest-source,readonly"
            )
        pytest_rc, pytest_output = run_capture(
            container_run_command(
                image_tag,
                mounts=[tests_mount, pytest_mount],
                inner=pytest_inner,
                network=network,
                passthrough_env=passthrough_env,
            )
        )
        pytest_counts = parse_test_counts(pytest_output, "pytest")
        pytest_collected = re.search(r"collected (\d+) item", pytest_output)
        pytest_status, pytest_message = classify_pytest(
            pytest_counts, pytest_rc, pytest_collected
        )
        print(pytest_message, flush=True)

        # ---- Runner 2: unittest-style suites ----------------------------------
        unittest_inner = " ".join(
            ["/opt/venv/bin/python", "-m", "unittest", *UNITTEST_MODULES, "-v"]
        )
        unittest_rc, unittest_output = run_capture(
            container_run_command(
                image_tag,
                mounts=[tests_mount],
                inner=unittest_inner,
                network=network,
                passthrough_env=passthrough_env,
            )
        )
        unittest_counts = parse_test_counts(unittest_output, "unittest")
        unittest_ran = re.search(r"Ran (\d+) tests?", unittest_output)
        unittest_status, unittest_message = classify_unittest(
            unittest_counts, unittest_rc, unittest_ran
        )
        print(unittest_message, flush=True)

        # Per-runner summary: a fully passed run is distinguishable from one
        # padded with skips, and skips are never folded into the pass count.
        print("Isolated ABAC suite summary:", flush=True)
        for label, counts in (
            ("pytest", pytest_counts),
            ("unittest", unittest_counts),
        ):
            print(
                f"  {label:<9} {counts['passed']} passed, "
                f"{counts['skipped']} skipped, {counts['failed']} failed",
                flush=True,
            )

        exit_status = runner_exit_status([pytest_status, unittest_status])
        if exit_status == 0:
            print("All isolated ABAC suites passed.", flush=True)
        elif exit_status == 3:
            print(
                "INCOMPLETE VERIFICATION: the suites executed but skipped "
                "tests mean complete verification was not achieved (exit 3 — "
                "distinct from a pass (0) and a failure (1)).",
                flush=True,
            )
        else:
            print(
                "FAILED: at least one runner failed or collected no tests "
                "(exit 1).",
                flush=True,
            )
        return exit_status
    except FileNotFoundError as exc:
        print(f"Required executable not found: {exc.filename}", file=sys.stderr)
        return 127
    finally:
        # The unique tag and the wheelhouse are owned by this invocation.
        # Cleanup is best-effort and cannot hide a build or test failure.
        if wheelhouse is not None:
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
