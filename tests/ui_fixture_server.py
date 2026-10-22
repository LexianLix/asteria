"""Isolated browser QA fixture; never seeds the user's real literature database."""
import argparse
import hashlib
import json
import os
import shutil
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import uvicorn
from reportlab.pdfgen import canvas


def main():
    args = argparse.ArgumentParser()
    args.add_argument("--port", type=int, default=8819)
    args.add_argument("--pdf", type=Path)
    args.add_argument('--discovery', action='store_true')
    args.add_argument('--scientific', action='store_true')
    args.add_argument('--complex-mock', action='store_true')
    args.add_argument('--chatgpt-mock', action='store_true')
    args.add_argument('--classification-mock', action='store_true')
    args.add_argument('--source-rate-mock', action='store_true')
    args.add_argument('--reader-cache', type=Path)
    options = args.parse_args()
    port = options.port
    with tempfile.TemporaryDirectory(prefix="paper-reader-ui-") as folder:
        root = Path(folder)
        os.environ["PAPER_READER_ROOT"] = str(root)
        os.environ["PAPER_READER_DATA"] = str(root / "data")
        os.environ["PAPER_READER_TOPICS"] = str(root / "topics.json")
        from paper_reader.api import app, database, settings
        pdf_path = settings.papers_dir / "qa.pdf"
        if options.pdf:
            shutil.copyfile(options.pdf.resolve(strict=True), pdf_path)
        elif options.scientific:
            from scientific_fixture import scientific_pdf
            pdf_path.write_bytes(scientific_pdf())
        else:
            pdf = canvas.Canvas(str(pdf_path))
            # The mocked continuation test explicitly spans source pages 2–3.
            for n in range(3 if options.complex_mock else 2):
                pdf.setFont("Times-Bold", 22)
                pdf.drawString(60, 760, f"{n + 1}. A quiet workspace for research")
                pdf.setFont("Times-Roman", 13)
                for line, text in enumerate(["This is a browser test fixture, not a real scientific paper.",
                                             "The reading interface keeps the source document and notes separate.",
                                             "A local-first library should remain useful when the network is unavailable.",
                                             "Numbers, equations and citations need to be checked against the original."]):
                    pdf.drawString(60, 700 - line * 24, text)
                pdf.showPage()
            pdf.save()
        for n, title in enumerate(["A Quiet Workspace for Research: Local-first Reading and Reliable Notes",
                                    "Retrieval-augmented Generation with Source-grounded Evidence",
                                    "Reading Scientific Papers with Multimodal Language Models"]):
            database.upsert_paper({"source": "test", "source_id": f"qa-{n}", "title": title,
                                   "authors": ["UI Test Fixture", "Not a Real Paper"], "abstract": "Isolated test content, never inserted into the user's library.",
                                   "published_at": "2026-10-02", "ai_tags": ["机器学习", "检索增强"] if n == 0 else ["大语言模型"],
                                   "local_pdf_path": str(pdf_path.relative_to(root)) if n == 0 else None})
        app.state.desktop_only = True
        source_server = None
        if options.source_rate_mock:
            import io
            import urllib.error
            import paper_reader.sources.arxiv as arxiv_module
            class SourceHTTP(BaseHTTPRequestHandler):
                requests, mode = 0, 'limited'
                def log_message(self, *args): pass
                def do_GET(self):
                    assert self.path.startswith('/api/query?')
                    SourceHTTP.requests += 1
                    body = b'QA source limit' if SourceHTTP.mode == 'limited' else b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
                    self.send_response(429 if SourceHTTP.mode == 'limited' else 200)
                    if SourceHTTP.mode == 'limited': self.send_header('Retry-After', '3600')
                    self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
            source_server = ThreadingHTTPServer(('127.0.0.1', 0), SourceHTTP)
            threading.Thread(target=source_server.serve_forever, daemon=True).start()
            original_arxiv_init = arxiv_module.ArxivSource.__init__
            def source_init(instance, user_agent, endpoint=None):
                original_arxiv_init(instance, user_agent, f'http://127.0.0.1:{source_server.server_port}/api/query')
            arxiv_module.ArxivSource.__init__ = source_init
            app.state.reader_service.save_config({'daily_enabled': False, 'auto_backup': False})
            database.add_annotation(1, {'page_number': 0, 'annotation_type': 'note', 'text': 'QA source-limit note must remain'})
            @app.get('/desktop/qa/source-state')
            def source_qa_state():
                return {'requests': SourceHTTP.requests, 'mode': SourceHTTP.mode,
                    'papers': len(database.overview_records()), 'notes': len(database.list_annotations(1)),
                    'model_requests': MockModel.requests}
            @app.post('/desktop/qa/source-expire')
            def source_qa_expire():
                value = app.state.source_cooldowns.active(['arxiv'])
                assert value is not None
                after = datetime.fromisoformat(value['retry_at']) + timedelta(seconds=1)
                app.state.source_cooldowns.clock = lambda: after
                SourceHTTP.mode = 'success'
                return {'expired_in_qa_clock_only': True}
        classification_mode = {'value': 'normal', 'calls': 0}
        if options.classification_mock:
            for number in range(2):
                database.upsert_paper({'source': 'arxiv', 'source_id': f'classification-qa-{number}',
                    'title': f'Explicit classification QA fixture {number}',
                    'abstract': 'Synthetic metadata for UI testing, not a real research paper.', 'categories': ['cs.AI']})
            import paper_reader.desktop_api as desktop_module
            desktop_module.sync_arxiv = lambda settings, db, **kwargs: {'fetched': 0, 'inserted': 0, 'failed': 0}
            from paper_reader.model_client import ModelResult, ModelError
            class ClassificationMock:
                def generate(self, prompt, *, system=None):
                    assert prompt.startswith('PAPER_READER_CLASSIFICATION_V1\n')
                    classification_mode['calls'] += 1
                    if classification_mode['value'] == 'quota':
                        raise ModelError('仅 QA 模拟用量限制，不是真实模型', 'quota', fatal=True)
                    return ModelResult(json.dumps({'summary_zh': '仅界面验收的模拟摘要，不是真实模型结果。',
                        'tags': ['模拟分类'], 'relevance': .5}), {}, .01)
            app.state.classification.reader.model_client = lambda cfg: ClassificationMock()
            @app.post('/desktop/qa/classification-mode')
            def classification_qa_mode(value: dict):
                classification_mode['value'] = value['mode']
                if value.get('seed'):
                    database.upsert_paper({'source': 'arxiv', 'source_id': 'classification-quota-qa',
                        'title': 'Explicit quota UI fixture', 'abstract': 'Synthetic QA abstract', 'categories': []})
                return classification_mode
            @app.get('/desktop/qa/classification-state')
            def classification_qa_state(): return classification_mode
        if options.reader_cache:
            from paper_reader.reader import PARSER_VERSION, file_hash
            legacy = json.loads(options.reader_cache.read_text(encoding='utf-8'))
            if not options.pdf or legacy['source_hash'] != file_hash(pdf_path) or not 1 <= legacy['parser_version'] < PARSER_VERSION:
                raise ValueError('Legacy UI fixture requires an exact matching PDF and an older parser cache')
            bid = legacy['pages'][0]['blocks'][0]['id']
            legacy['pages'][0]['translated'][bid] = '旧版译文（仅供迁移验收）'
            database.reader_put(1, 'document', legacy)
            database.reader_put(1, 'editor', {'edits': {bid: {'zh': '旧版手动译文（仅供迁移验收）',
                'base': hashlib.sha256(legacy['pages'][0]['translated'][bid].encode()).hexdigest()}}})
            database.add_annotation(1, {'page_number': 0, 'annotation_type': 'text-highlight',
                'text': json.dumps({'block_id': bid, 'quote': legacy['pages'][0]['blocks'][0]['en']})})
            database.add_annotation(1, {'page_number': 0, 'annotation_type': 'note', 'text': '旧版笔记保留（仅供迁移验收）'})
        if options.complex_mock:
            from complex_fixture import mock_complex_result
            parser_service = app.state.complex_parser
            mock_mode = {'value': 'normal'}
            parser_service.runtime = lambda ocr=False: {'available': True, 'reason': None, 'backend': 'qa-mock',
                'warning': '模拟解析，仅供界面验收，不代表真实识别质量'}
            parser_service._paths = lambda: (Path(__import__('sys').executable), root, 'qa-mock')
            def mock_execute(snapshot, output, paths, job, cancel):
                if mock_mode['value'] == 'fail': raise ValueError('模拟解析失败；原内容保留')
                if cancel.wait(30 if mock_mode['value'] == 'slow' else .4): raise InterruptedError('模拟解析取消；原内容保留')
                return mock_complex_result(job['start_page'], job['end_page'], job['ocr'])
            parser_service._execute = mock_execute
            @app.post('/desktop/qa/complex-mode')
            def complex_mode(value: dict):
                if value.get('mode') not in ('normal', 'fail', 'slow'): raise ValueError('Invalid QA mode')
                mock_mode['value'] = value['mode']; return mock_mode
        if options.discovery:
            alternate_path = settings.papers_dir / 'alternate.pdf'
            alternate = canvas.Canvas(str(alternate_path))
            for page_number in range(3):
                alternate.drawString(60, 760, f'Alternate source version page {page_number + 1}')
                alternate.drawString(60, 700, 'Explicit fixture content: annotations belong to this PDF version.')
                alternate.showPage()
            alternate.save()
            secondary, _ = database.upsert_paper({'source': 'openalex', 'source_id': 'qa-alternate',
                'doi': 'https://doi.org/10.1234/qa.work', 'title': 'Published Source Version (QA only)',
                'abstract': 'Alternative metadata, never inserted into the real library.',
                'local_pdf_path': str(alternate_path.relative_to(root))})
            yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec='seconds')
            with database._lock:
                database._connection.execute('UPDATE papers SET doi=?,created_at=? WHERE id=1', ('DOI:10.1234/QA.Work', yesterday))
                database._connection.commit()
            for pid, title in [(1, '主版本笔记'), (secondary, '另一版本专属笔记')]:
                database.add_annotation(pid, {'page_number': 0 if pid == 1 else 2, 'annotation_type': 'note', 'text': title})
                database.reader_put(pid, 'state', {'last_page': 0 if pid == 1 else 2})
                database.reader_put(pid, 'research-card', {'revision': 1, 'fields': {'method': title + '方法'}, 'thoughts': title + '个人想法'})
            alternate_doc = app.state.reader_service.document(secondary)
            for block in alternate_doc['pages'][2]['blocks']:
                alternate_doc['pages'][2]['translated'][block['id']] = '另一版本第三页的中文译文（模拟，仅供界面验收）'
            database.reader_put(secondary, 'document', alternate_doc)
        class MockModel(BaseHTTPRequestHandler):
            requests = 0
            last_prompt = ''
            def do_GET(self):
                payload = json.dumps({"models": [{"name": "qa-model"}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                MockModel.requests += 1
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                MockModel.last_prompt = body['prompt']
                try:
                    source = json.loads(body['prompt'].split('\n', 1)[1])
                    if body['prompt'].startswith('PAPER_READER_ASSISTANT_V1\n'):
                        block = source['context']['sources'][0]
                        claim = {'kind': 'inference', 'text': '（模拟回答，仅供界面验收）这是依据所列段落的解释，不是论文事实。', 'sources': [{'id': block['id'], 'quote': block['en'][:2000]}]}
                        unknown = {'kind': 'unknown', 'text': '当前上下文未找到实验结论，需核对原文。', 'sources': []}
                        answer = {'fields': {'problem': [claim], 'method': [claim], 'result': [unknown], 'limitations': [unknown]}} if source['task'] == 'card' else {'claims': [claim]}
                        translated = json.dumps(answer, ensure_ascii=False)
                    else:
                        translated = json.dumps([{"id": block['id'], "zh": ("表格单元格定位验收（模拟）" if '-table-' in block['id'] and block['id'].endswith('-r1c0') else "（模拟译文，仅供界面验收）") + block['en']} for block in source], ensure_ascii=False)
                except (ValueError, IndexError, TypeError):
                    translated = "（模拟译文，仅供界面验收）" + body['prompt'].split('\n')[-1]
                payload = json.dumps({"response": translated}, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        model = ThreadingHTTPServer(('127.0.0.1', 0), MockModel)
        @app.get('/desktop/qa/model-calls')
        def model_calls():
            return {'count': MockModel.requests, 'last_prompt': MockModel.last_prompt}
        threading.Thread(target=model.serve_forever, daemon=True).start()
        app.state.reader_service.save_config({"provider": "ollama", "base_url": f"http://127.0.0.1:{model.server_port}", "model": "isolated-mock-model"})
        if options.chatgpt_mock:
            import io
            import httpx
            import jwt
            from cryptography.hazmat.primitives.asymmetric import rsa
            from paper_reader.chatgpt_auth import JWKS, TOKEN, DISCOVERY, ISSUER, SCOPES
            import paper_reader.chatgpt_model as chatgpt_model
            import time
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
            jwk.update(kid='qa-chatgpt', alg='RS256', use='sig')
            login = app.state.reader_service.chatgpt_login
            mode = {'value': 'normal', 'requests': 0, 'last_body': None, 'browser_opened': 0}
            def auth_transport(url, form=None, **kwargs):
                if url == JWKS: return {'keys': [jwk]}
                if url == DISCOVERY: return {'issuer': ISSUER, 'revocation_endpoint': ISSUER + '/api/accounts/oauth/revoke'}
                if url.endswith('/revoke'): return {}
                assert url == TOKEN and form['grant_type'] == 'authorization_code'
                claims = {'iss': ISSUER, 'aud': form['client_id'], 'sub': 'qa-chatgpt-user',
                    'nonce': login._attempt.nonce, 'email': 'qa-only@example.invalid',
                    'iat': int(time.time()), 'exp': int(time.time()) + 3600}
                return {'access_token': 'qa-not-real-access', 'refresh_token': 'qa-not-real-refresh',
                    'id_token': jwt.encode(claims, key, algorithm='RS256', headers={'kid': 'qa-chatgpt'}),
                    'token_type': 'Bearer', 'expires_in': 3600, 'scope': SCOPES}
            def browser_mock(url):
                mode['browser_opened'] += 1
                return True
            login.open_browser = browser_mock
            login.auth.transport = auth_transport
            def plan_open(request, timeout):
                if request.full_url.endswith('/models'):
                    return io.BytesIO(json.dumps({'models': [{'slug': 'qa-chatgpt-only', 'display_name': 'QA 模拟模型，不是真实 ChatGPT', 'visibility': 'list'}]}).encode())
                assert request.full_url == 'https://api.openai.com/v1/responses'
                mode['requests'] += 1; mode['last_body'] = json.loads(request.data)
                if mode['value'] == 'quota':
                    events = [{'type': 'response.output_text.delta', 'delta': 'partial not saved'},
                        {'type': 'response.failed', 'response': {'error': {'code': 'subscription_sharing_usage_limit_exceeded'}}}]
                else:
                    events = [{'type': 'response.completed', 'response': {'status': 'completed', 'output': [{'type': 'message',
                        'content': [{'type': 'output_text', 'text': '（模拟试译，不是真实 ChatGPT）本研究使用 120 个样本评估三种方法 [1]。'}]}], 'usage': {'input_tokens': 12, 'output_tokens': 10}}}]
                return io.BytesIO(b''.join(b'data: ' + json.dumps(event).encode() + b'\n\n' for event in events))
            chatgpt_model.open_model_request = plan_open
            @app.post('/desktop/qa/chatgpt/complete')
            def complete_qa_chatgpt():
                attempt = login._attempt
                with httpx.Client(trust_env=False) as client:
                    response = client.get(attempt.redirect_uri, params={'state': attempt.state, 'code': 'qa-only-code', 'client_id': 'oaiapp_qa_chatgpt'})
                assert response.status_code == 200
                return {'completed': True}
            @app.get('/desktop/qa/chatgpt/state')
            def qa_chatgpt_state(): return mode
            @app.post('/desktop/qa/chatgpt/mode')
            def qa_chatgpt_mode(value: dict):
                mode['value'] = value['mode']; return {'mode': mode['value']}
        try:
            uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
        finally:
            if source_server:
                source_server.shutdown(); source_server.server_close()
            model.shutdown()
            model.server_close()
            database.close()


if __name__ == "__main__":
    main()
