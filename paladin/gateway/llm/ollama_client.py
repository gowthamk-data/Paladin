import json
import socket
import urllib.error
import urllib.request

from gateway.config import OLLAMA_HOST, OLLAMA_MODEL, OLLAMA_TIMEOUT_SECONDS
from gateway.errors import ServiceUnavailableError


class OllamaClient:
    def __init__(self, base_url: str = None, model: str = None, timeout: int = None):
        self.base_url = (base_url or OLLAMA_HOST).rstrip("/")
        self.model = model or OLLAMA_MODEL
        self.timeout = OLLAMA_TIMEOUT_SECONDS if timeout is None else timeout

    def chat(self, messages: list) -> dict:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # Ollama answered, but with an error status (e.g. model not pulled).
            raise ServiceUnavailableError(
                "ollama", f"LLM backend returned HTTP {exc.code}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ServiceUnavailableError("ollama", "LLM backend is unreachable") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise ServiceUnavailableError("ollama", "LLM backend timed out") from exc
        except OSError as exc:
            # ConnectionResetError / http.client.RemoteDisconnected and friends.
            raise ServiceUnavailableError("ollama", "LLM backend connection failed") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ServiceUnavailableError(
                "ollama", "LLM backend returned a malformed response"
            ) from exc

        if "message" not in data:
            raise ServiceUnavailableError("ollama", "LLM backend returned no message")

        message = data.get("message", {})
        return {
            "response": message.get("content", ""),
            "prompt_eval_count": data.get("prompt_eval_count", 0),
            "eval_count": data.get("eval_count", 0),
        }
