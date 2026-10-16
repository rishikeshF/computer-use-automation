"""The control lease: operator commands and automation's check. Plain files, no browser."""

from pathlib import Path

import pytest

from cua.core.runlog import utc_now
from cua.handoff.control import (
    ControlError,
    ControlFile,
    Intervention,
    Lease,
    approve,
    decline,
    release,
    take,
)
from cua.surface.guarded import ControlLost


def waiting(cf: ControlFile) -> None:
    iv = Intervention(
        id="s4-1", kind="approval", capability="c@0.1.0", reason="r", requested_at=utc_now()
    )
    cf.update(lambda s: s.model_copy(update={"intervention": iv}))


def test_fresh_lease_is_held_by_automation(tmp_path: Path) -> None:
    cf = ControlFile.create(tmp_path, "run")
    state = cf.read()
    assert (state.owner, state.holder, state.epoch) == ("automation", "automation", 1)
    Lease(cf).check()  # does not raise


def test_take_stops_automation_before_its_next_action(tmp_path: Path) -> None:
    cf = ControlFile.create(tmp_path, "run")
    lease = Lease(cf)
    cf.update(take("rishi"))
    with pytest.raises(ControlLost, match="rishi holds control"):
        lease.check()


def test_release_alone_does_not_hand_control_back(tmp_path: Path) -> None:
    """Owner is automation again, but the epoch moved: the page may have changed. Automation
    acts again only after it re-checked where it is and adopted the new epoch."""
    cf = ControlFile.create(tmp_path, "run")
    lease = Lease(cf)
    cf.update(take("rishi"))
    state = cf.update(release("rishi"))
    assert (state.owner, state.epoch) == ("automation", 3)
    assert state.answer is not None and state.answer.intervention_id is None
    with pytest.raises(ControlLost, match="changed hands"):
        lease.check()
    lease.adopt(state.epoch)
    lease.check()


def test_answers_name_the_waiting_intervention(tmp_path: Path) -> None:
    cf = ControlFile.create(tmp_path, "run")
    waiting(cf)
    state = cf.update(approve("rishi", "s4-1"))
    assert state.answer is not None
    assert (state.answer.kind, state.answer.by, state.answer.intervention_id) == (
        "approved",
        "rishi",
        "s4-1",
    )


def test_commands_that_do_not_fit_the_state_are_refused(tmp_path: Path) -> None:
    cf = ControlFile.create(tmp_path, "run")
    with pytest.raises(ControlError, match="nothing to release"):
        cf.update(release("rishi"))
    with pytest.raises(ControlError, match="no intervention"):
        cf.update(approve("rishi", "s4-1"))
    with pytest.raises(ControlError, match="no intervention"):
        cf.update(decline("rishi", "s4-1"))
    cf.update(take("rishi"))
    with pytest.raises(ControlError, match="already holds"):
        cf.update(take("sam"))
    assert cf.read().epoch == 2  # refused commands changed nothing


def test_approval_for_another_intervention_is_refused(tmp_path: Path) -> None:
    """The operator looked at s4-1, but the run is now waiting on something else (or s4-1 was
    answered and a new request opened). A yes meant for one must never land on another."""
    cf = ControlFile.create(tmp_path, "run")
    waiting(cf)
    for command in (approve("rishi", "s5-2"), decline("rishi", "s5-2")):
        with pytest.raises(ControlError, match="not the waiting intervention"):
            cf.update(command)
    state = cf.read()
    assert state.answer is None and state.epoch == 1  # nothing changed
