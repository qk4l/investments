from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas

from investments.calculators import compute_total_cost
from investments.currency import Currency
from investments.money import Money
from investments.ticker import Ticker
from investments.trade import Trade, FinishedTrade


@dataclass
class PortfolioElement:
    ticker: Ticker
    quantity: int
    average_price: Money = None


class TradesAnalyzer:
    def __init__(self, trades: Iterable[Trade]):
        self._finished_trades: List[FinishedTrade] = []
        self._trades: List[Trade] = []
        self._portfolio: List[PortfolioElement] = []
        self.trades = pandas.DataFrame(trades)
        self.analyze_trades(trades)

    def analyze_trades(self, trades: Iterable[Trade]):
        finished_trade_id = 1

        active_trades = _TradesFIFO()

        for trade in trades:
            self._trades.append(trade)

            total_profit = None

            quantity = trade.quantity
            while quantity != 0:

                matched_trade, q = active_trades.match(quantity, trade.ticker)
                if matched_trade is None:
                    assert q == 0
                    break
                assert q != 0

                total_cost = compute_total_cost(q, matched_trade.price, matched_trade.fee_per_piece)

                finished_trade = FinishedTrade(N=finished_trade_id, ticker=trade.ticker,
                                               trade_date=matched_trade.trade_date,
                                               settle_date=matched_trade.settle_date,
                                               quantity=q, price=matched_trade.price,
                                               fee=matched_trade.fee
                                               )

                self._finished_trades.append(finished_trade)

                q = -1 * q

                profit = compute_total_cost(q, trade.price, trade.fee_per_piece) + total_cost
                if total_profit is None:
                    total_profit = profit
                else:
                    total_profit += profit

                quantity -= q

            if total_profit is not None:
                q = trade.quantity - quantity
                self._finished_trades.append(FinishedTrade(
                    N=finished_trade_id, ticker=trade.ticker,
                    trade_date=trade.trade_date,
                    settle_date=trade.settle_date,
                    quantity=q, price=trade.price, fee=trade.fee
                ))
                finished_trade_id += 1

            if quantity != 0:
                active_trades.put(quantity, trade)

        # Create dictionaries to track total value and total quantity for each symbol
        total_value = {}
        total_quantity = {}

        # Calculate total value and total quantity for each symbol
        for ticker, group in self.trades.groupby('ticker'):
            total_value[ticker] = (group['price'] * group['quantity']).sum()
            total_quantity[ticker] = group['quantity'].sum()

        # Calculate the average holding price for each symbol
        average_holding_price = {}
        for ticker in total_value:
            if total_quantity[ticker]:
                average_holding_price[ticker] = total_value[ticker] / total_quantity[ticker]

        self._portfolio = [PortfolioElement(quantity=element['quantity'], ticker=element['ticker'],
                                            average_price=average_holding_price.get(element['ticker'])) for element in
                           active_trades.unmatched()]

    @property
    def finished_trades(self) -> List[FinishedTrade]:
        return self._finished_trades

    @property
    def final_portfolio(self) -> List[PortfolioElement]:
        return self._portfolio


class _TradesFIFO:
    def __init__(self):
        self._portfolio = defaultdict(list)

    @staticmethod
    def sign(v: int) -> int:
        assert v != 0
        return -1 if v < 0 else 1

    def put(self, quantity: int, trade: Trade):
        """
        Put trade to the storage.

        Args:
            quantity (int): The real quantity of the trade, >0 for BUY trades & <0 for SELL trades
            trade (Trade): Base trade, quantity field not used
        """
        assert self.sign(quantity) == self.sign(trade.quantity)
        assert abs(quantity) <= abs(trade.quantity)
        if self._portfolio[trade.ticker]:
            assert self.sign(quantity) == self.sign(self._portfolio[trade.ticker][0]['quantity'])

        self._portfolio[trade.ticker].append({
            'trade': trade,
            'quantity': quantity,
        })

    def match(self, quantity: int, ticker: Ticker) -> Tuple[Optional[Trade], int]:
        """
        Try to match trade.

        Args:
            quantity (int): The real quantity of the trade, >0 for BUY trades & <0 for SELL trades
            ticker (Ticker): Ticker to match

        Returns:
            matched_trade: A matched trade
            quantity: Real quantity 'used' from matched_trade
        """
        if (ticker not in self._portfolio) or (not self._portfolio[ticker]):
            return None, 0

        front = self._portfolio[ticker][0]
        fqsign = self.sign(front['quantity'])

        # only match BUY with SELL and vice versa
        if self.sign(quantity) == fqsign:
            return None, 0

        q = fqsign * min(abs(quantity), abs(front['quantity']))
        if q == front['quantity']:
            self._portfolio[ticker].pop(0)
        else:
            self._portfolio[ticker][0]['quantity'] = front['quantity'] - q

        return front['trade'], q

    def unmatched(self) -> List[Dict[str, Any]]:
        """
        Return basic information about unmatched trades (final portfolio).

        Returns:
            portfolio: Portfolio
        """
        ret = []
        for ticker, trades in self._portfolio.items():
            quantity = sum(v['quantity'] for v in trades)
            if quantity != 0:
                ret.append({'quantity': quantity, 'ticker': ticker})
        return ret
