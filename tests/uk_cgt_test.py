import datetime
from decimal import Decimal

from investments.corporate_action import CorporateAction, CorporateActionKind
from investments.currency import Currency
from investments.money import Money
from investments.ticker import Ticker, TickerKind
from investments.trade import Trade
from investments.uk_cgt import BED_AND_BREAKFAST, SAME_DAY, SECTION_104, UKCapitalGainsCalculator, uk_tax_year, \
    uk_tax_year_summary


class _GBPRates:
    """1 USD = 1 GBP, enough to check the matching rules."""

    def convert_to_base_currency(self, source: Money, rate_date: datetime.datetime) -> Money:
        return Money(source.amount, Currency.GBP)


def _trade(ticker: Ticker, date: str, qty, price: str, fee: str = '0') -> Trade:
    dt = datetime.datetime.strptime(date, '%Y-%m-%d')
    return Trade(ticker=ticker, trade_date=dt, settle_date=dt.date(), quantity=qty,
                 price=Money(price, Currency.USD), fee=Money(fee, Currency.USD))


TICKER = Ticker(symbol='TEST', kind=TickerKind.Stock, security_id='US0000000001')


def test_uk_tax_year():
    assert uk_tax_year(datetime.date(2025, 4, 5)) == '2024-25'
    assert uk_tax_year(datetime.date(2025, 4, 6)) == '2025-26'


def test_section_104_pool_uses_average_cost_with_fees():
    trades = [
        _trade(TICKER, '2024-01-10', 10, '10', '-1'),   # cost 101
        _trade(TICKER, '2024-06-10', 10, '20', '-1'),   # cost 201 -> pool 20 @ 15.1
        _trade(TICKER, '2025-05-01', -5, '30', '-1'),   # proceeds 149
    ]
    disposal, = UKCapitalGainsCalculator(_GBPRates()).calculate(trades, [])

    assert disposal.matches == [(SECTION_104, Decimal(5), Decimal('75.5'))]
    assert disposal.proceeds == Decimal(149)
    assert disposal.gain == Decimal('73.5')


def test_same_day_and_30_day_rules_take_priority_over_pool():
    trades = [
        _trade(TICKER, '2024-01-10', 10, '10'),
        _trade(TICKER, '2025-05-01', -6, '20'),
        _trade(TICKER, '2025-05-01', 2, '18'),   # same day
        _trade(TICKER, '2025-05-20', 3, '19'),   # within 30 days
        _trade(TICKER, '2025-07-01', 4, '50'),   # outside the window -> pool
    ]
    disposal, = UKCapitalGainsCalculator(_GBPRates()).calculate(trades, [])

    assert disposal.matches == [
        (SAME_DAY, Decimal(2), Decimal(36)),
        (BED_AND_BREAKFAST, Decimal(3), Decimal(57)),
        (SECTION_104, Decimal(1), Decimal(10)),
    ]


def test_pool_follows_conversion_and_spinoff():
    old = Ticker(symbol='UL', kind=TickerKind.Stock, security_id='OLD000000001')
    new = Ticker(symbol='UL', kind=TickerKind.Stock, security_id='NEW000000001')
    child = Ticker(symbol='MICC', kind=TickerKind.Stock, security_id='CHILD0000001')
    trades = [
        _trade(old, '2024-01-10', 18, '50'),     # cost 900
        _trade(child, '2026-03-16', -3, '15'),
        _trade(new, '2026-04-10', -16, '70'),
    ]
    actions = [
        CorporateAction(kind=CorporateActionKind.SPINOFF, date=datetime.datetime(2025, 12, 5),
                        source=old, target=child, quantity=Decimal(3)),
        CorporateAction(kind=CorporateActionKind.CONVERSION, date=datetime.datetime(2025, 12, 8),
                        source=old, target=new, ratio=Decimal(16) / Decimal(18), quantity=16),
    ]
    calculator = UKCapitalGainsCalculator(_GBPRates(), {'MICC': Decimal('0.1')})
    child_sale, parent_sale = calculator.calculate(trades, actions)

    assert child_sale.cost == Decimal(90)
    assert parent_sale.cost == Decimal(810)
    assert calculator.warnings == []

    summary = uk_tax_year_summary([child_sale, parent_sale])
    assert summary['tax_year'].tolist() == ['2025-26', '2026-27']
    assert summary['losses_gbp'].tolist() == [Decimal('45.00'), Decimal('0')]
