from __future__ import annotations

import json
from typing import Any

import httpx
from trading_core.config import LiteLLMConfig


class LiteLLMClient:
    def __init__(self, config: LiteLLMConfig):
        self.config = config

    async def chat_json(
        self, model: str, system_prompt: str, user_payload: dict[str, Any]
    ) -> dict[str, Any]:
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False),
                },
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            response = await client.post(url, json=body, headers=headers)
            response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)
