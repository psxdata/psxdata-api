"""Shared fixtures for API tests."""
from dataclasses import dataclass

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from api import telemetry
from api.cache.historical import HistoricalService
from api.cache.store import MemoryLRU, RedisStore, TieredStore
from api.main import app
from api.proxy import ProxyPassthrough
from api.upstream import UpstreamBudget


@pytest.fixture(autouse=True)
def proxy_passthrough_disabled(tmp_path) -> ProxyPassthrough:
    """Start every test with proxy passthrough off, an empty client pool and an empty disk cache.

    Every PSX fetch checks the SDK disk cache first, so a developer's real cache must not leak in.
    """
    passthrough = ProxyPassthrough(False, cache_dir=str(tmp_path / "psxdata-cache"))
    app.state.proxy_passthrough = passthrough
    return passthrough


@pytest.fixture(autouse=True)
def fresh_upstream_budget() -> UpstreamBudget:
    """Give every test unspent PSX fetch budgets."""
    budget = UpstreamBudget()
    app.state.upstream_budget = budget
    return budget


@pytest.fixture(autouse=True)
def fresh_historical_service() -> HistoricalService:
    """Give every test an empty, Redis-less /historical cache so cached data never leaks."""
    service = HistoricalService(TieredStore(MemoryLRU(), RedisStore(None)))
    app.state.historical_service = service
    return service


@dataclass
class OtelCapture:
    spans: InMemorySpanExporter
    logs: InMemoryLogRecordExporter

    def server_spans(self) -> list[ReadableSpan]:
        return [s for s in self.spans.get_finished_spans() if s.kind.name == "SERVER"]

    def span(self, name: str) -> ReadableSpan:
        matches = [s for s in self.spans.get_finished_spans() if s.name == name]
        assert len(matches) == 1, f"expected one {name!r} span, got {len(matches)}"
        return matches[0]

    def lines(self) -> list[str]:
        return [telemetry.span_line(s) for s in self.spans.get_finished_spans()] + [
            telemetry.log_line(r) for r in self.logs.get_finished_logs()
        ]


@pytest.fixture(scope="session")
def _otel_capture() -> OtelCapture:
    """Attach in-memory exporters to the app's providers once (processors can't be removed)."""
    capture = OtelCapture(InMemorySpanExporter(), InMemoryLogRecordExporter())
    telemetry.TELEMETRY.tracer_provider.add_span_processor(SimpleSpanProcessor(capture.spans))
    telemetry.TELEMETRY.logger_provider.add_log_record_processor(
        SimpleLogRecordProcessor(capture.logs)
    )
    return capture


@pytest.fixture
def otel(_otel_capture: OtelCapture) -> OtelCapture:
    _otel_capture.spans.clear()
    _otel_capture.logs.clear()
    return _otel_capture
