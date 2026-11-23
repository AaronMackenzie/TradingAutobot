"""Signal functions for the GOLDBEES autobot.

Every function takes a pandas DataFrame `data` with at least a 'Close' column (oldest row first) and
returns True/False for the LATEST row. Nothing here talks to the broker except fetch_historical_data().

Changes from the original file (see README, "Fixes made"):
  * no login or network call at import time; the Kite session is created by get_kite()
  * `data` is a parameter, not a module global that was never converted to a DataFrame
  * the signal is read from the latest row; the original used `.any()` over the whole history, so a
    single old row that satisfied the rule made the signal True for ever
  * the Bollinger, envelope and standard-deviation rules referred to columns they never created
  * hma() now computes a real Hull moving average (the original returned a plain 4-bar mean)
  * the KNN rule builds its own features and target (the original used columns that do not exist)
"""
import os
import datetime

import numpy as np
import pandas as pd
from sklearn.neighbors import KNeighborsClassifier
from pyswarm import pso

from kiteapi import KiteApp

INSTRUMENT_TOKEN = 9604354  # token the original used for its historical-data request
INTERVAL = "5minute"


# ------------------------------------------------------------------ data access
def get_kite():
    """Create the Kite client from the KITE_ENCTOKEN environment variable (never stored in source)."""
    enctoken = os.environ.get('KITE_ENCTOKEN')
    if not enctoken:
        raise SystemExit('Set the KITE_ENCTOKEN environment variable first (see README).')
    return KiteApp(enctoken=enctoken)


def fetch_historical_data(kite, instrument_token=INSTRUMENT_TOKEN, days=7, interval=INTERVAL):
    """Last `days` days of candles as a DataFrame with Open/High/Low/Close/Volume columns, or None on failure."""
    to_datetime = datetime.datetime.now()
    from_datetime = to_datetime - datetime.timedelta(days=days)
    records = kite.historical_data(instrument_token, from_datetime, to_datetime, interval, continuous=False, oi=False)
    if not records:
        return None
    df = pd.DataFrame(records).rename(columns={'date': 'Date', 'open': 'Open', 'high': 'High', 'low': 'Low',
                                               'close': 'Close', 'volume': 'Volume'})
    return df


def _last(condition):
    """Value of a boolean Series at the latest row; False when the indicator is not defined yet (NaN)."""
    if len(condition) == 0:
        return False
    return bool(condition.iloc[-1])


# ------------------------------------------------------------------ moving average (50 / 200)
def _ma_pair(data, short_window, long_window):
    return (data['Close'].rolling(window=short_window).mean(),
            data['Close'].rolling(window=long_window).mean())


def moving_average_buy_signal(data, short_window=50, long_window=200):
    short_ma, long_ma = _ma_pair(data, short_window, long_window)
    return _last(short_ma > long_ma)


def moving_average_sell_signal(data, short_window=50, long_window=200):
    short_ma, long_ma = _ma_pair(data, short_window, long_window)
    return _last(short_ma <= long_ma)


# ------------------------------------------------------------------ Bollinger bands (20, 2)
def _lower_band(data, window, num_std_dev):
    close = data['Close']
    return close.rolling(window=window).mean() - num_std_dev * close.rolling(window=window).std()


def bollinger_bands_buy_signal(data, window=20, num_std_dev=2):
    return _last(data['Close'] < _lower_band(data, window, num_std_dev))


def bollinger_bands_sell_signal(data, window=20, num_std_dev=2):
    # Same test as the original: sell once the close is no longer below the lower band.
    return _last(data['Close'] >= _lower_band(data, window, num_std_dev))


# ------------------------------------------------------------------ standard deviation (20, 2)
# Original rule: Close < rolling_std * -2, which compares a price with a negative number and can never be
# true. Interpretation used here: compare the one-bar RETURN with minus two rolling standard deviations
# of returns (a sudden drop of more than two sigma).
def _return_threshold(data, window, num_std_dev):
    ret = data['Close'].pct_change()
    return ret, -num_std_dev * ret.rolling(window=window).std()


def standard_deviation_buy_signal(data, window=20, num_std_dev=2):
    ret, thr = _return_threshold(data, window, num_std_dev)
    return _last(ret < thr)


