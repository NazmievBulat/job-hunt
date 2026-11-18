"""Source ingestion and document rendering adapters.

Public API:
- render_docx(resume)  → DOCX bytes
- validate_docx(buffer) → health check dict
"""

from resume_kit.documents.docx import render_docx, validate_docx

__all__ = ["render_docx", "validate_docx"]
