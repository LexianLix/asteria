"""Optional CPU parser lifecycle and isolated, source-bound review cache."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

import pdfplumber
import psutil

from .complex_pdf import normalize_complex_document
from .complex_scripts import restore_complex_scripts
from .complex_worker import effective_backend
from .reader import ReaderBusyError, file_hash, pdf_path
from .storage import utc_now


class ComplexParserService:
    def __init__(self, reader):
        self.reader, self.db = reader, reader.db
        self._active = {}
        self._threads = set()
        self.timeout = 600

    def runtime(self, ocr=False):
        interpreter = os.getenv('PAPER_READER_COMPLEX_PYTHON', '')
        models = os.getenv('PAPER_READER_COMPLEX_MODELS', '')
        backend = os.getenv('PAPER_READER_COMPLEX_BACKEND', 'docling_parse')
        reason = None
        if not interpreter or not models:
            reason = '尚未配置独立解析环境。请设置 PAPER_READER_COMPLEX_PYTHON 和 PAPER_READER_COMPLEX_MODELS 后重新启动。普通阅读不受影响。'
        elif not Path(interpreter).is_file() or not Path(models).is_dir():
            reason = '独立 Python 或本地模型目录不存在，请检查路径。'
        elif backend not in ('docling_parse', 'pypdfium'):
            reason = '解析后端配置无效。'
        else:
            required = ['docling-project--docling-layout-heron/model.safetensors',
                'docling-project--docling-models/model_artifacts/tableformer/accurate/tableformer_accurate.safetensors']
            if ocr:
                required += ['RapidOcr/ch_ppocr_mobile_v2.0_cls_mobile.onnx',
                             'RapidOcr/PP-OCRv6_det_small.onnx', 'RapidOcr/PP-OCRv6_rec_small.onnx']
            if any(not (Path(models) / name).is_file() for name in required):
                reason = '本地布局、表格或英文 OCR 模型不完整；程序不会自动联网下载，请先准备模型。'
        warning = '可选 CPU 增强，可能占用约 1.5 GB 内存；独立预览不自动替换已保存正文。'
        if ocr:
            warning += ' 英文扫描 OCR 固定使用 PDFium 渲染；识别结果仍需核对原页。'
        return {'available': reason is None, 'reason': reason, 'backend': effective_backend(backend, ocr),
                'configured_backend': backend,
                'ocr_language': '英文', 'network': '不调用远程推理；Python 连接边界禁止联网',
                'warning': warning}

    def _paths(self):
        # Do not resolve the interpreter's ASCII junction to its Unicode target.
        return (Path(os.environ['PAPER_READER_COMPLEX_PYTHON']).absolute(),
                Path(os.environ['PAPER_READER_COMPLEX_MODELS']).resolve(),
                os.getenv('PAPER_READER_COMPLEX_BACKEND', 'docling_parse'))

    def preview(self, paper_id):
        with self.reader._lock:
            paper = self.db.get_paper(paper_id)
            if not paper:
                raise ValueError('论文不存在')
            preview = self.db.reader_get(paper_id, 'complex-preview')
            if preview and preview.get('source_hash') != file_hash(pdf_path(self.reader.settings, paper)):
                raise ReaderBusyError('PDF 已变化，旧增强结果不能再定位；请重新解析')
            return preview

    def start(self, paper_id, start_page=1, end_page=None, ocr=False):
        with self.reader._lock:
            if paper_id in self._active:
                return self.db.job_get(f'complex:{paper_id}')
            if self._active:
                raise ReaderBusyError('另一篇论文正在增强解析，请完成或取消后再开始')
            runtime = self.runtime(ocr)
            if not runtime['available']:
                raise ValueError(runtime['reason'])
            paper = self.db.get_paper(paper_id)
            if not paper:
                raise ValueError('论文不存在')
            path = pdf_path(self.reader.settings, paper)
            if path.stat().st_size > 100 * 1024**2:
                raise ValueError('增强解析 PDF 不能超过 100 MB')
            with pdfplumber.open(path) as pdf:
                page_count = len(pdf.pages)
            end_page = end_page if end_page is not None else start_page
            if not 1 <= start_page <= end_page <= page_count or end_page - start_page >= 100:
                raise ValueError('请选择 PDF 内的有效页码范围；每次最多解析 100 页')
            cancel = threading.Event(); run_id = uuid.uuid4().hex
            job = {'status': 'queued', 'run_id': run_id, 'updated_at': utc_now(), 'error': None,
                'start_page': start_page, 'end_page': end_page, 'total': end_page - start_page + 1,
                'done': 0, 'ocr': ocr, 'stage': '准备本地模型；不上传 PDF'}
            # Capture interpreter/models before the background thread: destination cannot drift.
            paths = self._paths(); digest = file_hash(path)
            job.update(backend=effective_backend(paths[2], ocr), configured_backend=paths[2])
            self.db.job_put(f'complex:{paper_id}', job)
            self._active[paper_id] = cancel
            thread = threading.Thread(target=self._run, args=(paper_id, path, digest, paths, job, cancel), daemon=True)
            self._threads.add(thread); thread.start()
            return job

    @staticmethod
    def _alive(process):
        try:
            return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    def _execute(self, snapshot, output, paths, job, cancel):
        if cancel.is_set():
            raise InterruptedError('增强解析已取消；未启动解析进程')
        interpreter, models, backend = paths
        command = [str(interpreter), str(Path(__file__).with_name('complex_worker.py')),
            '--pdf', str(snapshot), '--output', str(output), '--models', str(models),
            '--start', str(job['start_page']), '--end', str(job['end_page']), '--backend', backend]
        if job['ocr']:
            command.append('--ocr')
        env = {key: value for key, value in os.environ.items() if not re.search(r'KEY|TOKEN|SECRET|PASSWORD', key, re.I)
               and not key.startswith('PAPER_READER_') and key not in ('OLLAMA_URL', 'PYTHONPATH')}
        env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
                   TOKENIZERS_PARALLELISM='false', PYTHONIOENCODING='utf-8')
        owned = {}; started = time.monotonic()
        with (output.parent / 'worker.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            launcher = psutil.Process(process.pid); owned[(launcher.pid, launcher.create_time())] = launcher
            try:
                while process.poll() is None or any(self._alive(child) for child in owned.values()):
                    try:
                        for child in launcher.children(recursive=True):
                            owned[(child.pid, child.create_time())] = child
                    except psutil.NoSuchProcess:
                        pass
                    if cancel.is_set():
                        raise InterruptedError('增强解析已取消；上次结果和原文阅读保留')
                    if time.monotonic() - started > self.timeout:
                        raise TimeoutError('本地解析超时；请缩小页码范围，上次结果和原文阅读保留')
                    cancel.wait(.1)
                if process.wait() != 0:
                    raise ValueError('本地增强解析失败；请检查独立环境、模型和 ASCII 路径。上次结果和原文阅读保留')
            finally:
                # Only processes observed beneath our own launcher, with creation identity.
                live = [child for child in owned.values() if self._alive(child)]
                for child in reversed(live):
                    try: child.terminate()
                    except psutil.NoSuchProcess: pass
                _, remaining = psutil.wait_procs(live, timeout=1)
                for child in remaining:
                    if self._alive(child):
                        try: child.kill()
                        except psutil.NoSuchProcess: pass
                psutil.wait_procs(remaining, timeout=2)
                process.wait(timeout=3)
        if not output.is_file() or output.stat().st_size > 100 * 1024**2:
            raise ValueError('增强解析没有完整结果或结果超限；原内容保留')
        return json.loads(output.read_text(encoding='utf-8'))

    def _run(self, paper_id, path, digest, paths, job, cancel):
        try:
            with tempfile.TemporaryDirectory(prefix='paper-complex-') as folder:
                directory = Path(folder); snapshot = directory / 'input.pdf'
                shutil.copyfile(path, snapshot)
                if file_hash(snapshot) != digest:
                    raise ValueError('复制期间 PDF 已变化，请重新解析')
                job.update(status='running', stage='本地模型解析中；尚无页级进度', updated_at=utc_now())
                self.db.job_put(f'complex:{paper_id}', job)
                raw = self._execute(snapshot, directory / 'result.json', paths, job, cancel)
                if raw.get('remote_inference') is not False or raw.get('blocked_network_attempts', 0) != 0:
                    raise ValueError('解析结果不满足本地执行边界，未保存增强预览')
                if set(map(int, raw['document'].get('pages', {}))) != set(range(job['start_page'], job['end_page'] + 1)):
                    raise ValueError('解析结果缺少指定页或包含范围外页面，未保存增强预览')
                normalized = normalize_complex_document(raw['document'], digest)
                normalized = restore_complex_scripts(normalized, raw['document'], snapshot,
                    ocr=bool(job['ocr'] or raw.get('ocr', True)))
                normalized.update(parser={key: raw.get(key) for key in ('versions', 'backend', 'configured_backend', 'ocr', 'remote_inference')},
                    requested_pages=[job['start_page'], job['end_page']], created_at=utc_now(), revision=1)
                with self.reader._lock:
                    if cancel.is_set():
                        raise InterruptedError('增强解析已取消；上次结果和原文阅读保留')
                    paper = self.db.get_paper(paper_id)
                    if not paper or file_hash(pdf_path(self.reader.settings, paper)) != digest:
                        raise ValueError('PDF 已变化，未保存旧增强结果；请重新解析')
                    previous = self.db.reader_get(paper_id, 'complex-preview')
                    normalized['revision'] = previous.get('revision', 0) + 1
                    self.db.reader_put(paper_id, 'complex-preview', normalized)
                    job.update(status='done', done=job['total'], stage='独立预览已就绪；请核对原 PDF', updated_at=utc_now())
        except InterruptedError as exc:
            job.update(status='cancelled', error=str(exc), updated_at=utc_now())
        except Exception as exc:
            # Do not expose worker stderr, prompts or raw extraction as an API error.
            message = str(exc) if isinstance(exc, (ValueError, TimeoutError)) else '增强解析失败；上次结果、正文和笔记保留'
            job.update(status='failed', error=message, updated_at=utc_now())
        finally:
            with self.reader._lock:
                self.db.job_put(f'complex:{paper_id}', job)
                self._active.pop(paper_id, None)
                self._threads.discard(threading.current_thread())

    def cancel(self, paper_id):
        with self.reader._lock:
            event = self._active.get(paper_id)
            if event:
                event.set()
                job = self.db.job_get(f'complex:{paper_id}'); job.update(status='cancelling', updated_at=utc_now())
                self.db.job_put(f'complex:{paper_id}', job)
            return self.db.job_get(f'complex:{paper_id}')

    def confirm_link(self, paper_id, source_hash, from_id, to_id, revision, confirmed=True):
        with self.reader._lock:
            if paper_id in self._active:
                raise ReaderBusyError('增强解析仍在执行，请等待完成后再确认续表')
            preview = self.preview(paper_id)
            if not preview or source_hash != preview['source_hash'] or revision != preview.get('revision'):
                raise ReaderBusyError('增强预览已变化，请重新打开并核对')
            link = next((link for link in preview['table_links'] if link['from_id'] == from_id and link['to_id'] == to_id), None)
            if not link:
                raise ValueError('没有这个续表候选')
            link.update(confirmed=confirmed, confirmed_at=utc_now() if confirmed else None)
            preview['revision'] += 1
            self.db.reader_put(paper_id, 'complex-preview', preview)
            return preview

    def shutdown(self):
        with self.reader._lock:
            for event in self._active.values(): event.set()
            threads = list(self._threads)
        for thread in threads: thread.join(timeout=8)
