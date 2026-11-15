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
    # country that withheld the tax ("... - CA Tax" in IB statements); used for Foreign Tax Credit Relief
    tax_country: str = ''

    def __str__(self):
        return f'{self.ticker}, {self.date} ({self.amount} tax:{self.tax})'

    def __repr__(self):
        return f'{self.ticker}, {self.date} ({self.amount} tax:{self.tax})'