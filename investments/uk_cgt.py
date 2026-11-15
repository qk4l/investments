"""UK Capital Gains Tax share identification rules.

Disposals are matched with acquisitions of the same security in this order (TCGA 1992 s.105-106A):
1. acquisitions on the same day;
2. acquisitions within the following 30 days ("bed and breakfast"), earlier disposals first;
3. the Section 104 pool - all other shares held, at their average cost.

Amounts are converted to GBP at the acquisition / disposal (trade) date. Fees are added to the cost
and deducted from the proceeds. Corporate actions are applied to the pools: conversions (splits,
ISIN changes, share-for-share mergers) carry the cost over, spinoffs move a share of the parent's cost.
"""
import datetime
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Tuple

import pandas  # type: ignore

from investments.corporate_action import CorporateAction, CorporateActionKind
from investments.data_providers.exchange_provider import ExchangeRatesProvider
from investments.money import Money
from investments.ticker import Ticker
from investments.trade import Trade

SAME_DAY = 'same day'
BED_AND_BREAKFAST = '30 days'
SECTION_104 = 'section 104'

_DUST = Decimal('0.0001')


def uk_tax_year(date: datetime.date) -> str:
    """UK tax year label, e.g. 2025-26 for 6 April 2025 - 5 April 2026."""
    start = date.year if date >= datetime.date(date.year, 4, 6) else date.year - 1
    return f'{start}-{(start + 1) % 100:02d}'


@dataclass
class _Acquisition:
    ticker: Ticker
    date: datetime.date
    quantity: Decimal
    cost: Decimal  # GBP for the whole quantity, fees included
    remaining: Decimal = Decimal(0)

    def __post_init__(self):
        self.remaining = self.quantity

    def take(self, quantity: Decimal) -> Decimal:
        self.remaining -= quantity
        return self.cost * quantity / self.quantity


@dataclass
class UKDisposal:
    ticker: Ticker
    date: datetime.date
    quantity: Decimal
    proceeds: Decimal  # GBP net of fees
    account_id: Optional[str] = None
    remaining: Decimal = Decimal(0)
    matches: List[Tuple[str, Decimal, Decimal]] = field(default_factory=list)  # (rule, quantity, cost)

    def __post_init__(self):
        self.remaining = self.quantity

    @property
    def cost(self) -> Decimal:
        return sum((cost for _, _, cost in self.matches), Decimal(0))

    @property
    def gain(self) -> Decimal:
        return self.proceeds - self.cost

    @property
    def tax_year(self) -> str:
        return uk_tax_year(self.date)

    @property
    def rules(self) -> str:
        return ', '.join(f'{rule} {_fmt_quantity(q)}' for rule, q, _ in self.matches)


def _fmt_quantity(value: Decimal) -> str:
    return format(value.normalize(), 'f')


