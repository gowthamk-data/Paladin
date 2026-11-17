import requests
import time
import os
from live_credentials import password_for

API_URL = os.environ.get("API_URL", "http://localhost:8000")
BURST_LIMIT = int(os.environ.get("RATE_LIMIT_BURST", 20))
SUSTAINED_LIMIT = int(os.environ.get("RATE_LIMIT_PER_MINUTE", 10))

# Step 0: Authenticate
print("=== Step 0: Authenticate as emp1 ===")
resp = requests.post(
    f"{API_URL}/auth/login",
    params={"username": "emp1", "password": password_for("emp1")},
)
if resp.status_code != 200:
    print(f"Login failed: {resp.status_code} {resp.text}")
    exit(1)
token = resp.json()["access_token"]
print(f"Token received: {token[:20]}...")

# Step 1: Send burst requests (BURST_LIMIT=20)
print(f"\n=== Step 1: Send {BURST_LIMIT} consecutive burst requests ===")
results_200 = 0
results_429 = 0
for i in range(BURST_LIMIT):
    resp = requests.post(
        f"{API_URL}/ask",
        params={"message": "test"},
        headers={"Authorization": f"Bearer {token}"},
    )
    status = resp.status_code
    if status == 200:
        results_200 += 1
    elif status == 429:
        results_429 += 1
    print(f"  Request {i+1}: {status}")

print(f"\nResults: 200 OK={results_200}, 429={results_429}")

# Step 2: Send request beyond burst limit (BURST_LIMIT + 1) - should be 429
print(f"\n=== Step 2: Send request {BURST_LIMIT + 1} (should be 429) ===")
resp = requests.post(
    f"{API_URL}/ask",
    params={"message": "test"},
    headers={"Authorization": f"Bearer {token}"},
)
status_over = resp.status_code
retry_after = resp.headers.get("Retry-After", "N/A")
print(f"  Request {BURST_LIMIT + 1}: {status_over}")
print(f"  Retry-After header: {retry_after}")

# Step 3: Wait for rate limit window reset (65 seconds)
print("\n=== Step 3: Wait 65 seconds for rate limit window reset ===")
print("  Waiting...")
time.sleep(65)
print("  Window should be reset. Sending test request...")

resp = requests.post(
    f"{API_URL}/ask",
    params={"message": "test"},
    headers={"Authorization": f"Bearer {token}"},
)
status_after_wait = resp.status_code
print(f"  Request after wait: {status_after_wait}")

# Summary
print("\n" + "=" * 60)
print("RATE LIMITING TEST SUMMARY")
print("=" * 60)

test1_pass = results_200 == BURST_LIMIT
test2_pass = status_over == 429
test3_pass = status_after_wait == 200

print(f"  Test 1 ({BURST_LIMIT} burst requests return 200): {'PASS' if test1_pass else 'FAIL'} ({results_200}/{BURST_LIMIT})")
print(f"  Test 2 (Request {BURST_LIMIT + 1} returns 429): {'PASS' if test2_pass else 'FAIL'} (got {status_over})")
print(f"  Test 3 (Request after wait succeeds): {'PASS' if test3_pass else 'FAIL'} (got {status_after_wait})")

if test1_pass and test2_pass and test3_pass:
    print("\n  ALL RATE LIMITING TESTS PASSED")
else:
    print("\n  SOME TESTS FAILED")
