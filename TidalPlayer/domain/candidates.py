"""Provider-neutral immutable candidates used before Tidal matching."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NormalizedCandidate:
    title: str
    artists: tuple[str, ...]
    isrc: str | None = None
    duration: int | None = None
    source: str = ""

    @property
    def query(self) -> str:
        return " ".join(part for part in (self.title, *self.artists) if part).strip()
