"""
Background-thread helper for GUI: keep the tkinter mainloop responsive.

Pattern:
    bus = EventBus()
    bus.run(lambda emit: long_op(emit), tag="crawl")
    # in mainloop:
    self.after(100, lambda: bus.drain(self._handle_event))
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional


log = logging.getLogger(__name__)


@dataclass
class Event:
    tag: str
    kind: str  # "progress" | "log" | "done" | "error"
    payload: Any = None


Emit = Callable[[str, Any], None]


class EventBus:
    """
    Tiny thread-safe pub/sub used to ferry messages from worker threads to the
    tkinter mainloop.

    Workers call `emit(kind, payload)`. The GUI periodically calls `drain(handler)`
    from `after(...)` to consume pending events without blocking.
    """

    def __init__(self) -> None:
        self._q: "queue.Queue[Event]" = queue.Queue()

    def run(
        self,
        target: Callable[[Emit], Any],
        *,
        tag: str,
        on_thread_start: Optional[Callable[[], None]] = None,
    ) -> threading.Thread:
        """
        Runs `target(emit)` on a daemon thread. `emit("progress"|"log", payload)`
        delivers events to the GUI. On normal completion, an event of kind "done"
        with payload = return value is emitted. On unhandled exception, an "error"
        event with the exception is emitted.
        """

        def emit(kind: str, payload: Any = None) -> None:
            self._q.put(Event(tag=tag, kind=kind, payload=payload))

        def runner() -> None:
            if on_thread_start is not None:
                try:
                    on_thread_start()
                except Exception:
                    log.exception("on_thread_start failed for %s", tag)
            try:
                result = target(emit)
                emit("done", result)
            except Exception as exc:
                log.exception("worker %s crashed", tag)
                emit("error", exc)

        t = threading.Thread(target=runner, name=f"worker-{tag}", daemon=True)
        t.start()
        return t

    def drain(self, handler: Callable[[Event], None], *, max_events: int = 100) -> int:
        """Consume up to `max_events` events from the queue, calling `handler(event)`."""
        n = 0
        while n < max_events:
            try:
                ev = self._q.get_nowait()
            except queue.Empty:
                break
            try:
                handler(ev)
            except Exception:
                log.exception("EventBus handler raised for tag=%s kind=%s", ev.tag, ev.kind)
            n += 1
        return n
