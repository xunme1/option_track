from __future__ import annotations

import hashlib
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import quote, urlparse


DEFAULT_REGION = "cn-guangzhou"
DEFAULT_BUCKET = "option-monitor-images"
DEFAULT_ENDPOINT = "https://oss-cn-guangzhou.aliyuncs.com"
DEFAULT_PUBLIC_HOST = (
    "option-monitor-images.oss-cn-guangzhou.aliyuncs.com"
)
DEFAULT_PREFIX = "option-monitor/charts"
# Compatibility names for the built-in example target. They are defaults, not
# an allowlist: deployments may use another validated Alibaba Cloud OSS target.
APPROVED_REGION = DEFAULT_REGION
APPROVED_BUCKET = DEFAULT_BUCKET
APPROVED_ENDPOINT = DEFAULT_ENDPOINT
APPROVED_PUBLIC_HOST = DEFAULT_PUBLIC_HOST
APPROVED_PREFIX = DEFAULT_PREFIX
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_IMAGE_BYTES = 32 * 1024 * 1024
REGION_NAME = re.compile(r"^cn-[a-z0-9-]+$")
BUCKET_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$")
PREFIX_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
CHART_NAME = re.compile(
    r"^(?:(?:iv|anomaly)-chart|openvlab-ranking)-"
    r"(\d{4})(\d{2})(\d{2})T\d{6}[+-]\d{4}\.png$"
)


class AliyunOssError(RuntimeError):
    """A safe-to-log OSS image-hosting failure."""


@dataclass(frozen=True)
class OssUploadResult:
    status_code: int
    request_id: str


