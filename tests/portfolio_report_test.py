import datetime
from decimal import Decimal

from investments.portfolio_report import PeriodResult, parse_ib_statement

STATEMENT = """Statement,Header,Field Name,Field Value
Statement,Data,Period,"January 1, 2025 - December 31, 2025"
Account Information,Header,Field Name,Field Value
Account Information,Data,Account,U1
Account Information,Data,Base Currency,GBP
Change in NAV,Header,Field Name,Field Value
Change in NAV,Data,Starting Value,1000
Change in NAV,Data,Mark-to-Market,80
Change in NAV,Data,Deposits & Withdrawals,365
Change in NAV,Data,Dividends,40
Change in NAV,Data,Withholding Tax,-6
Change in NAV,Data,Commissions,-2
Change in NAV,Data,Ending Value,1477
Deposits & Withdrawals,Header,Currency,Settle Date,Description,Amount
Deposits & Withdrawals,Data,GBP,2025-10-02,Electronic Fund Transfer,365
Cash Report,Header,Currency Summary,Currency,Total,Securities,Futures,
Cash Report,Data,Ending Cash,Base Currency Summary,100,100,0,
Mark-to-Market Performance Summary,Header,Asset Category,Symbol,Prior Quantity,Current Quantity,Prior Price,Current Price,Mark-to-Market P/L Position
Mark-to-Market Performance Summary,Data,Stocks,VOO,2,2,500,550,100
Dividends,Header,Currency,Date,Description,Amount
Dividends,Data,USD,2025-06-30,VOO(US9229083632) Cash Dividend USD 1.80 per Share (Ordinary Dividend),3.6
"""


def test_parse_ib_statement(tmp_path):
    path = tmp_path / 'statement.csv'
    path.write_text(STATEMENT)
    p = parse_ib_statement(str(path))

    assert (p.start, p.end, p.currency, p.account) == (datetime.date(2025, 1, 1), datetime.date(2025, 12, 31), 'GBP', 'U1')
    assert (p.start_value, p.end_value, p.flows) == (Decimal(1000), Decimal(1477), Decimal(365))
    assert p.gain == Decimal(112)
    assert (p.dividends, p.withholding, p.costs, p.end_cash) == (Decimal(40), Decimal(6), Decimal(2), Decimal(100))
    # deposit on 2 October was invested for 90 of 364 days
    assert p.flow_weight == Decimal(90) / Decimal(364)
    # VOO: +10% price and 1.8 / 500 dividends
    assert p.benchmark_return == Decimal('0.1036')


def test_modified_dietz():
    assert PeriodResult.dietz(Decimal(1000), Decimal(1200), Decimal(100), Decimal('0.5')) == Decimal(100) / Decimal(1050)
    assert PeriodResult.dietz(Decimal(0), Decimal(0), Decimal(0)) is None
