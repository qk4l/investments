import datetime
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum, unique
from typing import Optional, Union

from investments.ticker import Ticker

Quantity = Union[int, Decimal]


@unique
class CorporateActionKind(Enum):
    # Position is carried over to another (or the same) instrument keeping the original cost:
    # splits, reverse splits, CUSIP/ISIN changes, share-for-share mergers.
    CONVERSION = 'conversion'
    # New shares of a child company are received, the parent's cost is apportioned between them.
    SPINOFF = 'spinoff'


@dataclass
class CorporateAction:
    kind: CorporateActionKind
    date: datetime.datetime
    # CONVERSION: instrument before the action; SPINOFF: parent instrument
    source: Ticker
    # CONVERSION: instrument after the action; SPINOFF: child instrument
    target: Ticker
    # CONVERSION: new quantity per one old share; SPINOFF: number of child shares received
    ratio: Optional[Decimal] = None
    quantity: Optional[Quantity] = None
    description: str = ''

    def __str__(self):
        return f'{self.kind.value} {self.source} -> {self.target} at {self.date.date()} ({self.description})'
