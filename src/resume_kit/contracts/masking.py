"""A person's name and contact details, kept out of what a model reads.

Two ways, both plain code:

- `Masks` swaps email addresses, phone numbers, links and a known name for placeholders
  (`[EMAIL_1]`, `[PHONE_1]`, `[LINK_1]`, `[NAME]`) before a model reads a text, and swaps them
  back in what the model answers. The same address gets the same placeholder however often it
  is masked, so a link in the text and in the file's list of links stays one link. A
  placeholder the text itself already holds ("it showed [EMAIL_1]") is never made again, so it
  comes back as it was written.
- `without_own_details` takes a person's own name, email, phone and links out of a text the
  model does not have to copy them from (the writer's input: the code puts the person on the
  resume itself).

A name is masked only when it is known (the account's, the profile's). Nothing here guesses
which words of a text are a name, and a name of one word is not masked: it is too often an
ordinary word ("Owner", "Grant").
"""

import re
from collections.abc import Iterable
from typing import Literal, cast

from resume_kit.contracts.resume import Person

MaskKind = Literal["email", "phone", "link", "name"]
ALL_KINDS: frozenset[MaskKind] = frozenset({"email", "phone", "link", "name"})

# Letters of any script, as an address may hold them ("müller@example.com").
_EMAIL = re.compile(r"(?<![\w.%+-])[\w.%+-]+@[\w-]+(?:\.[\w-]+)*\.[^\W\d_]{2,}(?![\w-])")
# An address with its scheme or "www.", or a host followed by a path ("linkedin.com/in/maria").
# A host alone ("Booking.com") is often a company's name, and is left. A host's last part is
# written in small letters and is not a file type of code, so "Node.js/React", "ASP.NET/C#" and
# "M.Sc/MBA" are not links.
_LINK = re.compile(
    r"(?i:https?://|www\.)[^\s<>\"'()\[\]]+"
    r"|(?<![\w@./-])(?:[A-Za-z0-9-]+\.)+(?!(?:js|ts)/)[a-z]{2,}/[^\s<>\"'()\[\]]*"
)
_LINK_END = ".,;:!?"
# A host alone ("alexrivera.com") and a handle ("@alexrivera"): masked only when they hold a word
# of the known name, or are the host of a link in the file's list of links.
_BARE_HOST = re.compile(r"(?<![\w@./-])(?:[A-Za-z0-9-]+\.)+[a-z]{2,}(?![\w/@-]|\.[A-Za-z0-9])")
_HANDLE = re.compile(r"(?<![\w.@])@[A-Za-z0-9_](?:[A-Za-z0-9_.]*[A-Za-z0-9_])?")
_JOINERS = re.compile(r"[-_.@]")


def _joined(text: str) -> str:
    """`text` in small letters without the marks that join the words of a host or a handle."""
    return _JOINERS.sub("", text.casefold())


