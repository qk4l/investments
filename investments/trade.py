import datetime
from dataclasses import dataclass

from investments.money import Money
from investments.ticker import Ticker


@dataclass
class Trade:
    ticker: Ticker
    # дата сделки, нужна для расчёта комиссии в рублях на дату
    trade_date: datetime.datetime

    # дата поставки, нужна для расчёта цены сделки в рублях на дату
    settle_date: datetime.date

    # количество бумаг, положительное для операции покупки, отрицательное для операции продажи
    quantity: int

    # цена одной бумаги, всегда положительная
    price: Money

    # комиссия за сделку
    fee: Money

    # комиссия за сделку с одной бумагой, всегда отрицательная
    fee_per_piece: Money = None

    def __post_init__(self):
        self.fee_per_piece = self.fee / abs(self.quantity)

    # @property
    # def fields(self) -> List[str]:
    #     return [field.name for field in fields(self)]

    def __str__(self):
        return f"Trade {self.ticker} ({self.quantity}) for {self.price} at {self.trade_date}"

    def __repr__(self):
        return f"Trade {self.ticker} ({self.quantity}) for {self.price} at {self.trade_date}"


@dataclass
class FinishedTrade(Trade):
    N: int = None

