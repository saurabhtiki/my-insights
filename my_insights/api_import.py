"""Fetch JSON from a web API (GET or POST), safely.

Only public addresses are fetched: a host that points inside the company network or at this
server (localhost, 10.x, 192.168.x, 169.254.x …) is refused, also after a redirect, unless
the host lists it in API_ALLOWED_HOSTS. Python's own urllib is used: no extra package.
"""

import http.client
import ipaddress
import json
import logging
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from . import conf
from .workspace import ApiSource

logger = logging.getLogger(__name__)

CHUNK = 64 * 1024
STATUS_HELP = {
    400: "the API did not accept the request: check the Body and the URL's options",
    401: "not authorised: check the key in Headers",
    403: "access refused: check the key in Headers and its permissions",
    404: "not found: check the URL",
    405: "this method is not allowed here: try GET or POST",
    429: "too many requests: wait a minute and try again",
}


class ApiFailed(Exception):
    """The API could not be fetched. The message is shown to the user as it is."""


def safe_url(url: str) -> str:
    """The address without its ?query part, which may hold a key: for labels and logs."""
    parts = urlsplit(url)
    return f"{parts.hostname or ''}{parts.path}".rstrip("/")


def parse_headers(text: str) -> dict[str, str]:
    """ "Name: value" per line → {name: value}. Blank lines are skipped."""
    headers = {}
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        name, colon, value = line.partition(":")
        if not colon or not name.strip() or " " in name.strip():
            raise ApiFailed(
                f"Headers, line {line_no}: write it as Name: value, e.g. Authorization: Bearer abc123"
            )
        headers[name.strip()] = value.strip()
    return headers


def check_source(source: ApiSource) -> None:
    """The form's values make sense (before anything is fetched). Raises ApiFailed."""
    if not conf.get("ALLOW_API_IMPORT"):
        raise ApiFailed("Importing from an API is switched off on this server.")
    parts = urlsplit(source.url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ApiFailed("Type a full web address starting with https:// (or http://).")
    if source.method not in ("GET", "POST"):
        raise ApiFailed("Choose GET or POST.")
    parse_headers(source.headers)
    if source.method == "POST" and source.body.strip():
        try:
            json.loads(source.body)
        except json.JSONDecodeError as error:
            raise ApiFailed(
                f"The Body is not valid JSON (line {error.lineno}, column {error.colno}: "
                f"{error.msg})."
            ) from error


def _resolve(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def check_host(url: str) -> None:
    """Refuse a host that points inside the network, when API_BLOCK_INTERNAL is on."""
    if not conf.get("API_BLOCK_INTERNAL"):
        return
    host = (urlsplit(url).hostname or "").lower()
    allowed = {name.lower() for name in conf.get("API_ALLOWED_HOSTS")}
    if host in allowed:
        return
    try:
        addresses = _resolve(host)
    except (socket.gaierror, UnicodeError) as error:
        logger.info("API host not found: %s (%s)", host, error)
        raise ApiFailed(f"The server “{host}” was not found. Check the URL.") from error
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if address not in allowed and not ip.is_global:
            logger.warning("API address refused: %s → %s", host, address)
            raise ApiFailed(
                f"“{host}” is an address inside the network or on this server, which is "
                "not allowed. Ask the administrator to add it to API_ALLOWED_HOSTS."
            )


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme not in ("http", "https"):
            raise ApiFailed("The API redirected to an address that is not http(s).")
        check_host(newurl)  # a redirect may not lead inside the network either
        # shortcut: the address is checked, then looked up again when connecting; a server
        # that changes its DNS answer in between (DNS rebinding) is not caught.
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(request, timeout):
    """Send the request (replaced in tests)."""
    return urllib.request.build_opener(_CheckedRedirects).open(request, timeout=timeout)


def fetch_json(source: ApiSource) -> bytes:
    """The API's reply (bytes). Raises ApiFailed with a message for the user."""
    check_source(source)
    check_host(source.url)
    headers = {
        "Accept": "application/json",
        "User-Agent": "my-insights",
        **parse_headers(source.headers),
    }
    body = None
    if source.method == "POST":
        body = source.body.strip().encode("utf-8") or b"{}"
        if not any(name.lower() == "content-type" for name in headers):
            headers["Content-Type"] = "application/json"
    request = urllib.request.Request(source.url, data=body, headers=headers, method=source.method)
    timeout = conf.get("API_TIMEOUT_SECONDS")
    max_bytes = conf.get("MAX_FILE_MB") * 1024 * 1024
    try:
        with _open(request, timeout) as response:
            chunks, size = [], 0
            while chunk := response.read(CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise ApiFailed(
                        f"The reply is larger than the {conf.get('MAX_FILE_MB')} MB limit. "
                        "Ask the API for fewer rows (e.g. a date range in the URL or Body)."
                    )
                chunks.append(chunk)
    except urllib.error.HTTPError as error:
        reason = STATUS_HELP.get(error.code) or (
            "the API's server had a problem: try again later" if error.code >= 500 else error.reason
        )
        logger.info("API replied %s for %s", error.code, safe_url(source.url))
        raise ApiFailed(f"The API replied {error.code} ({reason}).") from error
    except TimeoutError as error:
        logger.info("API timed out: %s", safe_url(source.url))
        raise ApiFailed(f"The API took longer than {timeout} seconds to reply.") from error
    except urllib.error.URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise ApiFailed(f"The API took longer than {timeout} seconds to reply.") from error
        logger.info("API not reachable: %s (%s)", safe_url(source.url), error.reason)
        raise ApiFailed(f"Could not reach the API ({error.reason}).") from error
    except (OSError, http.client.HTTPException) as error:  # e.g. the connection dropped
        logger.info("API connection failed: %s (%r)", safe_url(source.url), error)
        raise ApiFailed("The API closed the connection without answering. Try again.") from error
    data = b"".join(chunks)
    if not data.strip():
        raise ApiFailed("The API sent an empty reply.")
    start = data.lstrip()[:1]
    if start not in (b"[", b"{"):
        preview = data.lstrip()[:40].decode("utf-8", errors="replace")
        raise ApiFailed(f"The reply is not JSON (it starts with “{preview}”).")
    return data
