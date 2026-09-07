from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import numpy as np
import pandas as pd

from quant.config import load as load_config
from quant.db.core import apply_schema, connect
from quant.types import World


def make_world(tmp_path: Path, *, seed: int = 0, months: int = 48) -> World:
    """Creates a deterministic synthetic test world with 60 securities and 48 months."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "state.db"
    prices_db_path = tmp_path / "prices.db"

    conn = connect(db_path)
    apply_schema(conn, kind="state")
    conn.close()

    p_conn = connect(prices_db_path)
    apply_schema(p_conn, kind="prices")
    p_conn.close()

    cfg = load_config().with_paths(db=db_path, prices_db=prices_db_path, data_dir=tmp_path / "data")

    # Generate weekdays from 2022-01-01 for roughly 48 months (to 2025-12-31)
    start_dt = datetime(2022, 1, 1)
    end_dt = datetime(2025, 12, 31)
    cur = start_dt
    session_rows = []
    month_set = set()
    while cur <= end_dt:
        if cur.weekday() < 5:
            d_str = cur.strftime("%Y-%m-%d")
            session_rows.append({"date": d_str, "close_at": f"{d_str}T10:00:00.000000Z"})
            month_set.add(cur.strftime("%Y-%m"))
        cur += timedelta(days=1)

    sessions_df = pd.DataFrame(session_rows)
    month_list = sorted(month_set)[:months]

    security_ids = list(range(1, 61))
    groups = {sid: f"GROUP_{(sid - 1) // 10 + 1}" for sid in security_ids}

    # Deterministic RNG
    rng = np.random.default_rng(seed)

    # Pre-generate normals per session
    n_sessions = len(sessions_df)
    n_securities = len(security_ids)
    n_groups = 6

    market_normals = rng.normal(0, 0.003, size=n_sessions)
    group_normals = rng.normal(0, 0.003, size=(n_sessions, n_groups))
    individual_normals = rng.normal(0, 0.008, size=(n_sessions, n_securities))

    events = {
        "split_sec_5": {"date": "2023-06-15", "security_id": 5, "ratio": 6},
        "div_sec_7": {"date": "2023-09-20", "security_id": 7, "dividend": 100},
        "group_change_sec_9": {"date": "2024-07-31", "security_id": 9, "new_group": "GROUP_2"},
        "missing_assets_sec_11": {"fy": "2024", "security_id": 11},
        "delisting_sec_13": {"date": "2025-03-15", "security_id": 13},
    }

    return World(
        db_path=db_path,
        prices_db_path=prices_db_path,
        cfg=cfg,
        sessions=sessions_df,
        months=month_list,
        security_ids=security_ids,
        events=events,
    )