class UKCapitalGainsCalculator:
    def __init__(self, exchange_provider: ExchangeRatesProvider,
                 spinoff_cost_allocation: Optional[Dict[str, Decimal]] = None):
        self._rates = exchange_provider
        self._spinoff_cost_allocation = spinoff_cost_allocation or {}
        self.warnings: List[str] = []

    def _to_gbp(self, amount: Decimal, trade: Trade) -> Decimal:
        dt = datetime.datetime.combine(trade.trade_date.date(), datetime.time())
        return self._rates.convert_to_base_currency(Money(amount, trade.price.currency), dt).amount

    def calculate(self, trades: Iterable[Trade], corporate_actions: Iterable[CorporateAction]) -> List[UKDisposal]:
        acquisitions, disposals = self._group_trades(trades)
        self._match_same_day(acquisitions, disposals)
        self._match_bed_and_breakfast(acquisitions, disposals)
        self._match_section_104(acquisitions, disposals, corporate_actions)
        return disposals

    def _group_trades(self, trades: Iterable[Trade]) -> Tuple[Dict[Ticker, List[_Acquisition]], List[UKDisposal]]:
        """All acquisitions (disposals) of a security on one day are treated as a single transaction."""
        acq: Dict[Tuple[Ticker, datetime.date], List[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
        disp: Dict[Tuple[Ticker, datetime.date], list] = defaultdict(lambda: [Decimal(0), Decimal(0), None])
        for trade in trades:
            quantity = Decimal(trade.quantity)
            gross = trade.price.amount * abs(quantity)
            fee = abs(trade.fee.amount)
            key = (trade.ticker, trade.trade_date.date())
            if quantity > 0:
                acq[key][0] += quantity
                acq[key][1] += self._to_gbp(gross + fee, trade)
            else:
                disp[key][0] += -quantity
                disp[key][1] += self._to_gbp(gross - fee, trade)
                disp[key][2] = disp[key][2] or trade.account_id

        acquisitions: Dict[Ticker, List[_Acquisition]] = defaultdict(list)
        for (ticker, date), (quantity, cost) in sorted(acq.items(), key=lambda x: x[0][1]):
            acquisitions[ticker].append(_Acquisition(ticker, date, quantity, cost))
        disposals = [
            UKDisposal(ticker, date, quantity, proceeds, account_id)
            for (ticker, date), (quantity, proceeds, account_id) in sorted(disp.items(), key=lambda x: x[0][1])
        ]
        return acquisitions, disposals

    @staticmethod
    def _match(disposal: UKDisposal, acquisition: _Acquisition, rule: str):
        quantity = min(acquisition.remaining, disposal.remaining)
        if quantity <= 0:
            return
        disposal.matches.append((rule, quantity, acquisition.take(quantity)))
        disposal.remaining -= quantity

    def _match_same_day(self, acquisitions, disposals):
        for disposal in disposals:
            for acquisition in acquisitions.get(disposal.ticker, []):
                if acquisition.date == disposal.date:
                    self._match(disposal, acquisition, SAME_DAY)

    def _match_bed_and_breakfast(self, acquisitions, disposals):
        for disposal in disposals:
            window_end = disposal.date + datetime.timedelta(days=30)
            for acquisition in acquisitions.get(disposal.ticker, []):
                if disposal.date < acquisition.date <= window_end:
                    self._match(disposal, acquisition, BED_AND_BREAKFAST)

    def _match_section_104(self, acquisitions, disposals, corporate_actions):  # noqa: C901
        pools: Dict[Ticker, List[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])  # quantity, cost
        events: list = [(a.date, 0, a) for lst in acquisitions.values() for a in lst]
        events += [(ca.date.date(), 1, ca) for ca in corporate_actions]
        events += [(d.date, 2, d) for d in disposals]
        events.sort(key=lambda e: (e[0], e[1]))

        for _, _, event in events:
            if isinstance(event, _Acquisition):
                if event.remaining > 0:
                    pool = pools[event.ticker]
                    pool[0] += event.remaining
                    pool[1] += event.cost * event.remaining / event.quantity
            elif isinstance(event, CorporateAction):
                self._apply_corporate_action(pools, event)
            else:
                self._take_from_pool(pools, event)

    def _apply_corporate_action(self, pools, action: CorporateAction):
        source = pools[action.source]
        if source[0] <= 0:
            return
        if action.kind is CorporateActionKind.CONVERSION:
            quantity, cost = source
            if action.quantity is not None and action.source != action.target:
                new_quantity = Decimal(action.quantity)
            else:
                new_quantity = quantity * action.ratio
            pools[action.source] = [Decimal(0), Decimal(0)]
            target = pools[action.target]
            target[0] += new_quantity
            target[1] += cost
        elif action.kind is CorporateActionKind.SPINOFF:
            fraction = Decimal(self._spinoff_cost_allocation.get(action.target.symbol, 0))
            moved = source[1] * fraction
            source[1] -= moved
            target = pools[action.target]
            target[0] += Decimal(action.quantity)
            target[1] += moved

    def _take_from_pool(self, pools, disposal: UKDisposal):
        if disposal.remaining <= 0:
            return
        pool = pools[disposal.ticker]
        if pool[0] <= 0:
            self._warn(f'{disposal.ticker} {disposal.date}: no shares in the pool for {disposal.remaining}')
            return
        quantity = min(disposal.remaining, pool[0])
        cost = pool[1] * quantity / pool[0]
        pool[0] -= quantity
        pool[1] -= cost
        disposal.matches.append((SECTION_104, quantity, cost))
        disposal.remaining -= quantity
        if disposal.remaining > _DUST:
            self._warn(f'{disposal.ticker} {disposal.date}: {disposal.remaining} shares are not matched')

    def _warn(self, message: str):
        logging.warning(f'UK CGT: {message}')
        self.warnings.append(message)


def uk_disposals_report(disposals: List[UKDisposal]) -> pandas.DataFrame:
    rows = [{
        'tax_year': d.tax_year,
        'account_id': d.account_id,
        'date': d.date,
        'ticker': d.ticker.symbol,
        'isin': d.ticker.security_id,
        'eminent': d.ticker.description,
        'quantity': d.quantity,
        'proceeds_gbp': round(d.proceeds, 2),
        'cost_gbp': round(d.cost, 2),
        'gain_gbp': round(d.gain, 2),
        'matching': d.rules,
    } for d in disposals]
    return pandas.DataFrame(rows)


def uk_tax_year_summary(disposals: List[UKDisposal]) -> pandas.DataFrame:
    """Figures for the 'Listed shares and securities' part of the Capital Gains summary (SA108)."""
    years: Dict[str, Dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for d in disposals:
        y = years[d.tax_year]
        y['disposals'] += 1
        y['proceeds_gbp'] += d.proceeds
        y['cost_gbp'] += d.cost
        y['gains_gbp' if d.gain > 0 else 'losses_gbp'] += abs(d.gain)
    rows = []
    for year in sorted(years):
        y = years[year]
        rows.append({
            'tax_year': year,
            'disposals': int(y['disposals']),
            'proceeds_gbp': round(y['proceeds_gbp'], 2),
            'cost_gbp': round(y['cost_gbp'], 2),
            'gains_gbp': round(y['gains_gbp'], 2),
            'losses_gbp': round(y['losses_gbp'], 2),
            'net_gbp': round(y['gains_gbp'] - y['losses_gbp'], 2),
        })
    return pandas.DataFrame(rows)
