import datetime
import logging
from typing import Dict, List, Optional, Tuple

import pandas  # type: ignore
import requests

from investments.currency import Currency
from investments.data_providers.cache import DataFrameCache
from investments.data_providers.exchange_provider import ExchangeRatesProvider
from investments.money import Money


class ExchangeRatesGBP(ExchangeRatesProvider):
    base_currency = Currency.GBP

    def convert_to_base_currency(self, source: Money, rate_date: datetime.datetime) -> Money:
        assert isinstance(rate_date, datetime.datetime)

        if source.currency == self.base_currency:
            return Money(source.amount, self.base_currency)

        rate = self.get_rate(source.currency, rate_date)
        return Money(source.amount / rate.amount, rate.currency)

    def _fetch_currency_rates(self, currency: Currency, dt: datetime.datetime):
        cache_key = f'hmrc_rates_{dt.year}_{currency.currency_name}.cache'
        logging.debug(f'load currency rates {currency} {cache_key}')
        frame_key = currency.name

        cache = DataFrameCache(self._cache_dir, cache_key, datetime.timedelta(days=1))
        df = cache.get()
        if df is not None:
            logging.debug('cache hit')
            self._frames_loaded[frame_key] = df
            return

        rates_data: List[Tuple[datetime.date, Money]] = []

        for month in range(1, datetime.datetime.utcnow().month + 1):
            url = f'https://hmrc.matchilling.com/rate/{dt.year}/{month:02}.json'
            r = requests.get(url, timeout=10)

            data = r.json()

            period_start = datetime.datetime.strptime(data["period"]["start"], "%Y-%m-%d")
            period_end = datetime.datetime.strptime(data["period"]["end"], "%Y-%m-%d")

            rate = data.get('rates')[currency.currency_name]

            # Iterate over each day to populate cache
            current_date = period_start
            while current_date <= period_end:
                rates_data.append((current_date.date(), Money(rate, self.base_currency)))
                current_date += datetime.timedelta(days=1)

        df = pandas.DataFrame(rates_data, columns=['date', 'rate'])
        df.set_index(['date'], inplace=True)
        today = datetime.datetime.utcnow().date()
        df = df.reindex(pandas.date_range(df.index.min(), today))
        df['rate'].fillna(method='pad', inplace=True)
        cache.put(df)
        self._frames_loaded[frame_key] = df
