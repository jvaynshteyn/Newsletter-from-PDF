"""Run with: python -m unittest test_converter -v (PyMuPDF required)."""
import base64
import html
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / ".vendor"))
import pymupdf as fitz

import converter
from converter import convert_pdf, safe_url, _reading_order


class Anchors(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.targets = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.targets.append(dict(attrs).get("href"))


class VisibleText(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.parts=[]
        self.feed(markup)

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in ("br","p","div","h1","h2","h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("p","div","h1","h2","h3"):
            self.parts.append("\n")

    @property
    def text(self):
        return "".join(self.parts)


class Images(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.images, self.anchor = [], None
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self.anchor = attrs.get("href")
        if tag == "img":
            self.images.append({**attrs, "href":self.anchor})

    def handle_endtag(self, tag):
        if tag == "a":
            self.anchor = None


def pdf_bytes(configure=None):
    with fitz.open() as doc:
        page = doc.new_page(width=612, height=792)
        if configure:
            configure(page)
        return doc.tobytes()


class ConverterTests(unittest.TestCase):
    def test_link_only_wraps_annotated_word(self):
        url = "https://example.org/news?a=1&b=2"
        def build(page):
            page.insert_text((50,70), "Read this newsletter and click here today.", fontsize=12)
            page.insert_link({"kind":fitz.LINK_URI, "from":page.search_for("click here")[0], "uri":url})
        result = convert_pdf(pdf_bytes(build), False)
        self.assertIn(url, Anchors(result["html"]).targets)
        self.assertIn("click here", result["plainText"])
        self.assertEqual(result["stats"]["annotationLinkCount"], 1)
        self.assertIn('href="'+html.escape(url,quote=True)+'"', result["html"])
        self.assertNotIn('<a href="'+html.escape(url,quote=True)+'" style="color:#125fa2;text-decoration:underline;">Read', result["html"])

    def test_nontext_link_gets_supplemental_anchor(self):
        url = "https://example.org/qr-target"
        def build(page):
            page.insert_text((50,70), "Newsletter with a link over a drawing, not selectable words.")
            page.draw_rect(fitz.Rect(50,150,140,240), color=(0,0,0))
            page.insert_link({"kind":fitz.LINK_URI,"from":fitz.Rect(50,150,140,240),"uri":url})
        result = convert_pdf(pdf_bytes(build), False)
        self.assertIn("Links from page 1", result["html"])
        self.assertIn(url, Anchors(result["html"]).targets)

    def test_one_anchor_for_multiword_phrase_with_mixed_styles(self):
        url = "https://example.org/mixed"
        def build(page):
            x=50
            for word,font in [("Click ","hebo"),("this ","heit"),("complete phrase","helv")]:
                page.insert_text((x,70),word,fontname=font,fontsize=12)
                x+=fitz.Font(fontname=font).text_length(word,fontsize=12)
            page.insert_link({"kind":fitz.LINK_URI,"from":fitz.Rect(49,54,x+1,74),"uri":url})
        result=convert_pdf(pdf_bytes(build),False)
        self.assertEqual(Anchors(result["html"]).targets.count(url),1)
        self.assertIn("<strong>Click </strong>",result["html"])
        self.assertIn("<em>this </em>",result["html"])
        self.assertIn("Click this complete phrase",VisibleText(result["html"]).text)

    def test_wrapped_link_is_one_anchor_with_word_spaces(self):
        url="https://example.org/wrapped"
        text="Read the complete newsletter for the school community. Find the latest announcements here."
        def build(page):
            page.insert_textbox(fitz.Rect(50,50,250,180),text,fontsize=12)
            page.insert_link({"kind":fitz.LINK_URI,"from":fitz.Rect(49,49,251,181),"uri":url})
        result=convert_pdf(pdf_bytes(build),False)
        self.assertEqual(Anchors(result["html"]).targets.count(url),1)
        self.assertIn(text,VisibleText(result["html"]).text)

    def test_unsafe_urls_never_become_html(self):
        for value in ["javascript:alert(1)","data:text/html,<script>","file:///C:/secret", "https://example.org/\nheader", "https://\\evil", "vbscript:1", "relative/page", "http://"]:
            self.assertIsNone(safe_url(value), value)
        for value in ["https://example.org/", "http://example.org", "mailto:office@example.org", "tel:+12125551234"]:
            self.assertEqual(safe_url(value), value)

    def test_html_text_escaped(self):
        data = pdf_bytes(lambda page:page.insert_text((50,70), '<script>alert("hello")</script> & newsletter', fontsize=12))
        result = convert_pdf(data, False)
        self.assertNotIn("<script>", result["html"])
        self.assertIn("&lt;script&gt;", result["html"])
        self.assertIn("&amp;", result["html"])

    def test_overprint_removed_but_repeated_real_text_remains(self):
        def build(page):
            page.insert_text((50,70), "PTA newsletter", fontsize=12)
            page.insert_text((50.5,70.5), "PTA newsletter", fontsize=12)
            page.insert_text((50,150), "PTA newsletter", fontsize=12)
        result = convert_pdf(pdf_bytes(build), False)
        self.assertEqual(result["plainText"].count("PTA newsletter"), 2)
        self.assertEqual(result["stats"]["duplicatesRemoved"], 1)

    def test_blank_page_falls_back_to_editable_image(self):
        result = convert_pdf(pdf_bytes())
        self.assertEqual(result["pageCount"], 1)
        self.assertEqual(result["imageCount"], 1)
        self.assertIn("data:image/jpeg;base64,", result["html"])
        self.assertTrue(any("no selectable text" in w for w in result["warnings"]))
        no_images = convert_pdf(pdf_bytes(), False)
        self.assertEqual(no_images["imageCount"], 0)
        self.assertIn("image-only content", no_images["html"])

    def test_column_order_keeps_article_together(self):
        items = [{"name":name,"rect":fitz.Rect(*rect)} for name,rect in [("left 1",(20,40,200,60)),("right 1",(330,40,550,60)),("left 2",(20,65,200,85)),("right 2",(330,65,550,85))]]
        self.assertEqual([i["name"] for i in _reading_order(items)], ["left 1","left 2","right 1","right 2"])

    def test_rejects_non_pdf(self):
        with self.assertRaises(ValueError):
            convert_pdf(b"not a PDF")
        with self.assertRaises(ValueError):
            convert_pdf(b"")

    def test_faithful_layout_covers_every_pixel_with_real_linked_images(self):
        url = "https://example.org/open?a=1&b=2"
        other = "mailto:pta@example.org"
        def build(page):
            page.draw_rect(page.rect, fill=(.9,.8,.2), color=None)
            page.draw_rect(fitz.Rect(0,0,612,140), fill=(.05,.1,.2), color=None)
            page.insert_text((30,80),"A COMPLETE NEWSLETTER HEADER",fontsize=23,color=(1,1,1))
            page.insert_text((40,200),"Read our school news and announcements.",fontsize=15)
            page.insert_link({"kind":fitz.LINK_URI,"from":fitz.Rect(40,180,350,210),"uri":url})
            page.insert_link({"kind":fitz.LINK_URI,"from":fitz.Rect(80,190,200,230),"uri":other})
            page.insert_text((40,300),"https://example.org/printed",fontsize=14)
        data = pdf_bytes(build)
        result = convert_pdf(data, layout="faithful")
        self.assertEqual(result["layout"], "faithful")
        self.assertIn('data-pdf-page="1"', result["html"])
        self.assertNotIn("position:absolute", result["html"])
        self.assertNotIn("usemap=", result["html"])
        images = Images(result["html"]).images
        self.assertTrue({url,other,"https://example.org/printed"}.issubset({image["href"] for image in images}))
        page = result["pages"][0]
        tiles = page["tiles"]
        width, height = page["pixelWidth"],page["pixelHeight"]
        self.assertEqual(len(tiles),len(images))
        self.assertEqual(sum(tile["width"]*tile["height"] for tile in tiles),width*height)
        # Every horizontal stripe starts at 0, finishes at the page edge, and
        # meets the next stripe. This catches blank pixels, overlaps and seams.
        rows = {}
        for tile in tiles:
            rows.setdefault((tile["y"],tile["height"]),[]).append(tile)
        edge = 0
        for (top,row_height),cells in sorted(rows.items()):
            self.assertEqual(top,edge)
            right = 0
            for cell in sorted(cells,key=lambda cell:cell["x"]):
                self.assertEqual(cell["x"],right)
                right += cell["width"]
            self.assertEqual(right,width)
            edge += row_height
        self.assertEqual(edge,height)
        # Decode actual delivered JPEGs, reassemble, and compare against the
        # original full page. Geometry alone would miss rendering the wrong crop.
        assembled = fitz.Pixmap(fitz.csRGB,(0,0,width,height),False)
        assembled.clear_with(255)
        for tile,image in zip(tiles,images):
            decoded = fitz.Pixmap(base64.b64decode(image["src"].split(",",1)[1]))
            self.assertEqual((decoded.width,decoded.height),(tile["width"],tile["height"]))
            decoded.set_origin(tile["x"],tile["y"])
            assembled.copy(decoded,decoded.irect)
        with fitz.open(stream=data,filetype="pdf") as doc:
            expected = doc[0].get_pixmap(matrix=fitz.Matrix(1200/612,1200/612),alpha=False,colorspace=fitz.csRGB)
        mean_error = sum(abs(a-b) for a,b in zip(assembled.samples,expected.samples))/len(expected.samples)
        self.assertLess(mean_error,3,"The delivered tiles must reproduce the complete source page")
        self.assertIn("A COMPLETE NEWSLETTER HEADER",images[0]["alt"])
        self.assertIn("A COMPLETE NEWSLETTER HEADER",result["plainText"])

    def test_editable_layout_preserves_small_logo_and_complete_masthead(self):
        def build(page):
            background = fitz.Pixmap(fitz.csRGB,(0,0,612,120),False)
            background.clear_with(0)
            page.insert_image(fitz.Rect(0,0,612,120),stream=background.tobytes("png"))
            logo = fitz.Pixmap(fitz.csRGB,(0,0,45,45),False)
            logo.set_rect(logo.irect,(235,20,35))
            page.insert_image(fitz.Rect(15,40,60,85),stream=logo.tobytes("png"))
            page.insert_text((75,75),"OUR PTA NEWSLETTER",fontsize=28,color=(1,1,1))
            page.insert_text((30,180),"Editable body text below the artwork.",fontsize=13)
        result = convert_pdf(pdf_bytes(build))
        images = Images(result["html"]).images
        header = next(image for image in images if "OUR PTA NEWSLETTER" in image.get("alt",""))
        decoded = fitz.Pixmap(base64.b64decode(header["src"].split(",",1)[1]))
        # The red logo is intentionally below the old image-size cutoff. Its
        # center must remain in the combined masthead alongside the full title.
        scale = decoded.width/612
        red,green,blue = decoded.pixel(round(36*scale),round(60*scale))
        self.assertGreater(red,180)
        self.assertLess(green,70)
        self.assertLess(blue,90)
        self.assertNotIn("OUR PTA NEWSLETTER",VisibleText(result["html"]).text)
        self.assertIn("Editable body text below the artwork.",VisibleText(result["html"]).text)

    def test_faithful_requires_images_and_rejects_unknown_layout(self):
        result = convert_pdf(pdf_bytes(),False,layout="faithful")
        self.assertEqual(result["layout"],"editable")
        self.assertTrue(any("requires images" in warning for warning in result["warnings"]))
        with self.assertRaises(ValueError):
            convert_pdf(pdf_bytes(),layout="arbitrary")

    def test_sample_faithful_retains_all_pages_links_and_header_without_reflow(self):
        sample = Path(r"C:\Users\Abimael\Desktop\PTA newsletter\October\PSIS 499 PTA Newsletter_ October.pdf")
        if not sample.exists():
            self.skipTest("User sample is not available on this computer")
        result = convert_pdf(sample.read_bytes(),layout="faithful")
        images = Images(result["html"]).images
        targets = set(Anchors(result["html"]).targets)
        self.assertTrue({link["url"] for link in result["links"]}.issubset(targets))
        self.assertEqual(result["html"].count('data-pdf-page="'),3)
        self.assertEqual(result["imageCount"],len(images))
        self.assertLess(result["stats"]["imageBytes"],len(sample.read_bytes())//3)
        for page in result["pages"]:
            # The complete header is one full-width tile, so 'NEWSLETTER' can
            # never wrap and the small source logos cannot be discarded.
            first = page["tiles"][0]
            self.assertEqual((first["x"],first["y"],first["width"]),(0,0,page["pixelWidth"]))
            self.assertGreater(first["height"],round(330*page["pixelWidth"]/page["width"]))
        self.assertTrue(any("THE QUEENS COLLEGE SCHOOL" in image["alt"] for image in images))

    def test_sample_retains_every_pdf_link_and_raster_calendar(self):
        sample = Path(r"C:\Users\Abimael\Desktop\PTA newsletter\October\PSIS 499 PTA Newsletter_ October.pdf")
        if not sample.exists():
            self.skipTest("User sample is not available on this computer")
        data = sample.read_bytes()
        result = convert_pdf(data)
        with fitz.open(stream=data,filetype="pdf") as doc:
            source_urls = {link["uri"] for page in doc for link in page.get_links() if safe_url(link.get("uri"))}
        self.assertEqual(result["pageCount"], 3)
        self.assertEqual(result["stats"]["annotationLinkCount"], 3)
        self.assertTrue(source_urls.issubset(set(Anchors(result["html"]).targets)))
        self.assertGreaterEqual(result["imageCount"], 3)
        self.assertGreater(result["stats"]["duplicatesRemoved"], 50)
        self.assertLess(result["stats"]["htmlBytes"], len(data)//2)
        self.assertEqual(result["plainText"].count("Welcome to the new PTA newsletter!"),1)
        visible=VisibleText(result["html"]).text
        self.assertIn("Meet the PTA and bring the whole family for Monster House & Casper!",visible)
        self.assertIn("already a PTA member! Dues are optional, but a suggested",visible)
        self.assertNotIn("8:00PMPS",visible)
        self.assertNotIn("fundfield",visible)
        # Raster-only calendar survives as an image near the KEY DATES heading.
        position = result["html"].index(">KEY DATES<")
        self.assertIn("data:image/jpeg;base64,",result["html"][position:])
        if converter.zxingcpp is not None:
            qr_urls = {link["url"] for link in result["links"] if link["source"]=="qr"}
            self.assertIn("https://499pta.org/", qr_urls)
            self.assertTrue(any("docs.google.com/document/" in url for url in qr_urls))
            self.assertTrue(qr_urls.issubset(set(Anchors(result["html"]).targets)))


if __name__ == "__main__":
    unittest.main()
