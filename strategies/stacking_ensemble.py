"""
Stacking ensemble strategy for Polymarket prediction markets.

Based on Navnoor Bawa's approach:
- 5 base estimators (XGBoost, LightGBM, HistGradientBoosting, ExtraTrees, RF)
- LogisticRegression meta-learner
- Platt scaling calibration
- 10 features: current_price, volume_24h, liquidity, RSI, momentum,
  order_imbalance, volatility, 1d_change, 1w_change, spread
- Fractional Kelly Criterion position sizing (quarter Kelly, max 5% bankroll)
"""

import numpy as np
import pandas as pd
from sklearn.ensemble import (
    ExtraTreesClassifier,
    HistGradientBoostingClassifier,
    RandomForestClassifier,
    StackingClassifier,
)
from sklearn.calibration import CalibratedClassifierCV
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import cross_val_score, TimeSeriesSplit
from sklearn.metrics import brier_score_loss
from xgboost import XGBClassifier
from lightgbm import LGBMClassifier


FEATURE_COLUMNS = [
    "current_price",
    "volume_24h",
    "liquidity",
    "rsi",
    "momentum",
    "order_imbalance",
    "volatility",
    "one_day_change",
    "one_week_change",
    "spread",
]


def build_base_estimators() -> list:
    """Return the 5 base estimators for the stacking ensemble."""
    return [
        ("xgb", XGBClassifier(
            n_estimators=100, max_depth=4, learning_rate=0.1,
            subsample=0.8, use_label_encoder=False, eval_metric="logloss",
            verbosity=0,
        )),
        ("lgbm", LGBMClassifier(
            n_estimators=100, max_depth=4, learning_rate=0.1, num_leaves=15,
            verbose=-1,
        )),
        ("hgb", HistGradientBoostingClassifier(
            max_iter=100, max_depth=4, learning_rate=0.1,
        )),
        ("et", ExtraTreesClassifier(n_estimators=100, max_depth=6)),
        ("rf", RandomForestClassifier(n_estimators=100, max_depth=6)),
    ]


def build_stacking_model() -> StackingClassifier:
    """Build the full stacking ensemble with logistic regression meta-learner."""
    return StackingClassifier(
        estimators=build_base_estimators(),
        final_estimator=LogisticRegression(C=1.0, max_iter=1000),
        cv=5,
        stack_method="predict_proba",
    )


def calibrate_model(model, X: np.ndarray, y: np.ndarray) -> CalibratedClassifierCV:
    """Apply Platt scaling calibration for reliable probability estimates."""
    return CalibratedClassifierCV(model, method="sigmoid", cv=2).fit(X, y)


def kelly_criterion(prob: float, market_price: float, fraction: float = 0.25) -> float:
    """
    Calculate fractional Kelly Criterion position size.
    
    f* = (bp - q) / b  where b=net odds, p=win prob, q=1-p
    Then apply fraction (0.25 = quarter Kelly) and cap at max 5%.
    
    Args:
        prob: Model's estimated true probability
        market_price: Current market price (0-1)
        fraction: Kelly fraction (0.25 = quarter Kelly)
    
    Returns:
        Position size as fraction of bankroll (0-0.05)
    """
    if prob <= market_price:
        return 0.0  # No edge
    
    # Net odds: if we buy YES at market_price, we get 1/market_price - 1 on a win
    b = (1.0 / market_price) - 1.0  # net odds
    p = prob
    q = 1.0 - p
    
    kelly = (b * p - q) / b
    fractional = kelly * fraction
    
    # Cap at 5% of bankroll
    return min(max(fractional, 0.0), 0.05)


def prepare_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Engineer the 10 features from raw market data.
    Expects columns: price, volume, liquidity, bid, ask, and timestamp.
    """
    features = pd.DataFrame(index=df.index)
    
    # Direct mappings
    features["current_price"] = df.get("price", df.get("current_price", 0.5))
    features["volume_24h"] = df.get("volume_24h", df.get("volume", 0))
    features["liquidity"] = df.get("liquidity", 0)
    
    # RSI (14-period)
    if "price" in df.columns:
        delta = df["price"].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss.replace(0, 1e-10)
        features["rsi"] = (100 - (100 / (1 + rs))) / 100  # Normalize to 0-1
    
    # Momentum (rate of change)
    if "price" in df.columns:
        features["momentum"] = df["price"].pct_change().fillna(0)
    
    # Order imbalance
    if all(c in df.columns for c in ["buy_volume", "sell_volume"]):
        total = df["buy_volume"] + df["sell_volume"].replace(0, 1e-10)
        features["order_imbalance"] = (df["buy_volume"] - df["sell_volume"]) / total
    else:
        features["order_imbalance"] = 0.0
    
    # Volatility
    if "price" in df.columns:
        features["volatility"] = df["price"].rolling(24).std().fillna(0)
    
    # Price changes
    if "price" in df.columns:
        features["one_day_change"] = df["price"].pct_change(24).fillna(0)
        features["one_week_change"] = df["price"].pct_change(168).fillna(0)
    
    # Spread
    if all(c in df.columns for c in ["bid", "ask"]):
        mid = (df["bid"] + df["ask"]) / 2
        features["spread"] = ((df["ask"] - df["bid"]) / mid.replace(0, 1e-10)).fillna(0)
    elif "spread" in df.columns:
        features["spread"] = df["spread"]
    else:
        features["spread"] = 0.0
    
    return features[FEATURE_COLUMNS].fillna(0)


def evaluate_model(model, X: np.ndarray, y: np.ndarray, n_splits: int = 5) -> dict:
    """Evaluate model with time-series cross-validation."""
    tscv = TimeSeriesSplit(n_splits=n_splits)
    scores = cross_val_score(model, X, y, cv=tscv, scoring="accuracy")
    
    # Brier score on last fold
    for train_idx, test_idx in tscv.split(X):
        X_train, X_test = X[train_idx], X[test_idx]
        y_train, y_test = y[train_idx], y[test_idx]
    
    model.fit(X_train, y_train)
    probs = model.predict_proba(X_test)[:, 1]
    brier = brier_score_loss(y_test, probs)
    
    return {
        "cv_accuracy_mean": scores.mean(),
        "cv_accuracy_std": scores.std(),
        "brier_score": brier,
    }
