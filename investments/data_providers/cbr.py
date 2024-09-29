"""
Клиент к API ЦБ РФ с курсами валют относительно рубля.

Необходим для перевода сумм сделок в других валютах в рубли по курсу ЦБ на дату поставки в соответствии с НК РФ

"""

import datetime
import logging
import xml.etree.ElementTree as ET  # noqa:N817
from typing import Dict, List, Optional, Tuple

import pandas  # type: ignore
import requests

from investments.currency import Currency
from investments.data_providers.cache import DataFrameCache
from investments.data_providers.exchange_provider import ExchangeRatesProvider
from investments.money import Money


class ExchangeRatesRUB(ExchangeRatesProvider):
    base_currency = Currency.RUB

    def __init__(self, year_from: int = 2000, cache_dir: Optional[str] = None):
        super().__init__()
        self._year_from = year_from
        self._cache_dir = cache_dir

    def get_rate(self, currency: Currency, dt: datetime.datetime) -> Money:
        logging.debug(f"Getting rate for {currency} for {dt}")
        if currency is self.base_currency:
            return Money(1, self.base_currency)

        if currency.name not in self._frames_loaded:
            self._fetch_currency_rates(currency, dt)

        rates = self._frames_loaded.get(currency.name)
        assert rates is not None
        return rates.loc[dt].item()

    def _fetch_currency_rates(self, currency: Currency, dt: datetime.datetime):
        """Загружаем курс запрошенной валюты из кеша или с cbr.ru."""
        cache_key = f'cbrates_{currency.cbr_code}_since{self._year_from}.cache'
        logging.info(f'load currency rates from cbr.ru {currency} {cache_key}')
        frame_key = currency.name

        cache = DataFrameCache(self._cache_dir, cache_key, datetime.timedelta(days=1))
        df = cache.get()
        if df is not None:
            logging.info('cache hit')
            self._frames_loaded[frame_key] = df
            return

        end_date = (datetime.datetime.utcnow() + datetime.timedelta(days=1)).strftime('%d/%m/%Y')
        r = requests.get(f'http://www.cbr.ru/scripts/XML_dynamic.asp?date_req1=01/01/{self._year_from}&date_req2={end_date}&VAL_NM_RQ={currency.cbr_code}', timeout=10)

        tree = ET.fromstring(r.text)

        rates_data: List[Tuple[datetime.date, Money]] = []
        for rec in tree.findall('Record'):
            assert rec.get('Id') == currency.cbr_code
            d = datetime.datetime.strptime(rec.attrib['Date'], '%d.%m.%Y').date()
            v = rec.findtext('Value')
            assert isinstance(v, str)
            rates_data.append((d, Money(v.replace(',', '.'), Currency.RUB)))

        df = pandas.DataFrame(rates_data, columns=['date', 'rate'])
        df.set_index(['date'], inplace=True)
        today = datetime.datetime.utcnow().date()
        df = df.reindex(pandas.date_range(df.index.min(), today))
        df['rate'].fillna(method='pad', inplace=True)

        cache.put(df)
        self._frames_loaded[frame_key] = df
