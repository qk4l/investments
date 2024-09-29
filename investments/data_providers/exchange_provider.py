import datetime
import logging
from typing import Dict, List, Optional, Tuple

import pandas  # type: ignore

from investments.currency import Currency
from investments.money import Money


class ExchangeRatesProvider:
    base_currency: Currency
    _cache_dir: Optional[str]
    _frames_loaded: Dict[str, pandas.DataFrame]

    def __init__(self, cache_dir: Optional[str] = None):
        self._frames_loaded = {}
        self._cache_dir = cache_dir

    def get_rate(self, currency: Currency, dt: datetime.datetime) -> Money:
        logging.debug(f"Getting rate for {currency} for {dt}")
        if currency is self.base_currency:
            return Money(1, self.base_currency)

        # This double check need because we do not use year_from and load data ondemand
        if currency.name not in self._frames_loaded or self._frames_loaded.get(currency.name).get(dt, None) is None:
            self._fetch_currency_rates(currency, dt)

        rates = self._frames_loaded.get(currency.name)
        assert rates is not None
        return rates.loc[dt].item()

    def convert_to_base_currency(self, source: Money, rate_date: datetime.datetime) -> Money:
        assert isinstance(rate_date, datetime.datetime)

        if source.currency == self.base_currency:
            return Money(source.amount, self.base_currency)

        rate = self.get_rate(source.currency, rate_date)
        return Money(source.amount * rate.amount, rate.currency)

    @classmethod
    def _fetch_currency_rates(self, currency: Currency, dt: datetime.datetime):
        pass
