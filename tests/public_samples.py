"""Optional, hash-pinned public PDFs; never download or require a private library.

Set ASTERIA_TEST_PDF_DIR to an external sample directory. The legacy ignored
sample location remains a fallback. A supplied but changed sample is an error.
"""
import hashlib
import os
from pathlib import Path


def resnet_pdf():
    root = Path(os.environ.get('ASTERIA_TEST_PDF_DIR',
        Path(__file__).resolve().parents[1] / 'data/parser-qa/reading-order'))
    path = root / 'resnet-1512.03385.pdf'
    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() != '1e0651b6810ecba34a3dbc5b5b0209226f889004607c1f203540a48d64e5a93a':
        raise ValueError('Public ResNet regression PDF does not match the pinned SHA256')
    return path
