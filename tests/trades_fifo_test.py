import datetime
from decimal import Decimal
from typing import List

import pytest

from investments.currency import Currency
from investments.money import Money
from investments.ticker import Ticker, TickerKind
from investments.trade import Trade
from investments.trades_fifo import TradesAnalyzer, FinishedTrade

analyze_trades_fifo_testdata = [
    # trades: [(Date, Symbol, Quantity, Price)]
    # expect_trades: (N, Symbol, Quantity)

    # basic cases
    ([('2018-01-01', 'TEST', 100, 4.2), ('2018-01-04', 'TEST', 50, 17.5), ('2018-01-07', 'TEST', -100, 50.3)],
     [(1, 'TEST', 100), (1, 'TEST', -100)]),

    ([('2018-01-01', 'TEST', 100, 4.2), ('2018-01-04', 'TEST', 50, 17.5), ('2018-01-07', 'TEST', -130, 50.3)],
     [(1, 'TEST', 100), (1, 'TEST', 30), (1, 'TEST', -130)]),

    ([('2018-01-01', 'TEST', -100, 4.2), ('2018-01-04', 'TEST', 30, 17.5)],
     [(1, 'TEST', -30), (1, 'TEST', 30)]),

    # issue #8 - sell all & open short in one trade
    ([('2018-01-01', 'TEST', 10, 4.2), ('2018-01-04', 'TEST', -10, 17.5), ('2018-01-05', 'TEST', -3, 17.5)],
     [(1, 'TEST', 10), (1, 'TEST', -10)]),

    ([('2018-01-01', 'TEST', 10, 4.2), ('2018-01-05', 'TEST', -13, 17.5)],
     [(1, 'TEST', 10), (1, 'TEST', -10)]),
]


@pytest.mark.parametrize("trades,expect_trades", analyze_trades_fifo_testdata)
def test_analyze_trades_without_fees(trades, expect_trades):
    request_trades = []
    for date, ticker, qty, price in trades:
        dt = datetime.datetime.strptime(date, '%Y-%m-%d')
        request_trades.append(Trade(
            ticker=Ticker(symbol=ticker, kind=TickerKind.Stock),
            trade_date=dt,
            settle_date=dt.date(),
            quantity=qty,
            price=Money(price, Currency.USD),
            fee=Money(0, Currency.USD),
        ))

    finished_trades = TradesAnalyzer(request_trades).finished_trades

    assert len(finished_trades) == len(
        expect_trades), f'expect {len(expect_trades)} finished trades but got {len(finished_trades)}'
    for trade, expected in zip(finished_trades, expect_trades):
        assert expected[0] == trade.N, f'expect trade N={expected[0]} but got {trade.N}'
        assert expected[1] == trade.ticker.symbol, f'expect trade ticker={expected[1]} but got {trade.ticker.symbol}'
        assert expected[2] == trade.quantity, f'expect trade quantity={expected[2]} but got {trade.quantity}'


def test_trades_fees_simple() -> None:
    request_trades = []
    ticker = Ticker(symbol='VT', kind=TickerKind.Stock)

    # buy 10
    request_trades.append(Trade(
        ticker=ticker,
        trade_date=datetime.datetime(year=2020, month=1, day=31),  # 63,0359₽
        settle_date=datetime.datetime(year=2020, month=2, day=4),  # 63,9091₽
        quantity=10,
        price=Money(80.62, Currency.USD),
        fee=Money(-1, Currency.USD),
    ))
    # sell 10
    request_trades.append(Trade(
        ticker=ticker,
        trade_date=datetime.datetime(year=2020, month=2, day=10),  # 63,4720₽
        settle_date=datetime.datetime(year=2020, month=2, day=12),  # 63,9490₽
        quantity=-10,
        price=Money(81.82, Currency.USD),
        fee=Money(Decimal('-1.01812674'), Currency.USD),
    ))

    finished_trades = TradesAnalyzer(request_trades).finished_trades

    assert len(finished_trades) == 2

    buy_trade: FinishedTrade = finished_trades[0]
    assert buy_trade.trade_date == datetime.datetime(year=2020, month=1, day=31)
    assert buy_trade.settle_date == datetime.datetime(year=2020, month=2, day=4)
    assert buy_trade.quantity == 10
    assert buy_trade.price.amount == Decimal('80.62')
    assert buy_trade.fee_per_piece.amount == Decimal('-0.1')

    sell_trade: FinishedTrade = finished_trades[1]
    assert sell_trade.trade_date == datetime.datetime(year=2020, month=2, day=10)
    assert sell_trade.settle_date == datetime.datetime(year=2020, month=2, day=12)
    assert sell_trade.quantity == -10
    assert sell_trade.price.amount == Decimal('81.82')
    assert sell_trade.fee_per_piece.amount == Decimal('-0.101812674')


