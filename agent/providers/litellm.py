from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .base import Provider, ProviderResult


class LiteLLMProvider(Provider):
    def chat(self, *, base_url: str, model: str, prompt: str, timeout_connect: float, timeout_read: float, api_key: str | None = None) -> ProviderResult:
        started = time.monotonic()
        url = base_url.rstrip("/") + "/chat/completions"
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": "Return compact, factual responses."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "stream": False,
            "max_tokens": 4096,
            "response_format": {"type": "json_object"},
        }
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        timeout = max(timeout_connect, timeout_read)
        retried_without_json_mode = False
        try:
            while True:
                req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                    text = payload["choices"][0]["message"].get("content") or ""
                    if text.strip() or retried_without_json_mode or "response_format" not in body:
                        latency_ms = int((time.monotonic() - started) * 1000)
                        return ProviderResult(ok=True, text=text, latency_ms=latency_ms, status_code=resp.status)

                body.pop("response_format", None)
                retried_without_json_mode = True
        except urllib.error.HTTPError as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            return ProviderResult(ok=False, text="", latency_ms=latency_ms, status_code=exc.code, error=str(exc))
        except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            return ProviderResult(ok=False, text="", latency_ms=latency_ms, status_code=None, error=str(exc))
