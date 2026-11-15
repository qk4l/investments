"""Portfolio performance and tax efficiency report for UK tax residents.

Sections:
1. returns per statement (IB activity statements, Freedom Finance broker reports): start/end value,
   external flows, gain, Modified Dietz return in the statement currency and in GBP, dividends,
   withholding tax, costs, cash share; VOO (S&P 500) over the same period as a benchmark (IB only);
2. dividend withholding tax by the withholding country per UK tax year, compared with the treaty rate
   creditable in the UK;
3. estimated UK tax per UK tax year: dividend tax over the allowance less foreign tax credit relief,
   net capital gains (UK share identification rules) against the annual exempt amount.

This is a factual summary of past results, not investment advice.

Usage:
    python -m investments.portfolio_report --activity-reports-dir DIR --confirmation-reports-dir DIR \
        [--freedom-report FILE ...] [--uk-resident-from 2022-23] [--dividend-tax-rate 0.3375] [--gsheet]
"""
import argparse
import csv
import datetime
import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Tuple

import pandas  # type: ignore
from tabulate import tabulate

from investments.currency import Currency
from investments.data_providers.hmrc import ExchangeRatesGBP
from investments.ibtax.ibtax import _parse_spinoff_allocation, csvs_in_dir, parse_reports
from investments.uk_cgt import UKCapitalGainsCalculator, uk_tax_year

D = Decimal

# dividend withholding rate creditable in the UK under the double tax treaties (portfolio dividends)
TREATY_RATES: Dict[str, Decimal] = {
    'US': D('0.15'), 'CA': D('0.15'), 'CN': D('0.10'), 'CH': D('0.15'), 'DE': D('0.15'),
    'NL': D('0.15'), 'FR': D('0.15'), 'JP': D('0.10'), 'AU': D('0.15'), 'IE': D('0'),
    'GB': D('0'), 'HK': D('0'), 'SG': D('0'), 'JE': D('0'),
}
DIVIDEND_ALLOWANCE = {'2021-22': D(2000), '2022-23': D(2000), '2023-24': D(1000)}  # later years: 500
ANNUAL_EXEMPT_AMOUNT = {'2021-22': D(12300), '2022-23': D(12300), '2023-24': D(6000)}  # later years: 3000


@dataclass
class PeriodResult:
    broker: str
    account: str
    start: datetime.date
    end: datetime.date
    currency: str
    start_value: Decimal
    end_value: Decimal
    flows: Decimal  # deposits less withdrawals, transfers of positions in/out
    dividends: Decimal = D(0)
    withholding: Decimal = D(0)  # positive number
    costs: Decimal = D(0)  # commissions and fees, positive number
    end_cash: Optional[Decimal] = None
    benchmark_return: Optional[Decimal] = None  # VOO total return in USD over the period
    # share of the period the flows were invested on average (0.5 = middle of the period when dates are unknown)
    flow_weight: Decimal = D('0.5')

    @property
    def gain(self) -> Decimal:
        return self.end_value - self.start_value - self.flows

    @staticmethod
    def dietz(start: Decimal, end: Decimal, flows: Decimal, weight: Decimal = D('0.5')) -> Optional[Decimal]:
        capital = start + flows * weight
        return (end - start - flows) / capital if capital > 0 else None

    @property
    def return_pct(self) -> Optional[Decimal]:
        return self.dietz(self.start_value, self.end_value, self.flows, self.flow_weight)


def _num(value) -> Decimal:
    if value in (None, '', '--'):
        return D(0)
    return D(str(value).replace(',', '').replace(' ', ''))


# ----------------------------------------------------------------------------------------------- IB

