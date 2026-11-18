"""Measured, lossless page flow for a heading and its content."""

from reportlab.platypus import Flowable, PageBreak, Paragraph


class HeadedBlock(Flowable):
    """Keep a block whole when possible, repeating its heading on necessary continuations.

    A paragraph taller than a page may split at its own line boundaries; ordinary paragraphs
    move whole. ReportLab's paragraph splitter enforces its orphan and widow rules.
    """

    def __init__(
        self,
        heading: Paragraph,
        contents: list[Paragraph],
        page_height: float,
        *,
        introduction: list[Paragraph] | None = None,
    ) -> None:
        super().__init__()
        self.heading = heading
        self.contents = contents
        self.page_height = page_height
        self.introduction = introduction or []
        self.spaceBefore = self.paragraphs[0].getSpaceBefore()
        self.spaceAfter = self.paragraphs[-1].getSpaceAfter()

    @property
    def paragraphs(self) -> list[Paragraph]:
        return [*self.introduction, self.heading, *self.contents]

    @staticmethod
    def _height(paragraphs: list[Paragraph], width: float) -> float:
        height = 0.0
        after = 0.0
        for index, paragraph in enumerate(paragraphs):
            if index:
                height += max(after, paragraph.getSpaceBefore())
            height += paragraph.wrap(width, 1e8)[1]
            after = paragraph.getSpaceAfter()
        return height

    def wrap(self, aW: float, aH: float) -> tuple[float, float]:
        self.width = aW
        self.height = self._height(self.paragraphs, aW)
        return self.width, self.height

    def split(self, availWidth: float, availHeight: float) -> list[Flowable]:
        if self._height(self.paragraphs, availWidth) <= self.page_height:
            return []  # An ordinary job belongs whole on the next page.
        prefix = [*self.introduction, self.heading]
        for index, paragraph in enumerate(self.contents):
            if self._height([*prefix, paragraph], availWidth) <= availHeight:
                prefix.append(paragraph)
                continue
            remaining = self.contents[index:]
            # Split only text too tall for a fresh page, including its required headings.
            required_headings = [
                *(self.introduction if index == 0 else []),
                self.heading,
            ]
            if self._height([*required_headings, paragraph], availWidth) > self.page_height:
                used = self._height(prefix, availWidth)
                gap = max(prefix[-1].getSpaceAfter(), paragraph.getSpaceBefore())
                fragments = paragraph.split(availWidth, max(0, availHeight - used - gap))
                if len(fragments) == 2 and all(isinstance(p, Paragraph) for p in fragments):
                    first_fragment, last_fragment = fragments
                    assert isinstance(first_fragment, Paragraph)
                    assert isinstance(last_fragment, Paragraph)
                    prefix.append(first_fragment)
                    remaining = [last_fragment, *self.contents[index + 1 :]]
            count = len(prefix) - len(self.introduction) - 1
            if count == 0:
                return []  # Never leave a heading without useful content.
            first = HeadedBlock(
                self.heading,
                prefix[len(self.introduction) + 1 :],
                self.page_height,
                introduction=self.introduction,
            )
            rest = HeadedBlock(self.heading, remaining, self.page_height)
            return [first, PageBreak(), rest]
        return []

    def draw(self) -> None:
        y = self.height
        after = 0.0
        for index, paragraph in enumerate(self.paragraphs):
            if index:
                y -= max(after, paragraph.getSpaceBefore())
            _, height = paragraph.wrap(self.width, 1e8)
            y -= height
            paragraph.drawOn(self.canv, 0, y)
            after = paragraph.getSpaceAfter()
