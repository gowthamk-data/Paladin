import sys
import json
import urllib.request
import urllib.error
from jose import jwt

BASE_URL = "http://localhost:8000"

with open("C:/Users/GOWTHAM/Paladin/paladin/keys/private.pem", "r") as f:
    PRIVATE_KEY = f.read()

with open("C:/Users/GOWTHAM/Paladin/paladin/keys/public.pem", "r") as f:
    PUBLIC_KEY = f.read()

passed = 0
failed = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS - {name} {detail}")
    else:
        failed += 1
        print(f"  FAIL - {name} {detail}")

# ============ SUCCESS CASES ============

# Test 1: emp1 login
print("Test 1: emp1 login...")
emp1_token = None
try:
    req = urllib.request.Request(
        f"{BASE_URL}/auth/login?username=emp1&password=emp1_pass", method="POST",
    )
    resp = urllib.request.urlopen(req)
    data = json.loads(resp.read().decode())
    check("Token received", "access_token" in data, f"expires in {data.get('expires_in')}s")
    check("Token type bearer", data.get("token_type") == "bearer")
    emp1_token = data["access_token"]
except Exception as e:
    check("emp1 login", False, str(e))

# Test 2: mgr1 login
print("Test 2: mgr1 login...")
mgr1_token = None
try:
    req = urllib.request.Request(
        f"{BASE_URL}/auth/login?username=mgr1&password=mgr1_pass", method="POST",
    )
    resp = urllib.request.urlopen(req)
    data = json.loads(resp.read().decode())
    check("mgr1 token received", "access_token" in data)
    mgr1_token = data["access_token"]
except Exception as e:
    check("mgr1 login", False, str(e))

# Test 3: admin1 login
print("Test 3: admin1 login...")
admin1_token = None
try:
    req = urllib.request.Request(
        f"{BASE_URL}/auth/login?username=admin1&password=admin1_pass", method="POST",
    )
    resp = urllib.request.urlopen(req)
    data = json.loads(resp.read().decode())
    check("admin1 token received", "access_token" in data)
    admin1_token = data["access_token"]
except Exception as e:
    check("admin1 login", False, str(e))

# Test 4: JWT contains all required claims
print("Test 4: JWT claim validation...")
if emp1_token:
    try:
        payload = jwt.decode(emp1_token, PUBLIC_KEY, algorithms=["RS256"], audience="paladin-gateway")
        required = ["sub", "department", "clearance_level", "role", "iss", "aud", "iat", "exp", "jti"]
        missing = [c for c in required if c not in payload]
        check("All 9 required claims", len(missing) == 0, f"missing: {missing}")
        if not missing:
            print(f"    sub={payload['sub']}, dept={payload['department']}, clearance={payload['clearance_level']}, role={payload['role']}")
            print(f"    iss={payload['iss']}, aud={payload['aud']}")
    except Exception as e:
        check("JWT decode", False, str(e))
else:
    check("JWT decode", False, "no emp1 token")

# Test 5: /auth/me with valid token
print("Test 5: /auth/me with valid token...")
if emp1_token:
    try:
        req = urllib.request.Request(
            f"{BASE_URL}/auth/me",
            headers={"Authorization": f"Bearer {emp1_token}"},
        )
        resp = urllib.request.urlopen(req)
        data = json.loads(resp.read().decode())
        check("Username emp1", data.get("username") == "emp1", f"got {data.get('username')}")
        check("Clearance level 1", data.get("clearance_level") == 1, f"got {data.get('clearance_level')}")
        check("Role employee", data.get("role") == "employee")
        print(f"    user={data['username']}, clearance={data['clearance_level']}, role={data['role']}")
    except Exception as e:
        check("/auth/me", False, str(e))
else:
    check("/auth/me", False, "no emp1 token")

# ============ FAILURE CASES ============

# Test 6: Incorrect password
print("Test 6: Incorrect password...")
try:
    req = urllib.request.Request(
        f"{BASE_URL}/auth/login?username=emp1&password=wrong_pass", method="POST",
    )
    resp = urllib.request.urlopen(req)
    check("Rejects wrong password", False, f"got {resp.status}")
