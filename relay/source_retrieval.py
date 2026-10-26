"""Deterministic, scope-authorized local retrieval; caller owns sharing consent."""
import hashlib
import json
import re
from .source_documents import read_local, extract_operation


class SourceRetrieval:
    def __init__(self, registry):
        self.registry = registry

    def retrieve(self, category, scope, environment, query, budget=20000):
        if not 1 <= budget <= 20000 or len(query) > 4000:
            raise ValueError('Invalid retrieval budget or query')
        terms = set(re.findall(r'[a-z0-9_]+', query.lower()))
        candidates, diagnostics = [], []
        reads, total = 0, 0
        for source_id in sorted(self.registry.sources):
            try:
                source = self.registry.authorize(source_id, category, scope, environment)
            except ValueError:
                continue
            if source.provider != 'local':
                diagnostics.append('Remote source retrieval is not yet supported.')
                continue
            for path in source.files:
                reads += 1
                if reads > 16:
                    diagnostics.append('Source file search limit reached.')
                    break
                try:
                    if total >= 2000000:
                        diagnostics.append('Source search byte limit reached.')
                        break
                    allowance = min(source.max_file_bytes, 2000000 - total)
                    def count_bytes(size):
                        nonlocal total
                        total += size
                    snapshot = read_local(source, path, max_bytes=allowance, byte_counter=count_bytes)
                    if source.format == 'openapi':
                        # Use the bounded extractor for explicit method/path questions.
                        match = re.search(r'\b(get|post|put|patch|delete|head|options|trace)\s+(/[^\s]+)', query, re.I)
                        if not match:
                            diagnostics.append('OpenAPI retrieval requires an explicit method and path.')
                            continue
                        endpoint = match[2]
                        # Prefer the literal path; only retry prose delimiters on lookup failure.
                        try:
                            section = extract_operation(snapshot, match[1], endpoint, budget)
                        except ValueError as exc:
                            if str(exc) != 'Operation unavailable':
                                raise
                            endpoint = endpoint.rstrip('`.,;!?)]}')
                            section = extract_operation(snapshot, match[1], endpoint, budget)
                    else:
                        section = dict(snapshot, selector='whole document', parser_version='text-v1')
                    score = len(terms & set(re.findall(r'[a-z0-9_]+', (path+' '+section['selector']+' '+section['text']).lower())))
                    if score:
                        section.update(source_id=source_id, category=category, scope_id=scope)
                        section['id'] = hashlib.sha256(json.dumps([source_id, category, scope, environment, section['path'], section['sha256'], section['selector'], section['parser_version']]).encode()).hexdigest()
                        candidates.append((score, source_id, path, section))
                except (OSError, ValueError, UnicodeError):
                    diagnostics.append('Source unavailable, unsupported or exceeds retrieval limits.')
            if reads > 16 or total > 2000000:
                break
        selected, used = [], 0
        for _, _, _, section in sorted(candidates, key=lambda c: (-c[0], c[1], c[2])):
            size = len(json.dumps(section).encode())
            if used + size > budget:
                diagnostics.append('Relevant source omitted to fit context budget.')
                continue
            selected.append(section)
            used += size
        if not selected:
            diagnostics.append('No matching source context available.')
        return {'sources': selected, 'diagnostics': sorted(set(diagnostics))}
