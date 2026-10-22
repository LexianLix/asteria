"""Explicit, reversible page-source adoption; exact anchors only, no fuzzy migration."""
from __future__ import annotations

import copy
import hashlib
import json

from .reader import ReaderBusyError, add_outline
from .storage import utc_now
from .review_order import REVIEW_ORDER_VERSION, order_candidates


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def document_token(doc):
    # Translation updates are independently guarded by the active-job lock.
    return digest([doc['source_hash'], doc.get('reading_revision', 0),
                   [[(b['id'], b['en'], b['box']) for b in p['blocks']] for p in doc['pages']]])


def candidates(preview, page):
    """Offer text and explicitly flagged prose-table cells, never flatten real tables."""
    result = []
    for index, text in enumerate(preview.get('texts', [])):
        provenance = [p for p in text['provenance'] if p['page'] == page]
        if not provenance or not text['text'].strip():
            continue
        result.append({'id': 'text-' + digest([text.get('ref'), index, provenance])[:24],
                       'text': text['text'], 'box': provenance[0]['box'],
                       'label': text.get('label') or 'text', 'estimated_box': False})
        if text.get('script_restoration') and 'original_text' in text:
            result[-1]['numeric_script_restoration'] = {
                'original_text': text['original_text'], 'restored_text': text['text'],
                'method': 'source-glyph-geometry'}
    for table in preview.get('tables', []):
        if not table.get('prose_column_candidates') or table['provenance'][0]['page'] != page:
            continue
        left, top, right, bottom = table['provenance'][0]['box']
        for column, cells in enumerate(table['prose_column_candidates']):
            for cell in cells:
                if not cell['text'].strip():
                    continue
                # Row boxes are estimates only when the parser omitted individual cell geometry.
                box = cell.get('box') or [left + (right-left)*column/table['columns'],
                    top + (bottom-top)*cell['row']/table['row_count'],
                    left + (right-left)*(column+1)/table['columns'],
                    top + (bottom-top)*(cell['row']+cell['row_span'])/table['row_count']]
                result.append({'id': f"{table['id']}-r{cell['row']}c{cell['column']}",
                    'text': cell['text'], 'box': box, 'label': f'疑似正文 · 第 {column+1} 列',
                    'estimated_box': 'box' not in cell})
    # Keep raw preview order and candidate IDs unchanged. Only the review list
    # gets a spatial suggestion; adoption still obeys the explicit user list.
    regions = [prov['box'] for table in preview.get('tables', [])
               if not table.get('prose_column_candidates')
               for prov in table.get('provenance', []) if prov['page'] == page]
    return order_candidates(result, table_regions=regions)


def extraction_candidates(doc, page):
    extracted = doc.get('extraction_candidates', {}).get(str(page), {})
    return [{'id': 'extraction-' + b['id'], 'text': b['en'], 'box': b['box'],
             'label': '新版文字提取', 'estimated_box': False, 'block': b}
            for b in extracted.get('blocks', [])]


