"""The dashboard's dependency bundle.

PipelineContext carries everything the payload builders need
(cfg, store, executors, the liveness state dicts) - the builders
are module-level functions over it, testable without building
the whole flask app.
"""

from dataclasses import dataclass, field

from core.store import Store


@dataclass
class PipelineContext:
    """Explicit dependency bundle for the dashboard payloads.

    The heavy builders below are module-level functions over
    this context (second-review refactor): testable without
    building the whole app. Routes in create_app stay thin
    closures over it.
    """

    cfg: object
    store: Store
    risk: object
    executor: object
    account: object = None
    mode: str = ""
    config_path: str = None
    reader_state: dict = field(default_factory=lambda: {
        "channel": None, "ok": False, "last_seen": None,
    })
    # the feed client's liveness (alerts pulled from the info
    # server) - the dashboard's status line shows this when set
    feed_state: dict = field(default_factory=lambda: {
        "last_seen": None, "ok": None, "cursor": None,
    })
    summary_cache: dict = field(default_factory=lambda: {
        "ts": 0.0, "accounts": None, "ver": -1,
    })