except urllib.error.HTTPError as e:
    check("Rejects wrong password", e.code == 401, f"got {e.code}")
except Exception as e:
    check("Rejects wrong password", False, str(e))

# Test 7: Missing token
print("Test 7: Missing token...")
try:
    req = urllib.request.Request(f"{BASE_URL}/auth/me")
    resp = urllib.request.urlopen(req)
    check("Rejects missing token", False, f"got {resp.status}")
except urllib.error.HTTPError as e:
    check("Rejects missing token", e.code == 401, f"got {e.code}")
except Exception as e:
    check("Rejects missing token", False, str(e))

# Test 8: Malformed token
print("Test 8: Malformed token...")
try:
    req = urllib.request.Request(
        f"{BASE_URL}/auth/me",
        headers={"Authorization": "Bearer not-a-valid-token"},
    )
    resp = urllib.request.urlopen(req)
    check("Rejects malformed token", False, f"got {resp.status}")
except urllib.error.HTTPError as e:
    check("Rejects malformed token", e.code == 401, f"got {e.code}")
except Exception as e:
    check("Rejects malformed token", False, str(e))

# Test 9: Tampered token
print("Test 9: Tampered token...")
if emp1_token:
    try:
        tampered = emp1_token[:-10] + "AAAAAAAAAA"
        req = urllib.request.Request(
            f"{BASE_URL}/auth/me",
            headers={"Authorization": f"Bearer {tampered}"},
        )
        resp = urllib.request.urlopen(req)
        check("Rejects tampered token", False, f"got {resp.status}")
    except urllib.error.HTTPError as e:
        check("Rejects tampered token", e.code == 401, f"got {e.code}")
    except Exception as e:
        check("Rejects tampered token", False, str(e))
else:
    check("Rejects tampered token", False, "no emp1 token")

# Test 10: Wrong algorithm (HS256 instead of RS256)
print("Test 10: Wrong algorithm...")
try:
    fake_token = jwt.encode({"sub": 1, "exp": 9999999999}, "secret", algorithm="HS256")
    req = urllib.request.Request(
        f"{BASE_URL}/auth/me",
        headers={"Authorization": f"Bearer {fake_token}"},
    )
    resp = urllib.request.urlopen(req)
    check("Rejects wrong algorithm", False, f"got {resp.status}")
except urllib.error.HTTPError as e:
    check("Rejects wrong algorithm", e.code == 401, f"got {e.code}")
except Exception as e:
    check("Rejects wrong algorithm", False, str(e))

# Test 11: Expired token
print("Test 11: Expired token...")
try:
    expired_token = jwt.encode({"sub": 1, "exp": 1}, PRIVATE_KEY, algorithm="RS256")
    req = urllib.request.Request(
        f"{BASE_URL}/auth/me",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    resp = urllib.request.urlopen(req)
    check("Rejects expired token", False, f"got {resp.status}")
except urllib.error.HTTPError as e:
    check("Rejects expired token", e.code == 401, f"got {e.code}")
except Exception as e:
    check("Rejects expired token", False, str(e))

# Test 12: Missing required claims
print("Test 12: Missing required claims...")
try:
    incomplete_token = jwt.encode({"sub": 1}, PRIVATE_KEY, algorithm="RS256")
    req = urllib.request.Request(
        f"{BASE_URL}/auth/me",
        headers={"Authorization": f"Bearer {incomplete_token}"},
    )
    resp = urllib.request.urlopen(req)
    check("Rejects incomplete claims", False, f"got {resp.status}")
except urllib.error.HTTPError as e:
    check("Rejects incomplete claims", e.code == 401, f"got {e.code}")
except Exception as e:
    check("Rejects incomplete claims", False, str(e))

# ============ SUMMARY ============
print("\n" + "=" * 60)
print("TEST RESULTS SUMMARY")
print("=" * 60)
print("-" * 60)
print(f"  Total: {passed + failed} | Passed: {passed} | Failed: {failed}")
if failed > 0:
    sys.exit(1)
