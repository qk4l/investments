import copy
import logging
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import pandas

from investments.calculators import compute_total_cost
from investments.corporate_action import CorporateAction, CorporateActionKind
from investments.money import Money
from investments.ticker import Ticker
from investments.trade import Trade, FinishedTrade

Quantity = Union[int, Decimal]

# precision for quantities produced by corporate action ratios
_QUANTITY_PRECISION = Decimal('0.00000001')


def _normalize_quantity(value: Quantity) -> Quantity:
    value = Decimal(value).quantize(_QUANTITY_PRECISION)
    return int(value) if value == value.to_integral_value() else value.normalize()


@dataclass
class PortfolioElement:
    ticker: Ticker
    quantity: Quantity
    average_price: Money = None


class TradesAnalyzer:
    def __init__(self, trades: Iterable[Trade], corporate_actions: Optional[Iterable[CorporateAction]] = None,
                 spinoff_cost_allocation: Optional[Dict[str, Decimal]] = None):
        """
        Args:
            trades: trades sorted by trade date
            corporate_actions: splits, ISIN changes, share-for-share mergers and spinoffs
            spinoff_cost_allocation: child symbol -> share of the parent's cost moved to the child (0..1).
                UK rules apportion the cost by market values on the first day of trading.
        """
        self._finished_trades: List[FinishedTrade] = []
        self._trades: List[Trade] = []
        self._portfolio: List[PortfolioElement] = []
        self._corporate_actions = list(corporate_actions or [])
        self._spinoff_cost_allocation = spinoff_cost_allocation or {}
        self.trades = pandas.DataFrame(trades)
        self.analyze_trades(trades)

    def analyze_trades(self, trades: Iterable[Trade]):  # noqa: C901
        finished_trade_id = 1

        active_trades = _TradesFIFO()

        # corporate actions go before trades with the same timestamp (e.g. cash in lieu of fractional shares)
        events: List[Tuple[Any, int, Any]] = [(ca.date, 0, ca) for ca in self._corporate_actions]
        events += [(trade.trade_date, 1, trade) for trade in trades]
        events.sort(key=lambda x: (x[0], x[1]))

        for _, _, event in events:
            if isinstance(event, CorporateAction):
                self._apply_corporate_action(active_trades, event)
                continue

            trade = event
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
                                               fee=matched_trade.fee,
                                               account_id=matched_trade.account_id
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
                    quantity=q, price=trade.price, fee=trade.fee,
                    account_id=trade.account_id,
                ))
                finished_trade_id += 1

            if quantity != 0:
                if trade.quantity < 0:
                    logging.warning(f'Sell without matching position, opening short: {trade} ({quantity})')
                active_trades.put(quantity, trade)

        self._portfolio = [
            PortfolioElement(quantity=element['quantity'], ticker=element['ticker'], average_price=element['average_price'])
            for element in active_trades.unmatched()
        ]

    def _apply_corporate_action(self, active_trades: '_TradesFIFO', action: CorporateAction):
        if action.kind is CorporateActionKind.CONVERSION:
            active_trades.convert(action)
        elif action.kind is CorporateActionKind.SPINOFF:
            fraction = Decimal(self._spinoff_cost_allocation.get(action.target.symbol, 0))
            if not fraction:
                logging.warning(
                    f'Spinoff {action}: child shares get zero cost basis and the parent keeps the whole cost. '
                    f'Set the cost allocation for {action.target.symbol} to apportion it.'
                )
            active_trades.spinoff(action, fraction)
        else:
            raise ValueError(action.kind)

    @property
    def finished_trades(self) -> List[FinishedTrade]:
        return self._finished_trades

    @property
    def final_portfolio(self) -> List[PortfolioElement]:
        return self._portfolio


def _copy_lot_trade(trade: Trade, *, ticker: Ticker, quantity: Quantity, price: Money, fee: Money) -> Trade:
    new_trade = copy.copy(trade)
    new_trade.ticker = ticker
    new_trade.quantity = quantity
    new_trade.price = price
    new_trade.fee = fee
    new_trade.fee_per_piece = fee / abs(quantity)
    return new_trade


