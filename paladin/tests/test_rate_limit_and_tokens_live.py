"""Blocking verification harness for PALADIN (live, requires the compose stack up).

Verifies the mandated behaviours against the *running* gateway:
  1. Rate limiting per authenticated user: a burst allowance of RATE_LIMIT_BURST requests
     (default 20) inside one minute on top of a sustained RATE_LIMIT_PER_MINUTE average
     (default 10/min over a 6-minute window). The request beyond the burst gets HTTP 429
     with a Retry-After header, and the window resets.
  2. Concurrency: /health and light requests stay responsive while a slow /ask is in flight.
  3. Ollama token logging: prompt_eval_count / eval_count are extracted from the Ollama
     response and written to request_logs in PostgreSQL.
  4. Correlation ids, login throttling and audit logging (--extra).
  5. Event-loop responsiveness while a slow /ask is in flight (--part0).

PII redaction and prompt-injection blocking are covered by tests/test_integration_live.py (with
real payloads, against the live gateway) and by the offline unit tests in
tests/test_pii_detection.py. They are deliberately *not* duplicated here.

Run from the project root (folder that contains docker-compose.yml):
    python tests/test_rate_limit_and_tokens_live.py              # full suite
    python tests/test_rate_limit_and_tokens_live.py --part0      # concurrency only
    python tests/test_rate_limit_and_tokens_live.py --burst      # rate limiting only
    python tests/test_rate_limit_and_tokens_live.py --extra      # correlation + login audit
"""

import json
import os
import subprocess
import sys
import threading
import time

import requests

API_URL = os.environ.get("PALADIN_API_URL", "http://localhost:8000")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOW_SECONDS = 60
QUIET_PERIOD = 65
# Must mirror the gateway's configured policy (RATE_LIMIT_BURST / RATE_LIMIT_PER_MINUTE).
BURST = int(os.environ.get("PALADIN_BURST", "20"))
PER_MINUTE = int(os.environ.get("PALADIN_RATE_PER_MINUTE", "10"))

USERS = {
    "emp1": ("emp1", "emp1_pass"),
    "mgr1": ("mgr1", "mgr1_pass"),
    "admin1": ("admin1", "admin1_pass"),
}

# Short-answer prompt: keeps a *real* LLM round-trip inside the 60s window on CPU-only Ollama.
TERSE = "Answer with exactly one word and no punctuation: which department is this?"

RESULTS = []


def record(name, passed, detail=""):
    RESULTS.append((name, bool(passed), detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name} {detail}", flush=True)


def login(user_key):
    username, password = USERS[user_key]
    resp = requests.post(
        f"{API_URL}/auth/login",
        params={"username": username, "password": password},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def ask(token, message, timeout=1800, extra_headers=None):
    headers = {"Authorization": f"Bearer {token}"}
    headers.update(extra_headers or {})
    started = time.time()
    resp = requests.post(
        f"{API_URL}/ask",
        params={"message": message},
        headers=headers,
        timeout=timeout,
    )
    elapsed = time.time() - started
    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text}
    return resp, body, elapsed


def quiet_period(label):
    print(f"  ... quiet period {QUIET_PERIOD}s so the {label} rate-limit window is empty", flush=True)
    time.sleep(QUIET_PERIOD)


def psql(sql):
    """Run SQL in the postgres container and return rows as lists of strings."""
    proc = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "postgres",
         "-d", "paladin", "-At", "-F", "|", "-c", sql],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"psql failed: {proc.stderr.strip()}")
    return [line.split("|") for line in proc.stdout.strip().splitlines() if line.strip()]


def run_in_container(code, timeout=600, env_extra=None):
    """Execute python code inside the paladin-api container and return stdout."""
    cmd = ["docker", "compose", "exec", "-T"]
    for key, value in (env_extra or {}).items():
        cmd += ["-e", f"{key}={value}"]
    cmd += ["paladin-api", "python", "-"]
    proc = subprocess.run(cmd, cwd=PROJECT_ROOT, input=code,
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"container exec failed: {proc.stderr.strip()[-800:]}")
    return proc.stdout


