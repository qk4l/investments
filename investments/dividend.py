import datetime
from dataclasses import dataclass
from investments.money import Money
from investments.ticker import Ticker

@dataclass
class Dividend:
    dtype: str
    ticker: Ticker
    date: datetime.date
    amount: Money
    tax: Money
    account_id: str = None

    def __str__(self):
        return f'{self.ticker}, {self.date} ({self.amount} tax:{self.tax})'

    def __repr__(self):
        return f'{self.ticker}, {self.date} ({self.amount} tax:{self.tax})'