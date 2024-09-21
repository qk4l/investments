import logging
import pathlib
import os
import pickle
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

    def set_values(self, spreadsheet_id, sheet_range: str, data: List[str | Any]):
        self.service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            valueInputOption='RAW',
            range=sheet_range,
            body=dict(
                majorDimension='ROWS',
                values=data
            )
        ).execute()
        logger.debug(f'Sheet {spreadsheet_id} successfully Updated')
