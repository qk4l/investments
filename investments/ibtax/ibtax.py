import argparse
import json
import logging
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Type

import pandas  # type: ignore
import requests

from investments.calculators import compute_total_cost
from investments.data_providers import cbr, hmrc
from investments.data_providers.exchange_provider import ExchangeRatesProvider
from investments.defaults import BASE_CURRENCY
from investments.dividend import Dividend
from investments.fees import Fee
from investments.ibtax.report_presenter import NativeReportPresenter, ReportPresenter, \
    GoogleSpeadSheetPresenter  # noqa: I001
from investments.interests import Interest
from investments.money import Money
from investments.report_parsers.ib import InteractiveBrokersReportParser
from investments.trades_fifo import FinishedTrade, TradesAnalyzer


def apply_round_for_dataframe(source: pandas.DataFrame, columns: Iterable, digits: int = 2) -> pandas.DataFrame:
    source[list(columns)] = source[list(columns)].applymap(
        lambda x: x.round(digits=digits) if isinstance(x, Money) else round(x, digits),
    )
    return source


def prepare_trades_report(finished_trades: List[FinishedTrade],
                          exchange_rate_provider: ExchangeRatesProvider) -> pandas.DataFrame:
    """
    Расчёт расхода/дохода и финансового результата по закрытым сделкам.

    Общая методика расчёта расхода/дохода по сделке:
    [сумма сделки] * [курс валюты на дату поставки] +/- [сумма комиссии] * [курс валюты на дату сделки]

    """
    trade_date_column = 'trade_date'
    tax_date_column = 'settle_date'

    df = pandas.DataFrame(finished_trades)

    df[trade_date_column] = df[trade_date_column].dt.normalize()
    df['date'] = df[trade_date_column].dt.date
    df[tax_date_column] = pandas.to_datetime(df[tax_date_column])

    tax_years = df.groupby('N')[tax_date_column].max().map(lambda x: x.year).rename('tax_year')
    df = df.join(tax_years, how='left', on='N')

    df['price_base_currency'] = df.apply(lambda x: exchange_rate_provider.convert_to_base_currency(x['price'], x[tax_date_column]), axis=1)
    df['fee_per_piece_base_currency'] = df.apply(lambda x: exchange_rate_provider.convert_to_base_currency(x['fee_per_piece'], x[trade_date_column]), axis=1)
    df['fee'] = df.apply(lambda x: (x['fee_per_piece'] * abs(x['quantity'])), axis=1)

    df['total'] = df.apply(
        lambda x: compute_total_cost(x['quantity'], x['price'], x['fee_per_piece']),
        axis=1,
    )
    df['total_base_currency'] = df.apply(
        lambda x: compute_total_cost(x['quantity'], x['price_base_currency'], x['fee_per_piece_base_currency']),
        axis=1,
    )

    df['settle_rate'] = df.apply(lambda x: exchange_rate_provider.get_rate(x['price'].currency, x[tax_date_column]), axis=1)
    df['fee_rate'] = df.apply(lambda x: exchange_rate_provider.get_rate(x['fee_per_piece'].currency, x[trade_date_column]), axis=1)
    df['profit_base_currency'] = df['total_base_currency']

    profit = df.groupby('N')['profit_base_currency'].sum().reset_index().set_index('N')
    df = df.join(profit, how='left', on='N', lsuffix='_delete')
    df.drop(columns=['profit_base_currency_delete'], axis=0, inplace=True)
    df.loc[~df.index.isin(df.groupby('N')[trade_date_column].idxmax()), 'profit_base_currency'] = Money(0, exchange_rate_provider.base_currency)

    return df


