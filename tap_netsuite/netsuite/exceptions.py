# pylint: disable=super-init-not-called

class TapNetSuiteException(Exception):
    pass

class TapNetSuiteQuotaExceededException(TapNetSuiteException):
    pass

# used for Symon Import error handling
class SymonException(Exception):
    def __init__(self, message, code, details=None):
        super().__init__(message)
        self.code = code
        self.details = details
