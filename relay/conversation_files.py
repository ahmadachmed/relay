"""Bounded, conversation-scoped file storage. Files are not analyzed or shared here."""
import hashlib
import io
import re
import time
import zipfile
from uuid import uuid4

from .execution import PlanError
from .report_evidence import MAX_REPORT_BYTES

MAX_FILES = 5
MAX_TOTAL_BYTES = 30_000_000
ALLOWED = {".xml", ".txt", ".md", ".pdf", ".docx"}
CONVERSATION = re.compile(r"^[a-f0-9]{32}$")
RETENTION = 7 * 86400
GLOBAL_LIMIT = 200_000_000


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
            db.execute("CREATE TABLE IF NOT EXISTS file_conversations (id TEXT PRIMARY KEY, created_at REAL NOT NULL)")

    def create(self):
        scope = uuid4().hex
        with self.execution.connect() as db:
            db.execute("INSERT INTO file_conversations VALUES (?,?)", (scope, time.time()))
        return {"id": scope}

    def validate_scope(self, conversation_id):
        if not CONVERSATION.fullmatch(conversation_id):
            raise PlanError("invalid_conversation", "Invalid conversation identifier", 422)
        with self.execution.connect() as db:
            db.execute("DELETE FROM conversation_files WHERE created_at < ?", (time.time() - RETENTION,))
            if not db.execute("SELECT 1 FROM file_conversations WHERE id=?", (conversation_id,)).fetchone():
                raise PlanError("not_found", "Unknown conversation", 404)

    def list(self, conversation_id):
        self.validate_scope(conversation_id)
        with self.execution.connect() as db:
            rows = db.execute("SELECT id,filename,media_type,sha256,size,created_at FROM conversation_files WHERE conversation_id=? ORDER BY created_at,id", (conversation_id,)).fetchall()
        return [dict(id=r[0], filename=r[1], media_type=r[2], sha256=r[3], size=r[4], status="stored_format_unverified", created_at=r[5]) for r in rows]

    def upload(self, conversation_id, filename, media_type, body):
        self.validate_scope(conversation_id)
        clean = filename.replace("\\", "/").split("/")[-1].strip()
        suffix = "." + clean.rsplit(".", 1)[-1].lower() if "." in clean else ""
        if not clean or len(clean) > 180 or suffix not in ALLOWED:
            raise PlanError("unsupported_attachment", "Supported files: XML, TXT, Markdown, text-based PDF and DOCX.", 415)
        if not body or len(body) > MAX_REPORT_BYTES:
            raise PlanError("attachment_size", "Attachment must be between 1 byte and 10 MB.", 413)
        try:
            if suffix == ".pdf" and not body.startswith(b"%PDF-"):
                raise ValueError()
            if suffix == ".docx":
                with zipfile.ZipFile(io.BytesIO(body)) as archive:
                    if "word/document.xml" not in archive.namelist() or "[Content_Types].xml" not in archive.namelist():
                        raise ValueError()
            if suffix in (".txt", ".md", ".xml"):
                decoded = body.decode("utf-8-sig")
                if "\x00" in decoded or (suffix == ".xml" and not decoded.lstrip().startswith("<")):
                    raise ValueError()
        except (ValueError, UnicodeDecodeError, zipfile.BadZipFile):
            raise PlanError("invalid_attachment", "File content does not match its extension.", 415) from None
        sha = hashlib.sha256(body).hexdigest()
        media_type = (media_type or "application/octet-stream").split(";", 1)[0][:100]
        with self.execution.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT id,filename,media_type,sha256,size,created_at FROM conversation_files WHERE conversation_id=? AND sha256=?", (conversation_id, sha)).fetchone()
            if existing:
                return dict(id=existing[0], filename=existing[1], media_type=existing[2], sha256=existing[3], size=existing[4], status="stored_format_unverified", created_at=existing[5])
            global_total = db.execute("SELECT coalesce(sum(size),0) FROM conversation_files").fetchone()[0]
            if global_total + len(body) > GLOBAL_LIMIT:
                raise PlanError("attachment_quota", "Global attachment storage limit reached.", 413)
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

    def report(self, conversation_id, attachment_id, offset=0, limit=20, test_id=None, sanitize=None, section_id=None, text_offset=0):
        from .report_sections import inspect_robot
        self.validate_scope(conversation_id)
        with self.execution.connect() as db:
            row = db.execute("SELECT body,sha256 FROM conversation_files WHERE conversation_id=? AND id=? AND created_at>=?", (conversation_id, attachment_id, time.time() - RETENTION)).fetchone()
        if not row:
            raise PlanError("not_found", "Attachment not found in this conversation", 404)
        return dict(inspect_robot(row[0], row[1], offset, limit, test_id, sanitize, section_id, text_offset), attachment_id=attachment_id)

    def reference(self, conversation_id, attachment_id, expected_hash, query, sanitize):
        """Read and rank bounded reference sections after explicit model sharing consent."""
        import json
        import subprocess
        import sys
        self.validate_scope(conversation_id)
        with self.execution.connect() as db:
            row = db.execute('SELECT filename,body,sha256 FROM conversation_files WHERE conversation_id=? AND id=? AND created_at>=?', (conversation_id, attachment_id, time.time()-RETENTION)).fetchone()
        if not row:
            raise PlanError('not_found', 'Attachment not found in this conversation', 404)
        filename, body, sha = row
        if sha != expected_hash:
            raise PlanError('attachment_changed', 'Reference attachment changed.', 409)
        suffix = filename.rsplit('.', 1)[-1].lower()
        if suffix not in ('txt', 'md', 'pdf', 'docx'):
            raise PlanError('document_format', 'Choose a TXT, Markdown, PDF or DOCX document.', 415)
        try:
            proc = subprocess.run([sys.executable, '-m', 'relay.document_worker', suffix], input=body,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8, check=False)
            if proc.returncode or len(proc.stdout) > 1_500_000:
                raise ValueError()
            data = json.loads(proc.stdout)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            raise PlanError('document_limit', 'Document extraction failed or exceeded resource limits.', 422) from None
        if data.get('error'):
            code = data['error']
            messages = {'document_no_text':'No extractable text; scanned PDFs require OCR, which is not supported.',
                        'document_encrypted':'Encrypted PDF documents are not supported.',
                        'document_too_large':'Document exceeds the 5 MB extraction limit.',
                        'document_limit':'Document exceeds extraction limits.',
                        'document_format':'File content does not match a supported document format.',
                        'document_invalid':'Document is corrupt or cannot be extracted.'}
            raise PlanError(code, messages.get(code, 'Document could not be extracted.'), 422)
        # Redact complete text before selecting/truncating to avoid leaking secret fragments.
        words = {w.lower() for w in re.findall(r'\w{3,}', query, flags=re.UNICODE)[:32]}
        sections = []
        # PEM blocks can span lines, paragraphs or pages. Redact every affected
        # section before ranking, so no fragment can reach the model alone.
        parts = data['sections']
        joined = '\n'.join(part['text'] for part in parts)
        spans = [m.span() for m in re.finditer(r'-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----', joined)]
        position_in_document = 0
        for index, part in enumerate(parts):
            end = position_in_document + len(part['text'])
            sensitive = any(start < end and stop > position_in_document for start, stop in spans)
            text = '[REDACTED]' if sensitive else sanitize(part['text'])
            position_in_document = end + 1
            lowered = text.lower()
            terms = set(re.findall(r'\w{3,}', lowered, flags=re.UNICODE))
            matched = words & terms
            positions = [lowered.find(word) for word in matched]
            position = min(positions) if positions else 0
            if positions and len(lowered) != len(text):
                # Unicode lowercase can expand one code point (e.g. U+0130).
                # Translate the match offset back to the original redacted string.
                consumed = 0
                for char_index, char in enumerate(text):
                    if consumed + len(char.lower()) > position:
                        position = char_index
                        break
                    consumed += len(char.lower())
            start = max(0, position - 250) if positions and len(text) > 1500 else 0
            sections.append({'id': str(index), 'location': part['location'], 'text': text[start:start+1500],
                             'text_offset': start, 'truncated': start > 0 or start + 1500 < len(text), '_score': len(matched)})
        ranked = sorted(sections, key=lambda item: (-item['_score'], int(item['id'])))[:3]
        for item in ranked:
            del item['_score']
        return {'attachment_id': attachment_id, 'sha256': sha, 'format': suffix,
                'sections': ranked, 'total_sections': len(sections),
                'omitted_sections': max(0, len(sections)-len(ranked)),
                'source': 'user_uploaded_unverified', 'extraction': 'text_only',
                'non_text_pages': data.get('non_text_pages', []),
                'limitations': ['Images and scanned content are not interpreted; OCR is not supported.']}

    def require_reference(self, conversation_id, attachment_id, expected_hash):
        self.validate_scope(conversation_id)
        with self.execution.connect() as db:
            row = db.execute('SELECT sha256 FROM conversation_files WHERE conversation_id=? AND id=? AND created_at>=?', (conversation_id, attachment_id, time.time()-RETENTION)).fetchone()
        if not row:
            raise PlanError('not_found', 'Attachment not found in this conversation', 404)
        if row[0] != expected_hash:
            raise PlanError('attachment_changed', 'Reference attachment changed.', 409)
