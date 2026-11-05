"""HTTP transport abstraction.

The retrying/caching logic lives in :mod:`lifesci.http`; this module only turns a
request into a raw response.  Separating the transport makes portal failure modes
(HTTP 500 HTML pages, JSON-in-200 error payloads, 429 storms, timeouts) testable
offline with :class:`FakeTransport`.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence


class TransportError(Exception):
    """Network-level failure (DNS, TLS, connection reset, timeout)."""

    def __init__(self, message: str, *, retriable: bool = True, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.retriable = retriable
        self.details = dict(details or {})


@dataclass
class TransportResponse:
    """Raw response returned by every transport implementation."""

    status: int | None
    headers: dict[str, str]
    body: bytes
    url: str = ""
    reason: str = ""
    elapsed_ms: float = 0.0
    error: str | None = None

    def header(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.lower(), default)

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding, errors="replace")


class Transport(Protocol):
    """Structural protocol for swappable transports."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 60.0,
    ) -> TransportResponse:  # pragma: no cover - protocol
        ...


class UrllibTransport:
    """Default transport built on :mod:`urllib.request` (no third-party deps)."""

    def __init__(self, *, ssl_context: ssl.SSLContext | None = None, allow_redirects: bool = True):
        self._ssl_context = ssl_context or ssl.create_default_context()
        self._allow_redirects = allow_redirects

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 60.0,
    ) -> TransportResponse:
        method = method.upper()
        request = urllib.request.Request(url, data=body, method=method)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=self._ssl_context) as response:
                payload = response.read()
                elapsed = (time.monotonic() - started) * 1000.0
                return TransportResponse(
                    status=getattr(response, "status", 200),
                    headers={k.lower(): v for k, v in response.headers.items()},
                    body=payload,
                    url=response.geturl(),
                    reason=getattr(response, "reason", ""),
                    elapsed_ms=elapsed,
                )
        except urllib.error.HTTPError as err:
            # HTTP errors are normal responses for our purposes (retry policy is
            # decided one level up); read the error body for diagnostics.
            try:
                payload = err.read()
            except Exception:  # pragma: no cover - defensive
                payload = b""
            elapsed = (time.monotonic() - started) * 1000.0
            return TransportResponse(
                status=err.code,
                headers={k.lower(): v for k, v in (err.headers or {}).items()},
                body=payload,
                url=getattr(err, "url", url),
                reason=str(err.reason),
                elapsed_ms=elapsed,
            )
        except urllib.error.URLError as err:
            reason = getattr(err, "reason", err)
            retriable = not isinstance(reason, (ssl.SSLError,))  # TLS config errors are not transient
            raise TransportError(
                f"network error for {method} {url}: {reason}",
                retriable=retriable,
                details={"url": url, "method": method, "reason": str(reason)},
            ) from err
        except TimeoutError as err:
            raise TransportError(
                f"timeout after {timeout:.1f}s for {method} {url}",
                retriable=True,
                details={"url": url, "method": method, "timeout": timeout},
            ) from err
        except ValueError as err:  # malformed URL
            raise TransportError(
                f"invalid request URL {url!r}: {err}",
                retriable=False,
                details={"url": url, "method": method},
            ) from err
        except (socket.timeout, TimeoutError) as err:
            raise TransportError(
                f"timeout after {timeout:.1f}s for {method} {url}",
                retriable=True,
                details={"url": url, "method": method, "timeout": timeout},
            ) from err
        except http.client.HTTPException as err:
            # IncompleteRead, RemoteDisconnected, BadStatusLine, ...: these are
            # network-level transient failures, not programming errors.
            raise TransportError(
                f"HTTP protocol error for {method} {url}: {type(err).__name__}: {err}",
                retriable=True,
                details={"url": url, "method": method, "exception": type(err).__name__},
            ) from err
        except ConnectionError as err:
            raise TransportError(
                f"connection error for {method} {url}: {err}",
                retriable=True,
                details={"url": url, "method": method},
            ) from err
        except ssl.SSLCertVerificationError as err:
            raise TransportError(
                f"TLS certificate verification failed for {method} {url}: {err}",
                retriable=False,
                details={"url": url, "method": method},
            ) from err
        except ssl.SSLError as err:
            raise TransportError(
                f"TLS error for {method} {url}: {err}",
                retriable=True,
                details={"url": url, "method": method},
            ) from err
        except OSError as err:
            raise TransportError(
                f"OS/network error for {method} {url}: {err}",
                retriable=True,
                details={"url": url, "method": method},
            ) from err