def prepare_dividends_report(dividends: List[Dividend], exchange_rate_provider: ExchangeRatesProvider, verbose: bool) -> pandas.DataFrame:
    operation_date_column = 'date'
    if not verbose:
        dividends = [x for x in dividends if x.amount.amount != 0 or x.tax.amount != 0]  # remove reversed dividends

    df_data = [(i + 1, x.ticker, x.ticker.issuer_country_code, pandas.to_datetime(x.date), x.amount, x.tax, x.account_id) for i, x in enumerate(dividends)]
    df = pandas.DataFrame(df_data, columns=['N', 'ticker', 'issuer_country_code', 'date', 'amount', 'tax_paid', 'account_id'])

    df['tax_year'] = df[operation_date_column].map(lambda x: x.year)
    df['rate'] = df.apply(lambda x: exchange_rate_provider.get_rate(x['amount'].currency, x[operation_date_column]), axis=1)
    df['amount_base_currency'] = df.apply(lambda x: exchange_rate_provider.convert_to_base_currency(x['amount'], x[operation_date_column]), axis=1)
    df['tax_paid_base_currency'] = df.apply(lambda x: exchange_rate_provider.convert_to_base_currency(x['tax_paid'], x[operation_date_column]), axis=1)
    # df['tax_rate'] = df.apply(lambda x: round(x['tax_paid'].amount * 100 / x['amount'].amount, 2), axis=1)

    return df


def prepare_fees_report(fees: List[Fee], exchange_rate_provider: ExchangeRatesProvider, verbose: bool) -> pandas.DataFrame:
    operation_date_column = 'date'
    df_data = [
        (i + 1, pandas.to_datetime(x.date), x.amount, x.description, x.date.year)
        for i, x in enumerate(fees)
    ]
    df = pandas.DataFrame(df_data, columns=['N', operation_date_column, 'amount', 'description', 'tax_year'])
    df['rate'] = df.apply(lambda x: exchange_rate_provider.get_rate(x['amount'].currency, x[operation_date_column]), axis=1)
    df['amount_base_currency'] = df.apply(lambda x: exchange_rate_provider.convert_to_base_currency(x['amount'], x[operation_date_column]), axis=1)

    if not verbose:
        df['abs_amount_del'] = df.apply(lambda x: abs(x.amount.amount), axis=1)
        df.drop_duplicates(subset=[operation_date_column, 'description', 'abs_amount_del'], keep=False, inplace=True)
        df.drop(columns=['abs_amount_del'], inplace=True)
        df['N'] = range(1, len(df) + 1)

    return df


def prepare_interests_report(interests: List[Interest], exchange_rate_provider: ExchangeRatesProvider) -> pandas.DataFrame:
    operation_date_column = 'date'
    df_data = [
        (i + 1, pandas.to_datetime(x.date), x.amount, x.description, x.date.year)
        for i, x in enumerate(interests)
    ]
    df = pandas.DataFrame(df_data, columns=['N', operation_date_column, 'amount', 'description', 'tax_year'])
    df['rate'] = df.apply(lambda x: exchange_rate_provider.get_rate(x['amount'].currency, x[operation_date_column]), axis=1)
    df['amount_base_currency'] = df.apply(lambda x: exchange_rate_provider.convert_to_base_currency(x['amount'], x[operation_date_column]), axis=1)
    return df