def parse_ib_statement(path: str) -> Optional[PeriodResult]:
    """Change in NAV, cash and VOO benchmark from one IB activity statement CSV."""
    sections: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    header: Dict[str, List[str]] = {}
    with open(path, newline='', encoding='utf-8-sig') as fh:
        for row in csv.reader(fh):
            if len(row) < 3:
                continue
            name = row[0].lstrip('﻿')
            if row[1] == 'Header':
                header[name] = row[2:]
            elif row[1] == 'Data' and name in header:
                sections[name].append(dict(zip(header[name], row[2:])))

    def field_value(section: str, key: str) -> Optional[str]:
        for item in sections.get(section, []):
            if item.get('Field Name') == key:
                return item.get('Field Value')
        return None

    period = field_value('Statement', 'Period')
    nav = {item.get('Field Name'): item.get('Field Value') for item in sections.get('Change in NAV', [])}
    if not period or 'Starting Value' not in nav:
        logging.warning(f'{path}: no statement period or Change in NAV section, skipped')
        return None
    start_s, end_s = (x.strip() for x in period.split(' - '))
    start = datetime.datetime.strptime(start_s, '%B %d, %Y').date()
    end = datetime.datetime.strptime(end_s, '%B %d, %Y').date()

    currency = field_value('Account Information', 'Base Currency') or ''
    account = field_value('Account Information', 'Account') or os.path.basename(path).split('_')[-1].split('.')[0]
    end_cash = None
    for item in sections.get('Cash Report', []):
        if item.get('Currency Summary') == 'Ending Cash' and item.get('Currency') == 'Base Currency Summary':
            end_cash = _num(item.get('Total'))

    result = PeriodResult(
        broker='Interactive Brokers', account=account, start=start, end=end, currency=currency,
        start_value=_num(nav.get('Starting Value')), end_value=_num(nav.get('Ending Value')),
        flows=_num(nav.get('Deposits & Withdrawals')) + _num(nav.get('Position Transfers')),
        dividends=_num(nav.get('Dividends')), withholding=-_num(nav.get('Withholding Tax')),
        costs=-(_num(nav.get('Commissions')) + _num(nav.get('Other Fees')) + _num(nav.get('Sales Tax'))),
        end_cash=end_cash,
    )

    # time weight of deposits/withdrawals from their dates (amounts in their own currencies, used only as weights)
    days = max((end - start).days, 1)
    weighted = total = D(0)
    for item in sections.get('Deposits & Withdrawals', []):
        date_s = item.get('Settle Date') or item.get('Date') or ''
        if not re.match(r'\d{4}-\d{2}-\d{2}', date_s):
            continue
        amount = abs(_num(item.get('Amount')))
        date = datetime.datetime.strptime(date_s[:10], '%Y-%m-%d').date()
        weighted += amount * D(max((end - date).days, 0)) / D(days)
        total += amount
    if total and not _num(nav.get('Position Transfers')):
        result.flow_weight = weighted / total

    # VOO: price return from Mark-to-Market prior/current price, plus its dividends per share
    for item in sections.get('Mark-to-Market Performance Summary', []):
        if item.get('Symbol') == 'VOO' and item.get('Asset Category') == 'Stocks':
            prior, current = _num(item.get('Prior Price')), _num(item.get('Current Price'))
            qty = _num(item.get('Current Quantity')) or _num(item.get('Prior Quantity'))
            if prior and current and qty:
                voo_dividends = sum((_num(d.get('Amount')) for d in sections.get('Dividends', [])
                                     if d.get('Description', '').startswith('VOO(')), D(0))
                result.benchmark_return = (current - prior) / prior + voo_dividends / qty / prior
    return result


# ------------------------------------------------------------------------------------------ Freedom

_FF_DATE_RE = re.compile(r'Account at (\d{8})')