def standard_deviation_sell_signal(data, window=20, num_std_dev=2):
    ret, thr = _return_threshold(data, window, num_std_dev)
    return _last(ret >= thr)


# ------------------------------------------------------------------ envelope (20, 5 %)
def _lower_envelope(data, window, percent):
    return data['Close'].rolling(window=window).mean() * (1 - percent)


def envelope_buy_signal(data, window=20, percent=0.05):
    return _last(data['Close'] < _lower_envelope(data, window, percent))


def envelope_sell_signal(data, window=20, percent=0.05):
    return _last(data['Close'] >= _lower_envelope(data, window, percent))


# ------------------------------------------------------------------ Hull moving average (20)
def _wma(series, window):
    weights = np.arange(1, window + 1, dtype=float)
    return series.rolling(window=window).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)


def hma(data, window=20):
    """Hull moving average: WMA(2 * WMA(n/2) - WMA(n), sqrt(n))."""
    close = data['Close']
    raw = 2 * _wma(close, int(window / 2)) - _wma(close, window)
    return _wma(raw, int(np.sqrt(window)))


def hma_buy_signal(data, window=20):
    return _last(data['Close'] > hma(data, window))


def hma_sell_signal(data, window=20):
    return _last(data['Close'] <= hma(data, window))


# ------------------------------------------------------------------ KNN with PSO-chosen k
def build_features(data):
    """Features: 1-, 5- and 10-bar returns. Target: 1 if the NEXT close is higher, else 0.
    (The original read Feature1..3 and Target columns that were never created.)"""
    close = data['Close']
    feats = pd.DataFrame({'Feature1': close.pct_change(1), 'Feature2': close.pct_change(5),
                          'Feature3': close.pct_change(10)})
    target = (close.shift(-1) > close).astype(int)
    return feats, target


def knn_prediction(data):
    """Fit on every row whose next close is known, choose k in [1, 10] with PSO, predict the latest row.
    Returns (prediction, k) where prediction is 1 (next bar up) or 0, or None when there is too little data."""
    feats, target = build_features(data)
    train = feats.iloc[:-1].dropna()
    if len(train) < 30:
        return None
    X, y = train, target.loc[train.index]
    latest = feats.iloc[[-1]]
    if latest.isna().any(axis=None):
        return None

    def objective_function(k):
        knn_model = KNeighborsClassifier(n_neighbors=int(np.ravel(k)[0]))
        knn_model.fit(X, y)
        accuracy = (knn_model.predict(X) == y).mean()   # training accuracy, as in the original
        return -accuracy                                # PSO minimises

    # k only takes 10 values, so a small swarm is enough (pyswarm's default 100 x 100 took ~25 s per call here)
    result = pso(objective_function, [1], [min(10, len(X))], swarmsize=10, maxiter=10)
    # pyswarm 0.6 returns (xopt, fopt); pyswarm 1.x returns a result object with .x
    best_k = result.x if hasattr(result, 'x') else result[0]
    k = int(np.ravel(best_k)[0])
    model = KNeighborsClassifier(n_neighbors=k)
    model.fit(X, y)
    return int(model.predict(latest)[0]), k


def knn_buy_signal(data):
    out = knn_prediction(data)
    return out is not None and out[0] == 1


def knn_sell_signal(data):
    out = knn_prediction(data)
    return out is not None and out[0] != 1


# ------------------------------------------------------------------ registry used by main.py
BUY_SIGNALS = [
    ('moving_average', moving_average_buy_signal),
    ('bollinger_bands', bollinger_bands_buy_signal),
    ('standard_deviation', standard_deviation_buy_signal),
    ('envelope', envelope_buy_signal),
    ('hma', hma_buy_signal),
    ('knn', knn_buy_signal),
]
SELL_SIGNALS = [
    ('moving_average', moving_average_sell_signal),
    ('bollinger_bands', bollinger_bands_sell_signal),
    ('standard_deviation', standard_deviation_sell_signal),
    ('envelope', envelope_sell_signal),
    ('hma', hma_sell_signal),
    ('knn', knn_sell_signal),
]
