"""Errors shared by the page parsers of every source."""


class ParseError(Exception):
    """An expected element is missing or malformed: the page layout changed."""

    def __init__(self, page: str, detail: str) -> None:
        super().__init__(f"{page}: {detail}")
        self.page = page
        self.detail = detail
