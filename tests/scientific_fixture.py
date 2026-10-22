"""Synthetic scientific PDF, explicitly a QA fixture, not a user artifact."""
import io
from reportlab.pdfgen import canvas


def scientific_pdf(*, unsafe_header=False):
    stream = io.BytesIO(); pdf = canvas.Canvas(stream, pagesize=(595, 842))
    pdf.setFont('Times-Roman', 13)
    pdf.drawString(60, 780, 'Scientific QA fixture - not a real paper.')
    pdf.drawString(60, 750, 'Body text stays separate from equations and experimental tables.')
    pdf.setFont('Times-Italic', 17); pdf.drawString(220, 680, 'E = m c')
    pdf.setFont('Times-Roman', 9); pdf.drawString(279, 688, '2')
    pdf.setFont('Times-Roman', 12); pdf.drawString(480, 680, '(1)')
    pdf.drawString(60, 620, 'Table 1: Numerical results (synthetic).')
    xs, ys = [60, 200, 340, 480], [595, 565, 535, 505]
    for x in xs: pdf.line(x, ys[-1], x, ys[0])
    for y in ys: pdf.line(xs[0], y, xs[-1], y)
    rows = [['Model', 'Accuracy', 'Latency'], ['Alpha', '95%', '2.5 ms'], ['Beta', '90%', '1.7 ms']]
    for row, cells in enumerate(rows):
        for col, text in enumerate(cells): pdf.drawString(xs[col] + 8, ys[row] - 21, text)
    pdf.drawString(60, 460, 'The table has three columns; do not join numbers into prose.')
    pdf.showPage()
    pdf.setFont('Times-Roman', 12)
    pdf.drawString(60, 780, 'Ordinary prose: the result = an initial estimate, not a displayed formula.')
    pdf.drawString(60, 735, 'Table 2: Horizontal rules without vertical borders (synthetic).')
    for y in [710, 680, 620]: pdf.line(60, y, 480, y)
    heading = ['Metric', 'Combined results across model versions', ''] if unsafe_header else ['Metric', 'Baseline', 'Ours']
    for y, row in [(690, heading), (655, ['Accuracy', '80%', '85%']), (635, ['Latency', '4.2 ms', '3.1 ms'])]:
        for x, text in zip([68, 210, 350], row): pdf.drawString(x, y, text)
    pdf.drawString(60, 565, 'The source image remains authoritative for merged cells and math glyphs.')
    pdf.save(); return stream.getvalue()


def qualitative_pdf():
    """Tall, multiline, all-text table with a no-space caption."""
    stream = io.BytesIO(); pdf = canvas.Canvas(stream, pagesize=(595, 842))
    pdf.setFont('Times-Roman', 12)
    pdf.drawString(60, 760, 'Table1: Synthetic qualitative risks, not a real paper.')
    for y in [710, 680, 300]:
        pdf.line(60, y, 520, y)
    for x, text in zip([68, 210, 380], ['Risk', 'Mechanism', 'Control']):
        pdf.drawString(x, 690, text)
    for y, title in zip([655, 580, 505, 430, 355], ['Drift', 'Overreach', 'Adaptation', 'Attribution', 'Replication']):
        pdf.drawString(68, y, title)
        pdf.drawString(210, y, 'Persistent behavior')
        pdf.drawString(210, y - 16, 'changes over time.')
        pdf.drawString(380, y, 'Review authority')
        pdf.drawString(380, y - 16, 'and consequences.')
    pdf.drawString(60, 250, 'Body prose below the table is not part of any cell.')
    pdf.save(); return stream.getvalue()