# Digits with spaces, dots, hyphens and brackets between them, on one line, at most 32
# characters (a phone has fewer), so a long line costs no more than its length. What counts as a
# phone is decided by `_is_phone`, on the whole run and then on its parts.
# No-break spaces count as spaces: text pasted from Word or a PDF holds them.
_PHONE_CANDIDATE = re.compile(
    r"(?<![\w.+/-])\+?[\d(][\d \u00a0\u2007\u202f().-]{0,30}\d(?![\w/]|\.\d)"
)
_FEWEST_PHONE_DIGITS = 9  # more than any date range ("2019-2023" has 8)
_MOST_PHONE_DIGITS = 15
_LONGEST_LOCAL_GROUP = 4
# A phone's label just before a number: "Phone:", "Tel.", "Mobile", "Teléfono", "Telemóvel".
_PHONE_LABEL = re.compile(
    r"(?i:phone|tel|telephone|mobile|cell|tel[eé]fono|telefone|telem[oó]vel|m[oó]vil)\W{0,3}\Z"
)
_LABEL_REACH = 16
_MOST_PHONE_WORDS = 8  # "+1 ( 555 ) 123 - 45 67"
# A phone written in two groups, as in India or Germany; a scan's misread dates have more digits.
_FEWEST_TWO_GROUP_DIGITS = 10
_MOST_TWO_GROUP_DIGITS = 12
_OPENING = re.compile(r"(?:\+\d{1,3} ?)?\([^(]*$")
# "(212) 555 2019" is a phone, though its last group could be a year.
_NORTH_AMERICAN = re.compile(r"\(\d{3}\)[ .-]?\d{3}[ .-]?\d{4}|\d{3}([ .-])\d{3}\1\d{4}")
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d\d(?!\d)")
# An amount written in thousands: "1 200 000 000", "250 000 000".
_THOUSANDS = re.compile(r"\d{1,3}(?:[ \u00a0\u2007\u202f]\d{3})+")
# Written with dots, a phone is "555.123.4567"; "12.000.000.000" and "192.168.100.200" are not.
_DOTTED_PHONE = re.compile(r"\(?\d{3}\)?\.\d{3}\.\d{4}")
# A placeholder, found by its brackets.
_PLACEHOLDER = re.compile(r"\[((?:EMAIL|PHONE|LINK|PERSON|PLACE)_\d+|NAME(?:_\d+)?)\]")
# Also catches an invented kind without a number, or a placeholder cut off at the end.
_PLACEHOLDER_PATTERN = re.compile(r"\[((?:NAME|EMAIL|PHONE|LINK|PERSON|PLACE)(?:_\d+)?)\]?")
# A link's placeholder may have the start of its address before it
# ("https://www.linkedin.com/[LINK_1]"), as it was sent or as a model wrote it, with or without
# more of the path ("linkedin.com/in/[LINK_1]"); that start is part of the placeholder. It is
# looked for only in the few characters before a link's placeholder, so a long text costs no
# more than its length.
_ADDRESS_START = re.compile(
    r"(?:(?i:https?://)?(?:[\w-]+\.)+[A-Za-z]{2,}/(?:[^\s\[\]/]+/)*|(?i:https?://))\Z"
)
_LONGEST_ADDRESS_START = 200
# Sites whose address says what a link is, and nothing about who: a link to one of them keeps the
# start of its address ("https://www.linkedin.com/[LINK_1]"), so a model can still tell a
# LinkedIn profile from a GitHub one. A person's own site is masked whole, as its address often
# holds their name.
_PROFILE_SITES = frozenset(
    {
        "linkedin.com",
        "github.com",
        "gitlab.com",
        "bitbucket.org",
        "stackoverflow.com",
        "behance.net",
        "dribbble.com",
        "medium.com",
        "twitter.com",
        "x.com",
        "kaggle.com",
        "huggingface.co",
        "leetcode.com",
        "orcid.org",
        "researchgate.net",
        "scholar.google.com",
        "youtube.com",
        "instagram.com",
        "facebook.com",
    }
)
_SITE = re.compile(r"^(?:https?://)?(?:www\.)?([^/:?#]+)", re.IGNORECASE)
# Between the first and the last name: an initial ("Maria J. Lopez") or one more name, written
# with a capital ("Maria Teresa Lopez"). "Alex at Rivera Park" holds no name.
_CAPITAL = "A-Z\u00c0-\u00d6\u00d8-\u00de\u0386-\u03ab\u0400-\u042f"
# Spaces between words, no-break spaces included: text pasted from Word or a PDF holds them.
_SPACES = " \\t\u00a0\u2007\u202f"
_MIDDLE = rf"(?:[{_SPACES}]+(?:[^\W\d_]\.|(?-i:[{_CAPITAL}])[\w'\u2019-]*\.?))?"
_GAP = rf"[{_SPACES}]+"
# A saved phone shorter than this is not looked for in a text: it would match plain numbers.
_FEWEST_OWN_PHONE_DIGITS = 7


