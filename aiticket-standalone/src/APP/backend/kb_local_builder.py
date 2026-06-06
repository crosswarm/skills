from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


DOCX_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
PPTX_NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
XLSX_NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
XLSX_RELS_NS = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
TEXT_EXTENSIONS = {".md", ".txt", ".csv", ".sql", ".html", ".xml"}
STOPWORDS = {
    "以及", "进行", "如果", "这个", "我们", "你们", "他们", "使用", "功能", "流程", "工作流",
    "the", "and", "for", "with", "from", "that", "this", "into", "are",
}
SKIP_NAMES = {"INDEX", "OUTPUT", ".git", ".claude", "__pycache__", "node_modules"}


@dataclass
class ParseResult:
    text: str
    parse_status: str = "parsed"
    parse_error: str = ""
    parser: str = "builtin"
    metadata_only: bool = False


class KBLocalBuilder:
    def __init__(self, project_root: Path, kb_root: Path | None = None, topic_file: Path | None = None) -> None:
        self.project_root = Path(project_root).resolve()
        self.kb_root = (kb_root or (self.project_root / "KB")).resolve()
        self.topic_file = (topic_file or (self.project_root / "APP" / "backend" / "data" / "topic.md")).resolve()
        self.manifest_path = self.kb_root / "INDEX" / "manifest.json"
        self.converted_root = self.kb_root / "OUTPUT" / "converted"
        self.index_root = self.kb_root / "INDEX" / "FILES"

    def build(self) -> dict[str, Any]:
        existing_manifest = self._load_existing_manifest()
        existing_by_rel = {
            item.get("source_rel_path", ""): item
            for item in existing_manifest.get("contents", {}).values()
            if item.get("source_rel_path")
        }
        next_counter = self._next_counter(existing_manifest)
        generated_at = datetime.now().replace(microsecond=0).isoformat()
        contents: dict[str, dict[str, Any]] = {}
        source_files = list(self._iter_source_files())

        if not source_files and existing_manifest.get("contents"):
            existing_count = len(existing_manifest.get("contents", {}))
            return {
                "generated_at": existing_manifest.get("generated_at", generated_at),
                "source_files": existing_manifest.get("source_files", existing_count),
                "converted_files": existing_manifest.get("converted_files", existing_count),
                "content_count": existing_count,
            }

        self.index_root.mkdir(parents=True, exist_ok=True)
        self.converted_root.mkdir(parents=True, exist_ok=True)

        for source_path in source_files:
            source_rel_path = source_path.relative_to(self.kb_root).as_posix()
            existing = existing_by_rel.get(source_rel_path, {})
            content_id = existing.get("content_id")
            if not content_id:
                content_id = f"CNT-{next_counter:04d}"
                next_counter += 1

            parsed = self._parse_file(source_path)
            converted_rel_path = Path("KB") / "OUTPUT" / "converted" / source_path.relative_to(self.kb_root).with_suffix(".md")
            converted_abs_path = self.project_root / converted_rel_path
            converted_abs_path.parent.mkdir(parents=True, exist_ok=True)
            converted_text = self._render_converted_markdown(source_rel_path, source_path.stem, parsed)
            converted_abs_path.write_text(converted_text, encoding="utf-8")

            top_category = source_path.relative_to(self.kb_root).parts[0] if len(source_path.relative_to(self.kb_root).parts) >= 2 else ""
            second_category = source_path.relative_to(self.kb_root).parts[1] if len(source_path.relative_to(self.kb_root).parts) >= 3 else ""
            summary = self._build_summary(parsed.text, parsed.parse_status, parsed.parse_error, source_rel_path)
            keywords = self._extract_keywords(" ".join([source_path.stem, parsed.text, source_rel_path]))
            l1_index_id = existing.get("l1_index_id") or self._make_topic_id("IDX-L1", top_category or "ROOT")
            l2_seed = "/".join(part for part in [top_category, second_category] if part) or top_category or "ROOT"
            l2_index_id = existing.get("l2_index_id") or self._make_topic_id("IDX-L2", l2_seed)
            index_id = existing.get("index_id") or f"IDX-F-{content_id}"
            l1_name = existing.get("l1_name") or top_category
            l2_name = existing.get("l2_name") or "/".join(part for part in [top_category, second_category] if part)

            item = {
                "content_id": content_id,
                "source_path": (Path("KB") / source_rel_path).as_posix(),
                "source_rel_path": source_rel_path,
                "ext": source_path.suffix.lower(),
                "name": source_path.stem,
                "top_category": top_category,
                "second_category": second_category,
                "converted_path": converted_rel_path.as_posix(),
                "text_chars": len(parsed.text),
                "summary": summary,
                "keywords": keywords,
                "index_id": index_id,
                "l1_index_id": l1_index_id,
                "l2_index_id": l2_index_id,
                "l1_name": l1_name,
                "l2_name": l2_name,
                "backlink_index_ids": ["IDX-L0-ROOT", l1_index_id, l2_index_id, index_id],
                "related_content_ids": [],
                "related_links": [],
                "parse_status": parsed.parse_status,
                "parse_error": parsed.parse_error,
                "parser": parsed.parser,
                "metadata_only": parsed.metadata_only,
            }
            contents[content_id] = item
            self._write_index_file(content_id, item)

        contents, next_counter = self._scan_orphans_in_converted(contents, next_counter)

        manifest = {
            "generated_at": generated_at,
            "source_files": len(source_files),
            "converted_files": len(source_files),
            "root_index_id": "IDX-L0-ROOT",
            "contents": contents,
        }
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "generated_at": generated_at,
            "source_files": len(source_files),
            "converted_files": len(source_files),
            "content_count": len(contents),
        }

    def _iter_source_files(self) -> list[Path]:
        files: list[Path] = []
        for path in sorted(self.kb_root.rglob("*")):
            if not path.is_file():
                continue
            rel_parts = path.relative_to(self.kb_root).parts
            if any(part in SKIP_NAMES for part in rel_parts):
                continue
            if path.name.startswith("._") or path.name in {".DS_Store"}:
                continue
            files.append(path)
        return files

    def _load_existing_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {}
        try:
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _next_counter(self, manifest: dict[str, Any]) -> int:
        counter = 1
        for content_id in manifest.get("contents", {}):
            match = re.match(r"^CNT-(\d+)$", content_id)
            if match:
                counter = max(counter, int(match.group(1)) + 1)
        return counter

    def _parse_file(self, path: Path) -> ParseResult:
        try:
            from kb_build_parsers import parse_file

            return parse_file(path)
        except ImportError:
            return self._parse_file_builtin(path)
        except Exception as exc:
            return ParseResult(
                text="",
                parse_status="degraded",
                parse_error=f"kb_build_parsers failed: {exc}",
                parser="kb-build-parsers",
                metadata_only=True,
            )

    def _parse_file_builtin(self, path: Path) -> ParseResult:
        ext = path.suffix.lower()
        try:
            if ext in TEXT_EXTENSIONS:
                return ParseResult(text=path.read_text(encoding="utf-8", errors="ignore"), parser="text")
            if ext == ".docx":
                return ParseResult(text=self._extract_docx_text(path), parser="docx")
            if ext == ".pptx":
                return ParseResult(text=self._extract_pptx_text(path), parser="pptx")
            if ext == ".xlsx":
                return ParseResult(text=self._extract_xlsx_text(path), parser="xlsx")
        except Exception as exc:
            return ParseResult(text="", parse_status="degraded", parse_error=str(exc), parser=f"builtin:{ext}", metadata_only=True)

        return ParseResult(
            text="",
            parse_status="degraded",
            parse_error=f"unsupported file type: {ext or '<noext>'}",
            parser="builtin",
            metadata_only=True,
        )

    def _render_converted_markdown(self, source_rel_path: str, name: str, parsed: ParseResult) -> str:
        if parsed.parse_status == "parsed" and parsed.text.strip():
            return parsed.text if parsed.text.startswith("#") else f"# {name}\n\n{parsed.text.strip()}\n"

        return (
            f"# {name}\n\n"
            f"- Source: `{source_rel_path}`\n"
            f"- Parse status: `{parsed.parse_status}`\n"
            f"- Parser: `{parsed.parser}`\n"
            f"- Metadata only: `{'yes' if parsed.metadata_only else 'no'}`\n"
            f"- Parse error: `{parsed.parse_error or 'unknown'}`\n"
        )

    def _write_index_file(self, content_id: str, item: dict[str, Any]) -> None:
        index_path = self.index_root / content_id / "INDEX.md"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(
            "\n".join(
                [
                    f"# {item['name']}",
                    "",
                    f"- Content ID: `{content_id}`",
                    f"- Source: `{item['source_rel_path']}`",
                    f"- Converted: `{item['converted_path']}`",
                    f"- Parse status: `{item['parse_status']}`",
                    f"- Summary: {item['summary']}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def _scan_orphans_in_converted(self, contents: dict[str, Any], next_counter: int) -> tuple[dict[str, Any], int]:
        """Register .md files in converted/ that have no manifest entry (orphans without source files)."""
        registered_converted = {
            item.get("converted_path", "")
            for item in contents.values()
            if item.get("converted_path")
        }

        if not self.converted_root.exists():
            return contents, next_counter

        for md_path in sorted(self.converted_root.rglob("*.md")):
            converted_path_str = (Path("KB") / md_path.relative_to(self.project_root / "KB")).as_posix()
            if converted_path_str in registered_converted:
                continue

            parts = md_path.relative_to(self.converted_root).parts
            top_category = parts[0] if parts else "unknown"
            second_category = parts[1] if len(parts) > 2 else ""
            name = md_path.stem
            source_rel_path = (Path("OUTPUT") / "converted" / md_path.relative_to(self.converted_root)).as_posix()

            text = md_path.read_text(encoding="utf-8", errors="ignore")
            summary = self._build_summary(text, "parsed", "", source_rel_path)
            keywords = self._extract_keywords(f"{name} {text}")

            content_id = f"CNT-{next_counter:04d}"
            next_counter += 1
            l1_index_id = self._make_topic_id("IDX-L1", top_category or "ROOT")
            l2_seed = "/".join(p for p in [top_category, second_category] if p) or top_category or "ROOT"
            l2_index_id = self._make_topic_id("IDX-L2", l2_seed)
            index_id = f"IDX-F-{content_id}"

            item = {
                "content_id": content_id,
                "source_path": converted_path_str,
                "source_rel_path": source_rel_path,
                "ext": ".md",
                "name": name,
                "top_category": top_category,
                "second_category": second_category,
                "converted_path": converted_path_str,
                "text_chars": len(text),
                "summary": summary,
                "keywords": keywords,
                "index_id": index_id,
                "l1_index_id": l1_index_id,
                "l2_index_id": l2_index_id,
                "l1_name": top_category,
                "l2_name": "/".join(p for p in [top_category, second_category] if p),
                "backlink_index_ids": ["IDX-L0-ROOT", l1_index_id, l2_index_id, index_id],
                "related_content_ids": [],
                "related_links": [],
                "parse_status": "parsed",
                "parse_error": "",
                "parser": "orphan-md",
                "metadata_only": False,
            }
            contents[content_id] = item
            self._write_index_file(content_id, item)
            registered_converted.add(converted_path_str)

        return contents, next_counter

    def _build_summary(self, text: str, parse_status: str, parse_error: str, source_rel_path: str) -> str:
        stripped = re.sub(r"\s+", " ", (text or "").strip())
        if stripped:
            return stripped[:160]
        if parse_error:
            return f"{source_rel_path} parse degraded: {parse_error}"
        return f"{source_rel_path} has no extracted text"

    def _extract_keywords(self, text: str, limit: int = 8) -> list[str]:
        counts: dict[str, int] = {}
        for token in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_-]{2,}", text or ""):
            token_lower = token.lower()
            if token_lower in STOPWORDS:
                continue
            counts[token] = counts.get(token, 0) + 1
        return [token for token, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]

    def _make_topic_id(self, prefix: str, value: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").upper() or "ROOT"
        return f"{prefix}-{slug}"

    def _extract_docx_text(self, path: Path) -> str:
        with zipfile.ZipFile(path) as zf:
            root = ET.fromstring(zf.read("word/document.xml"))
        return "\n".join(node.text for node in root.findall(".//w:t", DOCX_NS) if node.text)

    def _extract_pptx_text(self, path: Path) -> str:
        texts: list[str] = []
        with zipfile.ZipFile(path) as zf:
            for name in sorted(n for n in zf.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")):
                root = ET.fromstring(zf.read(name))
                texts.extend(node.text for node in root.findall(".//a:t", PPTX_NS) if node.text)
        return "\n".join(texts)

    def _extract_xlsx_text(self, path: Path) -> str:
        lines: list[str] = []
        with zipfile.ZipFile(path) as zf:
            shared = self._read_shared_strings(zf)
            workbook = ET.fromstring(zf.read("xl/workbook.xml"))
            rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            rel_map = {rel.attrib.get("Id"): rel.attrib.get("Target", "") for rel in rels.findall(".//r:Relationship", XLSX_RELS_NS)}
            rel_attr = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            for sheet in workbook.findall(".//s:sheet", XLSX_NS):
                rel_id = sheet.attrib.get(rel_attr)
                target = rel_map.get(rel_id, "")
                if not target:
                    continue
                root = ET.fromstring(zf.read(f"xl/{target}"))
                for row in root.findall(".//s:row", XLSX_NS):
                    values = [self._extract_cell_value(cell, shared) for cell in row.findall("./s:c", XLSX_NS)]
                    text = "\t".join(value for value in values if value).strip()
                    if text:
                        lines.append(text)
        return "\n".join(lines)

    def _read_shared_strings(self, zf: zipfile.ZipFile) -> list[str]:
        if "xl/sharedStrings.xml" not in zf.namelist():
            return []
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        return ["".join(node.itertext()).strip() for node in root.findall(".//s:si", XLSX_NS)]

    def _extract_cell_value(self, cell: ET.Element, shared: list[str]) -> str:
        value = cell.find("./s:v", XLSX_NS)
        if value is None or value.text is None:
            return ""
        if cell.attrib.get("t") == "s":
            index = int(value.text)
            return shared[index] if index < len(shared) else ""
        return value.text
