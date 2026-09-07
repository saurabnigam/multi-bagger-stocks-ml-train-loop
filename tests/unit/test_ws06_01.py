import math
import numpy as np
import pandas as pd
import pytest

from quant.model.learn import allocate_units, fit_family_weights


def test_exact_weight_fit_golden_case(cfg, spec_case):
    c = spec_case("weight_fit")
    history = pd.DataFrame([c["mean_ic"]] * c["n_months"], columns=c["families"])
    units, diagnostics = fit_family_weights(history, cfg)
    assert [units[k] for k in c["families"]] == c["expected_units"]
    assert sum(units.values()) == 10000
    assert abs(diagnostics["alpha"] - c["expected_alpha"]) < 1e-12
    assert diagnostics["n_months"] == 12
    assert abs(diagnostics["n_eff"] - 4.0) < 1e-12
    assert diagnostics["gate"] == "open"


def test_equal_weights_golden_case(spec_case):
    c = spec_case("equal_weights")
    target = {k: 1.0 / len(c["families"]) for k in c["families"]}
    units = allocate_units(target)
    assert [units[k] for k in c["families"]] == c["expected_units"]
    assert sum(units.values()) == 10000


def test_common_finite_intersection_determines_n(cfg):
    dates = [f"2026-{m:02d}-28" for m in range(1, 16)]
    families = ["a", "b", "c", "d", "e", "f"]
    data = np.full((15, 6), 0.05)
    # Put NaNs in specific rows
    # date index 0 and 1 NaN for family a
    data[0, 0] = np.nan
    data[1, 0] = np.nan
    # date index 2 NaN for family b
    data[2, 1] = np.nan
    df = pd.DataFrame(data, index=dates, columns=families)

    units, diagnostics = fit_family_weights(df, cfg)
    assert diagnostics["n_months"] == 12  # 15 - 3 dates dropped
    assert abs(diagnostics["n_eff"] - 4.0) < 1e-12
    assert diagnostics["gate"] == "open"
    assert diagnostics["omitted_dates"]["a"] == ["2026-01-28", "2026-02-28"]
    assert diagnostics["omitted_dates"]["b"] == ["2026-03-28"]
    assert diagnostics["omitted_dates"]["c"] == []
    assert sum(units.values()) == 10000


def test_below_12_matured_3m_rows_returns_ew(cfg, spec_case):
    c_ew = spec_case("equal_weights")
    families = c_ew["families"]
    # 11 rows: below 12 matured 3M rows
    skewed_ic = [0.10, 0.05, 0.08, 0.02, 0.01, 0.03]
    history = pd.DataFrame([skewed_ic] * 11, columns=families)

    units, diagnostics = fit_family_weights(history, cfg)
    assert diagnostics["n_months"] == 11
    assert abs(diagnostics["n_eff"] - 11.0 / 3.0) < 1e-12
    assert diagnostics["gate"] == "closed"
    assert [units[k] for k in families] == c_ew["expected_units"]
    assert sum(units.values()) == 10000


def test_all_nonpositive_means_return_ew_even_with_gate_open(cfg, spec_case):
    c_ew = spec_case("equal_weights")
    families = c_ew["families"]
    # 15 rows: gate open (n_eff = 5 >= 4)
    nonpos_ic = [-0.02, -0.05, 0.0, -0.01, -0.03, -0.04]
    history = pd.DataFrame([nonpos_ic] * 15, columns=families)

    units, diagnostics = fit_family_weights(history, cfg)
    assert diagnostics["n_months"] == 15
    assert abs(diagnostics["n_eff"] - 5.0) < 1e-12
    assert diagnostics["gate"] == "open"
    # Must return EW even with gate open
    assert [units[k] for k in families] == c_ew["expected_units"]
    assert sum(units.values()) == 10000


def test_bounds_hold_across_2_to_14_families():
    for f_count in range(2, 15):
        families = [f"fam_{i:02d}" for i in range(f_count)]
        # Create heavily skewed target to test boundary clipping
        target = {fam: 0.01 / (f_count - 1) for fam in families}
        target[families[0]] = 0.99

        lower_bound = math.ceil(10000 * 0.5 / f_count)
        upper_bound = math.floor(10000 * 2.0 / f_count)

        units = allocate_units(target, floor_mult=0.5, cap_mult=2.0, total=10000)

        assert sum(units.values()) == 10000, f"Sum != 10000 for F={f_count}"
        for fam, u in units.items():
            assert lower_bound <= u <= upper_bound, (
                f"Unit {u} out of [{lower_bound}, {upper_bound}] for {fam} (F={f_count})"
            )


def test_repeats_are_identical(cfg):
    dates = [f"2026-{m:02d}-28" for m in range(1, 15)]
    families = ["fam_a", "fam_b", "fam_c", "fam_d"]
    rng = np.random.default_rng(42)
    data = rng.normal(0.03, 0.05, size=(14, 4))
    df = pd.DataFrame(data, index=dates, columns=families)

    units1, diag1 = fit_family_weights(df, cfg)
    units2, diag2 = fit_family_weights(df, cfg)

    assert units1 == units2
    assert diag1 == diag2
