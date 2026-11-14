import os
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines from a .env file; real environment variables take precedence."""
    if not path.is_file():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# .env in the current directory first, then in the repository root
for _env_path in (Path.cwd() / '.env', Path(__file__).resolve().parent.parent / '.env'):
    _load_dotenv(_env_path)

BASE_CURRENCY = 'GBP'
# BASE_CURRENCY = 'RUB'
GOOGLE_SERVICE_ACCOUNT_FILE = os.path.expanduser(os.getenv('GOOGLE_SERVICE_ACCOUNT_FILE', '~/.config/investments.json'))
GOOGLE_SHEET_ID = os.getenv('GOOGLE_SHEET_ID')