def _is_phone(candidate: str) -> bool:
    """A phone starts with "+" or "(", is one run of 10 or 11 digits, two groups of 10 to 12
    digits ("98765 43210"), or groups of at most 4 digits ("555-123-4567"). Dates
    ("2015-2019 (4", "01.2015-12.2019", a scan's misread "201801 02019412"), long numbers
    ("12.000.000.000") and amounts ("1 200 000 000") are none of these."""
    groups = re.findall(r"\d+", candidate)
    digits = sum(len(group) for group in groups)
    if not _FEWEST_PHONE_DIGITS <= digits <= _MOST_PHONE_DIGITS:
        return False
    # A range of numbers or dates ("2010 - 2015 2016"), not one number.
    if " - " in candidate or ".." in candidate:
        return False
    # A bracket opens only the area code, at the start or after the country's code:
    # "(555) 123-4567 (555)" is a phone and part of the next.
    if "(" in candidate and not _OPENING.match(candidate):
        return False
    if candidate.startswith("+") or _NORTH_AMERICAN.fullmatch(candidate):
        return True
    if _THOUSANDS.fullmatch(candidate):  # an amount; "Phone: 612 345 678" is caught by its label
        return False
    if len(_YEAR.findall(candidate)) >= 2:  # "2015-2019", "Awards 2019 2020 2021"
        return False
    if any(_YEAR.fullmatch(word) for word in candidate.split()):  # "555-123-4567 2019"
        return False
    if "." in candidate:
        return _DOTTED_PHONE.fullmatch(candidate) is not None
    if candidate.startswith("("):
        return True
    if len(groups) == 1:
        return digits in (10, 11)
    if len(groups) == 2 and _FEWEST_TWO_GROUP_DIGITS <= digits <= _MOST_TWO_GROUP_DIGITS:
        return True  # "98765 43210", "030 12345678"
    return all(len(group) <= _LONGEST_LOCAL_GROUP for group in groups)


def _cased(word: str, *, as_saved: bool = False) -> str:
    """A name's word as a name is written: with a capital or in capitals. "We will price the
    plan" holds no "Will Price". A word inside a name ("de" of "Juan de la Cruz") may also be
    written as saved."""
    head, rest = word[:1].upper(), word[1:]
    spellings = {head + rest, head + rest.lower(), word.upper()}
    if as_saved:
        spellings.add(word)
    return "(?:" + "|".join(re.escape(s) for s in sorted(spellings, key=len, reverse=True)) + ")"


# After a last name, more of a double name: "Alex Rivera-Lopez" for the saved "Alex Rivera".
_MORE_OF_THE_NAME = rf"(?:-(?-i:[{_CAPITAL}])[\w'\u2019]*)*"


def _name_forms(name: str) -> list[str]:
    """Patterns for one known name, each word with a capital or in capitals: all its words,
    first [middle] last, and "last, first". A double last name ("Rivera-Lopez") also matches
    by each of its parts."""
    words = [word.strip(",") for word in name.split() if word.strip(",")]
    if len(words) < 2:
        return []
    if name.split()[0].endswith(","):  # saved as "Rivera, Alex"
        words = [*words[1:], words[0]]
    first = _cased(words[0])
    inner = [_cased(word, as_saved=True) for word in words[1:-1]]
    forms = [_GAP.join([first, *inner, _cased(words[-1])]) + _MORE_OF_THE_NAME]
    for last in dict.fromkeys([words[-1], *words[-1].split("-")]):
        if not last:
            continue
        cased_last = _cased(last) + _MORE_OF_THE_NAME
        forms += [f"{first}{_MIDDLE}{_GAP}{cased_last}", f"{cased_last},{_GAP}{first}"]
    return forms


def name_pattern(names: Iterable[str]) -> re.Pattern[str] | None:
    """One pattern for every known name, matched as whole words; None when no name has two
    words."""
    forms = [form for name in names for form in _name_forms(name)]
    if not forms:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(forms) + r")(?!\w)")


class UnresolvedPlaceholder(ValueError):
    """An answer holds a placeholder that neither the input nor its masks supplied."""


