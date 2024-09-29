from enum import Enum


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


class Ticker(object):
    def __init__(self, symbol: str, kind: TickerKind, security_id: str = '', multiplier: int = 1,
                 conid: str = '', exchange: str = '', description: str = '', issuer_country_code: str = ''):
        self.symbol = symbol
        self.kind = kind
        self.security_id = security_id
        self.multiplier = multiplier
        self.conid = conid
        self.exchange = exchange
        self.description = description
        self.issuer_country_code = issuer_country_code

    def __str__(self):
        if self.security_id:
            return f'{self.symbol} {self.security_id} ({self.kind}) {self.exchange}'.strip()
        else:
            return f'{self.symbol} ({self.kind}) {self.exchange}'.strip()

    def __eq__(self, other):
        if isinstance(other, Ticker):
            return (
                    self.kind == other.kind and
                    self.security_id == other.security_id and
                    self.multiplier == other.multiplier and
                    self.conid == other.conid
            )
        return False

    def __hash__(self):
        return hash((self.symbol, self.security_id, self.kind, self.multiplier, self.conid))

    def __lt__(self, other):
        if not isinstance(other, Ticker):
            return NotImplemented
        # First compare by symbol, if they're equal, compare by security_id
        return (self.symbol, self.security_id) < (other.symbol, other.security_id)

