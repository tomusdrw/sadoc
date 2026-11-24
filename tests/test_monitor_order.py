from sadoc.state import State


def test_monitor_prioritizes_unseen_then_least_recently_checked(tmp_path):
    state = State(tmp_path / "state.db")
    try:
        state.record_pr("o/r", 1, last_checked=100, created_at=1)
        state.record_pr("o/r", 2, last_checked=200, created_at=2)
        state.record_pr("o/r", 3, created_at=3)
        assert [r["number"] for r in state.open_prs()] == [3, 1, 2]
        assert [r["number"] for r in state.open_prs("o/r")] == [3, 1, 2]
    finally:
        state.close()


def test_existing_doctor_prs_recover_their_number_when_state_reopens(tmp_path):
    path = tmp_path / "state.db"
    with_old_rows = State(path)
    with_old_rows.record_pr(
        "o/r", 0, tier="D", branch="fix/r/old", url="https://github.com/o/r/pull/684"
    )
    with_old_rows.record_pr(
        "o/s", 0, tier="D", branch="fix/s/old", url="https://github.com/o/s/pull/23"
    )
    with_old_rows.record_pr("o/s", 23, tier="D", branch="fix/s/new", state="closed")
    with_old_rows.close()

    for _ in range(2):
        state = State(path)
        assert [(r["repo"], r["number"], r["branch"]) for r in state.open_prs()] == [
            ("o/r", 684, "fix/r/old")
        ]
        state.close()
