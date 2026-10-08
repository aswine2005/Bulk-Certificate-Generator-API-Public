"""The single, predefined certificate template, rendered to PDF with ReportLab.

The renderer is a pure function of its input (no database, no filesystem), so it
is easy to test and safe to call from several worker threads at once.
"""

import io
from dataclasses import dataclass
from datetime import date

from reportlab.graphics.barcode import qrencoder
from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

PAGE_WIDTH, PAGE_HEIGHT = landscape(A4)

NAVY = HexColor("#1F2A44")
GOLD = HexColor("#B8913A")
INK = HexColor("#4A5068")
PAPER = HexColor("#FBF8F1")

# The template uses ReportLab's built-in Type 1 fonts, which only cover the
# Windows-1252 (Western European) character set. Anything outside it would be
# drawn as a black box, so validation rejects such text up front.
TEMPLATE_ENCODING = "cp1252"


def can_render(text: str) -> bool:
    """True if every character in ``text`` exists in the template's fonts."""
    try:
        text.encode(TEMPLATE_ENCODING)
    except UnicodeEncodeError:
        return False
    return True


@dataclass(frozen=True)
class CertificateData:
    recipient_name: str
    title: str
    course_name: str
    issuer_name: str
    issue_date: date
    verification_code: str
    verification_url: str
    description: str | None = None
    signatory_name: str | None = None
    signatory_title: str | None = None


class CertificateRenderer:
    """Renders the certificate template to PDF bytes."""

    file_extension = "pdf"

    def render(self, data: CertificateData) -> bytes:
        buffer = io.BytesIO()
        # invariant=1 removes timestamps/random IDs, so the same input always
        # produces byte-identical output (makes re-generation idempotent).
        pdf = Canvas(buffer, pagesize=(PAGE_WIDTH, PAGE_HEIGHT), invariant=1)
        pdf.setTitle(f"{data.title} - {data.recipient_name}")
        pdf.setAuthor(data.issuer_name)
        pdf.setSubject(data.course_name)

        self._draw_frame(pdf)
        self._draw_header(pdf, data)
        self._draw_body(pdf, data)
        self._draw_footer(pdf, data)

        pdf.showPage()
        pdf.save()
        return buffer.getvalue()

    # -- layout ---------------------------------------------------------------

    def _draw_frame(self, pdf: Canvas) -> None:
        pdf.setFillColor(PAPER)
        pdf.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, stroke=0, fill=1)

        pdf.setStrokeColor(NAVY)
        pdf.setLineWidth(6)
        pdf.rect(18, 18, PAGE_WIDTH - 36, PAGE_HEIGHT - 36)

        pdf.setStrokeColor(GOLD)
        pdf.setLineWidth(1.2)
        pdf.rect(30, 30, PAGE_WIDTH - 60, PAGE_HEIGHT - 60)

        for x, y in (
            (30, 30),
            (30, PAGE_HEIGHT - 30),
            (PAGE_WIDTH - 30, 30),
            (PAGE_WIDTH - 30, PAGE_HEIGHT - 30),
        ):
            self._diamond(pdf, x, y, 7)

    def _draw_header(self, pdf: Canvas, data: CertificateData) -> None:
        cx = PAGE_WIDTH / 2

        size = _fit_single_line(data.issuer_name.upper(), "Helvetica-Bold", 13, 560, char_space=3)
        self._centered(
            pdf,
            data.issuer_name.upper(),
            "Helvetica-Bold",
            size,
            GOLD,
            PAGE_HEIGHT - 82,
            char_space=3,
        )

        title = data.title.upper()
        size = _fit_single_line(title, "Times-Bold", 34, 620, char_space=2)
        self._centered(pdf, title, "Times-Bold", size, NAVY, PAGE_HEIGHT - 135, char_space=2)

        # Divider: two gold rules with a diamond in the middle.
        y = PAGE_HEIGHT - 158
        pdf.setStrokeColor(GOLD)
        pdf.setLineWidth(1)
        pdf.line(cx - 140, y, cx - 12, y)
        pdf.line(cx + 12, y, cx + 140, y)
        self._diamond(pdf, cx, y, 5)

    def _draw_body(self, pdf: Canvas, data: CertificateData) -> None:
        cx = PAGE_WIDTH / 2

        self._centered(
            pdf, "This certificate is proudly presented to", "Helvetica", 13, INK, PAGE_HEIGHT - 200
        )

        size = _fit_single_line(data.recipient_name, "Times-BoldItalic", 46, 620)
        self._centered(pdf, data.recipient_name, "Times-BoldItalic", size, NAVY, PAGE_HEIGHT - 260)

        pdf.setStrokeColor(GOLD)
        pdf.setLineWidth(0.8)
        pdf.line(cx - 220, PAGE_HEIGHT - 278, cx + 220, PAGE_HEIGHT - 278)

        self._centered(pdf, "for successfully completing", "Helvetica", 13, INK, PAGE_HEIGHT - 308)

        size, lines = _fit_lines(data.course_name, "Times-Bold", 24, 16, 600, max_lines=2)
        y = PAGE_HEIGHT - 342
        for line in lines:
            self._centered(pdf, line, "Times-Bold", size, NAVY, y)
            y -= size * 1.2

        if data.description:
            size, lines = _fit_lines(data.description, "Helvetica-Oblique", 11, 9, 560, max_lines=3)
            y -= 6
            for line in lines:
                self._centered(pdf, line, "Helvetica-Oblique", size, INK, y)
                y -= size * 1.35

    def _draw_footer(self, pdf: Canvas, data: CertificateData) -> None:
        line_y = 112
        left_cx, right_cx = 205, PAGE_WIDTH - 205

        # Left: issue date.
        self._centered(
            pdf, _format_date(data.issue_date), "Helvetica", 13, NAVY, line_y + 10, x=left_cx
        )
        self._signature_line(pdf, left_cx, line_y)
        self._centered(
            pdf, "DATE OF ISSUE", "Helvetica", 8.5, INK, line_y - 16, x=left_cx, char_space=1.5
        )

        # Right: signatory.
        if data.signatory_name:
            size = _fit_single_line(data.signatory_name, "Times-Italic", 20, 210)
            self._centered(
                pdf, data.signatory_name, "Times-Italic", size, NAVY, line_y + 10, x=right_cx
            )
        self._signature_line(pdf, right_cx, line_y)
        label = (data.signatory_title or "Authorized Signatory").upper()
        size = _fit_single_line(label, "Helvetica", 8.5, 230, char_space=1.5)
        self._centered(pdf, label, "Helvetica", size, INK, line_y - 16, x=right_cx, char_space=1.5)

        # Centre: QR code that links to the public verification endpoint.
        qr_size = 70
        self._qr_code(pdf, data.verification_url, PAGE_WIDTH / 2 - qr_size / 2, 72, qr_size)

        self._centered(pdf, f"Verification code: {data.verification_code}", "Helvetica", 8, INK, 58)

    # -- drawing helpers ------------------------------------------------------

    def _centered(
        self,
        pdf: Canvas,
        text: str,
        font: str,
        size: float,
        color,
        y: float,
        x: float | None = None,
        char_space: float = 0,
    ) -> None:
        if x is None:
            x = PAGE_WIDTH / 2
        width = _text_width(text, font, size, char_space)
        pdf.setFillColor(color)
        text_obj = pdf.beginText(x - width / 2, y)
        text_obj.setFont(font, size)
        text_obj.setCharSpace(char_space)
        text_obj.textOut(text)
        pdf.drawText(text_obj)

    @staticmethod
    def _qr_code(pdf: Canvas, value: str, x: float, y: float, size: float) -> None:
        """Draw a QR code as a single filled path.

        ReportLab's QrCodeWidget encodes the data twice and creates one shape
        object per module; drawing the module matrix directly is ~5x faster,
        which matters when rendering thousands of certificates.
        """
        qr = qrencoder.QRCode(None, qrencoder.QRErrorCorrectLevel.M)
        qr.addData(value)
        qr.make()
        count = qr.getModuleCount()
        module = size / count
        quiet = 2 * module

        pdf.setFillColor(white)
        pdf.rect(x - quiet, y - quiet, size + 2 * quiet, size + 2 * quiet, stroke=0, fill=1)

        path = pdf.beginPath()
        for row in range(count):
            top = y + size - (row + 1) * module  # row 0 is the top of the symbol
            col = 0
            while col < count:
                if not qr.isDark(row, col):
                    col += 1
                    continue
                start = col
                while col < count and qr.isDark(row, col):
                    col += 1
                path.rect(x + start * module, top, (col - start) * module, module)
        pdf.setFillColor(black)
        pdf.drawPath(path, stroke=0, fill=1)

    @staticmethod
    def _signature_line(pdf: Canvas, cx: float, y: float) -> None:
        pdf.setStrokeColor(NAVY)
        pdf.setLineWidth(0.8)
        pdf.line(cx - 115, y, cx + 115, y)

    @staticmethod
    def _diamond(pdf: Canvas, cx: float, cy: float, r: float) -> None:
        pdf.setFillColor(GOLD)
        path = pdf.beginPath()
        path.moveTo(cx, cy + r)
        path.lineTo(cx + r, cy)
        path.lineTo(cx, cy - r)
        path.lineTo(cx - r, cy)
        path.close()
        pdf.drawPath(path, stroke=0, fill=1)