class IBFlexDownloader:
    """Downloads IB Flex Query reports via API."""

    BASE_URL = "https://gdcdyn.interactivebrokers.com/Universal/servlet"

    def __init__(
        self,
        flex_token: str,
        activity_query_id: str,
        account_id: str,
        reports_dir: str = "./ib_reports",
        confirmation_query_id: Optional[str] = None
    ):
        """Initialize downloader.

        Args:
            flex_token: Flex Query API token from IB Account Management
            activity_query_id: Flex Query ID for Activity reports
            account_id: IB account ID (e.g., U11920545)
            reports_dir: Directory to save downloaded reports
            confirmation_query_id: Optional Flex Query ID for Trade Confirmation reports
        """
        self.flex_token = flex_token
        self.activity_query_id = activity_query_id
        self.confirmation_query_id = confirmation_query_id
        self.account_id = account_id
        self.reports_dir = Path(reports_dir)
        self.reports_dir.mkdir(parents=True, exist_ok=True)

        # Create subdirectories for different report types
        self.activity_dir = self.reports_dir / "activity"
        self.confirmation_dir = self.reports_dir / "confirmation"
        self.activity_dir.mkdir(parents=True, exist_ok=True)
        if self.confirmation_query_id:
            self.confirmation_dir.mkdir(parents=True, exist_ok=True)

    def _generate_filename(self, start_date: date, end_date: date) -> str:
        """Generate filename in format YYYYMMDD_YYYYMMDD_ACCOUNT.csv."""
        start_str = start_date.strftime("%Y%m%d")
        end_str = end_date.strftime("%Y%m%d")
        return f"{start_str}_{end_str}_{self.account_id}.csv"

    def _file_exists(self, start_date: date, end_date: date, report_type: str = "activity") -> bool:
        """Check if report file already exists."""
        filename = self._generate_filename(start_date, end_date)
        if report_type == "activity":
            filepath = self.activity_dir / filename
        else:
            filepath = self.confirmation_dir / filename
        return filepath.exists()

    def _request_report(self, query_id: str, start_date: date, end_date: date, report_type: str) -> Optional[str]:
        """Request report generation and get reference code."""
        url = f"{self.BASE_URL}/FlexStatementService.SendRequest"
        params = {
            "t": self.flex_token,
            "q": query_id,
            "v": "3",
        }

        logging.info(f"Requesting {report_type} report for {start_date} to {end_date}...")

        try:
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()

            root = ET.fromstring(response.content)

            status = root.find("Status")
            if status is not None and status.text == "Success":
                ref_code = root.find("ReferenceCode")
                if ref_code is not None:
                    logging.info(f"Report requested successfully. Reference: {ref_code.text}")
                    return ref_code.text

            error_code = root.find("ErrorCode")
            error_msg = root.find("ErrorMessage")
            logging.error(f"Failed to request report: {error_code.text if error_code is not None else 'Unknown'} - {error_msg.text if error_msg is not None else 'Unknown'}")
            return None

        except Exception as e:
            logging.error(f"Error requesting report: {e}")
            return None

    def _download_report(self, reference_code: str, max_attempts: int = 10) -> Optional[str]:
        """Download report using reference code."""
        url = f"{self.BASE_URL}/FlexStatementService.GetStatement"
        params = {
            "t": self.flex_token,
            "q": reference_code,
            "v": "3",
        }

        for attempt in range(max_attempts):
            try:
                logging.info(f"Attempting to download report (attempt {attempt + 1}/{max_attempts})...")
                response = requests.get(url, params=params, timeout=30)
                response.raise_for_status()

                try:
                    root = ET.fromstring(response.content)
                    status = root.find("Status")

                    if status is not None:
                        if status.text == "Success":
                            logging.info("Report downloaded successfully")
                            return response.text
                        elif status.text == "Warn":
                            error_msg = root.find("ErrorMessage")
                            logging.info(f"Report not ready: {error_msg.text if error_msg is not None else 'Processing'}")
                            time.sleep(3)
                            continue
                        else:
                            error_code = root.find("ErrorCode")
                            error_msg = root.find("ErrorMessage")
                            logging.error(f"Failed to download report: {error_code.text if error_code is not None else 'Unknown'} - {error_msg.text if error_msg is not None else 'Unknown'}")
                            return None

                except ET.ParseError:
                    logging.error("Unexpected response format")
                    return None

            except Exception as e:
                logging.error(f"Error downloading report: {e}")
                return None

        logging.error("Max attempts reached, report not ready")
        return None

    def _save_report(self, xml_content: str, output_path: Path) -> bool:
        """Save XML report to file."""
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(xml_content)
            logging.info(f"Report saved to {output_path}")
            return True
        except Exception as e:
            logging.error(f"Error saving report: {e}")
            return False

    def download_report(self, query_id: str, start_date: date, end_date: date,
                       report_type: str = "activity", force: bool = False) -> bool:
        """Download report for specified date range."""
        if not force and self._file_exists(start_date, end_date, report_type):
            logging.info(f"{report_type.capitalize()} report for {start_date} to {end_date} already exists. Skipping.")
            return True

        ref_code = self._request_report(query_id, start_date, end_date, report_type)
        if not ref_code:
            return False

        report_content = self._download_report(ref_code)
        if not report_content:
            return False

        filename = self._generate_filename(start_date, end_date)
        if report_type == "activity":
            output_path = self.activity_dir / filename
        else:
            output_path = self.confirmation_dir / filename

        return self._save_report(report_content, output_path)

    def download_yearly_reports(self, force_current_year: bool = True, start_year: int = 2020):
        """Download yearly reports from start_year to current date."""
        current_date = date.today()
        current_year = current_date.year

        logging.info(f"Starting yearly report downloads from {start_year} to {current_year}")
        logging.info(f"Activity Query ID: {self.activity_query_id}")
        if self.confirmation_query_id:
            logging.info(f"Confirmation Query ID: {self.confirmation_query_id}")
        else:
            logging.info("Confirmation reports: DISABLED (no query ID provided)")

        for year in range(start_year, current_year + 1):
            start_date = date(year, 1, 1)

            if year == current_year:
                end_date = current_date
                force = force_current_year
            else:
                end_date = date(year, 12, 31)
                force = False

            logging.info(f"\n{'='*60}")
            logging.info(f"Processing year {year}: {start_date} to {end_date}")
            logging.info(f"{'='*60}")

            success_activity = self.download_report(
                query_id=self.activity_query_id,
                start_date=start_date,
                end_date=end_date,
                report_type="activity",
                force=force
            )

            if not success_activity:
                logging.error(f"Failed to download activity report for {year}")
                continue

            if self.confirmation_query_id:
                time.sleep(2)
                success_confirmation = self.download_report(
                    query_id=self.confirmation_query_id,
                    start_date=start_date,
                    end_date=end_date,
                    report_type="confirmation",
                    force=force
                )

                if not success_confirmation:
                    logging.warning(f"Failed to download confirmation report for {year}, continuing...")

            if year < current_year:
                time.sleep(2)