class FakeTransport:
    """Deterministic transport for tests.

    ``responses`` is a list of :class:`TransportResponse`, :class:`TransportError`
    instances, or callables taking ``(method, url, headers, body)`` and returning
    either.  Every request is recorded in :attr:`calls`; when the scripted list
    is exhausted the transport raises a clear assertion error.
    """

    def __init__(self, responses: Sequence[Any], *, repeat_last: bool = False):
        self.responses = list(responses)
        self.repeat_last = repeat_last
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 60.0,
    ) -> TransportResponse:
        self.calls.append(
            {
                "method": method.upper(),
                "url": url,
                "headers": dict(headers or {}),
                "body": body,
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError(f"FakeTransport exhausted: unexpected request {method} {url}")
        scripted = self.responses[0]
        if not (self.repeat_last and len(self.responses) == 1):
            self.responses.pop(0)
        if callable(scripted) and not isinstance(scripted, (TransportResponse, TransportError)):
            scripted = scripted(method, url, headers, body)
        if isinstance(scripted, TransportError):
            raise scripted
        if isinstance(scripted, TransportResponse):
            return scripted
        raise TypeError(f"unsupported scripted response type: {type(scripted)!r}")

    @classmethod
    def json_response(
        cls,
        payload: Any | None = None,
        *,
        status: int = 200,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        url: str = "",
    ) -> TransportResponse:
        import json

        if body is None:
            body = json.dumps(payload).encode("utf-8")
        merged = {"content-type": "application/json"}
        merged.update({k.lower(): v for k, v in (headers or {}).items()})
        return TransportResponse(status=status, headers=merged, body=body, url=url)

    @classmethod
    def text_response(
        cls,
        text: str,
        *,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
        url: str = "",
    ) -> TransportResponse:
        merged = {"content-type": "text/plain"}
        merged.update({k.lower(): v for k, v in (headers or {}).items()})
        return TransportResponse(status=status, headers=merged, body=text.encode("utf-8"), url=url)

    @classmethod
    def error_response(
        cls,
        status: int,
        *,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        url: str = "",
        reason: str = "",
    ) -> TransportResponse:
        return TransportResponse(
            status=status,
            headers={k.lower(): v for k, v in (headers or {}).items()},
            body=body or b"",
            url=url,
            reason=reason,
        )


def multipart_encode(
    fields: Mapping[str, str] | Sequence[tuple[str, str]] | None = None,
    files: Sequence[tuple[str, str, str, bytes]] | None = None,
    *,
    boundary: str | None = None,
) -> tuple[bytes, str]:
    """Build a ``multipart/form-data`` body.

    Args:
        fields: ordinary text form fields.
        files: ``(field_name, filename, content_type, content)`` tuples.
        boundary: optional fixed boundary (tests); generated when omitted.

    Returns:
        ``(body_bytes, content_type_header)``.
    """
    import hashlib

    if boundary is None:
        # Deterministic boundary: the same multipart content always encodes to
        # the same bytes, so a POST body hash is stable and offline replay of a
        # recorded upload is possible. 40 hex chars of SHA-256
        # make an accidental collision inside the payload negligible; content
        # containing this exact marker would still encode unambiguously because
        # the boundary is fixed and delimiters are position-checked by parsers.
        digest = hashlib.sha256()
        field_items_for_hash = (
            sorted(fields.items()) if isinstance(fields, Mapping) else list(fields or [])
        )
        for name, value in field_items_for_hash:
            digest.update(f"{name}\x00{value}\x1e".encode("utf-8", errors="replace"))
        for name, filename, content_type, content in files or []:
            digest.update(f"{name}\x00{filename}\x00{content_type}\x1e".encode("utf-8", errors="replace"))
            digest.update(content)
            digest.update(b"\x1e")
        boundary = "----lifesci" + digest.hexdigest()[:40]
    if isinstance(fields, Mapping):
        field_items: list[tuple[str, Any]] = list(fields.items())
    else:
        field_items = list(fields or [])
    parts: list[bytes] = []
    for name, value in field_items:
        parts.append(
            b"--" + boundary.encode()
            + b"\r\n"
            + f'Content-Disposition: form-data; name="{name}"'.encode()
            + b"\r\n\r\n"
            + str(value).encode("utf-8")
            + b"\r\n"
        )
    for name, filename, content_type, content in files or []:
        parts.append(
            b"--" + boundary.encode()
            + b"\r\n"
            + f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'.encode()
            + b"\r\n"
            + f"Content-Type: {content_type}".encode()
            + b"\r\n\r\n"
            + content
            + b"\r\n"
        )
    parts.append(b"--" + boundary.encode() + b"--\r\n")
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
