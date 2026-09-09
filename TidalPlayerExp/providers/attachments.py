"""Short-lived, private resolution of Discord CDN attachment audio."""
from __future__ import annotations

import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlsplit

from ..domain.models import TrackMeta
from ..playback.errors import SourceResolutionError
from ..playback.models import ResolvedSource, SourceKind, SourceReference
from ..ui.display import clamp_text

_MAX_ATTACHMENT_SIZE = 50 * 1024 * 1024
_MAX_ENTRIES = 1_000
_MAX_TTL_SECONDS = 12 * 60 * 60
_MAX_URL_LENGTH = 8_192
_TOKEN = re.compile(r"[0-9a-f]{32}\Z")
_SIGNED_EXPIRY = re.compile(r"[0-9a-fA-F]{1,16}\Z")
_AUDIO_TYPES = {
    "mp3": frozenset(("audio/mpeg", "audio/mp3")),
    "m4a": frozenset(("audio/mp4", "audio/x-m4a")),
    "flac": frozenset(("audio/flac", "audio/x-flac")),
    "wav": frozenset(("audio/wav", "audio/wave", "audio/x-wav")),
    "ogg": frozenset(("audio/ogg", "application/ogg")),
    "opus": frozenset(("audio/opus", "audio/ogg")),
    "aac": frozenset(("audio/aac", "audio/x-aac")),
    "webm": frozenset(("audio/webm",)),
    "mp4": frozenset(("audio/mp4",)),
}

_ERR_CDN = "Upload a file attached to this Discord message from the Discord CDN."
_ERR_LINK = "The uploaded file has an invalid attachment link."
_ERR_FORMAT = "Upload a supported audio file."
_ERR_SIZE = "Uploaded files must be between 1 byte and 50 MiB."
_ERR_EXPIRED = "This attachment is no longer available. Re-upload the file and try again."
_ERR_CAPACITY = "Too many uploaded files are waiting to play. Try again shortly."


class AttachmentResolutionError(SourceResolutionError):
    """A fixed public error that does not disclose an attachment URL or token."""

    _DEFAULT_MESSAGE = _ERR_EXPIRED


@dataclass(frozen=True, slots=True, repr=False)
class _StoredAttachment:
    """Private URL plus its strict expiry; its representation is intentionally inert."""

    url: str = field(repr=False)
    expires_at: float

    def __repr__(self) -> str:
        return "StoredAttachment(<redacted>)"