def _load_accounts_config() -> List[Dict[str, str]]:
    """Load account configurations from environment variables."""
    accounts_json = os.getenv("IB_ACCOUNTS")
    if accounts_json:
        try:
            accounts = json.loads(accounts_json)
            logging.info(f"Loaded {len(accounts)} account(s) from IB_ACCOUNTS")
            return accounts
        except json.JSONDecodeError as e:
            logging.error(f"Failed to parse IB_ACCOUNTS JSON: {e}")
            return []

    flex_token = os.getenv("IB_FLEX_TOKEN")
    activity_query_id = os.getenv("IB_ACTIVITY_QUERY_ID")
    confirmation_query_id = os.getenv("IB_CONFIRMATION_QUERY_ID")
    account_id = os.getenv("IB_ACCOUNT_ID")

    if flex_token and activity_query_id and account_id:
        logging.info("Loaded 1 account from individual environment variables")
        config = {
            "token": flex_token,
            "activity_query_id": activity_query_id,
            "account_id": account_id
        }
        if confirmation_query_id:
            config["confirmation_query_id"] = confirmation_query_id
        return [config]

    return []


def download_flex_reports(reports_dir: str = "./ib_reports") -> int:
    """Download IB Flex reports using configuration from environment variables."""
    accounts = _load_accounts_config()

    if not accounts:
        logging.error("No account configuration found!")
        logging.error("\nOption 1 - Single account:")
        logging.error("  IB_FLEX_TOKEN: Your Flex Query API token")
        logging.error("  IB_ACTIVITY_QUERY_ID: Your Activity Flex Query ID")
        logging.error("  IB_CONFIRMATION_QUERY_ID: Your Confirmation Flex Query ID (optional)")
        logging.error("  IB_ACCOUNT_ID: Your IB account ID (e.g., U11920545)")
        logging.error("\nOption 2 - Multiple accounts:")
        logging.error('  IB_ACCOUNTS: JSON array like [{"token":"...","activity_query_id":"123","confirmation_query_id":"456","account_id":"U111"},...]')
        logging.error("\nOptional:")
        logging.error("  IB_REPORTS_DIR: Directory to save reports (default: ./ib_reports)")
        return 1

    total_success = 0
    total_failed = 0

    for i, account_config in enumerate(accounts, 1):
        account_id = account_config.get("account_id", "Unknown")

        logging.info(f"\n{'#'*70}")
        logging.info(f"# Processing account {i}/{len(accounts)}: {account_id}")
        logging.info(f"{'#'*70}")

        try:
            downloader = IBFlexDownloader(
                flex_token=account_config["token"],
                activity_query_id=account_config["activity_query_id"],
                confirmation_query_id=account_config.get("confirmation_query_id"),
                account_id=account_id,
                reports_dir=reports_dir
            )

            downloader.download_yearly_reports()
            total_success += 1
            logging.info(f"✓ Account {account_id} completed successfully")

        except KeyError as e:
            logging.error(f"✗ Account configuration missing required field: {e}")
            total_failed += 1
        except Exception as e:
            logging.error(f"✗ Failed to process account {account_id}: {e}")
            total_failed += 1

        if i < len(accounts):
            time.sleep(3)

    logging.info(f"\n{'='*70}")
    logging.info(f"Download Summary:")
    logging.info(f"  Total accounts: {len(accounts)}")
    logging.info(f"  Successful: {total_success}")
    logging.info(f"  Failed: {total_failed}")
    logging.info(f"{'='*70}")

    return 0 if total_failed == 0 else 1


