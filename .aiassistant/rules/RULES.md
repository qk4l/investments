---
apply: always
---

# Project Rules and Guidelines

## General Information
- **Project Name:** investments
- **Version:** 0.1.28
- **Purpose:** Library for analyzing Interactive Brokers reports and preparing tax documentation for UK and Russian tax residents. Includes utilities `ibtax` and `ibdds`.
- **Core Language:** Python 3.11+
- **Dependency Management:** [uv](https://github.com/astral-sh/uv) (primary) or [Poetry](https://python-poetry.org/)
- **Repository:** https://github.com/qk4l/investments

## Code Style & Standards

### Python Version & Formatting
- **Minimum Python:** 3.11
- **Max line length:** 200 characters (flake8)
- **isort line length:** 128 characters
- **Linter:** `wemake-python-styleguide` with custom `.flake8` config
- **Type Checker:** `mypy` (mandatory, runs in CI)

### Naming Conventions
- **Modules/Packages:** `snake_case`
- **Classes:** `PascalCase`
- **Functions/Variables:** `snake_case` (min length: 1 character allowed)
- **Constants:** `UPPER_SNAKE_CASE`
- **Private attributes:** prefix with underscore `_`

### Code Complexity Limits
- Max line complexity: 25
- Max local variables: 25
- Max cognitive score: 24
- Max methods per class: 20
- Max function arguments: 8
- Max expressions: 15
- Max imports: 16

### Comments & Documentation
- **Code comments/docstrings:** English preferred (but optional - D100-D107 ignored)
- **User-facing docs:** Russian (target audience is Russian residents)
- Use `# noqa:` directives for specific rule violations
- TODOs acceptable for known limitations

## Architecture Patterns

### Domain Models - Use Dataclasses
Always use `@dataclass` for domain entities:
```python
from dataclasses import dataclass
from investments.money import Money
from investments.ticker import Ticker

@dataclass
class Trade:
    ticker: Ticker
    trade_date: datetime.datetime
    settle_date: datetime.date
    quantity: int
    price: Money
    fee: Money
    fee_per_piece: Money = None

    def __post_init__(self):
        self.fee_per_piece = self.fee / abs(self.quantity)
```

### Enums for Type Safety
Use `Enum` with `@unique` decorator:
```python
from enum import Enum, unique
from typing import Tuple

@unique
class Currency(Enum):
    USD = (('USD', '$', 'USD'), '840', 'R01235')
    EUR = (('EUR', 'EUR'), '978', 'R01239')

    def __init__(self, aliases: Tuple[str], iso_code: str, cbr_code: str):
        self._iso_code = iso_code
        self._cbr_code = cbr_code
        self.aliases = aliases
        self.currency_name = aliases[0]

    @staticmethod
    def parse(search: str):
        try:
            return [c for _, c in Currency.__members__.items() if search in c.aliases][0]
        except IndexError:
            raise ValueError(search)
```

### Money Value Object Pattern
**CRITICAL:** NEVER use `float` for monetary calculations. Always use `Money` class:
```python
from decimal import Decimal
from investments.money import Money
from investments.currency import Currency

# Always construct Money objects
amount = Money('10.50', Currency.USD)  # String or Decimal
amount = Money(Decimal('10.50'), Currency.USD)
amount = Money(10, Currency.USD)  # int acceptable

# Money enforces currency consistency
total = Money(100, Currency.USD) + Money(50, Currency.USD)  # OK
mixed = Money(100, Currency.USD) + Money(50, Currency.EUR)  # Raises TypeError
```

Key Money class features:
- Internal storage uses `Decimal` for precision
- Implements `__eq__`, `__lt__`, `__add__`, `__sub__`, `__mul__`, `__truediv__`
- Enforces same-currency operations
- Supports `round(digits)` method

## Financial Calculations
- **Precision:** ALWAYS use `Decimal` or `Money` class. NEVER `float`.
- **Money Class:** Use `investments.money.Money` for all monetary operations
- **Accounting Method:** FIFO (First-In, First-Out) for trade calculations (`investments/trades_fifo.py`)
- **Currency Conversion:** Always use exchange rate providers, never hardcode rates
- **Tax Calculations:** Follow Russian tax code (НК РФ) - convert to RUB using CBR rates on settlement date

## Data Providers Pattern

### Exchange Rate Provider Base Class
```python
from typing import Dict, Optional
import pandas
from investments.currency import Currency
from investments.money import Money

class ExchangeRatesProvider:
    base_currency: Currency
    _cache_dir: Optional[str]
    _frames_loaded: Dict[str, pandas.DataFrame]

    def __init__(self, cache_dir: Optional[str] = None):
        self._frames_loaded = {}
        self._cache_dir = cache_dir
        if cache_dir and not os.path.exists(cache_dir):
            os.makedirs(cache_dir)

    def get_rate(self, currency: Currency, dt: datetime.datetime) -> Money:
        if currency is self.base_currency:
            return Money(1, self.base_currency)

        if currency.name not in self._frames_loaded:
            self._fetch_currency_rates(currency, dt)

        rates = self._frames_loaded.get(currency.name)
        return rates.loc[dt].item()

    def convert_to_base_currency(self, source: Money, rate_date: datetime.datetime) -> Money:
        if source.currency == self.base_currency:
            return Money(source.amount, self.base_currency)
        rate = self.get_rate(source.currency, rate_date)
        return Money(source.amount * rate.amount, rate.currency)
```

### Implementing Providers
- Inherit from `ExchangeRatesProvider`
- Set `base_currency` class attribute
- Implement `_fetch_currency_rates(currency, dt)` method
- Use `DataFrameCache` for persistent caching

### Caching Strategy
```python
from investments.data_providers.cache import DataFrameCache

cache_key = f'cbrates_{currency.cbr_code}_since{self._year_from}.cache'
cache = DataFrameCache(self._cache_dir, cache_key, datetime.timedelta(days=1))

df = cache.get()
if df is not None:
    logging.info('cache hit')
    self._frames_loaded[frame_key] = df
    return

# Fetch data from external API
# ...

cache.put(df)
self._frames_loaded[frame_key] = df
```

Cache key patterns:
- `cbrates_{cbr_code}_since{year}.cache` - CBR exchange rates
- `hmrc_rates_{year}_{currency}.cache` - HMRC exchange rates
- Default TTL: 1 day

## Pandas Usage Patterns

### DataFrame Operations
```python
import pandas  # type: ignore

# Create from list of tuples
rates_data: List[Tuple[datetime.date, Money]] = []
df = pandas.DataFrame(rates_data, columns=['date', 'rate'])
df.set_index(['date'], inplace=True)

# Reindex to fill missing dates and forward-fill values
today = datetime.datetime.utcnow().date()
df = df.reindex(pandas.date_range(df.index.min(), today))
df['rate'].fillna(method='pad', inplace=True)
```

### Type Handling
- Always use `# type: ignore` for pandas imports
- Include `pandas-stubs` in dev dependencies
- Prefer explicit column operations over chaining

## Data Parsing Patterns

### CSV Parsing with Named Rows
```python
from typing import Dict, List

class NamedRowsParser:
    def __init__(self):
        self._fields = []

    def parse_header(self, fields: List[str]):
        self._fields = fields

    def parse(self, row: List[str]) -> Dict[str, str]:
        error_msg = f'expect {len(self._fields)} rows, but got {len(row)}'
        assert len(row) == len(self._fields), error_msg
        return dict(zip(self._fields, row))
```

### Date/Time Parsing
```python
from datetime import datetime

def parse_datetime(strval: str) -> datetime:
    return datetime.strptime(strval.replace(' ', ''), '%Y-%m-%d,%H:%M:%S')

def parse_date(strval: str) -> datetime.date:
    return datetime.strptime(strval, '%Y-%m-%d').date()
```

### Ticker Storage Pattern
```python
class TickersStorage:
    def __init__(self):
        self._tickers = {}  # type dict[str: Ticker]
        self._conid_to_ticker = {}
        self._symbols = {}  # type dict[str: Ticker]
        self._conflict_symbols = {}

    def put(self, *, symbol: str, conid: str, security_id: str, ...):
        ticker = Ticker(symbol=symbol, kind=kind, security_id=security_id, ...)

        # Handle symbol conflicts gracefully
        if ticker.symbol in self._symbols:
            logging.warning(f"Symbol conflict: {ticker} and {self._symbols[ticker.symbol]}")
            self._symbols[ticker.symbol] = None  # Disable lookup by symbol
            self._conflict_symbols[ticker.symbol] = ticker
        else:
            self._symbols[ticker.symbol] = ticker

        self._tickers[ticker.security_id] = ticker
```

## Error Handling

### Assertions
Use assertions liberally (S101 ignored in flake8):
```python
assert len(row) == len(self._fields), f'expect {len(self._fields)} rows {self._fields}, but got {len(row)} rows ({row})'
assert isinstance(rate_date, datetime.datetime)
assert rec.get('Id') == currency.cbr_code
```

### Logging Levels
```python
import logging

logging.debug(f"Getting rate for {currency} for {dt}")
logging.info(f'load currency rates from cbr.ru {currency} {cache_key}')
logging.warning(f"Disable get_ticker_by_symbol() for symbol {symbol}")
```

### Custom Exceptions
Define in `investments/exceptions.py`:
```python
class InvestmentsTickerNotFound(Exception):
    pass
```

## Testing Strategy

### Test File Naming
- Test files end with `_test.py` (e.g., `cbr_test.py`, `money_test.py`)
- Mirror source structure in `tests/` directory

### Parametrized Tests
```python
import pytest
from datetime import datetime
from investments.currency import Currency
from investments.money import Money

test_cases = [
    (datetime(2015, 1, 15), Currency.USD, Money('66.0983', Currency.RUB)),
    (datetime(2020, 3, 31), Currency.USD, Money('77.7325', Currency.RUB)),
]

@pytest.mark.parametrize('trade_date,currency,expect_rate', test_cases)
def test_exchange_rates_rub(trade_date: datetime, currency: Currency, expect_rate: Money):
    provider = ExchangeRatesRUB(year_from=2015, cache_dir=None)
    rate = provider.get_rate(currency, trade_date)
    assert rate == expect_rate
```

### Handling External Dependencies
```python
from requests.exceptions import ConnectionError

try:
    provider = ExchangeRatesRUB(year_from=2015, cache_dir=None)
except ConnectionError as ex:
    pytest.skip(f'connection error: {ex}')
    return
```

### Test Coverage
- High coverage for core calculations (FIFO, currency conversion, interest)
- Use representative CSV samples (mocked or anonymized) for parsers
- Always create reproduction test case when fixing bugs

## Type Annotations

### Required Type Hints
All function parameters and return types must be annotated:
```python
def get_rate(self, currency: Currency, dt: datetime.datetime) -> Money:
    pass

def parse(self, row: List[str]) -> Dict[str, str]:
    pass
```

### Type Comments for Collections
```python
self._tickers = {}  # type dict[str: Ticker]
self._symbols = {}  # type dict[str: Ticker]
```

### Type Checking
- Run `mypy investments/` in CI (mandatory)
- Use `# type: ignore` for third-party libraries without stubs
- Prefer explicit types over inference

## Project Structure
```
investments/
├── __init__.py
├── currency.py          # Currency enum with CBR codes
├── money.py             # Money value object
├── ticker.py            # Ticker domain entity
├── trade.py             # Trade dataclasses
├── dividend.py          # Dividend dataclass
├── fees.py              # Fee handling
├── interests.py         # Interest calculations
├── cash.py              # Cash operations
├── calculators.py       # Financial calculations
├── trades_fifo.py       # FIFO trade matching
├── data_providers/      # External data sources
│   ├── __init__.py
│   ├── cbr.py          # Central Bank of Russia rates
│   ├── hmrc.py         # UK HMRC rates
│   ├── moex.py         # Moscow Exchange data
│   ├── cache.py        # DataFrame caching utilities
│   └── exchange_provider.py  # Base provider class
├── report_parsers/      # Broker report parsers
│   ├── __init__.py
│   ├── ib.py           # Interactive Brokers CSV parser
│   └── open_fr.py      # OpenFR format parser
├── ibtax/              # Tax calculation utility
│   └── ibtax.py        # Main entry point
└── ibdds/              # Cash flow utility
    └── ibdds.py        # Main entry point

tests/                  # Mirror structure of investments/
├── currency_test.py
├── money_test.py
├── data_providers/
│   ├── cbr_test.py
│   └── moex_test.py
└── report_parsers/
    └── ib_test.py
```

## Dependencies

### Core Runtime Dependencies
```toml
requests >= 2.31         # HTTP client for external APIs
tabulate >= 0.9          # Table formatting for reports
pandas >= 2.3.3          # Data manipulation
aiomoex >= 2.0           # Moscow Exchange API
WeasyPrint >= 58.0       # PDF generation
jinja2 >= 3.1            # Template engine for reports
google-auth-oauthlib     # Google Sheets integration
google_spreadsheet       # Google Sheets API wrapper
```

### Development Dependencies
```toml
pytest >= 7.2            # Testing framework
mypy >= 1.1              # Type checking (mandatory)
wemake-python-styleguide # Linting rules
types-requests           # Type stubs for requests
types-tabulate           # Type stubs for tabulate
pandas-stubs             # Type stubs for pandas
```

## CLI Applications

### Entry Points (pyproject.toml)
```toml
[project.scripts]
ibtax = "investments.ibtax.ibtax:main"
ibdds = "investments.ibdds.ibdds:main"
ib-flex-download = "investments.ib_flex_downloader:main"
```

**IMPORTANT:** Always provide ONE unified entry point for users. Users should NOT need to run separate programs for downloading reports. The downloader automatically handles both Activity and Confirmation reports in a single execution.

### Argument Parsing Pattern
```python
import argparse
import sys

def main():
    parser = argparse.ArgumentParser(
        description='Interactive Brokers tax calculator for UK and Russian residents'
    )
    parser.add_argument('--activity-reports-dir', required=True,
                       help='Directory with IB Activity CSV reports')
    parser.add_argument('--confirmation-reports-dir',
                       help='Directory with IB Trade Confirmation CSV reports')
    parser.add_argument('--base-currency', choices=['RUB', 'GBP'],
                       help='Base currency for tax calculations (RUB for Russia, GBP for UK)')
    parser.add_argument('--verbose', action='store_true',
                       help='Show detailed calculations')
    parser.add_argument('--save-to', help='Save report to PDF file')

    args = parser.parse_args()
    # Implementation
```

## Allowed Relaxations (from .flake8)

The following are explicitly allowed:
- **S101:** Assertions in production code (encouraged)
- **D100-D107:** Missing docstrings (optional)
- **E800:** Commented out code (for reference/TODOs)
- **WPS305:** F-strings (preferred over %-formatting)
- **WPS237:** Complex f-strings
- **WPS318, WPS317:** Extra indentation, multilines
- **WPS420:** `pass` statements
- **WPS421:** `print()` function
- **WPS454:** Raising bare `Exception`
- **WPS602:** `@staticmethod` decorator
- **S314, S405:** XML parsing with ElementTree (for CBR API)

## Git Workflow
- **Main Branch:** `master`
- **Commit Messages:** Clear and descriptive, preferably in English
- **Co-authorship:** When using AI assistance, include:
  ```
  Co-Authored-By: Claude <noreply@anthropic.com>
  ```

## CI/CD (GitHub Actions)
```yaml
# .github/workflows/tests.yml
- Run pytest -ra -v
- Run mypy investments/  # Mandatory, must pass
- Run flake8 investments/ # Mandatory, must pass
```

Test matrix: Python 3.8, 3.9, 3.10 (should update to 3.11+)

## External API Integrations

### Central Bank of Russia (CBR)
- **URL:** `http://www.cbr.ru/scripts/XML_dynamic.asp`
- **Purpose:** Currency exchange rates for tax calculations
- **Data Format:** XML
- **Caching:** 1 day TTL
- **Provider:** `investments.data_providers.cbr.ExchangeRatesRUB`

### HMRC (UK Tax Authority)
- **URL:** `https://hmrc.matchilling.com/rate/{year}/{month}.json`
- **Purpose:** GBP exchange rates for UK tax calculations
- **Data Format:** JSON
- **Caching:** 1 day TTL
- **Provider:** `investments.data_providers.hmrc.ExchangeRatesGBP`
- **Parameters:** `year_from` (default: 2015), `cache_dir` (optional)

### Moscow Exchange (MOEX)
- **Library:** `aiomoex`
- **Purpose:** Stock market data
- **Provider:** `investments.data_providers.moex`

### Interactive Brokers Flex Web Service
- **URL:** `https://gdcdyn.interactivebrokers.com/Universal/servlet`
- **Purpose:** Automated download of Activity and Trade Confirmation reports
- **Authentication:** Token-based (generated in IB Account Management)
- **Data Format:** XML (saved as-is for later parsing)
- **Reports Downloaded:**
  - Activity reports → `ib_reports/activity/YYYYMMDD_YYYYMMDD_ACCOUNT.csv`
  - Confirmation reports → `ib_reports/confirmation/YYYYMMDD_YYYYMMDD_ACCOUNT.csv`
- **Provider:** `investments.ib_flex_downloader.IBFlexDownloader`
- **CLI Tool:** `ib-flex-download`
- **Configuration:** Via environment variables (see `.env.example`)
  - Single account: `IB_FLEX_TOKEN`, `IB_ACTIVITY_QUERY_ID`, `IB_CONFIRMATION_QUERY_ID`, `IB_ACCOUNT_ID`
  - Multiple accounts: `IB_ACCOUNTS` (JSON array)
- **Behavior:** Downloads both report types in one execution, organizes by subdirectories

## Common Pitfalls to Avoid

1. **Never use float for money:** Always use `Decimal` or `Money` class
2. **Never hardcode exchange rates:** Always use data providers
3. **Never skip cache_dir creation:** Check and create in `__init__`
4. **Never assume symbol uniqueness:** Use `security_id` as primary key
5. **Never commit without type checking:** `mypy` must pass
6. **Never ignore flake8:** Fix violations or add `# noqa:` with reason
7. **Handle symbol conflicts:** Log warnings, maintain multiple indexes