class Masks:
    """The placeholders of the texts sent to a model in one request, and what each stands for.

    `given` is every text that will be masked, so that a placeholder one of them already holds
    is not made again. A placeholder a `given` text holds is taken as that text's own words when
    an answer holds it; one only a `reserved` text holds is not made again either, and an answer
    that holds it is refused.
    """

    def __init__(
        self,
        names: Iterable[str] = (),
        *,
        kinds: Iterable[MaskKind] = ALL_KINDS,
        given: Iterable[str] = (),
        reserved: Iterable[str] = (),
        links: Iterable[str] = (),
    ):
        names = tuple(names)
        self._kinds = frozenset(kinds)
        self._names = name_pattern(names) if "name" in self._kinds else None
        # Each known name's first word, and its last word whole and in its parts.
        self._name_pairs = [
            (_joined(words[0]), {_joined(words[-1]), *map(_joined, words[-1].split("-"))} - {""})
            for name in names
            if len(words := [word.strip(",") for word in name.split() if word.strip(",")]) >= 2
        ]
        self._link_hosts = {site[1].casefold() for link in links if (site := _SITE.match(link))}
        self._placeholders: dict[tuple[str, str], str] = {}
        # By what a placeholder is called ("LINK_1", "NAME"), what it stands for.
        self._originals: dict[str, str] = {}
        literals = [found for text in given for found in _PLACEHOLDER_PATTERN.finditer(text)]
        self._literal_placeholders = {found[0] for found in literals}
        held = [found for text in reserved for found in _PLACEHOLDER_PATTERN.finditer(text)]
        self._taken = {found[1] for found in [*literals, *held]}
        # By kind, the number after the last one made, so each is counted past once.
        # Another spelling of the name is "[NAME_2]", so it comes back as it was written.
        self._next: dict[str, int] = {"NAME": 2}
        self._name_called = self._free("NAME", numbered=False)

    def _free(self, kind: str, *, numbered: bool = True) -> str:
        """The first name for a placeholder of `kind` that no text holds and none was given."""
        if not numbered and kind not in self._taken:
            return kind
        number = self._next.get(kind, 1)
        while f"{kind}_{number}" in self._taken:
            number += 1
        self._next[kind] = number + 1
        return f"{kind}_{number}"

    @property
    def name(self) -> str | None:
        """The known name as the text first wrote it, when the text held it."""
        return self._originals.get(self._name_called)

    def _placeholder(self, kind: str, original: str, start: str = "") -> str:
        key = (kind, original)
        if key not in self._placeholders:
            first_name = kind == "NAME" and self._name_called not in self._originals
            called = self._name_called if first_name else self._free(kind)
            placeholder = f"{start}/[{called}]" if start else f"[{called}]"
            self._placeholders[key] = placeholder
            self._originals[called] = original
        return self._placeholders[key]

    def mask(self, text: str) -> str:
        """`text` with every address, phone, link and known name swapped for its placeholder."""
        if "email" in self._kinds:
            text = _EMAIL.sub(lambda found: self._placeholder("EMAIL", found[0]), text)
        if "link" in self._kinds:
            text = _LINK.sub(self._link, text)
            text = _BARE_HOST.sub(self._own_host, text)
            text = _HANDLE.sub(self._own_host, text)
        if "phone" in self._kinds:
            text = _PHONE_CANDIDATE.sub(self._phone_or_labelled, text)
        if self._names is not None:
            text = self._names.sub(lambda found: self._placeholder("NAME", found[0]), text)
        return text

    def mask_entity(self, text: str, kind: Literal["PERSON", "PLACE"]) -> str:
        """Register a local detector's exact span, without replacing other occurrences.

        A place in the header and the same place in a role must remain independent.
        Numbering and literal-placeholder protection are shared with the code-only masks.
        """
        return self._placeholder(kind, text)

    def _link(self, found: re.Match[str]) -> str:
        link = found[0].rstrip(_LINK_END)
        site = _SITE.match(link)
        start = site[0] if site and site[1].lower() in _PROFILE_SITES else ""
        return self._placeholder("LINK", link, start) + found[0][len(link) :]

    def _own_host(self, found: re.Match[str]) -> str:
        """A host alone or a handle, masked when it is the person's: it holds both the first and
        the last word of their known name ("alex-rivera.dev", "@alexrivera"), or is a host of
        the file's links. "alexa.amazon.com" and "marketplace.com" are not theirs."""
        written = found[0].casefold()
        if written.removeprefix("www.") in self._link_hosts:
            return self._placeholder("LINK", found[0])
        joined = _joined(written)
        if any(
            first in joined and any(last in joined for last in lasts)
            for first, lasts in self._name_pairs
        ):
            return self._placeholder("LINK", found[0])
        return found[0]

    def _phone_or_labelled(self, found: re.Match[str]) -> str:
        """`_phones`, and any run of a phone's digits written straight after a phone's label
        ("Phone: 612345678", "Tel. 612 345 678"), which alone could be any number."""
        before = found.string[max(0, found.start() - _LABEL_REACH) : found.start()]
        digits = sum(character.isdigit() for character in found[0])
        if _PHONE_LABEL.search(before) and _FEWEST_PHONE_DIGITS <= digits <= _MOST_PHONE_DIGITS:
            return self._placeholder("PHONE", found[0])
        return self._phones(found[0])

    def _phones(self, run: str) -> str:
        """The phones in a run of digits: the whole run, or else the longest runs of its words
        that are one ("555-123-4567 555-987-6543", "555-123-4567 2019"). A part is never longer
        than a phone's most digits or words, so a long line of numbers or marks is read in one
        pass."""
        if _is_phone(run):
            return self._placeholder("PHONE", run)
        words = list(re.finditer(r"\S+", run))
        if len(words) < 2:
            return run
        digits = [sum(character.isdigit() for character in word[0]) for word in words]
        masked: list[str] = []
        done, first = 0, 0
        while first < len(words):
            last, count = first, 0
            while (
                last < len(words)
                and last - first < _MOST_PHONE_WORDS
                and count + digits[last] <= _MOST_PHONE_DIGITS
            ):
                count += digits[last]
                last += 1
            for end in range(last, first, -1):
                part = run[words[first].start() : words[end - 1].end()]
                if _is_phone(part):
                    masked += [run[done : words[first].start()], self._placeholder("PHONE", part)]
                    done, first = words[end - 1].end(), end
                    break
            else:
                first += 1
        return "".join([*masked, run[done:]])

    def first_in(self, text: str, kind: Literal["EMAIL", "PHONE", "NAME", "PERSON"]) -> str | None:
        """What the first placeholder of `kind` in `text` stands for, if this made it."""
        return next(iter(self.all_in(text, kind)), None)

    def all_in(
        self, text: str, kind: Literal["EMAIL", "PHONE", "NAME", "PERSON", "LINK"]
    ) -> list[str]:
        """What each placeholder of `kind` in `text` that this made stands for, in its order."""
        return [
            self._originals[found[1]]
            for found in re.finditer(rf"\[({kind}(?:_\d+)?)\]", text)
            if found[1] in self._originals
        ]

    def unmask(self, text: str) -> str:
        """Give back each mask; reject unresolved patterns unless the person supplied them.

        Check before substituting, so a literal inside an original is never mistaken for a
        mask the model invented. The error carries none of the answer's text.
        """
        for found in _PLACEHOLDER_PATTERN.finditer(text):
            if found[0] in self._literal_placeholders:
                continue
            if found[0].endswith("]") and found[1] in self._originals:
                continue
            raise UnresolvedPlaceholder("The answer holds an unresolved placeholder.")
        parts: list[str] = []
        done = 0
        for found in _PLACEHOLDER.finditer(text):
            original = self._originals.get(found[1])
            if original is None:
                continue
            start = found.start()
            if found[1].startswith("LINK_"):
                window = max(done, start - _LONGEST_ADDRESS_START)
                address = _ADDRESS_START.search(text, window, start)
                if address is not None:
                    start = address.start()
            parts += [text[done:start], original]
            done = found.end()
        return "".join([*parts, text[done:]])

    def unmask_all(self, value: object) -> object:
        """`unmask` on every string inside lists and dicts, such as a model's parsed answer."""
        if isinstance(value, str):
            return self.unmask(value)
        if isinstance(value, list):
            return [self.unmask_all(item) for item in cast("list[object]", value)]
        if isinstance(value, dict):
            items = cast("dict[object, object]", value).items()
            return {key: self.unmask_all(item) for key, item in items}
        return value


