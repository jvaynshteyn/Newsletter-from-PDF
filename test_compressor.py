"""PDF compression regression tests, including the real October newsletter."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / ".vendor"))
import pymupdf as fitz

from compressor import compress_pdf, _pack_soft_masks

SAMPLE = Path(r"C:\Users\Abimael\Desktop\PTA newsletter\October\PSIS 499 PTA Newsletter_ October.pdf")


def make_pdf(pages=1, encrypted=False):
    with fitz.open() as document:
        for number in range(pages):
            page = document.new_page(width=612, height=792)
            page.insert_text((60, 80), f"Newsletter page {number + 1}")
            page.draw_rect(fitz.Rect(50, 100, 300, 170), color=(0.8, 0.1, 0.1), width=2)
            page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(50, 60, 300, 85),
                              "uri": "https://example.com/news?month=october"})
        document.set_metadata({"title": "Newsletter", "author": "School PTA", "subject": "Updates"})
        if encrypted:
            return document.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256,
                                    owner_pw="owner-password", user_pw="reader-password")
        return document.tobytes()


def links(document):
    return [[(link["kind"], tuple(link["from"]), link.get("uri"),
              link.get("page"), tuple(link["to"]) if "to" in link else None)
             for link in page.get_links()] for page in document]


def graphics(document):
    return [[(path["items"], tuple(path["rect"]), path["type"], path["color"], path["fill"])
             for path in page.get_drawings()] for page in document]


class CompressionTests(unittest.TestCase):
    def test_tiny_pdf_never_grows(self):
        source = make_pdf()
        result = compress_pdf(source)
        self.assertLessEqual(result["compressedBytes"], len(source))
        self.assertEqual(result["compressedBytes"], len(result["pdf"]))
        self.assertFalse(result["targetReached"])
        self.assertTrue(result["warnings"])

    def test_text_vectors_links_and_metadata_remain(self):
        source = make_pdf()
        result = compress_pdf(source, 0.99)
        with fitz.open(stream=source, filetype="pdf") as original, fitz.open(stream=result["pdf"], filetype="pdf") as output:
            self.assertEqual([page.get_text() for page in original], [page.get_text() for page in output])
            self.assertEqual(links(original), links(output))
            self.assertEqual(graphics(original), graphics(output))
            for key in ("title", "author", "subject"):
                self.assertEqual(original.metadata[key], output.metadata[key])

    def test_forms_and_annotations_remain_interactive(self):
        with fitz.open(stream=make_pdf(), filetype="pdf") as document:
            page = document[0]
            widget = fitz.Widget()
            widget.field_name, widget.field_type = "Family", fitz.PDF_WIDGET_TYPE_TEXT
            widget.field_value, widget.rect = "PTA family", fitz.Rect(50, 220, 300, 250)
            page.add_widget(widget)
            page.add_text_annot((350, 220), "Review this newsletter.")
            source = document.tobytes()
        result = compress_pdf(source)
        with fitz.open(stream=result["pdf"], filetype="pdf") as output:
            fields = list(output[0].widgets())
            self.assertEqual([(w.field_name, w.field_value) for w in fields], [("Family", "PTA family")])
            self.assertEqual(next(output[0].annots()).info["content"], "Review this newsletter.")

    def test_impossible_target_is_reported_honestly(self):
        result = compress_pdf(make_pdf(), 0.001)
        self.assertFalse(result["targetReached"])
        self.assertGreater(result["ratio"], result["targetRatio"])
        self.assertTrue(any("could not be reached" in warning for warning in result["warnings"]))

    def test_source_is_returned_if_optimization_would_grow_it(self):
        source = make_pdf()
        with patch("compressor._write_pdf", return_value=source + b"larger"):
            result = compress_pdf(source)
        self.assertEqual(result["pdf"], source)
        self.assertEqual(result["ratio"], 1)
        self.assertTrue(any("original file was kept" in warning for warning in result["warnings"]))

    def test_non_pdf_and_damaged_pdf_are_rejected(self):
        for value in (b"", b"hello", b"<html>not a pdf</html>", "%PDF-1.7", b"%PDF-1.7\ninvalid"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                compress_pdf(value)

    def test_invalid_targets_are_rejected(self):
        for target in (0, -0.2, 1, 2, True, "0.18", None, float("nan"), float("inf")):
            with self.subTest(target=target), self.assertRaises(ValueError):
                compress_pdf(make_pdf(), target)

    def test_password_protected_pdf_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "password protected"):
            compress_pdf(make_pdf(encrypted=True))

    def test_page_limit(self):
        with self.assertRaisesRegex(ValueError, "1 to 50"):
            compress_pdf(make_pdf(pages=51))

    def test_opacity_repacking_handles_odd_width_and_row_padding(self):
        width, height = 129, 50
        opacity = bytes((x * 17) % 256 for y in range(height) for x in range(width))
        color = bytes([190, 60, 40]) * width * height
        with fitz.open() as document:
            page = document.new_page()
            mask = fitz.Pixmap(fitz.csGRAY, width, height, opacity, False)
            image = fitz.Pixmap(fitz.csRGB, width, height, color, False)
            page.insert_image(fitz.Rect(10, 10, 400, 200), stream=image.tobytes("png"), mask=mask.tobytes("png"))
            xref = page.get_images()[0][1]
            before = fitz.Pixmap(document, xref).samples
            changed = _pack_soft_masks(document, {})
            self.assertEqual(changed, 1)
            after = fitz.Pixmap(document, xref).samples
            self.assertEqual(len(before), len(after))
            self.assertLessEqual(max(abs(a - b) for a, b in zip(before, after)), 8)
            self.assertEqual(document.xref_get_key(xref, "BitsPerComponent")[1], "4")


@unittest.skipUnless(SAMPLE.is_file(), "The October newsletter sample is not installed.")
class OctoberNewsletterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SAMPLE.read_bytes()
        cls.result = compress_pdf(cls.source)

    def test_real_sample_reaches_about_18_percent(self):
        result = self.result
        self.assertTrue(result["targetReached"])
        self.assertLessEqual(result["ratio"], 0.18)
        self.assertGreaterEqual(result["ratio"], 0.17)
        self.assertEqual(result["originalBytes"], 6776488)
        self.assertEqual(result["compressedBytes"], len(result["pdf"]))
        self.assertEqual(result["pageCount"], 3)
        self.assertEqual(result["warnings"], [])

    def test_all_six_clickable_links_have_identical_targets_and_regions(self):
        with fitz.open(stream=self.source, filetype="pdf") as original, fitz.open(stream=self.result["pdf"], filetype="pdf") as output:
            self.assertEqual(links(original), links(output))
            self.assertEqual(self.result["linkCount"], 6)
            self.assertEqual(len({link["uri"] for page in output for link in page.get_links()}), 3)

    def test_selectable_text_words_and_positions_are_preserved(self):
        with fitz.open(stream=self.source, filetype="pdf") as original, fitz.open(stream=self.result["pdf"], filetype="pdf") as output:
            for source_page, output_page in zip(original, output):
                self.assertEqual(" ".join(source_page.get_text().split()), " ".join(output_page.get_text().split()))
                self.assertEqual(source_page.get_text("words"), output_page.get_text("words"))

    def test_vector_geometry_page_sizes_fonts_and_metadata_are_preserved(self):
        with fitz.open(stream=self.source, filetype="pdf") as original, fitz.open(stream=self.result["pdf"], filetype="pdf") as output:
            self.assertEqual(graphics(original), graphics(output))
            for source_page, output_page in zip(original, output):
                self.assertEqual(source_page.rect, output_page.rect)
                self.assertEqual([font[1:] for font in source_page.get_fonts()], [font[1:] for font in output_page.get_fonts()])
                self.assertTrue(output_page.get_pixmap().samples)
            for key in ("title", "author", "subject", "keywords", "creator", "producer", "creationDate", "modDate"):
                self.assertEqual(original.metadata[key], output.metadata[key])

    def test_both_original_qr_codes_still_decode(self):
        try:
            import zxingcpp
        except ImportError:
            self.skipTest("Optional QR decoder is not installed.")
        if not hasattr(zxingcpp, "read_barcodes"):
            self.skipTest("Optional QR decoder is not available for this Python version.")
        def decoded(data):
            targets = []
            with fitz.open(stream=data, filetype="pdf") as document:
                for page in document:
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), colorspace=fitz.csGRAY, alpha=False)
                    view = memoryview(pix.samples).cast("B", shape=(pix.height, pix.width))
                    targets.extend(code.text for code in zxingcpp.read_barcodes(view))
            return sorted(targets)
        original_codes = decoded(self.source)
        self.assertGreaterEqual(len(original_codes), 2)
        self.assertEqual(original_codes, decoded(self.result["pdf"]))


if __name__ == "__main__":
    unittest.main()
