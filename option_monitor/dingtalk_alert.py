from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request
from typing import Any


def build_signed_webhook_url(
    webhook: str,
    secret: str,
    timestamp_ms: int | None = None,
) -> str:
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)

    string_to_sign = f"{timestamp_ms}\n{secret}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))

    separator = "&" if urllib.parse.urlparse(webhook).query else "?"
    return f"{webhook}{separator}timestamp={timestamp_ms}&sign={sign}"


def build_markdown_payload(title: str, text: str) -> dict[str, Any]:
    return {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": text,
        },
    }


def send_markdown(
    webhook: str,
    secret: str,
    title: str,
    text: str,
    timeout_seconds: int = 10,
) -> dict[str, Any]:
    signed_url = build_signed_webhook_url(webhook, secret)
    payload = json.dumps(build_markdown_payload(title, text), ensure_ascii=False).encode(
        "utf-8"
    )
    request = urllib.request.Request(
        signed_url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        body = response.read().decode("utf-8")
    return json.loads(body)
