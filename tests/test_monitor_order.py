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
