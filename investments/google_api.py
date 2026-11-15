import logging
import math
import pathlib
import os
import pickle
from decimal import Decimal
from typing import List, Any

from googleapiclient.discovery import build
from google.oauth2 import service_account

from investments.defaults import GOOGLE_SERVICE_ACCOUNT_FILE
from investments.exceptions import Investments

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']


class GoogleAPI(object):
    def __init__(self):
        logger.debug(f"Init google sheet class")

        if GOOGLE_SERVICE_ACCOUNT_FILE is None:
            raise Investments(f"Define env variable GOOGLE_SERVICE_ACCOUNT_FILE")
        if not pathlib.Path(GOOGLE_SERVICE_ACCOUNT_FILE).exists():
            raise Investments(f"Google Service account file not found: {GOOGLE_SERVICE_ACCOUNT_FILE}")
        creds = service_account.Credentials.from_service_account_file(
            GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES)

        if os.path.exists('token.pickle'):
            with open('token.pickle', 'rb') as token:
                creds = pickle.load(token)

        self.service = build('sheets', 'v4', credentials=creds)
        self.sheet = self.service.spreadsheets()

    def get_values(self, spreadsheet_id: str, sheet_range: str):
        result_input = self.sheet.values().get(spreadsheetId=spreadsheet_id,
                                               range=sheet_range).execute()

        return result_input.get('values', [])

    def ensure_sheet(self, spreadsheet_id: str, title: str):
        """Create the sheet (tab) if the spreadsheet does not have it yet."""
        meta = self.sheet.get(spreadsheetId=spreadsheet_id, fields='sheets.properties.title').execute()
        if title in {s['properties']['title'] for s in meta.get('sheets', [])}:
            return
        self.sheet.batchUpdate(spreadsheetId=spreadsheet_id,
                               body={'requests': [{'addSheet': {'properties': {'title': title}}}]}).execute()
        logger.info(f'Sheet {title} created')

    def set_values(self, spreadsheet_id, sheet_range: str, data: List[str | Any], clear_below: bool = True):
        """Write rows starting at A1 of `sheet_range` (a sheet name).

        With `clear_below` the rows left from a previous longer upload are cleared,
        only in the columns written now (formulas in columns to the right stay untouched).
        """
        data = [[_to_cell(v) for v in row] for row in data]
        self.service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            valueInputOption='RAW',
            range=sheet_range,
            body=dict(
                majorDimension='ROWS',
                values=data
            )
        ).execute()
        logger.debug(f'Sheet {spreadsheet_id} successfully Updated. Sheet range: {sheet_range}')

        if clear_below and data and '!' not in sheet_range:
            last_column = _column_letter(max(len(row) for row in data))
            stale_range = f'{sheet_range}!A{len(data) + 1}:{last_column}'
            self.service.spreadsheets().values().clear(spreadsheetId=spreadsheet_id, range=stale_range, body={}).execute()
            logger.debug(f'Cleared stale rows {stale_range}')


def _to_cell(value: Any) -> Any:
    """Make values JSON serializable for the Sheets API."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float) and math.isnan(value):
        return ''
    return value


def _column_letter(n: int) -> str:
    letters = ''
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord('A') + rem) + letters
    return letters
