"""The language a resume is written in: the one the person saved their career in.

Code decides, never a model. The words of the saved career that a person wrote in sentences (job
titles, bullets, headline titles and the summary) are counted against the short words each
language uses most ("the", "der", "le"...). Skills are left out: their names are mostly the same
in every language. A language is found when enough of its words are there and clearly more than
of any other, from at least two different ones, and when the text has few words with letters the
language does not write. A tie that English is part of goes to English when the text has no
letter beyond A to Z. Otherwise the profile has none of these seven.

The found language does two things. The writer is told, in one line added by code, to write the
resume in it, and the resume prints its headings, month names and "Present" in it. Facts copied
from the profile are already in that language, so the fact checks keep working. A profile in none
of the seven is written as before: no line, and the printed words in English.

The printed words have a copy in the browser that
`tests/contracts/test_resume_language.py` compares with this one.
"""

import re
from collections import Counter
from typing import TYPE_CHECKING, Literal, NamedTuple

if TYPE_CHECKING:  # `generation` imports `resume`, which imports this module.
    from resume_kit.contracts.generation import CareerSnapshot

ResumeLanguage = Literal["en", "de", "fr", "es", "it", "pt", "nl"]

# How the line sent to the writer names each language.
LANGUAGE_NAMES: dict[ResumeLanguage, str] = {
    "en": "English",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
}

# The short words each language writes most. A word two languages share counts for both. Left
# out are words a resume in another language uses too: "per" ("30 patients per shift"), "do",
# "no" and "as" in English, "com" in a web address.
_COMMON_WORDS: dict[ResumeLanguage, frozenset[str]] = {
    "en": frozenset(
        {
            *("the", "a", "an", "and", "of", "to", "for", "with", "in", "on", "by", "from", "at"),
            *("as", "is", "was", "were", "are", "into", "over", "across", "using", "within"),
            *("while", "which", "that", "this", "their", "our"),
        }
    ),
    "de": frozenset(
        {
            *("der", "die", "das", "und", "mit", "für", "von", "zu", "zur", "zum", "im", "in"),
            *("an", "auf", "des", "den", "dem", "ein", "eine", "einer", "einem", "eines", "bei"),
            *("aus", "als", "sowie", "über", "durch", "nach", "unter", "vom", "am", "ist", "wurde"),
            *("sich", "nicht", "auch"),
        }
    ),
    "fr": frozenset(
        {
            *("le", "la", "les", "l", "et", "des", "du", "d", "de", "pour", "avec", "dans", "une"),
            *("un", "sur", "au", "aux", "par", "en", "à", "est", "ont", "été", "qui", "que", "qu"),
            *("ses", "leur", "plus", "chez", "entre", "afin", "lors", "depuis"),
        }
    ),
    "es": frozenset(
        {
            *("el", "la", "los", "las", "y", "del", "para", "con", "en", "una", "un", "por", "de"),
            *("al", "a", "que", "se", "sus", "como", "entre", "durante", "mediante", "más"),
        }
    ),
    "it": frozenset(
        {
            *("il", "lo", "la", "gli", "le", "l", "e", "del", "della", "delle", "dei", "degli"),
            *("dell", "con", "nel", "nella", "nei", "nell", "una", "un", "di", "da", "al", "alla"),
            *("ai", "all", "a", "che", "su", "sul", "sulla", "tra", "fra", "come", "dal", "dalla"),
        }
    ),
    "pt": frozenset(
        {
            *("o", "os", "a", "e", "da", "dos", "das", "para", "em", "na", "nas", "nos", "uma"),
            *("um", "de", "ao", "aos", "pelo", "pela", "pelos", "pelas", "que", "como", "entre"),
            *("mais", "seu", "sua"),
        }
    ),
    "nl": frozenset(
        {
            *("de", "het", "een", "en", "van", "voor", "met", "in", "op", "bij", "aan", "te"),
            *("door", "naar", "uit", "als", "om", "werd", "zijn", "worden", "binnen", "tussen"),
        }
    ),
}

