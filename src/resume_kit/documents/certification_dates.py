"""How a certification line is printed on a resume: its dates and the address ending it."""

import re
from collections.abc import Callable

from resume_kit.documents.printed_address import printed_address

# A year and a month standing alone: "2023-03". Not part of a longer number or code
# ("27001-2013", "A2023-03"), and not the start of a full date ("2023-03-15").
_YEAR_AND_MONTH = re.compile(r"(?<![\w-])\d{4}-(?:0[1-9]|1[0-2])(?![\w-])")


def with_months_as_printed(line: str, printed: Callable[[str], str]) -> str:
    """The certification line with each "2023-03" written the way the file prints a role's date.

    A certification is a line of text the model writes, and the model is told to write dates as
    "2023-03". A role's date is printed from that form as "Mar 2023"; this gives a certification's
    date the same look. A year alone, or no date, is left as it is.
    """
    return _YEAR_AND_MONTH.sub(lambda found: printed(found.group()), line)


# The saved verification address that code puts last on a line (`certification_details`).
_ADDRESS_AT_THE_END = re.compile(r"(?<= — )https?://\S+$")


def printed_certification(
    line: str, printed: Callable[[str], str], credential_word: str
) -> tuple[str, tuple[str, str] | None]:
    """The line as printed up to the verification address it ends with, and that address.

    Only what the writer wrote gets its months printed: the credential ID code adds after it
    (" — Credential ID 2024-06", `credential_word` in the resume's language) is printed as saved.
    The address is given as it is printed, as a contact link is (`printed_address`), and as the
    address it leads to; its months are left alone too, so ".../2024-06/..." stays an address.
    """
    found = _ADDRESS_AT_THE_END.search(line)
    rest = line if found is None else line[: found.start()]
    credential = rest.rfind(f" — {credential_word} ")
    if credential < 0:
        text = with_months_as_printed(rest, printed)
    else:
        text = with_months_as_printed(rest[:credential], printed) + rest[credential:]
    if found is None:
        return text, None
    return text, (printed_address(found.group()), found.group())
