"""Bearer token store: create, list, revoke and verify.

Tokens read chl_<id>_<secret>: id is 8 hex characters, secret is 43 URL-safe characters.
The store keeps the SHA-256 of each token, never the token.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

PREFIX = "chl_"
STORE_VERSION = 1
MAX_TOKEN_LENGTH = 200

_ID_RE = re.compile(r"^[0-9a-f]{8}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DURATION_RE = re.compile(r"^(\d+)([mhdw])$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


class TokenStoreError(Exception):
    """Raised when the token file cannot be read, parsed or written, or a lookup fails."""


@dataclass(frozen=True)
class TokenRecord:
    """One stored token.

    Attributes:
        id: 8 hex characters, also embedded in the token.
        name: Unique label.
        sha256: Hex digest of the full token.
        created: ISO 8601 UTC time.
        expires: ISO 8601 UTC time, or None.
    """

    id: str
    name: str
    sha256: str
    created: str
    expires: str | None = None

    def expired(self, now: datetime | None = None) -> bool:
        """Return True when the token has an expiry at or before now."""
        if not self.expires:
            return False
        return (now or datetime.now(timezone.utc)) >= parse_time(self.expires)


def format_time(moment: datetime) -> str:
    """Return a UTC time as YYYY-MM-DDTHH:MM:SSZ."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: str) -> datetime:
    """Parse a time written by format_time.

    Raises:
        ValueError: on malformed text.
    """
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def parse_duration(text: str | None) -> timedelta | None:
    """Parse 90m, 12h, 30d, 8w or never.

    Returns:
        timedelta, or None for never or empty.
    Raises:
        ValueError: on any other text.
    """
    value = (text or "").strip().lower()
    if value in ("", "never", "none"):
        return None
    match = _DURATION_RE.match(value)
    if not match or int(match.group(1)) == 0:
        raise ValueError(f"Expiry {text!r} is not a duration. Use a number with m, h, d or w (30d), or never.")
    return timedelta(**{_UNITS[match.group(2)]: int(match.group(1))})