class _TradesFIFO:
    def __init__(self):
        self._portfolio = defaultdict(list)

    @staticmethod
    def sign(v: Quantity) -> int:
        assert v != 0
        return -1 if v < 0 else 1

    def put(self, quantity: Quantity, trade: Trade):
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

    def match(self, quantity: Quantity, ticker: Ticker) -> Tuple[Optional[Trade], Quantity]:
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

    @staticmethod
    def _lot_cost(lot: Dict[str, Any]) -> Tuple[Money, Money]:
        """Price per share and fee of the remaining part of the lot."""
        trade: Trade = lot['trade']
        share = Decimal(lot['quantity']) / Decimal(trade.quantity)
        return trade.price, trade.fee * share

    def convert(self, action: CorporateAction):
        """Move open lots to the target instrument keeping their total cost and acquisition dates."""
        lots = self._portfolio.get(action.source, [])
        held = sum(lot['quantity'] for lot in lots)
        if not lots or held == 0:
            logging.warning(f'Corporate action {action}: no open position for {action.source}, nothing to convert')
            return

        total = Decimal(held) * action.ratio
        if action.quantity is not None and action.source != action.target:
            if abs(total - Decimal(action.quantity)) > Decimal('0.01'):
                logging.warning(f'Corporate action {action}: held {held}, expected {action.quantity}, converting what is held')
            else:
                total = Decimal(action.quantity)

        new_lots = []
        remaining = total
        for i, lot in enumerate(lots):
            old_q = Decimal(lot['quantity'])
            new_q = remaining if i == len(lots) - 1 else old_q * action.ratio
            new_q = _normalize_quantity(new_q)
            remaining -= Decimal(new_q)
            price, fee = self._lot_cost(lot)
            new_trade = _copy_lot_trade(lot['trade'], ticker=action.target, quantity=new_q,
                                        price=price * old_q / Decimal(new_q), fee=fee)
            new_lots.append({'trade': new_trade, 'quantity': new_q})

        self._portfolio[action.source] = []
        self._portfolio[action.target] = self._portfolio.get(action.target, []) + new_lots
        logging.info(f'Corporate action {action}: {held} -> {_normalize_quantity(total)}')

    def spinoff(self, action: CorporateAction, fraction: Decimal):
        """Create child lots (parent acquisition dates) and move `fraction` of the parent's cost to them."""
        lots = self._portfolio.get(action.source, [])
        held = sum(lot['quantity'] for lot in lots)
        if not lots or held <= 0:
            logging.warning(f'Spinoff {action}: no open long position for {action.source}')
            return

        child_total = Decimal(action.quantity)
        remaining = child_total
        child_lots = []
        for i, lot in enumerate(lots):
            old_q = Decimal(lot['quantity'])
            child_q = remaining if i == len(lots) - 1 else child_total * old_q / Decimal(held)
            child_q = _normalize_quantity(child_q)
            remaining -= Decimal(child_q)
            price, fee = self._lot_cost(lot)
            parent_trade = _copy_lot_trade(lot['trade'], ticker=action.source, quantity=lot['quantity'],
                                           price=price * (1 - fraction), fee=fee * (1 - fraction))
            lot['trade'] = parent_trade
            child_trade = _copy_lot_trade(parent_trade, ticker=action.target, quantity=child_q,
                                          price=price * old_q * fraction / Decimal(child_q), fee=fee * fraction)
            child_lots.append({'trade': child_trade, 'quantity': child_q})

        self._portfolio[action.target] = self._portfolio.get(action.target, []) + child_lots
        logging.info(f'Spinoff {action}: {child_total} {action.target.symbol}, cost share {fraction}')

    def unmatched(self) -> List[Dict[str, Any]]:
        """
        Return basic information about unmatched trades (final portfolio).

        Returns:
            portfolio: Portfolio with average cost price of the open lots
        """
        ret = []
        for ticker, lots in self._portfolio.items():
            quantity = sum(v['quantity'] for v in lots)
            if quantity != 0:
                currency = lots[0]['trade'].price.currency
                cost = sum((lot['trade'].price * Decimal(lot['quantity']) for lot in lots), Money(0, currency))
                ret.append({'quantity': _normalize_quantity(quantity), 'ticker': ticker,
                            'average_price': cost / Decimal(quantity)})
        return ret
