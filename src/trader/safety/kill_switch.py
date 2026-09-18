from threading import Event

from trader.storage.repository import Repository, SafetyState


class KillSwitch:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self._runtime_stop = Event()

    @property
    def active(self) -> bool:
        # Read the persistent flag on every check to observe other processes.
        return self._runtime_stop.is_set() or self.repository.safety_state().killed

    def kill(self) -> SafetyState:
        return self.trip("Operator requested stop", critical=False)

    def trip(self, reason: str, *, critical: bool = True) -> SafetyState:
        self._runtime_stop.set()  # Even a failed disk write stops this process.
        return self.repository.set_killed(True, reason, critical=critical)

    def resume(self) -> SafetyState:
        state = self.repository.set_killed(False, "Operator explicitly resumed")
        self._runtime_stop.clear()
        return state
