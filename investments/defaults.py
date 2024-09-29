import os

BASE_CURRENCY = 'GBP'
# BASE_CURRENCY = 'RUB'
GOOGLE_SERVICE_ACCOUNT_FILE = os.path.expanduser(os.getenv('GOOGLE_SERVICE_ACCOUNT_FILE', '~/.config/investments.json'))
GOOGLE_SHEET_ID = os.getenv('GOOGLE_SHEET_ID')