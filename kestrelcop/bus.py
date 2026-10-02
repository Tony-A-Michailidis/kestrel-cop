"""In-process pub/sub. Feeds publish, the web broadcaster and the TAK publisher subscribe.

Slow subscribers never block producers: when a subscriber's queue is full the oldest item is dropped.
"""

from __future__ import annotations

import asyncio
from typing import Any


class Bus:
    def __init__(self, maxsize: int = 5000) -> None:
        self._subs: list[asyncio.Queue] = []
        self._maxsize = maxsize

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subs:
            self._subs.remove(q)

    def publish(self, topic: str, payload: Any) -> None:
        for q in list(self._subs):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait((topic, payload))

    @property
    def subscribers(self) -> int:
        return len(self._subs)
