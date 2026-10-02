"""Core data model. Everything a feed produces is one of these; everything downstream consumes them."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

Domain = Literal["air", "sea"]
Affiliation = Literal["friend", "neutral", "unknown", "suspect", "hostile"]
EventKind = Literal["fire", "quake", "alert", "zone"]
Severity = Literal["LOW", "MEDIUM", "HIGH"]
FeedMode = Literal["live", "demo", "off"]
FeedState = Literal["starting", "ok", "degraded", "down", "off"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Track(BaseModel):
    """A moving thing: an aircraft or a vessel."""

    uid: str  # stable identity, e.g. adsb-a1b2c3 / ais-367123456
    domain: Domain
    source: str  # feed name
    callsign: str | None = None
    lat: float
    lon: float
    alt_m: float | None = None
    speed_kts: float | None = None
    course_deg: float | None = None
    heading_deg: float | None = None
    affiliation: Affiliation = "neutral"
    function: str = "unknown"  # fixed_wing / rotary / mil_fixed_wing / merchant / fishing / ...
    sidc: str  # MIL-STD-2525C letter SIDC (15 chars)
    cot_type: str  # Cursor-on-Target type, e.g. a-n-A-C-F
    attrs: dict[str, Any] = Field(default_factory=dict)
    flags: list[str] = Field(default_factory=list)  # EMERGENCY / DARK / IN_ZONE:<name> / MILITARY
    ts: datetime = Field(default_factory=utcnow)  # time of last report
    first_seen: datetime = Field(default_factory=utcnow)
    trail: list[list[float]] = Field(default_factory=list)  # [[lon, lat], ...] oldest -> newest

    def age_seconds(self, now: datetime | None = None) -> float:
        return ((now or utcnow()) - self.ts).total_seconds()

    @property
    def label(self) -> str:
        return self.callsign or self.uid


class Event(BaseModel):
    """A point or area that is not a track: fire detection, earthquake, weather alert, user zone."""

    uid: str
    kind: EventKind
    source: str
    title: str
    lat: float
    lon: float
    geometry: dict[str, Any] | None = None  # GeoJSON geometry for areas
    severity: str | None = None
    magnitude: float | None = None
    cot_type: str = "b-m-p-s-m"
    attrs: dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=utcnow)
    expires: datetime | None = None


class Alert(BaseModel):
    """Something the rules engine (or the watch officer) wants a human to look at."""

    id: str
    severity: Severity
    rule: str
    title: str
    text: str
    subject_uid: str | None = None
    lat: float | None = None
    lon: float | None = None
    ts: datetime = Field(default_factory=utcnow)
    triage: str | None = None  # AI SPOTREP, attached asynchronously
    triage_mode: str | None = None  # "claude:<model>" or "mock"


class FeedStatus(BaseModel):
    name: str
    label: str
    mode: FeedMode = "off"
    status: FeedState = "off"
    last_message: datetime | None = None
    messages_total: int = 0
    rate_per_min: float = 0.0
    items: int = 0
    stale_after_s: float | None = None  # per-feed stale window; None means the global rules.stale_feed_seconds
    error: str | None = None
    detail: str | None = None


class Sitrep(BaseModel):
    text: str
    mode: str  # "claude:<model>" or "mock"
    generated_at: datetime = Field(default_factory=utcnow)
    citations: list[str] = Field(default_factory=list)
    unverified: list[str] = Field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0


class Answer(BaseModel):
    question: str
    text: str
    mode: str
    citations: list[str] = Field(default_factory=list)
    unverified: list[str] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
