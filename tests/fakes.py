"""Explicit offline network dependencies for parser/ledger unit tests."""
from job_radar.safety import AccessBlocked, AccessState
from job_radar.storage import JobStore


class MemoryGate:
    def __init__(self):
        self.state = AccessState(mode="ready", manual_release_required=False)

    def read_state(self):
        return self.state

    def require_online(self, enabled=True):
        if not enabled or self.state.mode != "ready":
            raise AccessBlocked("offline test gate")
        return self.state

    def pause(self, reason, resume_after=None, source="test"):
        self.state = AccessState(mode="paused", reason=reason, manual_release_required=True)
        return self.state


def configure_offline_collector(collector, ledger=None):
    # Production never guesses whether a Page is fake. Tests supply every dependency.
    collector.config.network_enabled = True
    collector.gate = MemoryGate()
    collector.ledger = ledger if ledger is not None else JobStore(":memory:")
    return collector
