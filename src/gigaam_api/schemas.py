from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Job(BaseModel):
    id: str
    resource_id: str
    kind: str
    state: Literal["queued", "running", "completed", "failed"]
    stage: str | None = None
    attempts: int
    error_code: str | None
    created_at: float
    updated_at: float


class Segment(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(ge=0, allow_inf_nan=False)
    text: str = Field(max_length=20000)
    speaker: str | None = None

    @model_validator(mode="after")
    def interval(self):
        if self.end < self.start:
            raise ValueError("end must be >= start")
        return self


class Transcript(BaseModel):
    language: str = "ru"
    model: str
    duration: float = Field(ge=0, allow_inf_nan=False)
    text: str = Field(max_length=2_000_000)
    segments: list[Segment] = Field(max_length=20000)

    @model_validator(mode="after")
    def timeline(self):
        ids = [s.id for s in self.segments]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate segment IDs")
        if any(s.end > self.duration + 0.1 for s in self.segments):
            raise ValueError("Segment exceeds audio duration")
        if any(b.start < a.start for a, b in zip(self.segments, self.segments[1:])):
            raise ValueError("Segments must be ordered")
        return self


class URLImport(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class Lease(BaseModel):
    lease_token: str = Field(pattern=r"^[a-f0-9]{32}$")


class Completion(Lease):
    result: Transcript


class Failure(Lease):
    code: str = Field(pattern=r"^[a-z0-9_]{1,80}$")
    retryable: bool = True
