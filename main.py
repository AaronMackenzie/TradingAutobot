"""GOLDBEES autobot: poll candles, ask each strategy for a signal, buy when one fires, sell when one fires.

Orders are only sent when KITE_PLACE_ORDERS=1 is set in the environment; otherwise the loop prints what it
WOULD have done. The session token comes from KITE_ENCTOKEN (see README).
"""
import os
import time
import datetime

import StratDict as strategies

asset = "GOLDBEES"
POLL_SECONDS = 60          # the original asked every second, which means a 7-day candle request per second
PLACE_ORDERS = os.environ.get('KITE_PLACE_ORDERS') == '1'


def first_signal(signals, data):
    """Name of the first strategy (in list order) whose signal is True, or None. Mirrors the original elif chain."""
    for name, fn in signals:
        try:
            if fn(data):
                return name
        except Exception as e:  # one broken strategy must not stop the others
            print(f"{datetime.datetime.now()}: strategy {name} failed: {e}")
    return None


def decide(data, holding):
    """Return ('BUY'|'SELL'|None, strategy_name). Buys only when flat, sells only when holding."""
    if not holding:
        name = first_signal(strategies.BUY_SIGNALS, data)
        return ('BUY', name) if name else (None, None)
    name = first_signal(strategies.SELL_SIGNALS, data)
    return ('SELL', name) if name else (None, None)


def place_market_order(kite, transaction_type):
    if not PLACE_ORDERS:
        return "DRY RUN: no order sent (set KITE_PLACE_ORDERS=1 to send real orders)"
    return kite.place_order(variety=kite.VARIETY_REGULAR,
                            exchange=kite.EXCHANGE_NSE,
                            tradingsymbol=asset,
                            transaction_type=transaction_type,
                            quantity=1,
                            product=kite.PRODUCT_MIS,
                            order_type=kite.ORDER_TYPE_MARKET,
                            tag="TradeViaPython")


def run():
    kite = strategies.get_kite()
    holding = False
    while True:
        now = datetime.datetime.now()
        try:
            data = strategies.fetch_historical_data(kite)
            if data is None or data.empty:
                print(f"{now}: no candle data")
            else:
                action, name = decide(data, holding)
                if action == 'BUY':
                    order = place_market_order(kite, kite.TRANSACTION_TYPE_BUY)
                    holding = True
                    print(f"{now}: BUY signal from {name}: {order}")
                elif action == 'SELL':
                    # The original "sell" cancelled the buy order; an executed market order cannot be cancelled,
                    # so this places an opposite (SELL) market order instead.
                    order = place_market_order(kite, kite.TRANSACTION_TYPE_SELL)
                    holding = False
                    print(f"{now}: SELL signal from {name}: {order}")
                else:
                    print(f"{now}: no {'sell' if holding else 'buy'} signal")
        except Exception as e:
            print(f"{now}: error: {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run()