class ReviewedReadingService:
    def __init__(self, reader, parser):
        self.reader, self.parser, self.db = reader, parser, reader.db

    def _document(self, paper_id, page):
        doc = self.reader.document(paper_id)
        if not 0 <= page < len(doc['pages']):
            raise ValueError('正文核对页码超出 PDF 范围')
        return doc

    def review(self, paper_id, page):
        with self.reader._lock:
            doc = self._document(paper_id, page)
            preview = self.db.reader_get(paper_id, 'complex-preview')
            valid = preview.get('source_hash') == doc['source_hash']
            choices = candidates(preview, page) if valid else []
            source = 'enhanced'
            revision = preview.get('revision') if valid else None
            parser = preview.get('parser') if valid else None
            if not choices and extraction_candidates(doc, page):
                source = 'extraction'
                choices = extraction_candidates(doc, page)
                revision = doc['extraction_revision']
                parser = {'name': 'pdfplumber', 'reading_order_version': doc['extraction_checked_version']}
            history = self.db.reader_get(paper_id, 'reading-history')
            versions = history.get('sources', {}).get(doc['source_hash'], {}).get(str(page), {})
            active = doc['pages'][page].get('reading_source', {}).get('version', 'original')
            return {'source_hash': doc['source_hash'], 'document_token': document_token(doc),
                'preview_revision': revision, 'candidate_source': source,
                'parser': parser, 'page': page, 'active_version': active,
                'candidate_order_version': REVIEW_ORDER_VERSION if source == 'enhanced' else None,
                'candidates': [{k: v for k, v in c.items() if k != 'block'} for c in choices],
                'extraction_candidates': [{k: v for k, v in c.items() if k != 'block'} for c in extraction_candidates(doc, page)],
                'extraction_revision': doc.get('extraction_revision'),
                'current_blocks': [{'id': 'current-' + b['id'], 'text': b['en'], 'box': b['box'],
                    'label': '当前正文', 'estimated_box': b.get('reviewed_source', {}).get('estimated_box', False)}
                    for b in doc['pages'][page]['blocks']],
                'versions': [{'id': key, 'label': value['label'], 'created_at': value['created_at'],
                              'active': key == active} for key, value in versions.items()],
                'warning': '候选顺序按原页位置分区建议，仍需核对顺序、遗漏和科学符号后明确采用。真实表格不自动拆成正文；位置可能是估算。'}

    def _admit(self, paper_id, page, source_hash, token):
        if paper_id in self.reader._active or paper_id in self.parser._active:
            raise ReaderBusyError('本论文正在翻译或解析，请暂停/取消并等待任务结束后再切换正文')
        doc = self._document(paper_id, page)
        if doc['source_hash'] != source_hash or document_token(doc) != token:
            raise ReaderBusyError('正文或 PDF 已变化，请重新打开核对，不会覆盖当前内容')
        return doc

    def _history(self, paper_id, doc, page):
        history = self.db.reader_get(paper_id, 'reading-history')
        versions = history.setdefault('sources', {}).setdefault(doc['source_hash'], {}).setdefault(str(page), {})
        current = doc['pages'][page]
        version = current.get('reading_source', {}).get('version', 'original')
        # Capture the live translations before leaving, not only their adoption-time snapshot.
        versions[version] = {'page': copy.deepcopy(current), 'created_at': versions.get(version, {}).get('created_at', utc_now()),
            'label': '原始文字提取' if version == 'original' else current['reading_source']['label']}
        return history, versions

    def _save(self, paper_id, doc, page, updated, history):
        if len(json.dumps(history, ensure_ascii=False).encode()) > 50 * 1024**2:
            raise ValueError('正文版本历史超过 50 MB；未切换，也不会自动删除历史，请先备份')
        doc['pages'][page] = updated
        doc['reading_revision'] = doc.get('reading_revision', 0) + 1
        add_outline(doc)
        self.db.reader_put_many(paper_id, {'document': doc, 'reading-history': history})
        return {'document': doc, 'review': self.review(paper_id, page)}

    def adopt(self, paper_id, page, source_hash, document_token, preview_revision, blocks, confirmed, candidate_source='enhanced'):
        if confirmed is not True:
            raise ValueError('请明确确认已核对原页、顺序和科学符号')
        with self.reader._lock, self.reader._extract_lock:
            doc = self._admit(paper_id, page, source_hash, document_token)
            if candidate_source == 'extraction':
                extracted = doc.get('extraction_candidates', {}).get(str(page))
                if not extracted or doc.get('extraction_revision') != preview_revision:
                    raise ReaderBusyError('新版文字提取已变化，请重新核对后采用正文')
                choices = {c['id']: c for c in extraction_candidates(doc, page)}
                parser = {'name': 'pdfplumber', 'reading_order_version': doc['extraction_checked_version']}
            else:
                preview = self.parser.preview(paper_id)
                if not preview or preview['revision'] != preview_revision:
                    raise ReaderBusyError('增强预览已变化，请重新核对后采用正文')
                choices = {c['id']: c for c in candidates(preview, page)}
                parser = preview.get('parser')
            choices.update({'current-' + b['id']: {'box': b['box'],
                'estimated_box': b.get('reviewed_source', {}).get('estimated_box', False)} for b in doc['pages'][page]['blocks']})
            if not blocks or len(blocks) > 500 or sum(len(b['text']) for b in blocks) > 100000:
                raise ValueError('请保留至少一个段落；每页最多 500 段、10 万字')
            used_candidates = set()
            for block in blocks:
                candidate = block.get('candidate_id')
                if not block['text'].strip() or len(block['text']) > 10000:
                    raise ValueError('每段应包含 1–10000 个字符')
                if candidate and (candidate not in choices or candidate in used_candidates):
                    raise ValueError('候选段落不存在、属于其他页或重复，请重新核对')
                if candidate:
                    used_candidates.add(candidate)
            history, versions = self._history(paper_id, doc, page)
            if len(versions) >= 50:
                raise ValueError('本页已有 50 个正文版本；未切换，不会自动删除旧版本')
            pool = [doc['pages'][page], *[entry['page'] for entry in reversed(list(versions.values()))]]
            adopted, translated, warnings, used_ids = [], {}, {}, set()
            for submitted in blocks:
                choice = choices.get(submitted.get('candidate_id'))
                text = submitted['text'].strip()
                box = choice['box'] if choice else [0, 0, 1, 1]
                match = next(((p, b) for p in pool for b in p['blocks']
                    if b['en'] == text and b['box'] == box and b['id'] not in used_ids), None)
                if match:
                    previous, block = match
                    block = copy.deepcopy(block)
                    if block['id'] in previous.get('translated', {}):
                        translated[block['id']] = previous['translated'][block['id']]
                    if block['id'] in previous.get('warnings', {}):
                        warnings[block['id']] = previous['warnings'][block['id']]
                elif choice and choice.get('block') and choice['text'] == text:
                    block = copy.deepcopy(choice['block'])
                else:
                    identity = digest([source_hash, page, text, box])[:24]
                    base = f'{source_hash[:12]}-review-p{page}-{identity}'
                    key, duplicate = base, 0
                    while key in used_ids:
                        duplicate += 1; key = f'{base}-{duplicate}'
                    block = {'id': key, 'en': text, 'box': box, 'protected_spans': []}
                block['reviewed_source'] = {'candidate_id': submitted.get('candidate_id'),
                    'manual': choice is None, 'estimated_box': choice is None or choice['estimated_box']}
                used_ids.add(block['id']); adopted.append(block)
            version = 'review-' + digest([(b['id'], b['en'], b['box']) for b in adopted])[:24]
            label = '人工核对正文 · ' + utc_now()
            updated = copy.deepcopy(doc['pages'][page])
            updated.update(blocks=adopted, translated=translated, warnings=warnings, error=None)
            if candidate_source == 'extraction':
                for key in ('scientific_regions', 'scientific_diagnostics'):
                    updated[key] = copy.deepcopy(extracted.get(key, []))
                for region in updated['scientific_regions']:
                    region['block_ids'] = [key for key in region['block_ids'] if key in used_ids]
            updated.pop('error_code', None)
            updated['reading_source'] = {'version': version, 'label': label, 'preview_revision': preview_revision,
                'parser': parser, 'candidate_source': candidate_source,
                'confirmed_at': utc_now(), 'requires_math_review': True}
            versions[version] = {'page': copy.deepcopy(updated), 'label': label, 'created_at': utc_now()}
            return self._save(paper_id, doc, page, updated, history)

    def switch(self, paper_id, page, source_hash, document_token, version):
        with self.reader._lock, self.reader._extract_lock:
            doc = self._admit(paper_id, page, source_hash, document_token)
            history, versions = self._history(paper_id, doc, page)
            if version not in versions:
                raise ValueError('正文版本不存在或属于另一 PDF，未切换')
            return self._save(paper_id, doc, page, copy.deepcopy(versions[version]['page']), history)
