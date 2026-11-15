import csv
import logging
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterator, List, NamedTuple, Optional, Tuple, Callable, Union

from datetime import datetime
from investments.cash import Cash
from investments.corporate_action import CorporateAction, CorporateActionKind
from investments.currency import Currency
from investments.dividend import Dividend
from investments.exceptions import InvestmentsTickerNotFound
from investments.fees import Fee
from investments.interests import Interest
from investments.money import Money
from investments.ticker import Ticker, TickerKind
from investments.trade import Trade


def parse_datetime(strval: str) -> datetime:
    return datetime.strptime(strval.replace(' ', ''), '%Y-%m-%d,%H:%M:%S')


def parse_date(strval: str) -> datetime.date:
    return datetime.strptime(strval, '%Y-%m-%d').date()


def _parse_trade_quantity(strval: str) -> Union[int, Decimal]:
    """Whole quantities stay int, fractional ones (cash in lieu, spinoffs) become Decimal."""
    try:
        value = Decimal(strval.replace(',', ''))
    except InvalidOperation:
        raise ValueError(f"Failed for convert str to number for {strval} ")
    return int(value) if value == value.to_integral_value() else value


# "...(MICC, MAGNUM ICE CREAM CO BV/THE, NL0015002MS2)" - instrument affected by a corporate action row
_CA_TARGET_RE = re.compile(r'\(([^,()]+), (.*), ([A-Z0-9]{12})\)\s*$')
# "UL(US9047677045) Spinoff ..." / "ME8U (SG2C32962814) Stock Dividend ..." - instrument the action is about
_CA_SOURCE_RE = re.compile(r'^\s*([^\s(]*)\s*\(([A-Z0-9]{12})\)')
_CA_SPLIT_RE = re.compile(r'Split (\d+) for (\d+)')
_WITHHOLDING_COUNTRY_RE = re.compile(r'- ([A-Z]{2}) Tax\s*$')


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
            multiplier: int, security_id: str, exchange: str, issuer_country_code: str):
        ticker = Ticker(symbol=symbol, kind=kind, security_id=security_id,
                        conid=conid, exchange=exchange, multiplier=multiplier, description=description,
                        issuer_country_code=issuer_country_code)

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
                if ticker.symbol not in self._conflict_symbols:
                    self._conflict_symbols[ticker.symbol] = ticker
            else:
                self._symbols[ticker.symbol] = ticker

            self._tickers[ticker.security_id] = ticker
            self._conid_to_ticker[conid] = ticker
            self._description_to_ticker[description] = ticker
            # self._multipliers[ticker] = multiplier
            return

        ticker_in_storage = self._tickers[ticker.security_id]  # type: Ticker
        assert ticker_in_storage == ticker

    def all_tickers(self) -> List[Ticker]:
        return list(self._tickers.values())

    def get_ticker_by_symbol(self, symbol: str, dt: datetime = None) -> Ticker:
        ticker = self._symbols.get(symbol, None)
        if ticker is None:
            logging.info(f"Can not find Ticker by symbol {symbol}")
            logging.info(f"Check may be it is conflict symbol")
            if dt is not None and symbol in self._conflict_symbols:
                ticker = self._conflict_symbols[symbol] # .get(dt.year, None)
                if ticker is not None:
                    return ticker
            raise InvestmentsTickerNotFound(f"Failed to find ticker with symbol - {symbol}")
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
    account_id: str = None


