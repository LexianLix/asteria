"""Exercise the actual worker configuration seam without optional model imports."""
import json
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from paper_reader import complex_worker
from paper_reader.complex_service import ComplexParserService


class ComplexWorkerBackendTest(unittest.TestCase):
    def run_worker(self, ocr, configured):
        calls = []
        pdfium = type('PdfiumBackend', (), {})
        def option(**kwargs):
            calls.append(kwargs)
            return kwargs
        class Converter:
            def __init__(self, **kwargs): pass
            def convert(self, *args, **kwargs):
                return SimpleNamespace(status=SimpleNamespace(value='success'),
                    document=SimpleNamespace(export_to_dict=lambda: {'pages': {}}))
        modules = {
            'docling.datamodel.base_models': SimpleNamespace(DocumentStream=lambda **kw: kw, InputFormat=SimpleNamespace(PDF='pdf')),
            'docling.datamodel.pipeline_options': SimpleNamespace(PdfPipelineOptions=lambda **kw: SimpleNamespace(**kw),
                RapidOcrOptions=lambda **kw: kw, TableStructureOptions=lambda **kw: kw, TableFormerMode=SimpleNamespace(ACCURATE='accurate')),
            'docling.datamodel.accelerator_options': SimpleNamespace(AcceleratorOptions=lambda **kw: kw, AcceleratorDevice=SimpleNamespace(CPU='cpu')),
            'docling.document_converter': SimpleNamespace(DocumentConverter=Converter, PdfFormatOption=option),
            'docling.backend.pypdfium2_backend': SimpleNamespace(PyPdfiumDocumentBackend=pdfium),
        }
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'input.pdf'; source.write_bytes(b'%PDF-QA')
            output = root / 'result.json'
            argv = ['worker', '--pdf', str(source), '--output', str(output), '--models', str(root),
                    '--start', '1', '--end', '1', '--backend', configured] + (['--ocr'] if ocr else [])
            # main installs a process-local network boundary; restore it in this in-process test.
            with patch.dict(sys.modules, modules), patch.object(sys, 'argv', argv), \
                    patch.dict(complex_worker.os.environ), patch.object(socket.socket, 'connect'), \
                    patch.object(socket.socket, 'connect_ex'), patch.object(complex_worker.importlib.metadata, 'version', return_value='QA'):
                complex_worker.main()
            return calls[0], json.loads(output.read_text(encoding='utf-8')), pdfium

    def test_ocr_uses_pdfium_and_reports_effective_and_configured_backends(self):
        for configured in ('docling_parse', 'pypdfium'):
            with self.subTest(configured=configured):
                option, payload, pdfium = self.run_worker(True, configured)
                self.assertIs(option.get('backend'), pdfium)
                self.assertEqual(payload['backend'], 'pypdfium')
                self.assertEqual(payload['configured_backend'], configured)
                self.assertTrue(payload['ocr'])

    def test_digital_pdf_retains_configured_backend(self):
        for configured in ('docling_parse', 'pypdfium'):
            with self.subTest(configured=configured):
                option, payload, pdfium = self.run_worker(False, configured)
                self.assertEqual(payload['backend'], configured)
                self.assertIs(option.get('backend'), pdfium if configured == 'pypdfium' else None)

    def test_runtime_discloses_ocr_backend_before_launch(self):
        with patch.dict(complex_worker.os.environ, {'PAPER_READER_COMPLEX_BACKEND': 'docling_parse'}):
            runtime = ComplexParserService.runtime(None, ocr=True)
            self.assertEqual(runtime['backend'], 'pypdfium')
            self.assertEqual(runtime['configured_backend'], 'docling_parse')
            self.assertIn('PDFium', runtime['warning'])
            self.assertEqual(ComplexParserService.runtime(None, ocr=False)['backend'], 'docling_parse')


if __name__ == '__main__': unittest.main()
