import csv
import logging
import re
from typing import Dict, Iterator, List, NamedTuple, Optional, Tuple, Callable

from datetime import datetime
from investments.cash import Cash
from investments.currency import Currency
from investments.dividend import Dividend
from investments.fees import Fee
from investments.interests import Interest
from investments.money import Money
from investments.ticker import Ticker, TickerKind
from investments.trade import Trade


def _parse_datetime(strval: str) -> datetime:
    return datetime.strptime(strval.replace(' ', ''), '%Y-%m-%d,%H:%M:%S')


def _parse_date(strval: str) -> datetime.date:
    return datetime.strptime(strval, '%Y-%m-%d').date()


def _parse_trade_quantity(strval: str) -> int:
    return int(strval.replace(',', ''))


def _parse_dividend_description(description: str) -> Tuple[str, str, str]:
    # GSF(GB00BG0P0V73) Cash Dividend GBP 0.02 per Share (Ordinary Dividend)
    m = re.match(r'^(\w+)\s*\((\w+)\) (Cash Dividend|Payment in Lieu of Dividend|Choice Dividend|Stock Dividend)', description)
    if m is None:
        raise Exception(f'unsupported dividend description "{description}"')
    return m.group(1), m.group(2), m.group(3)


def _parse_tickerkind(strval: str):
    if strval == 'Stocks':
        return TickerKind.Stock
    if strval == 'Equity and Index Options':
        return TickerKind.Option
    if strval == 'Forex':
        return TickerKind.Forex
    raise ValueError(strval)


class NamedRowsParser:
    def __init__(self):
        self._fields = []

    def parse_header(self, fields: List[str]):
        self._fields = fields

    def parse(self, row: List[str]) -> Dict[str, str]:
        error_msg = f'expect {len(self._fields)} rows {self._fields}, but got {len(row)} rows ({row})'
        assert len(row) == len(self._fields), error_msg
        return dict(zip(self._fields, row))


class TickersStorage:
    def __init__(self):
        self._tickers = {}  # type dict[str: Ticker]
        self._conid_to_ticker = {}
        self._description_to_ticker = {}
        self._symbols = {}  # type dict[str: Ticker]
        # TODO: Some instruments could change their symbols over time.
        #  Try to manage assume that separate annual reports does not have this
        self._conflict_symbols = {}  # type dict[str: {datetime: Ticker}]

    def put(self, *, symbol: str, conid: str, description: str, kind: TickerKind,
            multiplier: int, security_id: str, exchange: str):
        ticker = Ticker(symbol=symbol, kind=kind, security_id=security_id,
                        conid=conid, exchange=exchange, multiplier=multiplier, description=description)

        if ticker.security_id not in self._tickers:
            assert conid not in self._conid_to_ticker
            # if description in self._description_to_ticker:
            #     description = f"{symbol}: {description}"
            # assert description not in self._description_to_ticker
            try:
                assert ticker.symbol not in self._symbols
            except AssertionError:
                logging.warning(f"There are two Tickets with same symbols: {ticker} and {self._symbols[ticker.symbol]}")
                logging.warning(f"Disable get_ticker_by_symbol() for that symbol")
                self._symbols[ticker.symbol] = None
                if ticker.symbol in self._conflict_symbols:
                    self._conflict_symbols[ticker.symbol]
            else:
                self._symbols[ticker.symbol] = ticker

            self._tickers[ticker.security_id] = ticker
            self._conid_to_ticker[conid] = ticker
            self._description_to_ticker[description] = ticker
            # self._multipliers[ticker] = multiplier
            return

        ticker_in_storage = self._tickers[ticker.security_id]  # type: Ticker
        assert ticker_in_storage == ticker

    def get_ticker_by_symbol(self, symbol: str, dt: datetime = None) -> Ticker:
        ticker = self._symbols.get(symbol, None)
        if ticker is None:
            logging.info(f"Can not find Ticker by symbol {symbol}")
            logging.info(f"Check may be it is conflict symbol")
            if dt is not None and symbol in self._conflict_symbols:
                ticker = self._conflict_symbols[symbol].get(dt.year, None)
                if ticker is not None:
                    return ticker
            raise KeyError(symbol)
        return ticker

    def get_ticker(self, symbol: str, security_id: str, kind: TickerKind) -> Ticker:
        if security_id in self._tickers:
            return self._tickers[security_id]
        logging.info(f"Can not find Ticker by security_id {security_id}")

        ticker = self.get_ticker_by_symbol(symbol)
        if ticker:
            return ticker

        raise KeyError(symbol)


