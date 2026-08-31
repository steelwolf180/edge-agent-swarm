"""Minimal Phoenix/OTel wiring. One-time register() call + a lightweight
span decorator for agent step functions. Not full OpenInference semantic
conventions — just enough to get real spans into Phoenix.
"""
import atexit
import functools
import time

from phoenix.otel import register

# Phoenix's own helper: configures TracerProvider + OTLP exporter to
# localhost:4317 in one call. project_name shows up as the Phoenix project.
tracer_provider = register(
    project_name="edge-agent-swarm",
    endpoint="http://localhost:4317",
    batch=True,
)
tracer = tracer_provider.get_tracer("edge-agent-swarm")

# IMPORTANT: BatchSpanProcessor buffers spans and flushes on an interval —
# if the process exits (e.g. a short standalone script run) before that
# flush fires, spans are silently lost. force_flush() on exit avoids that.
atexit.register(lambda: tracer_provider.force_flush())


def traced_agent_step(agent_name: str, model: str | None = None):
    """Decorator: wraps an agent's run function in a span.

    Usage:
        @traced_agent_step("researcher", model="gemma-4-e4b-qat")
        def run_researcher(...):
            ...
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with tracer.start_as_current_span(agent_name) as span:
                span.set_attribute("agent.name", agent_name)
                if model:
                    span.set_attribute("agent.model", model)
                start = time.monotonic()
                try:
                    result = fn(*args, **kwargs)
                    span.set_attribute("agent.status", "success")
                    return result
                except Exception as e:
                    span.set_attribute("agent.status", "error")
                    span.set_attribute("agent.error", str(e))
                    raise
                finally:
                    span.set_attribute(
                        "agent.duration_s", round(time.monotonic() - start, 2)
                    )
        return wrapper
    return decorator