def parse_freedom_report(path: str) -> Tuple[Optional[PeriodResult], List[Tuple[datetime.date, Decimal, Decimal]]]:
    """Net assets, flows, dividends and taxes from a Freedom Finance broker report (xlsx).

    Returns the period result and dividend rows (date, gross USD, tax USD) for the UK tax estimate.
    """
    import openpyxl  # type: ignore

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    accounts = []
    fx = {'USD': D(1)}
    for name in wb.sheetnames:
        m = _FF_DATE_RE.match(name)
        if not m:
            continue
        net = None
        for row in wb[name].iter_rows(values_only=True):
            cells = [c for c in row if c not in (None, '')]
            if not cells:
                continue
            label = str(cells[0]).strip()
            if label.lower().startswith('net assets') and len(cells) > 1:
                net = _num(re.sub(r'[^\d.,-]', '', str(cells[1])))
            if re.fullmatch(r'[A-Z]{3}/USD', label) and len(cells) > 1:
                fx[label[:3]] = _num(cells[1])
        accounts.append((datetime.datetime.strptime(m.group(1), '%Y%m%d').date(), net))
    if len(accounts) < 2 or any(net is None for _, net in accounts):
        logging.warning(f'{path}: start/end net assets not found, skipped')
        return None, []
    accounts.sort()

    for name in wb.sheetnames:  # currency conversions done in the period, e.g. RUR/USD
        if name.startswith('Trades'):
            rows = list(wb[name].iter_rows(values_only=True))
            head = [str(c or '').strip() for c in rows[0]]
            for row in rows[1:]:
                item = dict(zip(head, row))
                ticker = str(item.get('Ticker') or '')
                if re.fullmatch(r'[A-Z]{3}/USD', ticker) and item.get('Price'):
                    fx.setdefault(ticker[:3], _num(item['Price']))

    flows = dividends = taxes = costs = D(0)
    dividend_rows = []
    # unmatched dividends by ticker: a tax row is matched with the earliest unmatched dividend of the ticker
    pending: Dict[str, List[List]] = defaultdict(list)
    for name in wb.sheetnames:
        if not name.startswith('Cash In Out'):
            continue
        for row in list(wb[name].iter_rows(values_only=True))[1:]:
            kind = str(row[0] or '').strip()
            if not kind:
                continue
            amount, currency, comment = _num(row[3]), str(row[4] or 'USD').strip(), str(row[5] or '')
            rate = fx.get('RUB' if currency == 'RUR' else currency, fx.get(currency))
            if rate is None:
                logging.warning(f'{path}: no USD rate for {currency}, row skipped: {row}')
                continue
            usd = amount * rate
            date = datetime.datetime.strptime(str(row[1])[:10], '%Y-%m-%d').date()
            if kind in ('Bank transfer', 'Company internal transfer'):
                flows += usd
            elif kind == 'Dividends':
                dividends += usd
                pending[_ff_ticker(comment)].append([date, usd])
            elif kind == 'Taxes':
                taxes += -usd
                queue = pending.get(_ff_ticker(comment))
                if queue:
                    div_date, gross = queue.pop(0)
                    dividend_rows.append((div_date, gross, -usd))
                else:
                    logging.warning(f'{path}: tax without a dividend: {row}')
                    dividend_rows.append((date, D(0), -usd))
            elif 'fee' in kind.lower() or 'commission' in kind.lower():
                costs += -usd
    for queue in pending.values():  # dividends without tax
        dividend_rows.extend((date, gross, D(0)) for date, gross in queue)

    for name in wb.sheetnames:
        if name.startswith('Commissions'):
            for row in list(wb[name].iter_rows(values_only=True))[1:]:
                if row and row[2] is not None:
                    currency = str(row[3] or 'USD').strip()
                    costs += _num(row[2]) * fx.get(currency, D(1) if currency == 'USD' else D(0))

    (start, start_value), (end, end_value) = accounts[0], accounts[-1]
    result = PeriodResult(broker='Freedom Finance', account=os.path.basename(path)[:24], start=start, end=end,
                          currency='USD', start_value=start_value, end_value=end_value, flows=flows,
                          dividends=dividends, withholding=taxes, costs=costs)
    return result, dividend_rows


def _ff_ticker(comment: str) -> str:
    m = re.search(r'\(([A-Z0-9.]+\.[A-Z]+)\)', comment)
    return m.group(1) if m else ''


# ------------------------------------------------------------------------------------------- report

