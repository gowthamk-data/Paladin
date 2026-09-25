import requests
import json

API_URL = "http://localhost:8000"

# Step 1: Authenticate as emp1
print("=== Step 1: Authenticate as emp1 ===")
resp = requests.post(
    f"{API_URL}/auth/login",
    params={"username": "emp1", "password": "emp1_pass"},
)
print(f"Status: {resp.status_code}")
data = resp.json()
token = data["access_token"]
print(f"Token received: {token[:20]}...")

# Step 2: Submit query designed to retrieve engineering_policy.txt
print("\n=== Step 2: Submit /ask query ===")
query = "What are the key engineering security requirements?"
print(f"Query: {query}")
resp = requests.post(
    f"{API_URL}/ask",
    params={"message": query},
    headers={"Authorization": f"Bearer {token}"},
)
print(f"Status: {resp.status_code}")

# Step 3: Parse and display response
print("\n=== Step 3: Response ===")
if resp.status_code == 200:
    data = resp.json()
    print(f"Response: {data.get('response', 'N/A')}")
    print(f"Sources: {data.get('sources', [])}")
    print(f"Request ID: {data.get('request_id', 'N/A')}")
    print("\nTEST PASSED: Valid LLM-generated response received")
else:
    print(f"Error: {resp.text}")
    print("\nTEST FAILED: Expected 200, got", resp.status_code)