def _direct_urllib_open(request: Any, timeout: int) -> Any:
    """Open an OSS public URL without inheriting process proxy variables."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(request, timeout=timeout)


def _oss_http_client_without_environment_proxy() -> Any:
    """Build the OSS SDK transport with ``requests`` environment trust off."""
    try:
        import requests
        from alibabacloud_oss_v2.transport import RequestsHttpClient
    except ImportError:
        raise AliyunOssError("OSS SDK is unavailable") from None
    session = requests.Session()
    session.trust_env = False
    return RequestsHttpClient(session=session)


class OssTransport(Protocol):
    def upload_png(
        self, bucket: str, key: str, path: Path
    ) -> OssUploadResult:
        raise NotImplementedError


class PublicImageProbe(Protocol):
    def validate_png(self, url: str) -> None:
        raise NotImplementedError


class AliyunSdkOssTransport:
    def __init__(self, client: Any, oss_module: Any):
        self._client = client
        self._oss = oss_module

    def upload_png(
        self, bucket: str, key: str, path: Path
    ) -> OssUploadResult:
        try:
            result = self._client.put_object_from_file(
                self._oss.PutObjectRequest(
                    bucket=bucket,
                    key=key,
                    content_type="image/png",
                ),
                str(path),
            )
            return OssUploadResult(
                status_code=int(result.status_code),
                request_id=str(result.request_id or ""),
            )
        except Exception:
            raise AliyunOssError("OSS upload failed") from None


class UrllibPublicImageProbe:
    def __init__(
        self,
        public_host: str = DEFAULT_PUBLIC_HOST,
        prefix: str = DEFAULT_PREFIX,
        opener: Callable[..., Any] | None = None,
        timeout: int = 10,
    ):
        _validate_storage_identity(
            region=_region_from_public_host(public_host),
            bucket=public_host.split(".", 1)[0],
            endpoint=f"https://{public_host.split('.', 1)[1]}",
            prefix=prefix,
        )
        self._public_host = public_host
        self._prefix = prefix
        self._opener = opener or _direct_urllib_open
        self._timeout = timeout

    def validate_png(self, url: str) -> None:
        _validate_public_url(url, self._public_host, self._prefix)
        request = urllib.request.Request(url, method="HEAD")
        try:
            with self._opener(request, timeout=self._timeout) as response:
                status = int(response.status)
                final_url = str(response.geturl())
                content_type = response.headers.get_content_type()
        except Exception:
            raise AliyunOssError(
                "OSS public image validation failed"
            ) from None
        if (
            status != 200
            or final_url != url
            or content_type != "image/png"
        ):
            raise AliyunOssError("OSS public image validation failed")


class AliyunOssImageUploader:
    def __init__(
        self,
        *,
        transport: OssTransport,
        probe: PublicImageProbe,
        bucket: str,
        prefix: str,
        region: str,
        public_host: str,
    ):
        _validate_storage_identity(
            region=region,
            bucket=bucket,
            endpoint=f"https://{public_host.split('.', 1)[1]}"
            if "." in public_host else "",
            prefix=prefix,
        )
        if public_host != f"{bucket}.oss-{region}.aliyuncs.com":
            raise AliyunOssError("invalid OSS image configuration")
        self._transport = transport
        self._probe = probe
        self._bucket = bucket
        self._prefix = prefix
        self._public_host = public_host

    def upload_png(self, path: Path) -> str:
        image_path = Path(path)
        image = _read_png(image_path)
        key = _object_key(self._prefix, image_path, image)
        try:
            result = self._transport.upload_png(
                self._bucket, key, image_path
            )
        except Exception:
            raise AliyunOssError("OSS upload failed") from None
        if (
            not 200 <= result.status_code < 300
            or not result.request_id
        ):
            raise AliyunOssError("OSS upload failed")

        url = (
            f"https://{self._public_host}/"
            f"{quote(key, safe='/')}"
        )
        _validate_public_url(url, self._public_host, self._prefix)
        try:
            self._probe.validate_png(url)
        except Exception:
            raise AliyunOssError(
                "OSS public image validation failed"
            ) from None
        return url


def create_aliyun_oss_uploader(
    *,
    access_key_id: str | None,
    access_key_secret: str | None,
    region: str,
    bucket: str,
    endpoint: str,
    prefix: str,
    probe: PublicImageProbe | None = None,
    _oss_module: Any | None = None,
) -> AliyunOssImageUploader:
    if (
        not isinstance(access_key_id, str)
        or not access_key_id.strip()
        or not isinstance(access_key_secret, str)
        or not access_key_secret.strip()
    ):
        raise AliyunOssError("invalid OSS image configuration")
    endpoint, public_host = _validate_storage_identity(
        region=region,
        bucket=bucket,
        endpoint=endpoint,
        prefix=prefix,
    )

    if _oss_module is None:
        try:
            import alibabacloud_oss_v2 as oss_module
        except ImportError:
            raise AliyunOssError("OSS SDK is unavailable") from None
    else:
        oss_module = _oss_module

    try:
        credentials = oss_module.credentials.StaticCredentialsProvider(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
        )
        config = oss_module.config.load_default()
        config.credentials_provider = credentials
        config.region = region
        config.endpoint = endpoint
        config.http_client = _oss_http_client_without_environment_proxy()
        client = oss_module.Client(config)
    except Exception:
        raise AliyunOssError(
            "OSS client initialization failed"
        ) from None

    return AliyunOssImageUploader(
        transport=AliyunSdkOssTransport(client, oss_module),
        probe=probe or UrllibPublicImageProbe(public_host, prefix),
        bucket=bucket,
        prefix=prefix,
        region=region,
        public_host=public_host,
    )


def _read_png(path: Path) -> bytes:
    try:
        with path.open("rb") as source:
            image = source.read(MAX_IMAGE_BYTES + 1)
    except OSError:
        raise AliyunOssError("monitor chart image is unavailable") from None
    if (
        len(image) <= len(PNG_SIGNATURE)
        or len(image) > MAX_IMAGE_BYTES
        or not image.startswith(PNG_SIGNATURE)
    ):
        raise AliyunOssError("invalid monitor chart image")
    return image


def _object_key(prefix: str, path: Path, image: bytes) -> str:
    match = CHART_NAME.fullmatch(path.name)
    if match is None:
        raise AliyunOssError("invalid monitor chart image")
    year, month, day = match.groups()
    digest = hashlib.sha256(image).hexdigest()[:12]
    return (
        f"{prefix}/{year}/{month}/{day}/"
        f"{path.stem}-{digest}.png"
    )


def _validate_storage_identity(
    *, region: str, bucket: str, endpoint: str, prefix: str
) -> tuple[str, str]:
    if (
        not isinstance(region, str)
        or REGION_NAME.fullmatch(region) is None
        or not isinstance(bucket, str)
        or BUCKET_NAME.fullmatch(bucket) is None
        or not isinstance(prefix, str)
        or not prefix
        or any(PREFIX_SEGMENT.fullmatch(item) is None for item in prefix.split("/"))
    ):
        raise AliyunOssError("invalid OSS image configuration")
    if not isinstance(endpoint, str):
        raise AliyunOssError("invalid OSS image configuration")
    raw_endpoint = endpoint.strip()
    if not raw_endpoint:
        raise AliyunOssError("invalid OSS image configuration")
    parsed = urlparse(
        raw_endpoint if "://" in raw_endpoint else f"https://{raw_endpoint}"
    )
    expected_host = f"oss-{region}.aliyuncs.com"
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected_host
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise AliyunOssError("invalid OSS image configuration")
    return f"https://{expected_host}", f"{bucket}.{expected_host}"


def _region_from_public_host(public_host: str) -> str:
    match = re.fullmatch(r"[a-z0-9-]+\.oss-(cn-[a-z0-9-]+)\.aliyuncs\.com", public_host)
    if match is None:
        raise AliyunOssError("invalid OSS image configuration")
    return match.group(1)


def _validate_public_url(url: str, public_host: str, prefix: str) -> None:
    if (
        not isinstance(url, str)
        or not url
        or any(
            character.isspace()
            or character in "()[]<>\\"
            or ord(character) == 127
            for character in url
        )
    ):
        raise AliyunOssError("invalid OSS public image URL")
    try:
        parsed = urlparse(url)
        port = parsed.port
    except (TypeError, ValueError):
        raise AliyunOssError("invalid OSS public image URL") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != public_host
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith(f"/{prefix}/")
        or not parsed.path.endswith(".png")
    ):
        raise AliyunOssError("invalid OSS public image URL")