class AttachmentResolver:
    """Resolve only attachments admitted from Discord's attachment CDN.

    The signed URL never leaves this bounded in-memory registry except at the
    final ``ResolvedSource`` boundary consumed by FFmpeg.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        max_entries: int = _MAX_ENTRIES,
    ) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or not 1 <= max_entries <= _MAX_ENTRIES:
            raise ValueError("Attachment registry capacity is invalid")
        self._clock = clock
        self._max_entries = max_entries
        self._entries: dict[str, _StoredAttachment] = {}

    def _purge_expired(self, now: float) -> None:
        for identifier, stored in tuple(self._entries.items()):
            if stored.expires_at <= now:
                self._entries.pop(identifier, None)

    @staticmethod
    def _file_details(attachment: object) -> tuple[str, str]:
        filename = getattr(attachment, "filename", None)
        content_type = getattr(attachment, "content_type", None)
        if not isinstance(filename, str) or not filename or any(ord(char) < 32 for char in filename):
            raise ValueError(_ERR_FORMAT)
        if not isinstance(content_type, str):
            raise ValueError(_ERR_FORMAT)  # noqa: TRY004 - admission errors have one public type
        stem, separator, extension = filename.rpartition(".")
        if not separator or not stem:
            raise ValueError(_ERR_FORMAT)
        allowed_types = _AUDIO_TYPES.get(extension.casefold())
        if allowed_types is None or content_type.casefold() not in allowed_types:
            raise ValueError(_ERR_FORMAT)
        return filename, stem

    @staticmethod
    def _validated_url(attachment: object, filename: str, now: float) -> tuple[str, float]:
        raw_url = getattr(attachment, "url", None)
        if (
            not isinstance(raw_url, str) or not raw_url or len(raw_url) > _MAX_URL_LENGTH
            or any(char.isspace() or ord(char) < 32 for char in raw_url) or "\\" in raw_url
        ):
            raise ValueError(_ERR_LINK)
        try:
            parts = urlsplit(raw_url)
            port = parts.port
        except ValueError as error:
            raise ValueError(_ERR_CDN) from error
        if (
            parts.scheme.lower() != "https"
            or parts.hostname not in {"cdn.discordapp.com", "media.discordapp.net"}
            or port not in {None, 443}
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
        ):
            raise ValueError(_ERR_CDN)

        path_parts = parts.path.split("/")
        if len(path_parts) != 5 or path_parts[0] or path_parts[1] != "attachments":
            raise ValueError(_ERR_LINK)
        channel_id, attachment_id, encoded_filename = path_parts[2:]
        if not (
            channel_id.isascii() and channel_id.isdecimal()
            and attachment_id.isascii() and attachment_id.isdecimal()
        ):
            raise ValueError(_ERR_LINK)
        try:
            decoded_filename = unquote(encoded_filename, encoding="utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ValueError(_ERR_LINK) from error
        if (
            decoded_filename != filename or not decoded_filename
            or decoded_filename in {".", ".."}
            or "/" in decoded_filename or "\\" in decoded_filename
            or any(ord(char) < 32 for char in decoded_filename)
        ):
            raise ValueError(_ERR_LINK)

        expires_at = now + _MAX_TTL_SECONDS
        expiry_values = parse_qs(parts.query, keep_blank_values=True).get("ex")
        if expiry_values is not None:
            if len(expiry_values) != 1 or _SIGNED_EXPIRY.fullmatch(expiry_values[0]) is None:
                raise ValueError(_ERR_LINK)
            expires_at = min(expires_at, float(int(expiry_values[0], 16)))
        if expires_at <= now:
            raise ValueError(_ERR_EXPIRED)
        return raw_url, expires_at

    def register(self, attachment: object) -> tuple[SourceReference, TrackMeta]:
        """Admit one real Discord attachment and return an opaque playback reference."""
        now = self._clock()
        self._purge_expired(now)
        if len(self._entries) >= self._max_entries:
            raise ValueError(_ERR_CAPACITY)
        size = getattr(attachment, "size", None)
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= _MAX_ATTACHMENT_SIZE:
            raise ValueError(_ERR_SIZE)
        filename, title = self._file_details(attachment)
        url, expires_at = self._validated_url(attachment, filename, now)

        identifier = secrets.token_hex(16)
        while identifier in self._entries:
            identifier = secrets.token_hex(16)
        reference = SourceReference(SourceKind.ATTACHMENT, identifier)
        self._entries[identifier] = _StoredAttachment(url, expires_at)
        metadata: TrackMeta = {
            "title": clamp_text(title, 200), "artist": "Uploaded file", "album": None,
            "duration": 0, "quality": "File audio", "audio_resolution": None,
            "track_id": None, "image": None, "share_url": None, "source": "Uploaded file",
        }
        return reference, metadata

    async def resolve(self, reference: SourceReference) -> ResolvedSource:
        """Return a live CDN URL only for a registered opaque attachment reference."""
        now = self._clock()
        self._purge_expired(now)
        if (
            not isinstance(reference, SourceReference)
            or reference.kind is not SourceKind.ATTACHMENT
            or _TOKEN.fullmatch(reference.identifier) is None
        ):
            raise AttachmentResolutionError()
        stored = self._entries.get(reference.identifier)
        if stored is None:
            raise AttachmentResolutionError()
        return ResolvedSource(stored.url, {})

    async def close(self) -> None:
        """Forget every private signed URL during cog shutdown."""
        self._entries.clear()