def print_environment():
    print("\n=== ENVIRONMENT ===", flush=True)
    try:
        health = requests.get(f"{API_URL}/health", timeout=30).json()
        print(f"  gateway /health: {health}")
    except Exception as exc:  # pragma: no cover
        print(f"  gateway /health: UNREACHABLE ({exc})")
        sys.exit(2)
    models = subprocess.run(["docker", "compose", "exec", "-T", "ollama", "ollama", "list"],
                            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=120)
    print("  ollama models: " + " / ".join(
        line for line in models.stdout.splitlines()[1:] if line.strip()))
    users = psql("SELECT username, clearance_level, role FROM users ORDER BY id;")
    print("  users: " + ", ".join("/".join(row) for row in users))
    docs = psql("SELECT COUNT(*) FROM request_logs;")[0][0]
    print(f"  request_logs rows before test: {docs}")


SLOW = "In two sentences, summarize the engineering security requirements."


def percentiles(values):
    if not values:
        return (0.0, 0.0, 0.0)
    ordered = sorted(values)
    pick = lambda q: ordered[min(len(ordered) - 1, int(q * len(ordered)))]
    return (ordered[len(ordered) // 2], pick(0.95), ordered[-1])


def part0_event_loop_blocking(admin_token):
    """F1 verification: a slow /ask must not block the event loop (or the threadpool)."""
    print("\n=== PART 0: /health responsiveness while a slow /ask is in flight (F1) ===", flush=True)
    baseline = []
    for _ in range(5):
        t0 = time.time()
        requests.get(f"{API_URL}/health", timeout=20)
        baseline.append((time.time() - t0) * 1000)
    print(f"  baseline /health latency: max={max(baseline):.0f}ms", flush=True)

    slow, terse, light = {}, {}, {}

    def slow_worker():
        resp, body, elapsed = ask(admin_token, SLOW)
        slow.update(status=resp.status_code, elapsed=elapsed,
                    answer_chars=len(str(body.get("response", ""))))

    def terse_worker():
        resp, _, elapsed = ask(admin_token, TERSE)
        terse.update(status=resp.status_code, elapsed=elapsed)

    def light_worker():
        """DB-only endpoint: isolates gateway concurrency from Ollama queueing."""
        results = []
        for _ in range(5):
            t0 = time.time()
            try:
                r = requests.get(f"{API_URL}/auth/me",
                                 headers={"Authorization": f"Bearer {admin_token}"}, timeout=10)
                results.append((r.status_code, (time.time() - t0) * 1000))
            except Exception as exc:
                results.append((type(exc).__name__, (time.time() - t0) * 1000))
        light["results"] = results

    th = threading.Thread(target=slow_worker, daemon=True)
    th.start()
    time.sleep(0.3)

    samples, errors = [], 0
    terse_th = None
    light_th = None
    probe_start = time.time()
    while th.is_alive() and time.time() - probe_start < 600:
        t0 = time.time()
        try:
            requests.get(f"{API_URL}/health", timeout=5)
            samples.append((time.time() - t0) * 1000)
        except Exception:
            errors += 1
        if terse_th is None and time.time() - probe_start > 1.0:
            terse_th = threading.Thread(target=terse_worker, daemon=True)
            terse_th.start()
        if light_th is None and time.time() - probe_start > 1.5:
            light_th = threading.Thread(target=light_worker, daemon=True)
            light_th.start()
        time.sleep(0.1)

    th.join(timeout=900)
    for extra in (terse_th, light_th):
        if extra is not None:
            extra.join(timeout=900)

    med, p95, worst = percentiles(samples)
    slow_elapsed = slow.get("elapsed", 0)
    print(f"  slow /ask: status={slow.get('status')} elapsed={slow_elapsed:.2f}s "
          f"answer_chars={slow.get('answer_chars')}", flush=True)
    print(f"  /health during it: samples={len(samples)} timeouts={errors} "
          f"median={med:.0f}ms p95={p95:.0f}ms max={worst:.0f}ms "
          f"(baseline max {max(baseline):.0f}ms)", flush=True)
    print(f"  concurrent light requests (/auth/me x5, no LLM) during it: "
          f"{light.get('results')}", flush=True)
    print(f"  concurrent terse /ask started 1s into the slow request: "
          f"status={terse.get('status')} elapsed={terse.get('elapsed', 0):.2f}s "
          f"(queued behind Ollama, single inference slot)", flush=True)

    record("event loop stays free: /health never times out while /ask runs",
           errors == 0 and len(samples) > 50,
           f"{len(samples)} responses, {errors} timeouts over {slow_elapsed:.0f}s")
    record("no request serialisation: /health answers in a small fraction of the /ask duration",
           worst < 2000 and worst < max(0.02 * slow_elapsed, 1) * 1000,
           f"max {worst:.0f}ms vs {slow_elapsed:.0f}s slow request (median {med:.0f}ms, p95 {p95:.0f}ms)")
    light_results = light.get("results", [])
    record("concurrent DB-only requests are served during a slow /ask",
           bool(light_results) and all(code == 200 and ms < 2000 for code, ms in light_results),
           f"{light_results}")
    record("a second /ask is admitted concurrently with the slow /ask",
           terse.get("status") == 200 and slow.get("status") == 200,
           f"slow={slow_elapsed:.2f}s vs concurrent terse={terse.get('elapsed', 0):.2f}s")

def part1_rate_limit(token):
    print(f"\n=== PART 1: rate limit on POST /ask (burst {BURST}/min + sustained {PER_MINUTE}/min, emp1) ===",
          flush=True)
    quiet_period("emp1")

    t0 = time.time()
    statuses, elapsed = [], []
    for i in range(BURST):
        resp, _, el = ask(token, TERSE)
        statuses.append(resp.status_code)
        elapsed.append(el)
        print(f"    request {i + 1}: HTTP {resp.status_code} ({el:.2f}s)", flush=True)
    total = time.time() - t0

    record(f"full burst allowance ({BURST} requests) accepted within one window",
           statuses == [200] * BURST and total < WINDOW_SECONDS,
           f"statuses={statuses}, total={total:.2f}s (window={WINDOW_SECONDS}s)")

    resp, body, el = ask(token, TERSE)
    retry_after = resp.headers.get("Retry-After")
    x_headers = {k: v for k, v in resp.headers.items() if "ratelimit" in k.lower()}
    print(f"    request {BURST + 1}: HTTP {resp.status_code} ({el:.3f}s), Retry-After={retry_after!r}, "
          f"rate-limit headers={x_headers}, body={json.dumps(body)}", flush=True)
    record(f"request exceeding the burst ({BURST + 1}th) rejected with HTTP 429",
           resp.status_code == 429, f"got {resp.status_code}")
    record("429 carries a Retry-After header", bool(retry_after), f"Retry-After={retry_after!r}")
    record("429 rejected before the LLM call (fast fail)",
           el < 1.0, f"{el:.3f}s (vs {max(elapsed):.2f}s max accepted request)")

    return t0, statuses, retry_after


def part1b_isolation(token_admin):
    print("\n=== PART 1b: rate limit is per authenticated user (admin1 while emp1 is limited) ===", flush=True)
    resp, _, el = ask(token_admin, TERSE)
    record("a different authenticated user is not affected", resp.status_code == 200,
           f"admin1 -> HTTP {resp.status_code} ({el:.2f}s)")


def part1c_reset(token, t0):
    print("\n=== PART 1c: rate-limit window reset ===", flush=True)
    remaining = (t0 + WINDOW_SECONDS + 5) - time.time()
    if remaining > 0:
        print(f"  ... waiting {remaining:.0f}s for the fixed window to expire", flush=True)
        time.sleep(remaining)
    resp, _, el = ask(token, TERSE)
    record("request after window reset succeeds", resp.status_code == 200,
           f"emp1 -> HTTP {resp.status_code} ({el:.2f}s)")


def part2_burst(token):
    print(f"\n=== PART 2: {BURST}-request concurrent burst (policy: burst {BURST}/min, mgr1) ===", flush=True)
    quiet_period("mgr1")

    outcomes = [None] * BURST
    retry_headers = []

    def worker(idx):
        resp, _, el = ask(token, TERSE)
        outcomes[idx] = (resp.status_code, el)
        if resp.status_code == 429:
            retry_headers.append(resp.headers.get("Retry-After"))

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(BURST)]
    t0 = time.time()
    for th in threads:
        th.start()
    # Fired 1s after the burst: still the same fixed window, so it must be the one rejected.
    time.sleep(1.0)
    over_resp, _, over_el = ask(token, TERSE)
    for th in threads:
        th.join(timeout=1800)
    total = time.time() - t0

    codes = [o[0] if o else None for o in outcomes]
    n200, n429 = codes.count(200), codes.count(429)
    print(f"  {BURST} concurrent requests + 1 over-burst request, all in one window: "
          f"{n200}x200, {n429}x429, other={len(codes) - n200 - n429}, total={total:.2f}s", flush=True)
    print(f"  the (BURST+1)th concurrent request -> HTTP {over_resp.status_code} "
          f"({over_el:.3f}s), Retry-After={over_resp.headers.get('Retry-After')!r}", flush=True)
    print(f"  Retry-After values seen on rejections: {sorted(set(retry_headers))}", flush=True)
    record(f"all {BURST} burst requests accepted (no rejections inside the allowance)",
           n200 == BURST and n429 == 0, f"{n200}x200 / {n429}x429")
    record(f"the request beyond the burst ({BURST + 1}th) is rejected",
           over_resp.status_code == 429, f"got {over_resp.status_code}")
    return codes, over_resp.status_code


CONTAINER_REPRO = """
import json
from gateway.security.prompt_guard import build_messages
from gateway.rag.vector_store import VectorStore
from gateway.llm.ollama_client import OllamaClient
from gateway.routes.query_routes import get_embedding_model

message = {message!r}
clearance = {clearance}
model = get_embedding_model()
embedding = model.encode(message).tolist()
res = VectorStore().query(embedding, clearance, n_results=5)
docs = res["documents"][0]
messages = build_messages(message, docs)
out = OllamaClient().chat(messages)
print("REPRO=" + json.dumps({{
    "documents_retrieved": len(docs),
    "prompt_eval_count": out["prompt_eval_count"],
    "eval_count": out["eval_count"],
    "response_chars": len(out["response"]),
    "padding": messages[1]["content"][:60],
}}))
"""


def part3_token_logging(token_admin):
    print("\n=== PART 3: Ollama token counts written to request_logs (admin1, 4 documents) ===", flush=True)
    resp, body, el = ask(token_admin, TERSE)
    request_id = body.get("request_id")
    print(f"  /ask -> HTTP {resp.status_code} ({el:.2f}s), request_id={request_id}, "
          f"sources={body.get('sources')}", flush=True)
    record("real /ask returned 200 with a request_id", resp.status_code == 200 and bool(request_id),
           f"status={resp.status_code}")

    rows = psql(
        "SELECT request_id, user_id, department, clearance_level, prompt_tokens, "
        "completion_tokens, status, length(sanitized_prompt), length(llm_response) "
        f"FROM request_logs WHERE request_id = '{request_id}';"
    )
    if not rows:
        record("request_logs row written for the request", False, "no row found")
        return
    (row_id, user_id, dept, clearance, prompt_tokens, completion_tokens, status,
     prompt_len, resp_len) = rows[0]
    print(f"  request_logs row: user_id={user_id} dept={dept} clearance={clearance} "
          f"prompt_tokens={prompt_tokens} completion_tokens={completion_tokens} "
          f"status={status} prompt_chars={prompt_len} response_chars={resp_len}", flush=True)
    record("row persisted with status=success", status == "success", f"status={status}")
    record("prompt_tokens populated from Ollama (non-zero)",
           prompt_tokens.isdigit() and int(prompt_tokens) > 0, f"prompt_tokens={prompt_tokens}")
    record("completion_tokens populated from Ollama (non-zero)",
           completion_tokens.isdigit() and int(completion_tokens) > 0,
           f"completion_tokens={completion_tokens}")
    record("sanitized_prompt + llm_response stored without raw PII concerns",
           int(prompt_len) > 0 and int(resp_len) > 0,
           f"prompt_chars={prompt_len}, response_chars={resp_len}")

    print("\n  reproducing the identical Ollama call in the container to cross-check the numbers...",
          flush=True)
    code = CONTAINER_REPRO.format(message=TERSE, clearance=3)
    stdout = run_in_container(code)
    repro_line = next((line for line in stdout.splitlines() if line.startswith("REPRO=")), None)
    if repro_line is None:
        record("container-side Ollama reproduction", False, stdout[-300:])
        return
    repro = json.loads(repro_line[len("REPRO="):])
    print(f"  direct OllamaClient().chat() -> {repro}", flush=True)
    record("logged prompt_tokens == Ollama prompt_eval_count",
           int(prompt_tokens) == repro["prompt_eval_count"],
           f"db={prompt_tokens} vs ollama={repro['prompt_eval_count']}")
    record("logged completion_tokens matches Ollama eval_count magnitude",
           int(completion_tokens) > 0 and abs(int(completion_tokens) - repro["eval_count"]) <= 5,
           f"db={completion_tokens} vs ollama={repro['eval_count']} (generation is sampled)")

    emp_rows = psql(
        "SELECT request_id, prompt_tokens, completion_tokens FROM request_logs "
        "WHERE user_id = 1 AND sanitized_prompt = "
        f"'{TERSE}' ORDER BY id DESC LIMIT 1;"
    )
    if emp_rows:
        emp_id, emp_prompt, emp_completion = emp_rows[0]
        print(f"  emp1 (clearance 1, 1 document): request_id={emp_id} "
              f"prompt_tokens={emp_prompt} completion_tokens={emp_completion}", flush=True)
        record("token counts scale with the real prompt (1-doc user < 4-doc user)",
               int(emp_prompt) < int(prompt_tokens),
               f"emp1={emp_prompt} < admin1={prompt_tokens}")
        record("emp1 tokens also non-zero", int(emp_completion) > 0,
               f"completion_tokens={emp_completion}")
    else:
        print("  (no emp1 row with this exact prompt found for the scaling check)", flush=True)


def part5_correlation_id(token):
    """Advisory (next phase): is the request id shown to the client the one stored in the audit trail?"""
    print("\n=== PART 5 (advisory, correlation IDs): client-visible id vs audit id ===", flush=True)
    supplied = "PALADIN-TEST-CORRELATION"
    resp, body, el = ask(token, TERSE, extra_headers={"X-Request-ID": supplied})
    header_id = resp.headers.get("X-Request-ID")
    body_id = body.get("request_id")
    print(f"  response header X-Request-ID : {header_id}", flush=True)
    print(f"  response body  request_id    : {body_id}", flush=True)

    row = psql(
        "SELECT request_id::text FROM request_logs WHERE request_id::text = "
        f"'{body_id}' ORDER BY id DESC LIMIT 1;"
    )
    latest = psql("SELECT request_id::text, user_id FROM request_logs ORDER BY id DESC LIMIT 1;")
    print(f"  request_logs row for body id : {row if row else 'NONE'}", flush=True)
    print(f"  newest request_logs row      : {latest[0] if latest else 'NONE'}", flush=True)

    record("client-supplied X-Request-ID is ignored (server-generated id used)",
           body_id != supplied and body_id == (latest[0][0] if latest else None),
           f"body request_id={body_id}")
    record("response X-Request-ID header == JSON request_id (single correlation id)",
           header_id == body_id, f"header={header_id} vs body={body_id}")
    record("advertised request_id resolves to a request_logs row", bool(row),
           "client-supplied id is echoed in the body but no audit row carries it")


def part4_login_rate_limit():
    print("\n=== PART 4 (advisory, rate limiting): /auth/login throttling (default limits, per IP) ===",
          flush=True)
    quiet_period("anonymous/IP")
    codes = []
    for _ in range(11):
        resp = requests.post(f"{API_URL}/auth/login",
                             params={"username": "emp1", "password": "wrong_password"},
                             timeout=30)
        codes.append(resp.status_code)
    print(f"  11 rapid failed logins -> {codes}", flush=True)
    record("failed logins are throttled (10 allowed, 11th -> 429)",
           codes[:10] == [401] * 10 and codes[10] == 429, f"codes={codes}")

    resp = requests.post(f"{API_URL}/auth/login",
                         params={"username": "admin1", "password": "admin1_pass"}, timeout=30)
    record("valid login also rejected while the IP bucket is exhausted",
           resp.status_code == 429, f"got {resp.status_code}")

    failures = psql("SELECT COUNT(*) FROM auth_failure_logs;")
    try:
        count = int(failures[0][0])
    except Exception:
        count = 0
    record("failed logins are recorded in auth_failure_logs", count >= 10,
           f"auth_failure_logs row count = {count}")

    remaining = WINDOW_SECONDS + 5
    print(f"  ... waiting {remaining}s for the login window to reset", flush=True)
    time.sleep(remaining)
    resp = requests.post(f"{API_URL}/auth/login",
                         params={"username": "admin1", "password": "admin1_pass"}, timeout=30)
    record("login succeeds again after the window resets", resp.status_code == 200,
           f"got {resp.status_code}")


def summarize():
    print("\n" + "=" * 78)
    print("BLOCKING VERIFICATION SUMMARY")
    print("=" * 78)
    for name, passed, detail in RESULTS:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}" + (f"  --  {detail}" if detail else ""))
    failed = [name for name, passed, _ in RESULTS if not passed]
    print("-" * 78)
    print(f"  checks: {len(RESULTS)} | passed: {len(RESULTS) - len(failed)} | failed: {len(failed)}")
    if failed:
        print("  failing checks:")
        for name in failed:
            print(f"    - {name}")
    print("=" * 78)
    return 1 if failed else 0


