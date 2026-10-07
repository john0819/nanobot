"""Semantic progress guards independently tested from iteration limits."""

from testpilot.progress import ProgressState, advance


def test_repeated_observations_stop_on_third_repeat():
    initial = advance(ProgressState(), ["a", "a"])
    assert not initial.stalled
    assert advance(initial, ["a"]).stalled


def test_a_b_oscillation_detected_without_identical_consecutive_calls():
    assert advance(ProgressState(), ["a", "b", "a", "b"]).stalled


def test_novel_observations_do_not_stall_and_history_is_bounded():
    updated = advance(ProgressState(), [str(value) for value in range(20)])
    assert not updated.stalled and len(updated.history) == 4


def test_stalled_progress_is_not_reset_by_another_observation():
    assert advance(advance(ProgressState(), ["a", "a", "a"]), ["new"]).stalled
