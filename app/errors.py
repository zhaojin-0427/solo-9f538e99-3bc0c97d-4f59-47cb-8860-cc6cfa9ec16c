"""Domain errors and their HTTP mapping."""

from __future__ import annotations


class DomainError(Exception):
    """Base class for expected, client-visible failures."""

    status_code: int = 400
    code: str = "bad_request"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class DuplicatePatientError(DomainError):
    status_code = 409
    code = "duplicate_patient_in_week"


class WeekAlreadyImportedError(DomainError):
    status_code = 409
    code = "week_already_imported"


class ImportNotFoundError(DomainError):
    status_code = 404
    code = "import_version_not_found"


class PublicationNotFoundError(DomainError):
    status_code = 404
    code = "publication_version_not_found"


class BudgetExhaustedError(DomainError):
    status_code = 409
    code = "privacy_budget_exhausted"


class ReconciliationDateOutsideWeekError(DomainError):
    """The submitted day is not one of the import's seven ISO-week days."""

    status_code = 422
    code = "reconciliation_day_outside_week"


class ReconciliationClosedError(DomainError):
    """The version was already published; corrections can no longer land."""

    status_code = 409
    code = "reconciliation_closed"


class ReconciliationIncompleteError(DomainError):
    """Publication blocked: not all seven daily totals have been registered."""

    status_code = 409
    code = "reconciliation_incomplete"


class ReconciliationTotalMismatchError(DomainError):
    """Publication blocked: the seven-day total differs from the import size."""

    status_code = 409
    code = "reconciliation_total_mismatch"


class StorageError(DomainError):
    """A write failed; the transaction was rolled back and no budget was spent."""

    status_code = 503
    code = "storage_write_failed"
