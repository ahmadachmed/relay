"""Bounded, conversation-scoped file storage. Files are not analyzed or shared here."""
import hashlib
import json
import re
import time
from uuid import uuid4

from .execution import PlanError
from .report_evidence import MAX_REPORT_BYTES

MAX_FILES = 5
MAX_TOTAL_BYTES = 30_000_000
ALLOWED = {".xml", ".txt", ".md", ".pdf", ".docx"}
CONVERSATION = re.compile(r"^(planning|[a-f0-9]{32})$")


class ConversationFiles:
    def __init__(self, execution):
        self.execution = execution
        with execution.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS conversation_files (
                id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, filename TEXT NOT NULL,
                media_type TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
                body BLOB NOT NULL, created_at REAL NOT NULL,
                UNIQUE(conversation_id, sha256))""")
            db.execute("CREATE INDEX IF NOT EXISTS conversation_files_scope ON conversation_files(conversation_id)")

    @staticmethod
    def validate_scope(conversation_id):
        if not CONVERSATION.fullmatch(conversation_id):
            raise PlanError("invalid_conversation", "Invalid conversation identifier", 422)

    def list(self, conversation_id):
        self.validate_scope(conversation_id)
        with self.execution.connect() as db:
            rows = db.execute("SELECT id,filename,media_type,sha256,size,created_at FROM conversation_files WHERE conversation_id=? ORDER BY created_at,id", (conversation_id,)).fetchall()
        return [dict(id=r[0], filename=r[1], media_type=r[2], sha256=r[3], size=r[4], status="stored_not_analyzed", created_at=r[5]) for r in rows]

    def upload(self, conversation_id, filename, media_type, body):
        self.validate_scope(conversation_id)
        clean = filename.replace("\\", "/").split("/")[-1].strip()
        suffix = "." + clean.rsplit(".", 1)[-1].lower() if "." in clean else ""
        if not clean or len(clean) > 180 or suffix not in ALLOWED:
            raise PlanError("unsupported_attachment", "Supported files: XML, TXT, Markdown, text-based PDF and DOCX.", 415)
        if not body or len(body) > MAX_REPORT_BYTES:
            raise PlanError("attachment_size", "Attachment must be between 1 byte and 10 MB.", 413)
        sha = hashlib.sha256(body).hexdigest()
        media_type = (media_type or "application/octet-stream").split(";", 1)[0][:100]
        with self.execution.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT id FROM conversation_files WHERE conversation_id=? AND sha256=?", (conversation_id, sha)).fetchone()
            if existing:
                return next(item for item in self.list(conversation_id) if item["id"] == existing[0])
            count, total = db.execute("SELECT count(*),coalesce(sum(size),0) FROM conversation_files WHERE conversation_id=?", (conversation_id,)).fetchone()
            if count >= MAX_FILES or total + len(body) > MAX_TOTAL_BYTES:
                raise PlanError("attachment_quota", "Conversation attachment limit reached (5 files / 30 MB total).", 413)
            artifact_id = uuid4().hex
            db.execute("INSERT INTO conversation_files VALUES (?,?,?,?,?,?,?,?)", (artifact_id, conversation_id, clean, media_type, sha, len(body), body, time.time()))
        return next(item for item in self.list(conversation_id) if item["id"] == artifact_id)

    def delete(self, conversation_id, attachment_id):
        self.validate_scope(conversation_id)
        with self.execution.connect() as db:
            result = db.execute("DELETE FROM conversation_files WHERE conversation_id=? AND id=?", (conversation_id, attachment_id))
        if not result.rowcount:
            raise PlanError("not_found", "Attachment not found in this conversation", 404)
        return {"deleted": True}
