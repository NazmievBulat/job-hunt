"""How a link is printed on the contact line of the PDF and the Word file."""

import re
import string
import unicodedata

from resume_kit.contracts.printable import unprintable_characters

# The scheme, and a "www." in front of a host that still has a dot after it ("www.com" stays).
_SCHEME_AND_WWW = re.compile(r"^https?://(?:www\.(?=[^/?#:]*\.))?", re.IGNORECASE)
# A whole label of the host written as "xn--" and its letters encoded in ASCII (IDNA).
_ENCODED_LABEL = re.compile(r"xn--([A-Za-z0-9-]+)", re.IGNORECASE)
# A run of encoded bytes ("%C3%B3").
_ENCODED_BYTES = re.compile(r"(?:%[0-9A-Fa-f]{2})+")
# The one invisible character the PDF prints that is no control, format or space character:
# the combining grapheme joiner. The page finds it by its Unicode property, which Python's
# `unicodedata` lacks; `resume-draft.test.ts` checks it is still the only one.
_INVISIBLE = "\u034f"
# The ASCII characters an address means the same by, written or encoded (RFC 3986, "unreserved").
_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")


def printed_address(url: str) -> str:
    """The address as a reader of the printed page types it: "linkedin.com/in/rosa".

    A label alone ("Website") leads nowhere on paper, so the line holds the address, without its
    scheme, a leading "www." and one "/" at the end of its path. A "/" that ends a query or a
    fragment is part of a value and stays. Letters the stored address holds encoded are printed
    as the person wrote them (`readable_letters`); the rest is printed as stored. The resume
    page follows the same rule.
    """
    printed = readable_letters(_SCHEME_AND_WWW.sub("", url, count=1))
    if "?" in printed or "#" in printed:
        return printed
    return printed.removesuffix("/")


def readable_letters(address: str) -> str:
    """The address with its encoded letters shown: "müller-design.de/józef".

    The host name's "xn--" labels are decoded, and so is each encoded character of the rest that
    is a letter beyond ASCII, or an ASCII letter, digit, "-", ".", "_" or "~" ("%7E" is "~").
    What would not read the same is kept encoded: any other ASCII character ("%2F" is not "/"),
    bytes that are not UTF-8, white space, a control, format or other invisible character, and a
    character the PDF cannot print.
    """
    end = len(address)
    for mark in "/?#":
        found = address.find(mark)
        if found != -1:
            end = min(end, found)
    # The host name is what stands between a "user@" and a ":port".
    start = address.rfind("@", 0, end) + 1
    colon = address.find(":", start, end)
    host_end = end if colon == -1 else colon
    labels = (_decoded_label(label) for label in address[start:host_end].split("."))
    host = ".".join(labels)
    rest = _ENCODED_BYTES.sub(_decoded_bytes, address[host_end:])
    return address[:start] + host + rest


def _readable(character: str) -> bool:
    return (
        not character.isascii()
        and character != _INVISIBLE
        and not _noncharacter(ord(character))
        and unicodedata.category(character) not in ("Cc", "Cf", "Zs", "Zl", "Zp")
        and not unprintable_characters(character)
    )


def _noncharacter(code: int) -> bool:
    """One of the code points Unicode keeps from ever being a character. The fonts hold U+FFFF,
    but the XML of a Word file has no place for it."""
    return 0xFDD0 <= code <= 0xFDEF or code & 0xFFFE == 0xFFFE


def _decoded_label(label: str) -> str:
    encoded = _ENCODED_LABEL.fullmatch(label)
    if encoded is None:
        return label
    try:
        letters = encoded[1].lower().encode("ascii").decode("punycode")
    except UnicodeError:
        return label
    if letters.isascii():
        return label
    if not all(character.isascii() or _readable(character) for character in letters):
        return label
    return letters


def _decoded_bytes(match: re.Match[str]) -> str:
    encoded = match[0]
    data = bytes.fromhex(encoded.replace("%", ""))
    printed: list[str] = []
    index = 0
    while index < len(data):
        length = _utf8_length(data[index])
        try:
            character = data[index : index + length].decode("utf-8") if length else ""
        except UnicodeDecodeError:
            character = ""
        if len(character) == 1 and (character in _UNRESERVED or _readable(character)):
            printed.append(character)
            index += length
        else:
            printed.append(encoded[index * 3 : index * 3 + 3])
            index += 1
    return "".join(printed)


def _utf8_length(lead: int) -> int:
    """How many bytes a UTF-8 character that starts with `lead` has; 0 when none starts so."""
    if lead < 0x80:
        return 1
    if 0xC2 <= lead <= 0xDF:
        return 2
    if 0xE0 <= lead <= 0xEF:
        return 3
    if 0xF0 <= lead <= 0xF4:
        return 4
    return 0
