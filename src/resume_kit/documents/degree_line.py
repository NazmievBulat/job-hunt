"""How the field of study is printed on a degree's line."""

import re


def printed_field_of_study(degree: str, field_of_study: str | None) -> str | None:
    """The field of study to print after the school, or None when there is none to print.

    A degree is often saved with its subject in its name ("Bachelor of Science in Computer
    Science") and the same subject again as its field. The line then names it once. The field
    counts as named when its words stand in the degree as whole words, in the same order, without
    regard to case or to how many spaces lie between them: "Computer Science" is named in the
    degree above, "Science" alone in "Bachelor of Sciences" is not. What was saved is not changed.
    """
    if not field_of_study or not field_of_study.split():
        return None
    # Both are lowered rather than matched ignoring case: `lower` is Unicode's own mapping, which
    # the browser's `toLowerCase` follows too, so the page and the files agree on every letter.
    words = r"\s+".join(re.escape(word) for word in field_of_study.lower().split())
    # A letter, digit or hyphen next to the match would make it part of a longer word.
    if re.search(rf"(?<![\w-]){words}(?![\w-])", degree.lower()):
        return None
    return field_of_study
