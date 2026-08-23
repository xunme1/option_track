from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol


BASE_URL = "https://api.hitick.top"
BASIC_URL = f"{BASE_URL}/api/v1/basic-data"
VOL_URL = f"{BASE_URL}/api/v1/vol-data"
MCP_URL = f"{BASE_URL}/mcp"


class HitickError(RuntimeError):
    """A safe-to-log error returned while talking to Orange Hitick."""


class _HitickHttpError(HitickError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"Hitick request failed with HTTP status {status}")


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: dict[str, str]
    body: bytes


class JsonTransport(Protocol):
    def post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> HttpResult:
        ...


class UrllibTransport:
    def post_json(
        self, url: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> HttpResult:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return HttpResult(
                    response.status,
                    {
                        key.lower(): value
                        for key, value in response.headers.items()
                    },
                    response.read(),
                )
        except urllib.error.HTTPError as error:
            return HttpResult(
                error.code,
                {
                    key.lower(): value
                    for key, value in error.headers.items()
                },
                error.read(),
            )
        except urllib.error.URLError:
            raise HitickError("Hitick request failed") from None


class HitickClient:
    def __init__(self, api_token: str, transport: JsonTransport | None = None):
        self._api_token = api_token
        self._transport = transport or UrllibTransport()
        self._mcp_session_id: str | None = None
        self._mcp_session_lock = threading.Lock()

    def basic_by_expire(self, underlying: str, expire: str) -> dict[str, Any]:
        request = {
            "action": "by_expire",
            "params": {"underlying": underlying, "expire": expire},
        }
        return self._rest_or_mcp(
            BASIC_URL,
            "orange_basic_data",
            request,
        )

    def vol_by_underlying(
        self,
        underlying: str,
        expire: str,
        multiplier: object,
        *,
        full_chain: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(full_chain, bool):
            raise HitickError("full chain flag must be boolean")
        request = {
            "action": "by_underlying",
            "params": {
                "underlying": underlying,
                "expire": expire,
                "multiplier": str(multiplier),
                "fullChain": full_chain,
            },
        }
        return self._rest_or_mcp(
            VOL_URL,
            "orange_vol_data",
            request,
        )

    def vol_time_series(self, underlying: str, start_ms: int, end_ms: int) -> dict[str, Any]:
        request = {
            "action": "ts_by_underlying",
            "params": {
                "underlying": underlying,
                "start": str(start_ms),
                "end": str(end_ms),
            },
        }
        return self._rest_or_mcp(
            VOL_URL,
            "orange_vol_data",
            request,
        )

    def resolve_subject(self, subject: str) -> dict[str, Any]:
        resolved = self._mcp_tool_call(
            "orange_resolve_subject",
            {"query": subject, "limit": 5},
            require_found=False,
        )
        if resolved.get("found") is False:
            return resolved
        if resolved.get("found") is not True:
            raise HitickError("unexpected subject resolution shape")
        if resolved.get("ambiguous") is True:
            raise HitickError("subject resolution is ambiguous")
        if not isinstance(resolved.get("selected"), dict):
            raise HitickError("subject resolution has no selected contract")
        return resolved

    def _rest_call(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = self._post(url, payload, self._headers())
        response = self._decode_json_bytes(result.body, "invalid Hitick response")
        self._require_found(response)
        return response

    def _rest_or_mcp(
        self,
        url: str,
        tool_name: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            return self._rest_call(url, request)
        except _HitickHttpError as error:
            if error.status < 500:
                raise
        return self._mcp_tool_call(tool_name, request)

    def _mcp_tool_call(
        self,
        tool_name: str,
        request: dict[str, Any],
        *,
        require_found: bool = True,
    ) -> dict[str, Any]:
        session_id = self._ensure_mcp_session()
        payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": {"request": request},
            },
        }
        try:
            message = self._mcp_call(payload, session_id)
        except _HitickHttpError as error:
            if error.status != 404 or not session_id:
                raise
            with self._mcp_session_lock:
                if self._mcp_session_id == session_id:
                    self._mcp_session_id = None
            session_id = self._ensure_mcp_session()
            message = self._mcp_call(payload, session_id)

        result = self._json_rpc_result(message)
        content = result.get("content")
        if not isinstance(content, list):
            raise HitickError("unexpected MCP response shape")
        text = next(
            (
                item.get("text")
                for item in content
                if isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
            ),
            None,
        )
        if text is None:
            raise HitickError("unexpected MCP response shape")
        response = self._decode_json(text, "unexpected MCP response shape")
        if require_found:
            self._require_found(response)
        return response

    def _ensure_mcp_session(self) -> str:
        if self._mcp_session_id is not None:
            return self._mcp_session_id

        with self._mcp_session_lock:
            if self._mcp_session_id is not None:
                return self._mcp_session_id

            initialize = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "options-monitor", "version": "1.0"},
                },
            }
            result = self._mcp_call(initialize, None)
            initialize_result = self._json_rpc_result(result)
            if (
                initialize_result.get("protocolVersion") != "2025-03-26"
                or not isinstance(initialize_result.get("capabilities"), dict)
                or not isinstance(initialize_result.get("serverInfo"), dict)
            ):
                raise HitickError("unexpected MCP initialize response shape")
            session_id = self._header(result.headers, "mcp-session-id")
            if not session_id:
                raise HitickError("MCP initialization did not return a session")

            self._post(
                MCP_URL,
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                },
                self._mcp_headers(session_id),
            )
            self._mcp_session_id = session_id
            return session_id

    def _mcp_call(
        self, payload: dict[str, Any], session_id: str | None
    ) -> HttpResult:
        result = self._post(MCP_URL, payload, self._mcp_headers(session_id))
        return result

    def _post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> HttpResult:
        try:
            result = self._transport.post_json(url, payload, headers)
        except Exception:
            raise HitickError("Hitick request failed") from None
        if (
            not isinstance(result, HttpResult)
            or not isinstance(result.status, int)
            or not isinstance(result.headers, dict)
            or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in result.headers.items()
            )
            or not isinstance(result.body, bytes)
        ):
            raise HitickError("unexpected Hitick response shape")
        if not 200 <= result.status < 300:
            raise _HitickHttpError(result.status)
        return result

    def _headers(self, session_id: str | None = None) -> dict[str, str]:
        headers = {"X-API-Token": self._api_token}
        if session_id is not None:
            headers["Mcp-Session-Id"] = session_id
        return headers

    def _mcp_headers(self, session_id: str | None) -> dict[str, str]:
        return {
            **self._headers(session_id),
            "Accept": "application/json, text/event-stream",
        }

    @staticmethod
    def _header(headers: dict[str, str], name: str) -> str | None:
        target = name.lower()
        for key, value in headers.items():
            if key.lower() == target:
                return value
        return None

    @staticmethod
    def _decode_json_bytes(body: bytes, error_message: str) -> dict[str, Any]:
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            raise HitickError(error_message) from None
        return HitickClient._decode_json(text, error_message)

    @staticmethod
    def _decode_json(text: str, error_message: str) -> dict[str, Any]:
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            raise HitickError(error_message) from None
        if not isinstance(parsed, dict):
            raise HitickError(error_message)
        return parsed

    def _json_rpc_result(self, result: HttpResult) -> dict[str, Any]:
        content_type = self._header(result.headers, "content-type") or ""
        if "text/event-stream" in content_type.lower():
            message = self._decode_sse_message(result.body)
        else:
            message = self._decode_json_bytes(result.body, "invalid MCP response")
        if "error" in message:
            raise HitickError("Hitick JSON-RPC error")
        rpc_result = message.get("result")
        if not isinstance(rpc_result, dict):
            raise HitickError("unexpected MCP response shape")
        return rpc_result

    def _decode_sse_message(self, body: bytes) -> dict[str, Any]:
        try:
            lines = body.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            raise HitickError("invalid MCP SSE response") from None

        events: list[str] = []
        data_fields: list[str] = []
        for line in lines:
            if line == "":
                if data_fields:
                    events.append("\n".join(data_fields))
                    data_fields = []
                continue
            if line.startswith("data:"):
                data_fields.append(line[5:].lstrip())
        if data_fields:
            events.append("\n".join(data_fields))

        selected: dict[str, Any] | None = None
        for data in events:
            if data == "[DONE]":
                continue
            try:
                decoded = json.loads(data)
            except (TypeError, ValueError):
                raise HitickError("invalid MCP SSE response") from None
            messages = decoded if isinstance(decoded, list) else [decoded]
            for message in messages:
                if isinstance(message, dict) and (
                    "result" in message or "error" in message
                ):
                    selected = message
        if selected is None:
            raise HitickError("unexpected MCP response shape")
        return selected

    @staticmethod
    def _require_found(response: dict[str, Any]) -> None:
        if response.get("found") is not True:
            raise HitickError("Hitick did not find matching data")