def _link_spellings(url: str) -> set[str]:
    """How a resume may write the address of a saved link: with or without its scheme or "www.".
    The bare address of a profile site ("https://github.com/") is not the person's own: it would
    take every mention of the site out."""
    bare = re.sub(r"^(?:https?://)?(?:www\.)?", "", url).rstrip("/")
    if not bare or bare.lower() in _PROFILE_SITES:
        return set()
    return {
        f"{scheme}{www}{bare}" for scheme in ("", "http://", "https://") for www in ("", "www.")
    }


# After a saved link, more of its path ("alex.dev/blog"), without the mark that ends a sentence.
_MORE_PATH = r"(?:/(?:[^\s<>\"'()\[\]]*[^\s<>\"'()\[\].,;:!?])?)?"
# What a detail is replaced with in the writer's input, so the sentence around it still reads:
# "Led Alex Rivera's team" becomes "Led the candidate's team".
NEUTRAL_WORDS = {
    "name": "the candidate",
    "email": "an email address",
    "phone": "a phone number",
    "link": "a personal link",
}


def _phone_forms(saved: str) -> list[str]:
    """The saved phone's digits however they are spaced ("+1 212 555 1234" for
    "+1 (212) 555-1234"), with or without its country's code ("(212) 555-1234"), and as saved,
    with what is not a digit ("ext. 10")."""
    digits = re.findall(r"\d", saved)
    if len(digits) < _FEWEST_OWN_PHONE_DIGITS:
        return []
    dropped = range(1, 4) if saved.lstrip().startswith("+") else range(0)
    runs = [digits] + [
        digits[code:] for code in dropped if len(digits) - code >= _FEWEST_OWN_PHONE_DIGITS
    ]
    return [re.escape(saved)] + [r"\+?\(?" + rf"[{_SPACES}().-]*".join(run) for run in runs]


