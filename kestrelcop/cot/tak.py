"""Publish the picture to a TAK server (TAK Server, FreeTAKServer, OpenTAKServer, GoATAK...) over PyTAK.

Anything published on the bus as a track, event or alert is encoded to CoT and pushed to PyTAK's
transmit queue. Transport (tcp/tls/udp, client certificates) is PyTAK's job; configure it with tak.url
and the tak.tls_* settings. PyTAK is imported lazily so the rest of Kestrel runs without it.
"""

from __future__ import annotations

import asyncio
import logging
from configparser import ConfigParser
from typing import Any

from ..bus import Bus
from ..config import TakConfig
from ..models import Alert, Event, Track
from .encode import alert_to_cot, event_to_cot, track_to_cot

log = logging.getLogger("kestrel.tak")


def encode_bus_message(topic: str, payload: Any, tak: TakConfig) -> bytes | None:
    """Bus message -> CoT bytes, or None for messages TAK does not need."""
    if topic == "track" and isinstance(payload, Track):
        stale = tak.stale_seconds_air if payload.domain == "air" else tak.stale_seconds_sea
        return track_to_cot(payload, stale_seconds=stale)
    if topic == "event" and isinstance(payload, Event):
        return event_to_cot(payload)
    if topic == "alert" and isinstance(payload, Alert) and tak.publish_alerts:
        return alert_to_cot(payload)
    return None


def _make_sender_class():
    """Built at runtime because the base class comes from the optional pytak package."""
    import pytak

    class BusSender(pytak.QueueWorker):
        """Reads Kestrel bus messages, writes CoT bytes into PyTAK's tx_queue."""

        def __init__(self, tx_queue: asyncio.Queue, config: Any, bus_queue: asyncio.Queue, tak: TakConfig, stats: dict) -> None:
            super().__init__(tx_queue, config)
            self.bus_queue = bus_queue
            self.tak = tak
            self.stats = stats

        async def handle_data(self, data: bytes) -> None:
            await self.put_queue(data)

        async def run(self, number_of_iterations: int = -1) -> None:  # noqa: ARG002 (pytak signature)
            while True:
                topic, payload = await self.bus_queue.get()
                data = encode_bus_message(topic, payload, self.tak)
                if data:
                    await self.handle_data(data)
                    self.stats["sent"] += 1

    return BusSender


class TakPublisher:
    def __init__(self, tak: TakConfig, bus: Bus) -> None:
        self.tak = tak
        self.bus = bus
        self.stats: dict[str, Any] = {"sent": 0, "connections": 0, "last_error": None, "connected": False}

    def _pytak_config(self) -> Any:
        parser = ConfigParser()
        section: dict[str, str] = {"COT_URL": self.tak.url}
        if self.tak.tls_client_cert:
            section["PYTAK_TLS_CLIENT_CERT"] = self.tak.tls_client_cert
        if self.tak.tls_client_key:
            section["PYTAK_TLS_CLIENT_KEY"] = self.tak.tls_client_key
        if self.tak.tls_dont_verify:
            section["PYTAK_TLS_DONT_VERIFY"] = "1"
            section["PYTAK_TLS_DONT_CHECK_HOSTNAME"] = "1"
        parser["kestrel"] = section
        return parser["kestrel"]

    async def run(self) -> None:
        try:
            import pytak
        except ImportError:
            self.stats["last_error"] = "python package 'pytak' is not installed"
            log.error("TAK output requested but pytak is not installed (pip install pytak)")
            return
        BusSender = _make_sender_class()
        backoff = 2.0
        while True:
            bus_queue = self.bus.subscribe()
            clitool = None
            try:
                config = self._pytak_config()
                clitool = pytak.CLITool(config)
                await clitool.setup()
                self.stats["connections"] += 1
                self.stats["connected"] = True
                self.stats["last_error"] = None
                log.info("TAK publisher connected to %s", self.tak.url)
                backoff = 2.0
                clitool.add_tasks({BusSender(clitool.tx_queue, config, bus_queue, self.tak, self.stats)})
                await clitool.run()
                log.warning("TAK publisher loop ended; reconnecting")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the picture alive whatever the transport does
                self.stats["last_error"] = str(exc)
                log.warning("TAK publisher error (%s); retry in %.0fs", exc, backoff)
            finally:
                self.stats["connected"] = False
                self.bus.unsubscribe(bus_queue)
                if clitool is not None:
                    await self._shutdown(clitool)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    @staticmethod
    async def _shutdown(clitool: Any) -> None:
        """PyTAK's CLITool.run only cancels its workers when one of them fails. When we are cancelled, or the
        transport drops and we reconnect, its TX/RX workers and our BusSender would otherwise stay behind as
        orphan tasks (one more set per reconnect), so cancel and reap them here."""
        tasks = [t for t in getattr(clitool, "running_tasks", ()) if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
