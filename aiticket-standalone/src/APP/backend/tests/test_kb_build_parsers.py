import os
import sys
import zipfile
from pathlib import Path


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _write_docx(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "word/document.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>"
                "</w:document>"
            ),
        )


def _write_pptx(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "ppt/slides/slide1.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                f"<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld>"
                "</p:sld>"
            ),
        )


def _write_xlsx(path: Path, shared_value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "xl/workbook.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>'
                "</workbook>"
            ),
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                "</Relationships>"
            ),
        )
        zf.writestr(
            "xl/sharedStrings.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f"<si><t>{shared_value}</t></si>"
                "</sst>"
            ),
        )
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row></sheetData>'
                "</worksheet>"
            ),
        )


def _write_vsdx(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "visio/pages/page1.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<PageContents>"
                f"<Text>{text}</Text>"
                "</PageContents>"
            ),
        )


def test_plain_text_file_parses(tmp_path: Path):
    from kb_build_parsers import KBBuildParsers

    parser = KBBuildParsers()
    path = tmp_path / "kb.md"
    path.write_text("流程监控支持未来审批人查询", encoding="utf-8")

    result = parser.parse(path)

    assert result.parse_status == "parsed"
    assert result.metadata_only is False
    assert "未来审批人" in result.text


def test_docx_pptx_xlsx_parse_from_zip_xml(tmp_path: Path):
    from kb_build_parsers import KBBuildParsers

    parser = KBBuildParsers()
    docx = tmp_path / "a.docx"
    pptx = tmp_path / "b.pptx"
    xlsx = tmp_path / "c.xlsx"
    _write_docx(docx, "文档正文")
    _write_pptx(pptx, "幻灯片正文")
    _write_xlsx(xlsx, "表格正文")

    docx_result = parser.parse(docx)
    pptx_result = parser.parse(pptx)
    xlsx_result = parser.parse(xlsx)

    assert "文档正文" in docx_result.text
    assert "幻灯片正文" in pptx_result.text
    assert "表格正文" in xlsx_result.text
    assert docx_result.parse_status == "parsed"
    assert pptx_result.parse_status == "parsed"
    assert xlsx_result.parse_status == "parsed"


def test_pdf_without_pymupdf_returns_degraded(tmp_path: Path, monkeypatch):
    import kb_build_parsers
    from kb_build_parsers import KBBuildParsers

    parser = KBBuildParsers()
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    def _raise_import(name):
        if name == "fitz":
            raise ImportError("missing fitz")
        return __import__(name)

    monkeypatch.setattr(kb_build_parsers.importlib, "import_module", _raise_import)

    result = parser.parse(pdf)

    assert result.parse_status == "degraded"
    assert result.metadata_only is True
    assert "fitz" in (result.parse_error or "")


def test_doc_and_xls_without_office_returns_degraded(tmp_path: Path, monkeypatch):
    import kb_build_parsers
    from kb_build_parsers import KBBuildParsers

    parser = KBBuildParsers()
    doc = tmp_path / "a.doc"
    xls = tmp_path / "b.xls"
    doc.write_bytes(b"doc-binary")
    xls.write_bytes(b"xls-binary")
    monkeypatch.setattr(kb_build_parsers.shutil, "which", lambda _: None)

    doc_result = parser.parse(doc)
    xls_result = parser.parse(xls)

    assert doc_result.parse_status == "degraded"
    assert xls_result.parse_status == "degraded"
    assert doc_result.metadata_only is True
    assert xls_result.metadata_only is True


def test_png_without_tesseract_returns_degraded(tmp_path: Path, monkeypatch):
    import kb_build_parsers
    from kb_build_parsers import KBBuildParsers

    parser = KBBuildParsers()
    png = tmp_path / "a.png"
    png.write_bytes(b"fake-png")
    monkeypatch.setattr(kb_build_parsers.shutil, "which", lambda _: None)

    result = parser.parse(png)

    assert result.parse_status == "degraded"
    assert result.metadata_only is True
    assert "tesseract" in (result.parse_error or "")


def test_mp4_without_asr_returns_degraded_even_when_ffmpeg_exists(tmp_path: Path, monkeypatch):
    import kb_build_parsers
    from kb_build_parsers import KBBuildParsers

    parser = KBBuildParsers()
    mp4 = tmp_path / "a.mp4"
    mp4.write_bytes(b"fake-mp4")

    def _which(name: str):
        if name == "ffmpeg":
            return "/usr/bin/ffmpeg"
        return None

    monkeypatch.setattr(kb_build_parsers.shutil, "which", _which)

    result = parser.parse(mp4)

    assert result.parse_status == "degraded"
    assert result.metadata_only is True
    assert "ASR" in (result.parse_error or "")


def test_vsdx_tries_zip_xml_and_parses_text(tmp_path: Path):
    from kb_build_parsers import KBBuildParsers

    parser = KBBuildParsers()
    vsdx = tmp_path / "a.vsdx"
    _write_vsdx(vsdx, "流程图节点文本")

    result = parser.parse(vsdx)

    assert result.parse_status == "parsed"
    assert "流程图节点文本" in result.text