# The letters beyond A to Z each language writes. A word with another one ("pielęgniarki",
# "și") is not written in that language.
_OWN_LETTERS: dict[ResumeLanguage, frozenset[str]] = {
    "en": frozenset(),
    "de": frozenset("äöüß"),
    "fr": frozenset("àâæçéèêëîïôœùûüÿ"),
    "es": frozenset("áéíñóúü"),
    "it": frozenset("àèéìíîòóùú"),
    "pt": frozenset("áâãàçéêíóôõú"),
    "nl": frozenset("áéèëïóöüú"),
}

# A language is found from at least this many of its words, and as many different ones: "na"
# twice is Polish as often as it is Portuguese...
MIN_COMMON_WORDS = 2
# ...when it has at least this many times as many as the next language...
LEAD_OVER_NEXT = 1.5
# ...and fewer than this share of the words hold a letter it does not write. A name or a place
# from elsewhere ("Zürich" in English) stays under it; Polish or Romanian does not.
MAX_FOREIGN_WORDS = 0.1

# A word is what lies between spaces and apostrophes ("l'équipe" is "l" and "équipe"), with the
# punctuation around it taken off. "e-commerce" stays one word, so it is not the Italian "e".
_WORD_BREAK = re.compile(r"[\s'\u2019]+")  # a space or an apostrophe, straight or curly
_PUNCTUATION = '.,;:!?()[]{}"«»\u201c\u201d\u201e\u2013\u2014-/·•*'


def _career_words(career: CareerSnapshot) -> list[str]:
    texts = [role.title for role in career.roles]
    texts.extend(bullet.text for role in career.roles for bullet in role.bullets)
    texts.extend(title.text for title in career.titles)
    if career.summary:
        texts.append(career.summary)
    return _words(" ".join(texts))


def _words(text: str) -> list[str]:
    words = (word.strip(_PUNCTUATION) for word in _WORD_BREAK.split(text.casefold()))
    return [word for word in words if word]


def profile_language(career: CareerSnapshot) -> ResumeLanguage | None:
    """The language the career is written in, or None when it is none of the seven."""
    return _language_of(_career_words(career))


def text_language(text: str) -> ResumeLanguage | None:
    """The language a text is written in, by the same count, or None when it is none of the
    seven."""
    return _language_of(_words(text))


def _language_of(words: list[str]) -> ResumeLanguage | None:
    counts: Counter[ResumeLanguage] = Counter(
        {
            language: sum(1 for word in words if word in common)
            for language, common in _COMMON_WORDS.items()
        }
    )
    (best, most), (_, next_most) = counts.most_common(2)
    different = {word for word in words if word in _COMMON_WORDS[best]}
    if (
        most >= MIN_COMMON_WORDS
        and len(different) >= MIN_COMMON_WORDS
        and most >= next_most * LEAD_OVER_NEXT
        and _foreign_share(words, best) < MAX_FOREIGN_WORDS
    ):
        return best
    # Short English bullets ("Fixed bugs in Node.js services") often hold no common word but
    # "in", which German, Dutch and Italian write too. Written in letters A to Z alone, with no
    # "ü", "é" or "ñ" of the other six, such a tie is English.
    if counts["en"] >= MIN_COMMON_WORDS and counts["en"] == most and _only_a_to_z(words):
        return "en"
    return None


def _foreign_share(words: list[str], language: ResumeLanguage) -> float:
    """The share of the words that hold a letter beyond A to Z that the language does not write."""
    own = _OWN_LETTERS[language]
    foreign = [
        word
        for word in words
        if any(char.isalpha() and not char.isascii() and char not in own for char in word)
    ]
    return len(foreign) / len(words)


def _only_a_to_z(words: list[str]) -> bool:
    return all(character.isascii() for word in words for character in word if character.isalpha())


def printed_language(career: CareerSnapshot) -> ResumeLanguage:
    """The language a resume's headings, months and "Present" are printed in."""
    return profile_language(career) or "en"


class PrintedWords(NamedTuple):
    """What a resume prints that is not written by a model or copied from the profile."""

    summary: str
    skills: str
    experience: str
    education: str
    # Before the list of certifications, followed by a colon.
    certifications: str
    # The end of a job or a degree that is still going on.
    present: str
    # Before the number in the PDF's and the Word file's footer.
    page: str
    # Before the date a certification expires, in brackets after the certification's line:
    # "(valid until Jun 2024)". It reads right whether that date has passed or not.
    valid_until: str
    # Before a certification's saved credential ID, after its line: "— Credential ID BHA-482615".
    credential_id: str
    months: tuple[str, ...]


