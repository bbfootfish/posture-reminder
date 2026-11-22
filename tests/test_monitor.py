

def test_range_buckets_supports_arbitrary_window_and_one_second_granularity(tmp_path):
    """The interactive timeline needs an explicit [start, end] window and
    1-second granularity when zoomed far enough in."""
    from datetime import datetime, timedelta

    from posture.monitor import MonitorHistory

    history = MonitorHistory(tmp_path / "m.sqlite3", interval_seconds=0.0)
    base = datetime(2026, 11, 20, 18, 0, 0)
    for i in range(10):
        history.maybe_add(
            base + timedelta(seconds=i),
            {
                "smoothed": {"s_norm": 0.07, "head_up": 0.3 + i / 100, "head_fwd": 150.0},
                "status": "ok",
                "issue": None,
                "window": {"occupancy": 0.0},
            },
            person=True,
        )

    rows = history.range_buckets(
        start=base, end=base + timedelta(seconds=9), bucket_seconds=1
    )
    assert len(rows) == 10
    assert rows[0]["t"] == "2026-11-20T18:00:00"
    assert rows[1]["t"] == "2026-11-20T18:00:01"
    assert all(row["samples"] == 1 for row in rows)
    history.close()


def test_range_buckets_respects_window_bounds(tmp_path):
    from datetime import datetime, timedelta

    from posture.monitor import MonitorHistory

    history = MonitorHistory(tmp_path / "m.sqlite3", interval_seconds=0.0)
    base = datetime(2026, 11, 20, 18, 0, 0)
    for i in range(20):
        history.maybe_add(
            base + timedelta(seconds=i),
            {"smoothed": {"s_norm": 0.07, "head_up": 0.3, "head_fwd": 150.0},
             "status": "ok", "issue": None, "window": {"occupancy": 0.0}},
            person=True,
        )
    rows = history.range_buckets(
        start=base + timedelta(seconds=5),
        end=base + timedelta(seconds=9),
        bucket_seconds=1,
    )
    # 5..9 inclusive
    assert len(rows) == 5
    history.close()