class SettleDatesStorage:
    def __init__(self) -> None:
        self._settle_data: Dict[Tuple[str, datetime], SettleDate] = {}

    def __len__(self):
        return len(self._settle_data)

    def put(
            self,
            symbol: str,
            operation_date: datetime,
            settle_date: datetime.date,
            order_id: str,
            account_id: str = None,
    ):
        existing_item = self.get(symbol, operation_date)
        if existing_item:
            if existing_item.settle_date != settle_date and existing_item.order_id != order_id:
                raise AssertionError(f'Duplicate settle date for key {(symbol, operation_date)} with {order_id}')
        self._settle_data[(symbol, operation_date)] = SettleDate(order_id, settle_date, account_id)

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
        self._confirmation_account_ids: set = set()
        self._trades: List[Trade] = []
        self._dividends: List[Dividend] = []
        self._fees: List[Fee] = []
        self._interests: List[Interest] = []
        self._cash: List[Cash] = []
        self._deposits_and_withdrawals: List[Tuple[datetime.date, Money]] = []
        self._tickers = TickersStorage()
        self._settle_dates = SettleDatesStorage()
        self._corporate_actions: List[CorporateAction] = []
        self._instrument_types: Dict[str, str] = {}  # security_id -> IB instrument type (COMMON, ADR, ETF, ...)
        self._corporate_action_rows: Dict[tuple, Dict[str, str]] = {}
        # symbol -> [(date, ticker before, ticker after)] for symbols reused by a new ISIN (splits, ISIN changes)
        self._symbol_changes: Dict[str, List[Tuple[datetime, Ticker, Ticker]]] = defaultdict(list)

    def __repr__(self):
        return f'IbParser(trades={len(self.trades)}, dividends={len(self.dividends)}, fees={len(self.fees)}, interests={len(self.interests)})'  # noqa: WPS221

    @property
    def trades(self) -> List[Trade]:
        return self._trades

    @property
    def corporate_actions(self) -> List[CorporateAction]:
        return self._corporate_actions

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

    def get_dividends_to_date(self, ticker: Ticker, date: datetime.date) -> Optional[Dividend]:
        exact_matches = [d for d in self._dividends if d.ticker == ticker and d.date == date]
        if exact_matches:
            return exact_matches[0]  # Assuming at most one per ticker per date

        # Log if no exact match found
        logging.info(f"No dividend found for {ticker} on requested date {date}")

        # Find the most recent previous dividend
        previous_divs = [d for d in self._dividends if d.ticker == ticker and d.date < date]
        if not previous_divs:
            return None

        # Get the one with the maximum date
        last_previous = max(previous_divs, key=lambda d: d.date)
        return last_previous

    def parse_csv(self, *, activity_csvs: List[str], trade_confirmation_csvs: List[str]):
        # 1. parse ticker info and account information
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
                ac_fh.seek(0)
                self._real_parse_activity_csv(csv.reader(ac_fh, delimiter=','), {
                    'Account Information': self._parse_account_information,
                })
                logging.info(f"After parsing account info from {ac_fname}: account={getattr(self, 'account', 'NOT SET')}")

        # 1.1 corporate actions (need all instruments; must be known before trades to resolve reused symbols)
        for ca_fname in activity_csvs:
            with open(ca_fname, newline='') as ca_fh:
                self._real_parse_activity_csv(csv.reader(ca_fh, delimiter=','), {
                    'Corporate Actions': self._parse_corporate_action_row,
                }, source=ca_fname)
        self._build_corporate_actions()

        # 2. parse settle_date and account_id from trade confirmation
        for tc_fname in trade_confirmation_csvs:
            with open(tc_fname, newline='') as tc_fh:
                self._parse_trade_confirmation_csv(csv.reader(tc_fh, delimiter=','))
        logging.info(f"After parsing confirmations: {len(self._settle_dates)} settle dates, confirmation_account_ids={self._confirmation_account_ids}")
        self._fill_issuer_countries()

        # 3. parse everything else from activity (trades, dividends, ...)
        for activity_fname in activity_csvs:
            logging.info(f"Parsing {activity_fname} file...")
            with open(activity_fname, newline='') as activity_fh:
                self._real_parse_activity_csv(csv.reader(activity_fh, delimiter=','), {
                    'Trades': self._parse_trades,
                    'Dividends': self._parse_dividends,
                    'Withholding Tax': self._parse_withholding_tax,
                    'Deposits & Withdrawals': self._parse_deposits,
                    # 'Cash Report', 'Change in Dividend Accruals', 'Change in NAV',
                    # 'Codes',
                    'Fees': self._parse_fees,
                    # 'Interest Accruals',
                    'Interest': self._parse_interests,
                    # 'Mark-to-Market Performance Summary',
                    # 'Net Asset Value', 'Notes/Legal Notes', 'Open Positions', 'Realized & Unrealized Performance Summary',
                    # 'Statement', '\ufeffStatement', 'Total P/L for Statement Period', 'Transaction Fees',
                    'Cash Report': self._parse_cash_report,
                }, source=activity_fname)

        # 4. sort
        self._trades.sort(key=lambda x: x.trade_date)
        self._corporate_actions.sort(key=lambda x: x.date)
        self._dividends.sort(key=lambda x: x.date)
        self._interests.sort(key=lambda x: x.date)
        self._deposits_and_withdrawals.sort(key=lambda x: x[0])
        self._fees.sort(key=lambda x: x.date)

    def _parse_trade_confirmation_csv(self, csv_reader: Iterator[List[str]]):
        parser = NamedRowsParser()
        parser.parse_header(next(csv_reader))
        for row in csv_reader:
            f = parser.parse(row)

            # Track ClientAccountID from confirmation reports
            if 'ClientAccountID' in f and f['ClientAccountID']:
                self._confirmation_account_ids.add(f['ClientAccountID'])

            if f['LevelOfDetail'] != 'EXECUTION':
                continue
            if f['TransactionType'] == 'TradeCancel':
                continue
            if f['TransactionType'] == 'FracShare':
                logging.warning(f"Corporate action {f['TransactionType']} for {f['Symbol']}. Currently unsupported")
                continue
            symbol = f['Symbol']
            if f['AssetClass'] == 'CASH':
                continue
            operation_date = parse_datetime(f['Date/Time'])
            try:
                # ISIN first: a symbol may belong to several ISINs (splits, ISIN changes)
                isin = f.get('ISIN') or f.get('SecurityID') or ''
                ticker = self._tickers.get_ticker(symbol, isin, TickerKind.Stock) if isin else \
                    self._tickers.get_ticker_by_symbol(symbol, dt=operation_date)
            except (InvestmentsTickerNotFound, KeyError):
                pass
            else:
                if f.get('IssuerCountryCode', ''):
                    if ticker.issuer_country_code and ticker.issuer_country_code != f['IssuerCountryCode']:
                        logging.warning(
                            f"IssuerCountryCode for {ticker} is not {f['IssuerCountryCode']}. Something wrong.")
                    else:
                        ticker.issuer_country_code = f['IssuerCountryCode']
            self._settle_dates.put(symbol=symbol,
                                   operation_date=operation_date, settle_date=parse_date(f['SettleDate']),
                                   order_id=f['OrderID'],
                                   account_id=f.get('ClientAccountID')
                                   )

    def _parse_statement(self, f: Dict[str, str]):
        # Currently we are interesting only in Period to resolve instrument's symbol duplication over time
        # Statement	Data	Period	January 1, 2020 - December 31, 2020
        if f['Field Name'] == 'Period':
            # January 1, 2020 - December 31, 2020
            date_format = "%B %d, %Y"
            start_period, end_period = f['Field Value'].split('-')
            self._current_parsed_period = (
                datetime.strptime(start_period.strip(), date_format),
                datetime.strptime(end_period.strip(), date_format)
            )

    def _real_parse_activity_csv(self, csv_reader: Iterator[List[str]], parsers: Dict[str, Callable], source: str = ''):
        nrparser = NamedRowsParser()
        # A statement template with both "Combined Dividends" and optional "Dividends" sections repeats
        # the dividends a second time (with an extra 'Code' column). Rows already seen in an earlier block
        # of the same section are skipped and reported, so totals are not doubled; identical rows inside
        # one block are kept as they may be legitimate.
        seen_rows: Dict[str, set] = {}
        block_rows: Dict[str, set] = {}
        skipped: Dict[str, int] = defaultdict(int)
        for row in csv_reader:
            try:
                parser_fn = parsers[row[0]]
            except KeyError:
                # raise Exception(f'Unknown data {row}')
                continue

            section = row[0]
            if row[1] == 'Header':
                nrparser.parse_header(row[2:])
                seen_rows.setdefault(section, set()).update(block_rows.pop(section, set()))
                continue

            if row[1] in {'Total', 'SubTotal', 'Notes'} or row[2].startswith('Total'):
                continue

            if row[1] == 'Data':
                fields = nrparser.parse(row[2:])
                row_key = frozenset((k, v) for k, v in fields.items() if k not in {'Code', ''})
                if row_key in seen_rows.get(section, set()):
                    logging.debug(f'Skip duplicated {section} row: {fields}')
                    skipped[section] += 1
                    continue
                block_rows.setdefault(section, set()).add(row_key)
                parser_fn(fields)
            else:
                raise Exception(f'Unknown data {row}')

        for section, count in skipped.items():
            logging.warning(
                f'{source}: section "{section}" is repeated in the statement, {count} duplicated rows skipped. '
                f'Check the IB statement template (e.g. both "Combined Dividends" and optional "Dividends" enabled).'
            )

    def _fill_issuer_countries(self):
        """Fill issuer country for instruments not covered by trade confirmations."""
        # the same issuer before/after a split or ISIN change (not a merger into another company)
        for action in self._corporate_actions:
            if action.kind is CorporateActionKind.CONVERSION and 'Merged' not in action.description:
                src, dst = action.source, action.target
                if src.issuer_country_code and not dst.issuer_country_code:
                    dst.issuer_country_code = src.issuer_country_code
                elif dst.issuer_country_code and not src.issuer_country_code:
                    src.issuer_country_code = dst.issuer_country_code

        # fallback: ISIN country prefix, but not for depositary receipts (ADR ISIN country is the ADR's, not the issuer's)
        for ticker in self._tickers.all_tickers():
            if ticker.issuer_country_code or len(ticker.security_id) != 12 or not ticker.security_id[:2].isalpha():
                continue
            if self._instrument_types.get(ticker.security_id, '') in {'ADR', 'GDR'}:
                logging.warning(f'Issuer country of {ticker} is unknown (depositary receipt, not traded in confirmations)')
                continue
            ticker.issuer_country_code = ticker.security_id[:2]
            logging.info(f'Issuer country of {ticker} taken from ISIN: {ticker.issuer_country_code}')

    def _parse_instrument_information(self, f: Dict[str, str]):
        self._instrument_types[f['Security ID']] = f.get('Type', '')
        self._tickers.put(
            symbol=f['Symbol'],
            conid=f['Conid'],
            description=f['Description'],
            kind=_parse_tickerkind(f['Asset Category']),
            multiplier=int(f['Multiplier']),
            security_id=f['Security ID'],
            exchange=f['Listing Exch'],
            issuer_country_code=f.get('IssuerCountryCode', '')
            # period=self._current_parsed_period
        )

    def _parse_trades(self, f: Dict[str, str]):
        ticker_kind = _parse_tickerkind(f['Asset Category'])
        if ticker_kind == TickerKind.Forex:
            logging.warning(f'Skipping FOREX trade (not supported yet), your final report may be incorrect! {f}')
            return

        dt = parse_datetime(f['Date/Time'])

        ticker = self._resolve_ticker_by_symbol(f['Symbol'], dt)
        currency = Currency.parse(f['Currency'])

        settle_info = self._settle_dates.get(f['Symbol'], dt) or self._settle_dates.get(ticker.symbol, dt)
        settle_date = settle_info.settle_date if settle_info else None

        # Get account_id: prefer from confirmation report, fallback to activity report
        if settle_info and settle_info.account_id:
            account_id = settle_info.account_id
        else:
            account_id = getattr(self, 'account', None)

        try:
            trade_quantity = _parse_trade_quantity(f['Quantity'])
            quantity = trade_quantity * ticker.multiplier
        except ValueError:
            logging.warning(f"Skip trade for {ticker.symbol}, quantity is {f['Quantity']}. Failed to parse quantity")
            return

        if settle_date is None:
            # Cash in lieu of fractional shares after corporate actions has no settle date in confirmations
            is_corpact = ticker.exchange == 'CORPACT' or 'spinoff' in ticker.description.lower() or isinstance(quantity, Decimal)
            if not is_corpact:
                raise AssertionError(f'settle date not found for trade {f}')
            logging.info(f'No settle date for corporate action related trade, using trade date: {f}')
            settle_date = dt.date()
        self._trades.append(Trade(
            ticker=ticker,
            trade_date=dt,
            settle_date=settle_date,
            quantity=quantity,
            price=Money(f['T. Price'], currency),
            fee=Money(f['Comm/Fee'], currency),
            account_id=account_id,
        ))

    def _parse_withholding_tax(self, f: Dict[str, str]):
        logging.debug(f'Parsing Withholding tax: {f}')
        div_symbol, div_security_id, div_type = _parse_dividend_description(f['Description'])
        ticker = self._tickers.get_ticker(div_symbol, div_security_id, TickerKind.Stock)
        date = parse_date(f['Date'])
        tax_amount = Money(f['Amount'], Currency.parse(f['Currency']))

        dividend = self.get_dividends_to_date(ticker, date)
        if dividend is None:
            raise Exception(f'Account {self.account}: dividend not found for {ticker} on {date}')
        dividend.tax += tax_amount * -1

        # "BEPC (CA11285B1085) Cash Dividend USD 0.373 - CA Tax": the country that withheld the tax,
        # which may differ from the issuer country reported by IB
        tax_country = _WITHHOLDING_COUNTRY_RE.search(f['Description'])
        if tax_country:
            if dividend.tax_country and dividend.tax_country != tax_country.group(1):
                logging.info(f'{ticker} {date}: tax withheld by both {dividend.tax_country} and {tax_country.group(1)}')
            dividend.tax_country = dividend.tax_country or tax_country.group(1)

    def _parse_dividends(self, f: Dict[str, str]):
        div_symbol, div_security_id, div_type = _parse_dividend_description(f['Description'])
        ticker = self._tickers.get_ticker(div_symbol, div_security_id, TickerKind.Stock)
        date = parse_date(f['Date'])
        amount = Money(f['Amount'], Currency.parse(f['Currency']))
        account_id = getattr(self, 'account', None)

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
                        account_id=account_id,
                        tax_country=v.tax_country,
                    )
                    return

        assert amount.amount > 0, f'unsupported dividend with non positive amount: {f}'
        self._dividends.append(Dividend(
            dtype=div_type,
            ticker=ticker,
            date=date,
            amount=amount,
            tax=Money(0, amount.currency),
            account_id=account_id,
        ))

    def _resolve_ticker_by_symbol(self, symbol: str, dt: datetime) -> Ticker:
        """Symbol lookup aware of splits / ISIN changes that reuse the same symbol for a new ISIN."""
        changes = self._symbol_changes.get(symbol)
        if changes:
            ticker = changes[0][1]
            for change_date, before, after in sorted(changes, key=lambda x: x[0]):
                ticker = before if dt < change_date else after
                if dt < change_date:
                    break
            return ticker
        return self._tickers.get_ticker_by_symbol(symbol, dt)

    def _parse_corporate_action_row(self, f: Dict[str, str]):
        if f['Asset Category'] != 'Stocks' or not f.get('Date/Time'):
            return
        # the same action may be present in overlapping statements
        key = (f['Date/Time'], f['Description'], f['Quantity'])
        self._corporate_action_rows.setdefault(key, f)

    def _corporate_action_ticker(self, symbol: str, isin: str, name: str) -> Ticker:
        try:
            return self._tickers.get_ticker(symbol, isin, TickerKind.Stock)
        except (KeyError, InvestmentsTickerNotFound):
            logging.info(f'Instrument {symbol} ({isin}) from corporate actions is not in instrument information, adding it')
            self._tickers.put(symbol=symbol, conid=f'CA:{isin}', description=name, kind=TickerKind.Stock,
                              multiplier=1, security_id=isin, exchange='CORPACT', issuer_country_code='')
            return self._tickers.get_ticker(symbol, isin, TickerKind.Stock)

    def _corporate_action_trade(self, ticker: Ticker, dt: datetime, quantity, price: Money, f: Dict[str, str]):
        self._trades.append(Trade(
            ticker=ticker, trade_date=dt, settle_date=dt.date(), quantity=quantity, price=price,
            fee=Money(0, price.currency), account_id=getattr(self, 'account', None),
        ))

    def _build_corporate_actions(self):  # noqa: C901, WPS231
        groups: Dict[tuple, List[Dict[str, str]]] = defaultdict(list)
        for f in self._corporate_action_rows.values():
            prefix = _CA_TARGET_RE.sub('', f['Description']).strip()
            groups[(f['Date/Time'], prefix)].append(f)

        for (dt_str, prefix), rows in sorted(groups.items()):
            dt = parse_datetime(dt_str)
            items = []
            for f in rows:
                m = _CA_TARGET_RE.search(f['Description'])
                if m is None:
                    logging.warning(f'Unsupported corporate action description, skipped: {f}')
                    continue
                ticker = self._corporate_action_ticker(m.group(1), m.group(3), m.group(2))
                items.append((f, ticker, _parse_trade_quantity(f['Quantity'])))
            if not items:
                continue
            currency = Currency.parse(items[0][0]['Currency'])
            source_m = _CA_SOURCE_RE.match(prefix)
            prefix_symbol = source_m.group(1) if source_m else ''

            if 'Merged(Acquisition) for' in prefix:
                # cash takeover -> disposal of the whole position
                for f, ticker, qty in items:
                    proceeds = Decimal(f['Proceeds'].replace(',', ''))
                    self._corporate_action_trade(ticker, dt, qty, Money(proceeds / abs(qty), currency), f)
                    logging.info(f'Corporate action: cash takeover {ticker} {qty} for {proceeds} {currency}')

            elif 'Stock Dividend' in prefix or 'Rights Issue' in prefix:
                # new shares / rights received at zero cost
                for f, ticker, qty in items:
                    self._corporate_action_trade(ticker, dt, qty, Money(0, currency), f)
                if 'Stock Dividend' in prefix:
                    logging.warning(f'Corporate action: stock dividend {prefix} added with zero cost basis, review its tax treatment')

            elif 'Delisted' in prefix:
                # removed without proceeds (lapsed rights, worthless shares)
                for f, ticker, qty in items:
                    self._corporate_action_trade(ticker, dt, qty, Money(0, currency), f)

            elif 'Spinoff' in prefix:
                parent = self._tickers.get_ticker(source_m.group(1), source_m.group(2), TickerKind.Stock) if source_m else None
                for f, child, qty in items:
                    assert parent is not None, f'can not find spinoff parent for {f}'
                    self._corporate_actions.append(CorporateAction(
                        kind=CorporateActionKind.SPINOFF, date=dt, source=parent, target=child,
                        quantity=qty, description=prefix,
                    ))

            elif 'Split' in prefix or 'CUSIP/ISIN Change' in prefix or 'Merged(Acquisition) WITH' in prefix:
                removed = [(t, q) for _, t, q in items if q < 0]
                added = [(t, q) for _, t, q in items if q > 0]
                if len(removed) == 1 and len(added) == 1:
                    (before, old_qty), (after, new_qty) = removed[0], added[0]
                    self._corporate_actions.append(CorporateAction(
                        kind=CorporateActionKind.CONVERSION, date=dt, source=before, target=after,
                        ratio=Decimal(new_qty) / Decimal(abs(old_qty)), quantity=new_qty, description=prefix,
                    ))
                    symbols = {prefix_symbol, before.symbol} | ({after.symbol} if after.symbol == prefix_symbol else set())
                    for symbol in filter(None, symbols):
                        self._symbol_changes[symbol].append((dt, before, after))
                elif not removed and len(added) == 1 and (split_m := _CA_SPLIT_RE.search(prefix)):
                    # split reported as a single row with the additional shares
                    ticker = added[0][0]
                    self._corporate_actions.append(CorporateAction(
                        kind=CorporateActionKind.CONVERSION, date=dt, source=ticker, target=ticker,
                        ratio=Decimal(split_m.group(1)) / Decimal(split_m.group(2)), description=prefix,
                    ))
                else:
                    logging.warning(f'Unsupported corporate action layout, skipped: {prefix} {rows}')
            else:
                logging.warning(f'Unsupported corporate action, skipped, your final report may be incorrect: {prefix}')

    def _parse_deposits(self, f: Dict[str, str]):
        currency = Currency.parse(f['Currency'])
        date = parse_date(f['Settle Date'])
        amount = Money(f['Amount'], currency)
        self._deposits_and_withdrawals.append((date, amount))

    def _parse_account_information(self, f: Dict[str, str]):
        if f['Field Name'] == 'Account':
            self.account = f['Field Value']
        if f['Field Name'] == 'Account Type':
            self.account_type = f['Field Value']

    def _parse_fees(self, f: Dict[str, str]):
        currency = Currency.parse(f['Currency'])
        date = parse_date(f['Date'])
        amount = Money(f['Amount'], currency)
        description = f"{f['Subtitle']} - {f['Description']}"
        self._fees.append(Fee(date, amount, description))

    def _parse_interests(self, f: Dict[str, str]):
        currency = Currency.parse(f['Currency'])
        date = parse_date(f['Date'])
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