def own_details_pattern(person: Person) -> re.Pattern[str] | None:
    """The person's own name, email, phone and links as they saved them, in one pattern with a
    group for each kind."""
    links: set[str] = set()
    for link in person.links:
        links |= _link_spellings(str(link.url))
    kinds = {
        # A name is matched with its own capitals, whatever the rest.
        "name": [f"(?-i:{form})" for form in _name_forms(person.name)],
        "email": [re.escape(person.email)] if person.email else [],
        "phone": _phone_forms(person.phone or ""),
        # The longest first, so an address with its scheme is taken whole.
        "link": [
            re.escape(spelling) + _MORE_PATH for spelling in sorted(links, key=len, reverse=True)
        ],
    }
    groups = [f"(?P<{kind}>{'|'.join(forms)})" for kind, forms in kinds.items() if forms]
    if not groups:
        return None
    return re.compile(r"(?<![\w./@+-])(?:" + "|".join(groups) + r")(?![\w/@-])", re.IGNORECASE)


def without_own_details(text: str, person: Person) -> str:
    """`text` with the person's own name, email, phone and links replaced by neutral words
    (`NEUTRAL_WORDS`): unchanged when it holds none of them, and empty when nothing but them
    and marks is left."""
    pattern = own_details_pattern(person)
    if pattern is None:
        return text
    left, taken = pattern.subn("", text)
    if not taken:
        return text
    # What is left of "Alex Rivera, alex@example.com" is a comma: nothing.
    if not any(character.isalnum() for character in left):
        return ""
    return pattern.sub(lambda found: NEUTRAL_WORDS[found.lastgroup or "name"], text)