def csvs_in_dir(directory: str):
    ret = []
    for filename in os.scandir(directory):
        if not filename.is_file():
            continue
        if not filename.name.lower().endswith('.csv'):
            continue
        ret.append(filename.path)
    return sorted(ret)


def parse_reports(activity_reports_dir: str, confirmation_reports_dir: str) -> InteractiveBrokersReportParser:
    parser_object = InteractiveBrokersReportParser()

    activity_reports = csvs_in_dir(activity_reports_dir)
    confirmation_reports = csvs_in_dir(confirmation_reports_dir)

    for apath in activity_reports:
        logging.info('Activity report %s', apath)
    for cpath in confirmation_reports:
        logging.info('Confirmation report %s', cpath)

    logging.info('start reports parse')
    parser_object.parse_csv(
        activity_csvs=activity_reports,
        trade_confirmation_csvs=confirmation_reports,
    )
    logging.info(f'end reports parse {parser_object}')

    # Log account IDs found in reports
    activity_account = getattr(parser_object, 'account', None)
    confirmation_ids = getattr(parser_object, '_confirmation_account_ids', set())

    if activity_account:
        logging.info(f"✓ Account ID from activity reports: {activity_account}")

    if confirmation_ids:
        if len(confirmation_ids) > 1:
            logging.info(f"ℹ Multiple account IDs found in confirmation reports: {sorted(confirmation_ids)}")
            # If activity report has an account, validate confirmation reports include it
            if activity_account and activity_account not in confirmation_ids:
                logging.warning(
                    f"Activity report account '{activity_account}' not found in confirmation reports {sorted(confirmation_ids)}"
                )
        else:
            confirmation_account = next(iter(confirmation_ids))
            logging.info(f"✓ Account ID from confirmation reports: {confirmation_account}")

            # Validate activity and confirmation match if both present
            if activity_account and activity_account != confirmation_account:
                raise ValueError(
                    f"Account ID mismatch between reports: "
                    f"Activity reports show '{activity_account}', "
                    f"but confirmation reports show '{confirmation_account}'. "
                    f"Please verify you are processing reports for the same account."
                )

    return parser_object


