"""Content-free extraction diagnostics shared by local and Drive indexing."""

MAX_FILE_BYTES = 512 * 1024 * 1024
MESSAGES = {
    "xlsx_unsupported_color": "Workbook contains an unsupported stylesheet color.",
    "xlsx_stylesheet_unreadable": "Workbook stylesheet could not be read safely.",
    "xlsx_archive_limit": "Workbook exceeds an archive safety limit.",
    "xlsx_parse_failed": "Workbook could not be parsed.",
}


class ExtractionError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(MESSAGES[code])


def error_fields(stage: str, error: Exception) -> str:
    result = f"stage={stage} error={type(error).__name__}"
    if isinstance(error, ExtractionError):
        result += f" code={error.code}"
    return result
