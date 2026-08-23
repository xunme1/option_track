from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode, urlparse


BASE_URL = "https://api.imgbb.com/1/upload"
EXPIRATION_SECONDS = 2_592_000
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BYTES = 256_000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ImgBBError(RuntimeError):
    """A safe-to-log ImgBB upload failure."""


class ImgBBTransport(Protocol):
    def post(
        self, url: str, body: bytes, headers: dict[str, str]
    ) -> bytes:
        raise NotImplementedError


class UrllibImgBBTransport:
    def post(
        self, url: str, body: bytes, headers: dict[str, str]
    ) -> bytes:
        request = urllib.request.Request(
            url, data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                if not 200 <= response.status < 300:
                    raise ImgBBError("ImgBB upload failed")
                result = response.read(MAX_RESPONSE_BYTES + 1)
        except ImgBBError:
            raise
        except (
            OSError,
            TimeoutError,
            urllib.error.HTTPError,
            urllib.error.URLError,
        ):
            raise ImgBBError("ImgBB upload failed") from None
        if len(result) > MAX_RESPONSE_BYTES:
            raise ImgBBError("invalid ImgBB response")
        return result


class ImgBBClient:
    def __init__(
        self,
        api_key: str,
        transport: ImgBBTransport | None = None,
    ):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ImgBBError("ImgBB API key is unavailable")
        self._api_key = api_key.strip()
        self._transport = transport or UrllibImgBBTransport()

    def upload_png(self, path: Path) -> str:
        image_path = Path(path)
        try:
            if image_path.stat().st_size > MAX_IMAGE_BYTES:
                raise ImgBBError("invalid IV chart image")
            image = image_path.read_bytes()
        except ImgBBError:
            raise
        except OSError:
            raise ImgBBError("IV chart image is unavailable") from None
        if (
            not image.startswith(PNG_SIGNATURE)
            or len(image) > MAX_IMAGE_BYTES
        ):
            raise ImgBBError("invalid IV chart image")

        request_url = (
            f"{BASE_URL}?"
            + urlencode({
                "key": self._api_key,
                "expiration": str(EXPIRATION_SECONDS),
            })
        )
        body = urlencode({
            "image": base64.b64encode(image).decode("ascii"),
            "name": image_path.stem,
        }).encode("ascii")
        try:
            raw = self._transport.post(
                request_url,
                body,
                {"Content-Type": "application/x-www-form-urlencoded"},
            )
        except Exception:
            raise ImgBBError("ImgBB upload failed") from None
        return _validated_direct_url(raw)


def _validated_direct_url(raw: bytes) -> str:
    if not isinstance(raw, (bytes, bytearray)):
        raise ImgBBError("invalid ImgBB response")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ImgBBError("invalid ImgBB response")
    try:
        document = json.loads(bytes(raw).decode("utf-8"))
        if not isinstance(document, dict):
            raise ValueError
        if document.get("success") is not True:
            raise ValueError
        status = document.get("status")
        if type(status) is not int or status != 200:
            raise ValueError
        data = document.get("data")
        if not isinstance(data, dict):
            raise ValueError
        url = data.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "i.ibb.co"
            or parsed.username is not None
            or parsed.password is not None
            or not parsed.path
        ):
            raise ValueError
    except (AttributeError, TypeError, UnicodeError, ValueError, json.JSONDecodeError):
        raise ImgBBError("invalid ImgBB response") from None
    return url