class SettleDate(NamedTuple):
    order_id: str
    settle_date: datetime.date


class SettleDatesStorage:
    def __init__(self) -> None:
        self._settle_data: Dict[Tuple[str, datetime], SettleDate] = {}

    def __len__(self):
        return len(self._settle_data)

    def put(
            self,
            ticker: str,
            operation_date: datetime,
            settle_date: datetime.date,
            order_id: str,
    ):
        existing_item = self.get(ticker, operation_date)
        if existing_item:
            if existing_item.settle_date != settle_date and existing_item.order_id != order_id:
                raise AssertionError(f'Duplicate settle date for key {(ticker, operation_date)} with {order_id}')
        self._settle_data[(ticker, operation_date)] = SettleDate(order_id, settle_date)

    def get(
            self,
            ticker: str,
            operation_date: datetime,
    ) -> Optional[SettleDate]:
        return self._settle_data.get((ticker, operation_date))

    def get_date(
            self,
            ticker: str,
            operation_date: datetime,
    ) -> Optional[datetime.date]:
        existing_settle_item = self.get(ticker, operation_date)
        if existing_settle_item:
            return existing_settle_item.settle_date
        return None


class InteractiveBrokersReportParser:
    def __init__(self) -> None:
        self.account: str
        self.account_type: str
        self._trades: List[Trade] = []
        self._dividends: List[Dividend] = []
        self._fees: List[Fee] = []
        self._interests: List[Interest] = []
        self._cash: List[Cash] = []
        self._deposits_and_withdrawals: List[Tuple[datetime.date, Money]] = []
        self._tickers = TickersStorage()
        self._settle_dates = SettleDatesStorage()

    def __repr__(self):
        return f'IbParser(trades={len(self.trades)}, dividends={len(self.dividends)}, fees={len(self.fees)}, interests={len(self.interests)})'  # noqa: WPS221

    @property
    def trades(self) -> List[Trade]:
        return self._trades

    @property
    def dividends(self) -> List[Dividend]:
        return self._dividends

    @property
    def deposits_and_withdrawals(self) -> List[Tuple[datetime.date, Money]]:
        return self._deposits_and_withdrawals

    @property
    def fees(self) -> List[Fee]:
        return self._fees

    @property
    def interests(self) -> List[Interest]:
        return self._interests

    @property
    def cash(self) -> List[Cash]:
        return self._cash

    def parse_csv(self, *, activity_csvs: List[str], trade_confirmation_csvs: List[str]):
        # 1. parse tickers info
        for ac_fname in activity_csvs:
            with open(ac_fname, newline='') as ac_fh:
                # logging.info(ac_fh.readline())
                self._real_parse_activity_csv(csv.reader(ac_fh, delimiter=','), {
                    # IB generate it =(
                    '﻿Statement': self._parse_statement,
                    'Statement': self._parse_statement,
                })
                ac_fh.seek(0)
                self._real_parse_activity_csv(csv.reader(ac_fh, delimiter=','), {
                    'Financial Instrument Information': self._parse_instrument_information,
                })

        # 2. parse settle_date from trade confirmation
        for tc_fname in trade_confirmation_csvs:
            with open(tc_fname, newline='') as tc_fh:
                self._parse_trade_confirmation_csv(csv.reader(tc_fh, delimiter=','))

        # 3. parse everything else from activity (trades, dividends, ...)
        for activity_fname in activity_csvs:
            logging.info(f"Parsing {activity_fname} file...")
            with open(activity_fname, newline='') as activity_fh:
                self._real_parse_activity_csv(csv.reader(activity_fh, delimiter=','), {
                    'Trades': self._parse_trades,
                    'Dividends': self._parse_dividends,
                    'Withholding Tax': self._parse_withholding_tax,
                    'Deposits & Withdrawals': self._parse_deposits,
                    'Account Information': self._parse_account_information,
                    # 'Cash Report', 'Change in Dividend Accruals', 'Change in NAV',
                    # 'Codes',
                    'Fees': self._parse_fees,
                    # 'Interest Accruals',
                    'Interest': self._parse_interests,
                    # 'Mark-to-Market Performance Summary',
                    # 'Net Asset Value', 'Notes/Legal Notes', 'Open Positions', 'Realized & Unrealized Performance Summary',
                    # 'Statement', '\ufeffStatement', 'Total P/L for Statement Period', 'Transaction Fees',
                    'Cash Report': self._parse_cash_report,
                })

        # 4. sort
        self._trades.sort(key=lambda x: x.trade_date)
        self._dividends.sort(key=lambda x: x.date)
        self._interests.sort(key=lambda x: x.date)
        self._deposits_and_withdrawals.sort(key=lambda x: x[0])
        self._fees.sort(key=lambda x: x.date)

    def _parse_trade_confirmation_csv(self, csv_reader: Iterator[List[str]]):
        parser = NamedRowsParser()
        parser.parse_header(next(csv_reader))
        for row in csv_reader:
            f = parser.parse(row)
            if f['LevelOfDetail'] != 'EXECUTION':
                continue
            if f['TransactionType'] == 'TradeCancel':
                continue
            if f['TransactionType'] == 'FracShare':
                logging.warning(f"Corporate action {f['TransactionType']} for {f['Symbol']}. Currently unsupported")
                continue
            self._settle_dates.put(
                f['Symbol'],
                _parse_datetime(f['Date/Time']),
                _parse_date(f['SettleDate']),
                f['OrderID'],
            )

    def _parse_statement(self, f: Dict[str, str]):
        # Current we are interesting only in Period to resolve instrument's symbol duplication over time
        # Statement	Data	Period	January 1, 2020 - December 31, 2020
        if f['Field Name'] == 'Period':
            # January 1, 2020 - December 31, 2020
            date_format = "%B %d, %Y"
            start_period, end_period = f['Field Value'].split('-')
            self._current_parsed_period = (
                datetime.strptime(start_period.strip(), date_format),
                datetime.strptime(end_period.strip(), date_format)
            )

    def _real_parse_activity_csv(self, csv_reader: Iterator[List[str]], parsers: Dict[str, Callable]):
        nrparser = NamedRowsParser()
        for row in csv_reader:
            try:
                parser_fn = parsers[row[0]]
            except KeyError:
                # raise Exception(f'Unknown data {row}')
                continue

            if row[1] == 'Header':
                nrparser.parse_header(row[2:])
                continue

            if row[1] in {'Total', 'SubTotal', 'Notes'} or row[2].startswith('Total'):
                continue

            if row[1] == 'Data':
                fields = nrparser.parse(row[2:])
                parser_fn(fields)
            else:
                raise Exception(f'Unknown data {row}')

    def _parse_instrument_information(self, f: Dict[str, str]):
        self._tickers.put(
            symbol=f['Symbol'],
            conid=f['Conid'],
            description=f['Description'],
            kind=_parse_tickerkind(f['Asset Category']),
            multiplier=int(f['Multiplier']),
            security_id=f['Security ID'],
            exchange=f['Listing Exch']
            # period=self._current_parsed_period
        )

    def _parse_trades(self, f: Dict[str, str]):
        ticker_kind = _parse_tickerkind(f['Asset Category'])
        if ticker_kind == TickerKind.Forex:
            logging.warning(f'Skipping FOREX trade (not supported yet), your final report may be incorrect! {f}')
            return

        dt = _parse_datetime(f['Date/Time'])

        # TODO: Remove
        if f['Symbol'] in ('FRT', 'SPCE'):
            return
        ticker = self._tickers.get_ticker_by_symbol(f['Symbol'], dt)
        currency = Currency.parse(f['Currency'])

        settle_date = self._settle_dates.get_date(ticker.symbol, dt)
        try:
            assert settle_date is not None
        except AssertionError:
            if ticker.exchange == 'CORPACT':
                logging.warning(f'Skipping CORPACT trade (not supported yet), your final report may be incorrect! {f}')
                return
            if 'spinoff' in ticker.description.lower():
                logging.warning(f'Skipping SPINOFF trade (not supported yet), your final report may be incorrect! {f}')
                return
            raise

        self._trades.append(Trade(
            ticker=ticker,
            trade_date=dt,
            settle_date=settle_date,
            quantity=_parse_trade_quantity(f['Quantity']) * ticker.multiplier,
            price=Money(f['T. Price'], currency),
            fee=Money(f['Comm/Fee'], currency),
        ))

    def _parse_withholding_tax(self, f: Dict[str, str]):
        div_symbol, div_security_id, div_type = _parse_dividend_description(f['Description'])
        ticker = self._tickers.get_ticker(div_symbol, div_security_id, TickerKind.Stock)
        date = _parse_date(f['Date'])
        tax_amount = Money(f['Amount'], Currency.parse(f['Currency']))

        tax_amount *= -1
        found = False
        for i, v in enumerate(self._dividends):
            # difference in reports for the same past year, but generated in different time
            # read more at https://github.com/cdump/investments/issues/17
            cash_choice_hack = (v.dtype == 'Cash Dividend' and div_type == 'Choice Dividend')

            if v.ticker == ticker and v.date == date and (v.dtype == div_type or cash_choice_hack):
                assert v.amount.currency == tax_amount.currency
                self._dividends[i] = Dividend(
                    dtype=v.dtype,
                    ticker=v.ticker,
                    date=v.date,
                    amount=v.amount,
                    tax=v.tax + tax_amount,
                )
                found = True
                break

        if not found:
            # TODO: REMOVE
            if div_symbol == 'FRT' or div_symbol == 'WPC' or div_symbol == 'DLR':
                return
            raise Exception(f'Account {self.account}: dividend not found for {ticker} on {date}')

    def _parse_dividends(self, f: Dict[str, str]):
        div_symbol, div_security_id, div_type = _parse_dividend_description(f['Description'])
        ticker = self._tickers.get_ticker(div_symbol, div_security_id, TickerKind.Stock)
        date = _parse_date(f['Date'])
        amount = Money(f['Amount'], Currency.parse(f['Currency']))

        if amount.amount < 0:
            assert 'Reversal' in f['Description'], f'unsupported dividend with negative amount: {f}'
            for i, v in enumerate(self._dividends):
                if v.dtype == div_type and v.ticker == ticker and v.date == date and v.amount == -1 * amount:
                    self._dividends[i] = Dividend(
                        dtype=div_type,
                        ticker=ticker,
                        date=date,
                        amount=amount + v.amount,
                        tax=v.tax,
                    )
                    return

        assert amount.amount > 0, f'unsupported dividend with non positive amount: {f}'
        self._dividends.append(Dividend(
            dtype=div_type,
            ticker=ticker,
            date=date,
            amount=amount,
            tax=Money(0, amount.currency),
        ))

    def _parse_deposits(self, f: Dict[str, str]):
        currency = Currency.parse(f['Currency'])
        date = _parse_date(f['Settle Date'])
        amount = Money(f['Amount'], currency)
        self._deposits_and_withdrawals.append((date, amount))

    def _parse_account_information(self, f: Dict[str, str]):
        if f['Field Name'] == 'Account':
            self.account = f['Field Value']
        if f['Field Name'] == 'Account Type':
            self.account_type = f['Field Value']

    def _parse_fees(self, f: Dict[str, str]):
        currency = Currency.parse(f['Currency'])
        date = _parse_date(f['Date'])
        amount = Money(f['Amount'], currency)
        description = f"{f['Subtitle']} - {f['Description']}"
        self._fees.append(Fee(date, amount, description))

    def _parse_interests(self, f: Dict[str, str]):
        currency = Currency.parse(f['Currency'])
        date = _parse_date(f['Date'])
        amount = Money(f['Amount'], currency)
        description = f['Description']
        self._interests.append(Interest(date, amount, description))

    def _parse_cash_report(self, f: Dict[str, str]):
        currency_code = f['Currency']
        if currency_code != 'Base Currency Summary':
            currency = Currency.parse(currency_code)
            description = f['Currency Summary']
            amount = Money(f['Total'], currency)
            self._cash.append(Cash(description, amount))
