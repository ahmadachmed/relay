"""Bounded, subprocess-only extraction of untrusted reference documents."""
import io
import json
import resource
import sys
import zipfile
from defusedxml import ElementTree as ET

resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))


def extract(kind, body):
    if len(body) > 5_000_000:
        return {'error': 'document_too_large'}
    sections = []
    non_text_pages = []
    if kind in ('txt', 'md'):
        if body.startswith((b'%PDF-', b'PK\x03\x04')):
            return {'error': 'document_format'}
        text = body.decode('utf-8-sig')
        if '\x00' in text:
            return {'error': 'document_format'}
        for index, paragraph in enumerate(text.splitlines(), 1):
            if paragraph.strip():
                sections.append({'location': f'line {index}', 'text': paragraph})
    elif kind == 'pdf':
        if not body.startswith(b'%PDF-'):
            return {'error': 'document_format'}
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(body), strict=True)
        if reader.is_encrypted:
            return {'error': 'document_encrypted'}
        if len(reader.pages) > 100:
            return {'error': 'document_limit'}
        for index, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ''
            if text.strip():
                sections.append({'location': f'page {index}', 'text': text})
            else:
                non_text_pages.append(index)
    elif kind == 'docx':
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(infos) > 1000 or names.count('word/document.xml') != 1 or '[Content_Types].xml' not in names:
                return {'error': 'document_format'}
            info = archive.getinfo('word/document.xml')
            if info.file_size > 2_000_000 or info.flag_bits & 1:
                return {'error': 'document_limit'}
            xml = archive.read(info)
        root = ET.fromstring(xml, forbid_dtd=True)
        ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
        for index, para in enumerate(root.iter(ns + 'p'), 1):
            parts = []
            for node in para.iter():
                if node.tag == ns + 't':
                    parts.append(node.text or '')
                elif node.tag == ns + 'tab':
                    parts.append('\t')
                elif node.tag in (ns + 'br', ns + 'cr'):
                    parts.append('\n')
            value = ''.join(parts)
            if value.strip():
                sections.append({'location': f'paragraph {index}', 'text': value})
    else:
        return {'error': 'document_format'}
    if not sections:
        return {'error': 'document_no_text'}
    # Reject isolated Unicode surrogates before JSON/model UTF-8 serialization.
    for section in sections:
        section['text'].encode('utf-8', errors='strict')
    if len(sections) > 2000 or sum(len(s['text']) for s in sections) > 200_000:
        return {'error': 'document_limit'}
    return {'sections': sections, 'non_text_pages': non_text_pages}


def main():
    try:
        result = extract(sys.argv[1], sys.stdin.buffer.read(5_000_001))
    except Exception:
        result = {'error': 'document_invalid'}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
