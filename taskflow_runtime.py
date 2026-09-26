"""Local TaskFlow dispatcher. Does not launch provider processes."""
from __future__ import annotations


class Conflict(ValueError):
    pass


def tick(store, rpc_factory=None, launcher=None):
    """Advance local task board state. Provider execution is out of scope here."""
    if store.recovery_hold:
        return
    store.adopt_intake()
    store.reconcile()
    store.dispatch(managed=False)
    store.cycle.dispatch()
    from taskflow_retention import retain

    retain(store)
    from taskflow_learning import drain

    drain(store)
    from taskflow_learning_library import tick as retain_learning

    retain_learning(store)