def main() -> None:
    sys.stdout.reconfigure(encoding='utf-8')  # type: ignore

    available_report_types: Dict[str, Type[ReportPresenter]] = {
        'native': NativeReportPresenter,
        'gspreadsheet': GoogleSpeadSheetPresenter,
    }

    parser = argparse.ArgumentParser()
    parser.add_argument('--download', action='store_true',
                        help='download IB Flex reports using environment variables (IB_FLEX_TOKEN, etc.)')
    parser.add_argument('--activity-reports-dir', type=str,
                        help='directory with InteractiveBrokers .csv activity reports')
    parser.add_argument('--confirmation-reports-dir', type=str,
                        help='directory with InteractiveBrokers .csv confirmation reports')
    parser.add_argument('--cache-dir', type=str, default='.cache', help='directory for caching exchange rates')
    parser.add_argument('--base-currency', type=str, default=BASE_CURRENCY, choices=['RUB', 'GBP'],
                        help='base currency for tax calculations (RUB for Russia, GBP for UK)')
    parser.add_argument('--years', type=lambda x: [int(v.strip()) for v in x.split(',')], default=[],
                        help='comma separated years for final report, omit for all')
    parser.add_argument('--verbose', nargs='?', default=False, const=True,
                        help='do not "prune" reversed dividends, show dividends tax percent, disable rounding & etc.')
    parser.add_argument('--quiet', nargs='?', default=False, const=True, help='suppress non-error messages')
    parser.add_argument('--report-type', type=str, default='gspreadsheet',
                        choices=available_report_types.keys(), help='report type [native by default]')
    parser.add_argument('--save-to', type=str, default=None, help='filepath for save report')

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    elif args.quiet:
        logging.basicConfig(level=logging.ERROR)
    else:
        logging.basicConfig(level=logging.INFO)

    # Handle download mode
    if args.download:
        reports_dir = os.getenv("IB_REPORTS_DIR", "./ib_reports")
        exit_code = download_flex_reports(reports_dir)
        sys.exit(exit_code)

    # Require report directories for processing mode
    if not args.activity_reports_dir or not args.confirmation_reports_dir:
        parser.error("--activity-reports-dir and --confirmation-reports-dir are required (or use --download mode)")

    if os.path.abspath(args.activity_reports_dir) == os.path.abspath(args.confirmation_reports_dir):
        logging.error('--activity-reports-dir and --confirmation-reports-dir MUST be different directories')
        return

    parser_object = parse_reports(args.activity_reports_dir, args.confirmation_reports_dir)

    trades = parser_object.trades
    dividends = parser_object.dividends
    fees = parser_object.fees
    interests = parser_object.interests

    if not trades:
        logging.error('no trades found')
        return

    # fixme(?) first_year without dividends
    first_year = min(trades[0].trade_date.year, dividends[0].date.year) if dividends else trades[0].trade_date.year
    if args.base_currency == 'GBP':
        exchange_provider = hmrc.ExchangeRatesGBP(year_from=first_year, cache_dir=args.cache_dir)
    elif args.base_currency == 'RUB':
        exchange_provider = cbr.ExchangeRatesRUB(year_from=first_year, cache_dir=args.cache_dir)
    else:
        logging.error(f'unsupported base currency: {args.base_currency}')
        sys.exit(1)

    dividends_report = prepare_dividends_report(dividends, exchange_provider, args.verbose) if dividends else None
    fees_report = prepare_fees_report(fees, exchange_provider, args.verbose) if fees else None
    interests_report = prepare_interests_report(interests, exchange_provider) if interests else None

    analyzer = TradesAnalyzer(trades)
    finished_trades = analyzer.finished_trades
    portfolio = analyzer.final_portfolio

    # Upload all trades not only finished
    if args.report_type == 'gspreadsheet':
        # trades_report = pandas.DataFrame(trades)
        trades_report = prepare_trades_report(finished_trades, exchange_provider) if finished_trades else None
    else:
        trades_report = prepare_trades_report(finished_trades, exchange_provider) if finished_trades else None

    if args.report_type == 'gspreadsheet':
        presenter = available_report_types[args.report_type](args.verbose, args.save_to, account_id=parser_object.account)
    else:
        presenter = available_report_types[args.report_type](args.verbose, args.save_to)
    presenter.prepare_report(trades_report, dividends_report, fees_report, interests_report, portfolio, args.years, all_trades=trades)
    presenter.present()


if __name__ == '__main__':
    main()
