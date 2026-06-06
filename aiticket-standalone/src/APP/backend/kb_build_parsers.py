from __future__ import annotations

import importlib
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET


DOCX_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
PPTX_NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
XLSX_NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
XLSX_RELS_NS = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}

TEXT_EXTENSIONS = {".md", ".txt", ".csv", ".sql", ".html", ".xml"}
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | {".docx", ".pptx", ".xlsx", ".pdf", ".doc", ".xls", ".png", ".mp4", ".vsdx"}


@dataclass
class ParseResult:
    text: str
    parse_status: str
    parse_error: str
    parser: str
    metadata_only: bool


class KBBuildParsers:
    def parse(self, path: Path | str) -> ParseResult:
        file_path = Path(path)
        ext = file_path.suffix.lower()

        try:
            if ext in TEXT_EXTENSIONS:
                return self._parse_plain_text(file_path)
            if ext == ".docx":
                return self._parse_docx(file_path)
            if ext == ".pptx":
                return self._parse_pptx(file_path)
            if ext == ".xlsx":
                return self._parse_xlsx(file_path)
            if ext == ".pdf":
                return self._parse_pdf(file_path)
            if ext in {".doc", ".xls"}:
                return self._parse_legacy_office(file_path)
            if ext == ".png":
                return self._parse_png(file_path)
            if ext == ".mp4":
                return self._parse_mp4(file_path)
            if ext == ".vsdx":
                return self._parse_vsdx(file_path)
            return self._degraded("unsupported extension", "unsupported")
        except Exception as exc:
            return self._degraded(f"unexpected parser error: {exc}", "parser-exception")

    def _parsed(self, text: str, parser: str) -> ParseResult:
        cleaned = text.strip()
        if cleaned:
            return ParseResult(
                text=cleaned,
                parse_status="parsed",
                parse_error="",
                parser=parser,
                metadata_only=False,
            )
        return self._degraded("empty extracted text", parser)

    def _degraded(self, reason: str, parser: str) -> ParseResult:
        return ParseResult(
            text="",
            parse_status="degraded",
            parse_error=reason,
            parser=parser,
            metadata_only=True,
        )

    def _parse_plain_text(self, path: Path) -> ParseResult:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="ignore")
        return self._parsed(text, "plain-text")

    def _parse_docx(self, path: Path) -> ParseResult:
        try:
            with zipfile.ZipFile(path) as zf:
                root = ET.fromstring(zf.read("word/document.xml"))
                texts = [node.text for node in root.findall(".//w:t", DOCX_NS) if node.text]
            return self._parsed("\n".join(texts), "docx-xml")
        except Exception as exc:
            return self._degraded(f"docx parse failed: {exc}", "docx-xml")

    def _parse_pptx(self, path: Path) -> ParseResult:
        texts: list[str] = []
        try:
            with zipfile.ZipFile(path) as zf:
                slide_names = sorted(
                    name
                    for name in zf.namelist()
                    if name.startswith("ppt/slides/slide") and name.endswith(".xml")
                )
                for name in slide_names:
                    root = ET.fromstring(zf.read(name))
                    texts.extend(node.text for node in root.findall(".//a:t", PPTX_NS) if node.text)
            return self._parsed("\n".join(texts), "pptx-xml")
        except Exception as exc:
            return self._degraded(f"pptx parse failed: {exc}", "pptx-xml")

    def _parse_xlsx(self, path: Path) -> ParseResult:
        try:
            with zipfile.ZipFile(path) as zf:
                shared = self._read_shared_strings(zf)
                workbook = ET.fromstring(zf.read("xl/workbook.xml"))
                rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
                rel_map = {
                    rel.attrib.get("Id"): rel.attrib.get("Target", "")
                    for rel in rels.findall(".//r:Relationship", XLSX_RELS_NS)
                }
                rel_attr = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
                lines: list[str] = []
                for sheet in workbook.findall(".//s:sheet", XLSX_NS):
                    rel_id = sheet.attrib.get(rel_attr)
                    target = rel_map.get(rel_id, "")
                    if not target:
                        continue
                    root = ET.fromstring(zf.read(f"xl/{target}"))
                    for row in root.findall(".//s:row", XLSX_NS):
                        row_values = [self._extract_cell_value(cell, shared) for cell in row.findall("./s:c", XLSX_NS)]
                        text = "\t".join(value for value in row_values if value).strip()
                        if text:
                            lines.append(text)
            return self._parsed("\n".join(lines), "xlsx-xml")
        except Exception as exc:
            return self._degraded(f"xlsx parse failed: {exc}", "xlsx-xml")

    def _read_shared_strings(self, zf: zipfile.ZipFile) -> list[str]:
        try:
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        except Exception:
            return []
        values: list[str] = []
        for si in root.findall(".//s:si", XLSX_NS):
            values.append("".join(node.text for node in si.findall(".//s:t", XLSX_NS) if node.text))
        return values

    def _extract_cell_value(self, cell: ET.Element, shared: list[str]) -> str:
        cell_type = cell.attrib.get("t", "")
        if cell_type == "s":
            value_node = cell.find("./s:v", XLSX_NS)
            if value_node is not None and value_node.text and value_node.text.isdigit():
                index = int(value_node.text)
                if 0 <= index < len(shared):
                    return shared[index]
            return ""
        if cell_type == "inlineStr":
            text_node = cell.find("./s:is/s:t", XLSX_NS)
            return text_node.text.strip() if text_node is not None and text_node.text else ""
        value_node = cell.find("./s:v", XLSX_NS)
        return value_node.text.strip() if value_node is not None and value_node.text else ""

    def _parse_pdf(self, path: Path) -> ParseResult:
        try:
            fitz = importlib.import_module("fitz")
        except Exception as exc:
            return self._degraded(f"fitz unavailable: {exc}", "pdf-pymupdf")

        try:
            doc = fitz.open(path)
            text = "\n".join(page.get_text("text") for page in doc)
            doc.close()
            if text.strip():
                return self._parsed(text, "pdf-pymupdf")
            return self._degraded("pymupdf extracted no text; OCR fallback not configured", "pdf-pymupdf")
        except Exception as exc:
            return self._degraded(f"pdf parse failed: {exc}", "pdf-pymupdf")

    def _parse_legacy_office(self, path: Path) -> ParseResult:
        office_bin = shutil.which("soffice") or shutil.which("libreoffice")
        if not office_bin:
            return self._degraded("soffice/libreoffice not found", "office-convert-placeholder")
        return self._degraded(
            f"{path.suffix.lower()} conversion placeholder pending (binary detected at {office_bin})",
            "office-convert-placeholder",
        )

    def _parse_png(self, path: Path) -> ParseResult:
        tesseract_bin = shutil.which("tesseract")
        if not tesseract_bin:
            return self._degraded("tesseract not found", "png-ocr")
        try:
            proc = subprocess.run(
                [tesseract_bin, str(path), "stdout"],
                check=False,
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                error = proc.stderr.strip() or f"tesseract exited with {proc.returncode}"
                return self._degraded(error, "png-ocr")
            return self._parsed(proc.stdout, "png-ocr")
        except Exception as exc:
            return self._degraded(f"tesseract execution failed: {exc}", "png-ocr")

    def _parse_mp4(self, path: Path) -> ParseResult:
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            return self._degraded("ffmpeg not found", "mp4-asr-placeholder")
        try:
            importlib.import_module("faster_whisper")
        except Exception:
            return self._degraded(
                f"ffmpeg detected at {ffmpeg_bin} but ASR backend is not available",
                "mp4-asr-placeholder",
            )
        return self._degraded("ASR pipeline placeholder not implemented", "mp4-asr-placeholder")

    def _parse_vsdx(self, path: Path) -> ParseResult:
        try:
            with zipfile.ZipFile(path) as zf:
                xml_names = [name for name in zf.namelist() if name.endswith(".xml")]
                chunks = [self._collect_xml_text(ET.fromstring(zf.read(name))) for name in xml_names]
            return self._parsed("\n".join(chunk for chunk in chunks if chunk.strip()), "vsdx-xml")
        except Exception as exc:
            return self._degraded(f"vsdx parse failed: {exc}", "vsdx-xml")

    def _collect_xml_text(self, root: ET.Element) -> str:
        fragments: list[str] = []
        for node in root.iter():
            text = (node.text or "").strip()
            if text:
                fragments.append(text)
        return "\n".join(fragments)


def parse_file(path: Path | str) -> ParseResult:
    return KBBuildParsers().parse(path)


def supported_extensions() -> Iterable[str]:
    return sorted(SUPPORTED_EXTENSIONS)
