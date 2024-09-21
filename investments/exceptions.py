import logging

logger = logging.getLogger(__name__)


class Investments(Exception):
    def __init__(self, message=None, errors=None):
        if errors:
            message = ', '.join(errors)
        self.errors = errors
        if message:
            logger.error(message.rstrip())
        super(Exception, self).__init__(message)


