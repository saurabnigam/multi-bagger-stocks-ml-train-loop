"""Yahoo to NSE sector crosswalk mapping."""
from typing import Tuple, Optional
import pandas as pd


def yahoo_to_nse(
    sector: str,
    industry: Optional[str],
    rules: pd.DataFrame,
) -> Tuple[str, float]:
    """Map Yahoo sector and industry to NSE sector with confidence score.
    
    Returns (nse_sector, confidence). If unmapped, returns ('UNCLASSIFIED', 0.0).
    """
    if rules.empty or not sector:
        return ("UNCLASSIFIED", 0.0)

    # 1. Exact match on both sector and industry
    if industry:
        match = rules[(rules["yahoo_sector"] == sector) & (rules["yahoo_industry"] == industry)]
        if not match.empty:
            row = match.iloc[0]
            return (str(row["nse_sector"]), float(row["confidence"]))

    # 2. Match on sector only (where yahoo_industry is empty or None or NaN)
    match_sec = rules[
        (rules["yahoo_sector"] == sector)
        & (rules["yahoo_industry"].isna() | (rules["yahoo_industry"] == ""))
    ]
    if not match_sec.empty:
        row = match_sec.iloc[0]
        return (str(row["nse_sector"]), float(row["confidence"]))

    return ("UNCLASSIFIED", 0.0)
