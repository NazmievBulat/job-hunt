# The words on a resume page

Each `.txt` file here is the text of one resume as it is printed, top to bottom, without the
bullet marks and without the page footer.

Two tests read these files, and they are what keeps the PDF and the preview in the browser
saying the same thing in the same order:

- `tests/documents/test_page_text.py` holds the PDF to them.

Both compare with white space removed, because the PDF wraps lines where the browser does not.

| Text | Resume |
| --- | --- |
| `full.txt` | `examples/resume.example.json`: every section present |
| `cleared.txt` | `cleared.json`: no headline, summary, skills, education or certifications |
| `german.txt` | `german.json`: a resume printed in German, with a job and a degree still going on |

When the wording or the order of the page changes on purpose, change the renderer, the preview
and these files together.