# -- text fitting helpers -----------------------------------------------------


def _text_width(text: str, font: str, size: float, char_space: float = 0) -> float:
    return stringWidth(text, font, size) + char_space * max(len(text) - 1, 0)


def _fit_single_line(
    text: str, font: str, max_size: float, max_width: float, char_space: float = 0
) -> float:
    """Largest font size (<= max_size) at which ``text`` fits on one line."""
    width = _text_width(text, font, max_size, char_space)
    if width <= max_width:
        return max_size
    return max_size * max_width / width


def _fit_lines(
    text: str, font: str, max_size: float, min_size: float, max_width: float, max_lines: int
) -> tuple[float, list[str]]:
    """Wrap ``text``, shrinking the font until it fits in ``max_lines`` lines."""
    size = max_size
    while size >= min_size:
        lines = simpleSplit(text, font, size, max_width)
        if len(lines) <= max_lines and all(
            stringWidth(ln, font, size) <= max_width for ln in lines
        ):
            return size, lines
        size -= 1
    # Still too long at the minimum size: keep the first lines and truncate,
    # then shrink further if a single unbreakable word is wider than the box.
    all_lines = simpleSplit(text, font, min_size, max_width)
    lines = all_lines[:max_lines]
    if len(all_lines) > max_lines:
        lines[-1] = lines[-1].rstrip() + "…"
    widest = max(stringWidth(ln, font, min_size) for ln in lines)
    return min(min_size, min_size * max_width / widest), lines


def _format_date(value: date) -> str:
    return f"{value.day} {value.strftime('%B %Y')}"
