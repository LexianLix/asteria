"""Standalone local parser worker, run only by an explicitly configured Python.

No app imports: the optional environment needs Docling but not Paper Reader.
This source file is shipped as a data file in the desktop bundle.
"""
import argparse
import importlib.metadata
import io
import json
import os
from pathlib import Path
import socket


def effective_backend(configured, ocr=False):
    # Real scanned-page QA found dropped OCR lines with the native raster backend.
    # Keep native digital-PDF/table extraction; full-page OCR uses PDFium explicitly.
    return 'pypdfium' if ocr else configured


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pdf', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--models', type=Path, required=True)
    parser.add_argument('--start', type=int, required=True)
    parser.add_argument('--end', type=int, required=True)
    parser.add_argument('--backend', choices=['docling_parse', 'pypdfium'], required=True)
    parser.add_argument('--ocr', action='store_true')
    args = parser.parse_args()
    # Do not rely on HF offline flags alone. A missing artifact must fail locally.
    original = socket.socket.connect
    blocked = []
    def offline(connection, address):
        if connection.family in (socket.AF_INET, socket.AF_INET6):
            blocked.append(True)
            raise RuntimeError('Local parser network connection rejected')
        return original(connection, address)
    socket.socket.connect = offline
    socket.socket.connect_ex = offline
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1')
    from docling.datamodel.base_models import DocumentStream, InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions, TableStructureOptions, TableFormerMode
    from docling.datamodel.accelerator_options import AcceleratorOptions, AcceleratorDevice
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
    options = PdfPipelineOptions(artifacts_path=args.models.resolve(), do_ocr=args.ocr,
        do_table_structure=True, do_formula_enrichment=False, do_picture_description=False,
        enable_remote_services=False,
        accelerator_options=AcceleratorOptions(num_threads=4, device=AcceleratorDevice.CPU),
        table_structure_options=TableStructureOptions(mode=TableFormerMode.ACCURATE, do_cell_matching=True))
    if args.ocr:
        options.ocr_options = RapidOcrOptions(force_full_page_ocr=True, backend='onnxruntime', lang=['iso:en'])
    backend = effective_backend(args.backend, args.ocr)
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options,
        **({'backend': PyPdfiumDocumentBackend} if backend == 'pypdfium' else {}))})
    result = converter.convert(DocumentStream(name='input.pdf', stream=io.BytesIO(args.pdf.read_bytes())),
                               page_range=(args.start, args.end))
    if str(result.status.value) != 'success':
        raise RuntimeError('Local parser did not complete every requested page')
    versions = {}
    for name in ('docling', 'docling-core', 'docling-ibm-models', 'torch', 'rapidocr', 'onnxruntime'):
        versions[name] = importlib.metadata.version(name)
    payload = {'document': result.document.export_to_dict(), 'versions': versions,
               'backend': backend, 'configured_backend': args.backend, 'ocr': args.ocr,
               'remote_inference': False, 'blocked_network_attempts': len(blocked)}
    temporary = args.output.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    temporary.replace(args.output)


if __name__ == '__main__':
    main()