PRINTED_WORDS: dict[ResumeLanguage, PrintedWords] = {
    "en": PrintedWords(
        summary="SUMMARY",
        skills="SKILLS",
        experience="PROFESSIONAL EXPERIENCE",
        education="EDUCATION & CREDENTIALS",
        certifications="Certifications",
        present="Present",
        page="Page",
        valid_until="valid until",
        credential_id="Credential ID",
        months=(
            *("Jan", "Feb", "Mar", "Apr", "May", "Jun"),
            *("Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        ),
    ),
    "de": PrintedWords(
        summary="PROFIL",
        skills="KENNTNISSE",
        experience="BERUFSERFAHRUNG",
        education="AUSBILDUNG & ZERTIFIKATE",
        certifications="Zertifikate",
        present="heute",
        page="Seite",
        valid_until="gültig bis",
        credential_id="Zertifikats-ID",
        months=(
            *("Jan.", "Feb.", "März", "Apr.", "Mai", "Juni"),
            *("Juli", "Aug.", "Sept.", "Okt.", "Nov.", "Dez."),
        ),
    ),
    "fr": PrintedWords(
        summary="PROFIL",
        skills="COMPÉTENCES",
        experience="EXPÉRIENCE PROFESSIONNELLE",
        education="FORMATION & CERTIFICATIONS",
        certifications="Certifications",
        present="aujourd'hui",
        page="Page",
        valid_until="valable jusqu'en",
        credential_id="ID du certificat",
        months=(
            *("janv.", "févr.", "mars", "avr.", "mai", "juin"),
            *("juil.", "août", "sept.", "oct.", "nov.", "déc."),
        ),
    ),
    "es": PrintedWords(
        summary="PERFIL",
        skills="COMPETENCIAS",
        experience="EXPERIENCIA PROFESIONAL",
        education="FORMACIÓN Y CERTIFICACIONES",
        certifications="Certificaciones",
        present="actualidad",
        page="Página",
        valid_until="válido hasta",
        credential_id="ID de credencial",
        months=(
            *("ene", "feb", "mar", "abr", "may", "jun"),
            *("jul", "ago", "sept", "oct", "nov", "dic"),
        ),
    ),
    "it": PrintedWords(
        summary="PROFILO",
        skills="COMPETENZE",
        experience="ESPERIENZA PROFESSIONALE",
        education="FORMAZIONE E CERTIFICAZIONI",
        certifications="Certificazioni",
        present="oggi",
        page="Pagina",
        valid_until="valido fino a",
        credential_id="ID credenziale",
        months=(
            *("gen", "feb", "mar", "apr", "mag", "giu"),
            *("lug", "ago", "set", "ott", "nov", "dic"),
        ),
    ),
    "pt": PrintedWords(
        summary="PERFIL",
        skills="COMPETÊNCIAS",
        experience="EXPERIÊNCIA PROFISSIONAL",
        education="FORMAÇÃO E CERTIFICAÇÕES",
        certifications="Certificações",
        present="atual",
        page="Página",
        valid_until="válido até",
        credential_id="ID da credencial",
        months=(
            *("jan.", "fev.", "mar.", "abr.", "mai.", "jun."),
            *("jul.", "ago.", "set.", "out.", "nov.", "dez."),
        ),
    ),
    "nl": PrintedWords(
        summary="PROFIEL",
        skills="VAARDIGHEDEN",
        experience="WERKERVARING",
        education="OPLEIDING & CERTIFICATEN",
        certifications="Certificaten",
        present="heden",
        page="Pagina",
        valid_until="geldig tot",
        credential_id="Certificaat-ID",
        months=(
            *("jan.", "feb.", "mrt.", "apr.", "mei", "jun."),
            *("jul.", "aug.", "sep.", "okt.", "nov.", "dec."),
        ),
    ),
}


def printed_date(value: str, language: ResumeLanguage) -> str:
    """`2019-03` as "Mar 2019" in the resume's language. A year alone is printed as it is."""
    year, _, month = value.partition("-")
    if not month:
        return year
    return f"{PRINTED_WORDS[language].months[int(month) - 1]} {year}"
