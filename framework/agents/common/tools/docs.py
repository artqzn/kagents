"""Documentation tools — write_doc, read_doc."""
from __future__ import annotations
from pathlib import Path
from framework.agents.common.tools.base import Tool, WORKSPACE


def tools(subsystem) -> list[Tool]:
    """Create documentation tools."""

    def write_doc(filename: str, content: str, doc_type: str = "rfcs") -> dict:
        """Write a document to the docs directory.

        doc_type: 'rfcs' for RFCs (default), 'decisions' for ADRs.
        Documents are written to /workspace/docs/<doc_type>/<filename>.
        """
        # Normalize doc_type: accept both 'rfc' and 'rfcs', use 'rfcs'
        if doc_type == "rfc":
            doc_type = "rfcs"
        doc_dir = WORKSPACE / "docs" / doc_type
        doc_dir.mkdir(parents=True, exist_ok=True)
        doc_path = doc_dir / filename
        doc_path.write_text(content)
        return {
            "ok": True,
            "path": str(doc_path),
            # Return relative path for consistency with what agents expect
            "relative_path": f"docs/{doc_type}/{filename}",
        }

    def read_doc(filename: str, doc_type: str = "rfcs") -> dict:
        """Read a document from the docs directory.

        doc_type: 'rfcs' for RFCs (default), 'decisions' for ADRs.
        """
        if doc_type == "rfc":
            doc_type = "rfcs"
        doc_path = WORKSPACE / "docs" / doc_type / filename
        if not doc_path.exists():
            return {"error": f"Document not found: {doc_path}"}
        return {"path": str(doc_path), "content": doc_path.read_text()}

    return [
        Tool(
            name="write_doc",
            description=(
                "Write a document to /workspace/docs/<doc_type>/<filename>. "
                "doc_type: 'rfcs' for RFCs (default), 'decisions' for ADRs. "
                "Returns the path. Use this to write RFC documents."
            ),
            fn=write_doc,
            schema={
                "type": "object",
                "properties": {
                    "filename": {"type": "string",
                                 "description": "e.g. 'abc123.md'"},
                    "content": {"type": "string",
                                "description": "Full document content (markdown)"},
                    "doc_type": {"type": "string", "default": "rfcs"},
                },
                "required": ["filename", "content"],
            },
        ),
        Tool(
            name="read_doc",
            description="Read a document from /workspace/docs/<doc_type>/<filename>.",
            fn=read_doc,
            schema={
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "doc_type": {"type": "string", "default": "rfcs"},
                },
                "required": ["filename"],
            },
        ),
    ]
