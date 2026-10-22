"""In-memory vector/OCR-layer fixtures, not real papers or user artifacts."""
import io

from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from PIL import Image


def numeric_pdf_and_document(*, image_layer=False):
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream, pagesize=(600, 800))
    if image_layer:
        pdf.drawImage(ImageReader(Image.new('RGB', (4, 4), 'white')), 0, 0, 600, 800)
    pdf.setFont('Times-Roman', 12)
    for x, text in [(70, '256'), (200, '10'), (320, 'b'), (450, '2562')]:
        pdf.drawString(x, 680, text)
    pdf.setFont('Times-Roman', 8)
    pdf.drawString(88, 684, '2')
    pdf.drawString(212, 684, '-4')
    pdf.drawString(326, 677, '1')
    pdf.save()
    cells = []
    for column, (left, right, text) in enumerate([
        (68, 100, '2562'), (198, 230, '10-4'), (318, 340, 'b1'), (448, 480, '2562')
    ]):
        cells.append({'text': text, 'start_row_offset_idx': 0, 'end_row_offset_idx': 1,
            'start_col_offset_idx': column, 'end_col_offset_idx': column + 1,
            'row_span': 1, 'col_span': 1,
            'bbox': {'l': left, 't': 105, 'r': right, 'b': 135, 'coord_origin': 'TOPLEFT'}})
    raw = {'pages': {'1': {'size': {'width': 600, 'height': 800}}}, 'texts': [],
        'tables': [{'self_ref': '#/tables/0', 'prov': [{'page_no': 1,
            'bbox': {'l': 50, 't': 705, 'r': 550, 'b': 660, 'coord_origin': 'BOTTOMLEFT'}}],
            'data': {'num_rows': 1, 'num_cols': 4, 'table_cells': cells}}]}
    return stream.getvalue(), raw
