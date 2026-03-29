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

    def __init__(self, year_from: int = 2015, cache_dir: Optional[str] = None):
        super().__init__(cache_dir)
        self._year_from = year_from

    def convert_to_base_currency(self, source: Money, rate_date: datetime.datetime) -> Money:
        assert isinstance(rate_date, datetime.datetime)

        if source.currency == self.base_currency:
            return Money(source.amount, self.base_currency)

        rate = self.get_rate(source.currency, rate_date)
        return Money(source.amount / rate.amount, rate.currency)

    def _fetch_currency_rates(self, currency: Currency, dt: datetime.datetime):
        cache_key = f'hmrc_rates_{self._year_from}_{currency.currency_name}.cache'
        logging.debug(f'load currency rates from HMRC {currency} {cache_key}')
        frame_key = currency.name

        cache = DataFrameCache(self._cache_dir, cache_key, datetime.timedelta(days=1))
        df = cache.get()
        if df is not None:
            logging.debug('cache hit')
            self._frames_loaded[frame_key] = df
            return

        rates_data: List[Tuple[datetime.date, Money]] = []

        current_year = datetime.datetime.utcnow().year
        current_month = datetime.datetime.utcnow().month

        # Fetch data for all years from year_from to current year
        for year in range(self._year_from, current_year + 1):
            max_month = current_month if year == current_year else 12
            for month in range(1, max_month + 1):
                url = f'https://hmrc.matchilling.com/rate/{year}/{month:02}.json'
                logging.debug(f'fetching {url}')
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
        df['rate'] = df['rate'].ffill()  # type: ignore
        cache.put(df)
        self._frames_loaded[frame_key] = df