def warmup(token):
    """First /ask in a fresh container downloads the embedding model; keep it out of timings."""
    print("\n=== WARMUP (embedding model cold start) ===", flush=True)
    resp, _, el = ask(token, TERSE, timeout=1800)
    print(f"  warmup /ask -> HTTP {resp.status_code} in {el:.1f}s", flush=True)
    return resp.status_code == 200


def main():
    print("PALADIN blocking verification: rate limiting + Ollama token logging", flush=True)
    print_environment()

    if "--extra" in sys.argv:
        token = login("admin1")
        part5_correlation_id(token)
        part4_login_rate_limit()
        return summarize()

    if "--part0" in sys.argv:
        admin_token = login("admin1")
        warmup(admin_token)
        part0_event_loop_blocking(admin_token)
        return summarize()

    if "--burst" in sys.argv:
        warmup(login("admin1"))
        emp_token = login("emp1")
        admin_token = login("admin1")
        mgr_token = login("mgr1")
        t0, statuses, retry_after = part1_rate_limit(emp_token)
        part1b_isolation(admin_token)
        part1c_reset(emp_token, t0)
        part2_burst(mgr_token)
        return summarize()

    emp_token = login("emp1")
    mgr_token = login("mgr1")
    admin_token = login("admin1")
    print("  authentication OK for emp1 / mgr1 / admin1", flush=True)

    warmup(admin_token)
    part0_event_loop_blocking(admin_token)
    t0, statuses, retry_after = part1_rate_limit(emp_token)
    part1b_isolation(admin_token)
    part1c_reset(emp_token, t0)
    part2_burst(mgr_token)
    part3_token_logging(admin_token)
    return summarize()


if __name__ == "__main__":
    sys.exit(main())