def hash_token(token: str) -> str:
    """Return the SHA-256 hex digest of a token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def split_token(token) -> str | None:
    """Return the id embedded in a well-formed token, or None."""
    if not isinstance(token, str) or len(token) > MAX_TOKEN_LENGTH or not token.startswith(PREFIX):
        return None
    token_id, sep, secret = token[len(PREFIX):].partition("_")
    if not sep or not secret or not _ID_RE.match(token_id):
        return None
    return token_id


def load(path: str) -> list:
    """Read the store.

    Args:
        path: Token file.
    Returns:
        List of TokenRecord; empty when the file does not exist.
    Raises:
        TokenStoreError: when the file is unreadable or malformed.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise TokenStoreError(f"Cannot read token file {path}: {exc}") from None
    if not isinstance(data, dict) or data.get("version") != STORE_VERSION or not isinstance(data.get("tokens"), list):
        raise TokenStoreError(f"{path} is not a comfyui-headless token file (version {STORE_VERSION}).")
    records = []
    for index, item in enumerate(data["tokens"]):
        try:
            record = TokenRecord(id=item["id"], name=item["name"], sha256=item["sha256"],
                                 created=item["created"], expires=item.get("expires"))
            valid = (_ID_RE.match(record.id) and _HASH_RE.match(record.sha256)
                     and isinstance(record.name, str) and isinstance(record.created, str))
            if record.expires is not None:
                parse_time(record.expires)
        except (KeyError, TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise TokenStoreError(f"{path}: token entry {index} is malformed.")
        records.append(record)
    return records


def _replace(source, target):
    """os.replace with retries for Windows sharing violations."""
    for attempt in range(20):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def save(path: str, records: list):
    """Write the store atomically, owner-only on POSIX.

    Raises:
        TokenStoreError: when the file cannot be written.
    """
    directory = os.path.dirname(os.path.abspath(path))
    payload = {"version": STORE_VERSION, "tokens": [asdict(r) for r in records]}
    try:
        os.makedirs(directory, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".tokens-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
            if os.name != "nt":
                os.chmod(temp, 0o600)
            _replace(temp, path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise TokenStoreError(f"Cannot write token file {path}: {exc}") from None


def create(path: str, name: str | None = None, expires: timedelta | None = None,
           now: datetime | None = None) -> tuple:
    """Create and store a token.

    Args:
        path: Token file.
        name: Unique label; defaults to token-<id>.
        expires: Lifetime, or None for no expiry.
        now: Creation time; defaults to the current UTC time.
    Returns:
        (token, TokenRecord).
    Raises:
        TokenStoreError: on a duplicate or invalid name, or a store error.
    """
    records = load(path)
    now = now or datetime.now(timezone.utc)
    taken = {r.id for r in records}
    token_id = secrets.token_hex(4)
    while token_id in taken:
        token_id = secrets.token_hex(4)
    name = name or f"token-{token_id}"
    if not _NAME_RE.match(name):
        raise TokenStoreError(f"Token name {name!r} is invalid. Use 1-64 letters, digits, '.', '_' or '-'.")
    if any(r.name == name for r in records):
        raise TokenStoreError(f'A token named "{name}" exists. Revoke it first or choose another --name.')
    token = f"{PREFIX}{token_id}_{secrets.token_urlsafe(32)}"
    record = TokenRecord(id=token_id, name=name, sha256=hash_token(token), created=format_time(now),
                         expires=format_time(now + expires) if expires else None)
    save(path, records + [record])
    return token, record


def revoke(path: str, key: str) -> TokenRecord:
    """Remove a token by id or name.

    Returns:
        The removed TokenRecord.
    Raises:
        TokenStoreError: when no token matches.
    """
    records = load(path)
    matches = [r for r in records if r.id == key or r.name == key]
    if not matches:
        raise TokenStoreError(f'No token with id or name "{key}" in {path}.')
    removed = matches[0]
    save(path, [r for r in records if r is not removed])
    return removed


class Verifier:
    """Checks presented tokens against the store and reloads it when the file changes.

    Args:
        path: Token file.
        min_interval: Seconds between file checks.
        on_error: Called with a message when the file becomes unreadable; every token is then rejected.
    """

    def __init__(self, path: str, min_interval: float = 0.5, on_error=None):
        self.path = path
        self.min_interval = min_interval
        self.on_error = on_error
        self._by_id = {}
        self._signature = ()
        self._checked = float("-inf")
        self.refresh()

    @property
    def count(self) -> int:
        """Number of loaded tokens, expired ones included."""
        return len(self._by_id)

    def _stat(self):
        """Return a change signature for the token file, or None when it is absent."""
        try:
            info = os.stat(self.path)
        except FileNotFoundError:
            return None
        except OSError:
            return ("error",)
        return (info.st_mtime_ns, info.st_size, info.st_ino)

    def refresh(self):
        """Reload the store when its signature changed and min_interval has passed."""
        now = time.monotonic()
        if now - self._checked < self.min_interval:
            return
        self._checked = now
        signature = self._stat()
        if signature == self._signature:
            return
        self._signature = signature
        try:
            records = load(self.path) if signature is not None else []
        except TokenStoreError as exc:
            self._by_id = {}
            if self.on_error:
                self.on_error(str(exc))
            return
        self._by_id = {r.id: r for r in records}

    def verify(self, token, now: datetime | None = None) -> TokenRecord | None:
        """Return the matching unexpired record, or None."""
        self.refresh()
        token_id = split_token(token)
        record = self._by_id.get(token_id) if token_id else None
        digest = hash_token(token) if token_id else ""
        expected = record.sha256 if record else "0" * 64
        if not hmac.compare_digest(digest.encode("ascii"), expected.encode("ascii")) or record is None:
            return None
        if record.expired(now):
            return None
        return record
