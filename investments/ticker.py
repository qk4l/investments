from enum import Enum
from typing import NamedTuple


class TickerKind(Enum):
    Stock = 1
    Option = 2
    Futures = 3
    Bond = 4
    Forex = 5
    Rdr = 6
    Index = 7
    Gdr = 8

    def __str__(self):
        return self.name

    def __lt__(self, other):
        return self.value < other.value


class Ticker(NamedTuple):
    symbol: str
    kind: TickerKind
    security_id: str
    multiplier: int
    conid: str = ''
    exchange: str = ''
    description: str = ''
    # periods =

    def __str__(self):
        return f'{self.symbol} {self.security_id} ({self.kind}) on {self.exchange}'

    def __eq__(self, other):
        if isinstance(other, Ticker):
            return (
                    self.kind == other.kind and
                    self.security_id == other.security_id and
                    self.multiplier == other.multiplier and
                    self.conid == other.conid
            )
        return False