class PortfolioReport:
    def __init__(self, rates: ExchangeRatesGBP, uk_resident_from: Optional[str], dividend_tax_rate: Decimal):
        self._rates = rates
        self._resident_from = uk_resident_from
        self._dividend_tax_rate = dividend_tax_rate

    def _fx(self, currency: str, date: datetime.date) -> Decimal:
        """Units of the currency per 1 GBP (HMRC monthly rate)."""
        if currency in ('', 'GBP'):
            return D(1)
        return self._rates.get_rate(Currency.parse(currency), datetime.datetime.combine(date, datetime.time())).amount

    def _resident(self, tax_year: str) -> bool:
        return self._resident_from is None or tax_year >= self._resident_from

    def returns_table(self, periods: Iterable[PeriodResult]) -> pandas.DataFrame:
        rows = []
        for p in sorted(periods, key=lambda x: (x.start, x.broker)):
            fx_start, fx_end = self._fx(p.currency, p.start), self._fx(p.currency, p.end)
            # the period return translated into GBP; not meaningful for a period that starts from zero
            gbp_return = None
            if p.return_pct is not None and p.start_value > 0:
                gbp_return = (1 + p.return_pct) * fx_start / fx_end - 1
            benchmark_gbp = None
            if p.benchmark_return is not None:
                usd_start, usd_end = self._fx('USD', p.start), self._fx('USD', p.end)
                benchmark_gbp = (1 + p.benchmark_return) * usd_start / usd_end - 1
            rows.append({
                'broker': p.broker, 'account': p.account, 'period': f'{p.start} – {p.end}', 'ccy': p.currency,
                'start': round(p.start_value), 'flows': round(p.flows), 'end': round(p.end_value), 'gain': round(p.gain),
                'return': _pct(p.return_pct), 'return_gbp': _pct(gbp_return), 'voo_gbp': _pct(benchmark_gbp),
                'dividends': round(p.dividends), 'withholding': round(p.withholding),
                'wht_rate': _pct(p.withholding / p.dividends if p.dividends else None),
                'costs': round(p.costs, 2),
                'cash_share': _pct(p.end_cash / p.end_value if p.end_cash is not None and p.end_value else None),
            })
        return pandas.DataFrame(rows)

    def withholding_table(self, dividends_report: pandas.DataFrame) -> pandas.DataFrame:
        groups: Dict[Tuple[str, str], List[Decimal]] = defaultdict(lambda: [D(0), D(0)])
        for _, r in dividends_report.iterrows():
            year = uk_tax_year(r['date'].date())
            if not self._resident(year):
                continue
            g = groups[(year, r['tax_country'] or '??')]
            g[0] += r['amount_base_currency'].amount
            g[1] += r['tax_paid_base_currency'].amount
        rows = []
        for (year, country), (gross, tax) in sorted(groups.items()):
            treaty = TREATY_RATES.get(country)
            excess = max(D(0), tax - treaty * gross) if treaty is not None else None
            rows.append({'tax_year': year, 'tax_country': country, 'gross_gbp': round(gross, 2), 'withheld_gbp': round(tax, 2),
                         'rate': _pct(tax / gross if gross else None), 'treaty': _pct(treaty),
                         'not_creditable_gbp': round(excess, 2) if excess is not None else ''})
        return pandas.DataFrame(rows)

    def uk_tax_table(self, dividends: List[Tuple[datetime.date, Decimal, Decimal]], cgt_summary: pandas.DataFrame) -> pandas.DataFrame:
        """dividends: (date, gross GBP, withheld GBP) from all brokers."""
        years: Dict[str, Dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        rate = self._dividend_tax_rate
        for date, gross, tax in dividends:
            y = years[uk_tax_year(date)]
            y['dividends'] += gross
            y['withheld'] += tax  # refunds of over-withheld tax come as negative rows
        cgt = {r['tax_year']: D(str(r['net_gbp'])) for _, r in cgt_summary.iterrows()} if not cgt_summary.empty else {}
        rows = []
        for year in sorted(set(years) | set(cgt)):
            if not self._resident(year):
                continue
            y = years[year]
            allowance = DIVIDEND_ALLOWANCE.get(year, D(500))
            uk_tax = max(D(0), y['dividends'] - allowance) * rate
            # foreign tax credit relief cannot exceed the UK tax on that income; withholding rates are below the UK
            # dividend rates here, so the yearly totals are a close approximation of the per-dividend limit
            ftcr = min(max(y['withheld'], D(0)), uk_tax)
            aea = ANNUAL_EXEMPT_AMOUNT.get(year, D(3000))
            net_gains = cgt.get(year, D(0))
            rows.append({
                'tax_year': year, 'dividends_gbp': round(y['dividends'], 2), 'allowance': allowance,
                'uk_dividend_tax_gbp': round(uk_tax, 2), 'ftcr_gbp': round(ftcr, 2),
                'net_dividend_tax_gbp': round(uk_tax - ftcr, 2), 'foreign_tax_gbp': round(y['withheld'], 2),
                'total_tax_on_dividends': _pct((uk_tax - ftcr + y['withheld']) / y['dividends'] if y['dividends'] else None),
                'net_gains_gbp': round(net_gains, 2), 'annual_exempt_amount': aea,
                'unused_exempt_amount_gbp': round(max(D(0), aea - max(net_gains, D(0))), 2),
            })
        return pandas.DataFrame(rows)


def _pct(value: Optional[Decimal]) -> str:
    return '' if value is None else f'{float(value) * 100:.1f}%'


def main() -> None:
    parser = argparse.ArgumentParser(description='Portfolio performance and UK tax efficiency report (not investment advice)')
    parser.add_argument('--activity-reports-dir', required=True)
    parser.add_argument('--confirmation-reports-dir', required=True)
    parser.add_argument('--freedom-report', action='append', default=[], help='Freedom Finance broker report xlsx (repeatable)')
    parser.add_argument('--cache-dir', default='.cache')
    parser.add_argument('--uk-resident-from', default=os.getenv('IBTAX_UK_RESIDENT_FROM'),
                        help='first UK tax year of residence, e.g. 2022-23 (default: env IBTAX_UK_RESIDENT_FROM)')
    parser.add_argument('--dividend-tax-rate', type=Decimal, default=Decimal(os.getenv('IBTAX_DIVIDEND_TAX_RATE', '0.3375')),
                        help='marginal UK dividend tax rate (0.0875 / 0.3375 / 0.3935)')
    parser.add_argument('--gsheet', action='store_true', help='also upload the tables to Google Sheets (PERF_* sheets)')
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    rates = ExchangeRatesGBP(year_from=2020, cache_dir=args.cache_dir)
    report = PortfolioReport(rates, args.uk_resident_from, args.dividend_tax_rate)

    periods = [p for p in (parse_ib_statement(f) for f in csvs_in_dir(args.activity_reports_dir)) if p]
    freedom_dividends = []
    for path in args.freedom_report:
        period, rows = parse_freedom_report(path)
        if period:
            periods.append(period)
            for date, gross, tax in rows:
                fx = report._fx('USD', date)
                freedom_dividends.append((date, gross / fx, tax / fx))

    from investments.ibtax.ibtax import prepare_dividends_report
    ib = parse_reports(args.activity_reports_dir, args.confirmation_reports_dir)
    dividends_report = prepare_dividends_report(ib.dividends, rates, False)
    ib_dividends = [(r['date'].date(), r['amount_base_currency'].amount, r['tax_paid_base_currency'].amount)
                    for _, r in dividends_report.iterrows()]
    calculator = UKCapitalGainsCalculator(rates, _parse_spinoff_allocation(os.getenv('IBTAX_SPINOFF_COST_ALLOCATION', '')))
    from investments.uk_cgt import uk_tax_year_summary
    cgt_summary = uk_tax_year_summary(calculator.calculate(ib.trades, ib.corporate_actions))

    tables = {
        'PERF_Returns': report.returns_table(periods),
        'PERF_Withholding': report.withholding_table(dividends_report),
        'PERF_UK_Tax': report.uk_tax_table(ib_dividends + freedom_dividends, cgt_summary),
    }
    titles = {
        'PERF_Returns': 'RETURNS PER STATEMENT (Modified Dietz; voo_gbp = VOO total return over the same period)',
        'PERF_Withholding': 'DIVIDEND WITHHOLDING TAX BY COUNTRY (IB), UK TAX YEARS',
        'PERF_UK_Tax': f'ESTIMATED UK TAX (IB + Freedom dividends, IB capital gains), dividend rate {args.dividend_tax_rate}',
    }
    for name, df in tables.items():
        print(f'\n>>> {titles[name]} <<<')
        print(tabulate(df, headers='keys', showindex=False, tablefmt='presto') if not df.empty else '(no data)')
    print('\nThis is a factual summary of past results, not investment advice.')

    if args.gsheet:
        from investments.defaults import GOOGLE_SHEET_ID
        from investments.google_api import GoogleAPI
        api = GoogleAPI()
        for name, df in tables.items():
            if df.empty:
                continue
            data = [df.columns.tolist()] + [[_cell(v) for v in row] for row in df.values.tolist()]
            api.ensure_sheet(GOOGLE_SHEET_ID, name)
            api.set_values(spreadsheet_id=GOOGLE_SHEET_ID, data=data, sheet_range=name)


def _cell(value):
    if isinstance(value, Decimal):
        return float(value)
    return value


if __name__ == '__main__':
    main()