def test_trades_precision() -> List[FinishedTrade]:
    ticker = Ticker(symbol='VT', kind=TickerKind.Stock)
    test_case = [
        Trade(
            ticker=ticker,
            trade_date=datetime.datetime(2020, 1, 31, 9, 30),  # 63,0359₽
            settle_date=datetime.date(2020, 2, 4),  # 63,9091₽
            quantity=10,
            price=Money('80.62', Currency.USD),
            fee=Money('-1', Currency.USD)
        ),
        Trade(
            ticker=ticker,
            trade_date=datetime.datetime(2020, 2, 10, 9, 38),  # 63,4720₽
            settle_date=datetime.date(2020, 2, 12),  # 63,9490₽
            quantity=-10,
            price=Money('81.82', Currency.USD),
            fee=Money('-1.01812674', Currency.USD)
        )
    ]

    finished_trades = TradesAnalyzer(test_case).finished_trades

    buy_trade: FinishedTrade = finished_trades[0]
    assert buy_trade.price.amount == Decimal('80.62')
    assert buy_trade.fee_per_piece.amount == Decimal('-0.1')

    sell_trade: FinishedTrade = finished_trades[1]
    assert sell_trade.price.amount == Decimal('81.82')
    assert sell_trade.fee_per_piece.amount == Decimal('-0.101812674')

    return finished_trades


def _trade(date: str, ticker: Ticker, qty, price: str, fee: str = '0') -> Trade:
    dt = datetime.datetime.strptime(date, '%Y-%m-%d')
    return Trade(ticker=ticker, trade_date=dt, settle_date=dt.date(), quantity=qty,
                 price=Money(price, Currency.USD), fee=Money(fee, Currency.USD))


def test_conversion_keeps_cost_and_moves_position_to_new_isin():
    from investments.corporate_action import CorporateAction, CorporateActionKind

    old = Ticker(symbol='UL', kind=TickerKind.Stock, security_id='US9047677045')
    new = Ticker(symbol='UL', kind=TickerKind.Stock, security_id='US9047678035')
    trades = [_trade('2024-01-01', old, 10, '50'), _trade('2024-06-01', old, 8, '59'),
              _trade('2026-01-01', new, -16, '70')]
    split = CorporateAction(kind=CorporateActionKind.CONVERSION, date=datetime.datetime(2025, 12, 8),
                            source=old, target=new, ratio=Decimal(16) / Decimal(18), quantity=16)

    analyzer = TradesAnalyzer(trades, [split])

    assert analyzer.final_portfolio == []
    buys = [t for t in analyzer.finished_trades if t.quantity > 0]
    # 18 shares 8 for 9 -> 16 shares, total cost 10*50 + 8*59 = 972 is preserved, acquisition dates too
    assert sum(t.price.amount * Decimal(t.quantity) for t in buys).quantize(Decimal('0.0001')) == Decimal('972.0000')
    assert [t.trade_date.year for t in buys] == [2024, 2024]


def test_single_row_split_and_spinoff_with_cost_allocation():
    from investments.corporate_action import CorporateAction, CorporateActionKind

    parent = Ticker(symbol='UL', kind=TickerKind.Stock, security_id='P')
    child = Ticker(symbol='MICC', kind=TickerKind.Stock, security_id='C')
    trades = [_trade('2024-01-01', parent, 18, '60'), _trade('2026-03-16', child, -3, '15'),
              _trade('2026-03-17', child, Decimal('-0.6'), '15')]
    spinoff = CorporateAction(kind=CorporateActionKind.SPINOFF, date=datetime.datetime(2025, 12, 5),
                              source=parent, target=child, quantity=Decimal('3.6'))
    split = CorporateAction(kind=CorporateActionKind.CONVERSION, date=datetime.datetime(2025, 12, 8),
                            source=parent, target=parent, ratio=Decimal(3) / Decimal(2))

    analyzer = TradesAnalyzer(trades, [spinoff, split], {'MICC': Decimal('0.05')})

    portfolio = analyzer.final_portfolio
    assert len(portfolio) == 1 and portfolio[0].ticker == parent and portfolio[0].quantity == 27
    # parent keeps 95% of 18 * 60 = 1080 -> 1026 spread over 27 shares
    assert portfolio[0].average_price == Money('38', Currency.USD)
    # child got 5% of the cost: 54 for 3.6 shares, sold in two parts (fractional cash in lieu included)
    child_buys = [t for t in analyzer.finished_trades if t.ticker == child and t.quantity > 0]
    assert sum(t.price.amount * Decimal(t.quantity) for t in child_buys) == Decimal('54')
