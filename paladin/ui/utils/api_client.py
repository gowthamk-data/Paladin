import os
import requests

API_URL = os.environ.get("API_URL", "http://localhost:8000")


def login(username: str, password: str) -> dict | None:
    try:
        resp = requests.post(
            f"{API_URL}/auth/login",
            params={"username": username, "password": password},
        )
        if resp.status_code == 200:
            return resp.json()
        return None
    except requests.ConnectionError:
        return None


def get_me(token: str) -> dict | None:
    try:
        resp = requests.get(
            f"{API_URL}/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 200:
            return resp.json()
        return None
    except requests.ConnectionError:
        return None


def ask(token: str, message: str) -> dict | None:
    try:
        resp = requests.post(
            f"{API_URL}/ask",
            params={"message": message},
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 401:
            return {"error": "UNAUTHORIZED", "status_code": 401}
        return {"error": resp.text, "status_code": resp.status_code}
    except requests.ConnectionError:
        return {"error": "Cannot connect to API"}