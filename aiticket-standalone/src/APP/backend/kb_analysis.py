"""
Knowledge Base Analysis Module
Scans KB directory, extracts knowledge from files, generates structured markdown
"""
import html
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[2]

# Supported file extensions
SUPPORTED_EXTENSIONS = ['.md', '.txt', '.docx', '.pdf']


def _shared_repo_root(repo_root: Path) -> Optional[Path]:
    marker = f"{os.sep}.worktrees{os.sep}"
    repo_root_str = str(repo_root)
    if marker not in repo_root_str:
        return None
    return Path(repo_root_str.split(marker, 1)[0])


def _is_populated_kb_dir(kb_dir: Path) -> bool:
    if (kb_dir / "INDEX" / "manifest.json").exists():
        return True
    if not kb_dir.exists():
        return False

    ignored_names = {"index.json", "__init__.py", ".DS_Store", "__pycache__"}
    for child in kb_dir.iterdir():
        if child.name in ignored_names:
            continue
        return True
    return False


def resolve_kb_dir(repo_root: Optional[Path] = None) -> Path:
    current_repo_root = Path(repo_root) if repo_root else REPO_ROOT
    candidates = [current_repo_root / "KB"]

    shared_root = _shared_repo_root(current_repo_root)
    if shared_root is not None:
        candidates.append(shared_root / "KB")

    for candidate in candidates:
        if _is_populated_kb_dir(candidate):
            return candidate

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return candidates[0]


KB_DIR = resolve_kb_dir()
KB_INDEX_FILE = KB_DIR / "index.json"


class KBAnalyzer:
    """Knowledge Base Analyzer - processes files in KB directory"""

    def __init__(self, llm_service=None, kb_dir: Optional[Path] = None):
        self.llm_service = llm_service
        self.kb_dir = Path(kb_dir) if kb_dir else KB_DIR
        self.kb_index_file = self.kb_dir / "index.json"
        self.pipeline_manifest_file = self.kb_dir / "INDEX" / "manifest.json"
        self._ensure_kb_dir()
    
    def _ensure_kb_dir(self):
        """Ensure KB directory exists"""
        self.kb_dir.mkdir(parents=True, exist_ok=True)
    
    def list_files(self) -> List[Dict]:
        """List all files in KB directory with metadata"""
        files = []
        if not self.kb_dir.exists():
            return files

        for filename in os.listdir(self.kb_dir):
            filepath = self.kb_dir / filename
            if filepath.is_file():
                ext = os.path.splitext(filename)[1].lower()
                if ext in SUPPORTED_EXTENSIONS or ext == '.json':
                    stat = filepath.stat()
                    files.append({
                        "filename": filename,
                        "extension": ext,
                        "size_bytes": stat.st_size,
                        "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "is_analyzed": self._check_if_analyzed(filename)
                    })
        
        # Sort by modified time descending
        files.sort(key=lambda x: x['modified_at'], reverse=True)
        return files
    
    def _check_if_analyzed(self, filename: str) -> bool:
        """Check if file has been analyzed (has corresponding _analysis.md)"""
        base_name = os.path.splitext(filename)[0]
        analysis_file = self.kb_dir / f"{base_name}_analysis.md"
        return analysis_file.exists()
    
    def get_file_content(self, filename: str) -> Optional[str]:
        """Read content of a KB file"""
        filepath = self.kb_dir / filename
        if not filepath.exists():
            return None

        ext = filepath.suffix.lower()

        try:
            if ext == '.md' or ext == '.txt':
                with open(filepath, 'r', encoding='utf-8') as f:
                    return f.read()
            elif ext == '.docx':
                return self._read_docx(filepath)
            elif ext == '.pdf':
                return self._read_pdf(filepath)
            elif ext in {'.pptx', '.xlsx', '.doc'}:
                converted_content = self._read_converted_content_for_source(filename)
                if converted_content:
                    return converted_content
                return f"Preview not available for {filename}"
            elif ext == '.json':
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.dumps(json.load(f), ensure_ascii=False, indent=2)
        except Exception as e:
            return f"Error reading file: {str(e)}"

        return None

    def _load_pipeline_manifest(self) -> Optional[Dict]:
        if not self.pipeline_manifest_file.exists():
            return None
        with open(self.pipeline_manifest_file, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _read_converted_content_for_source(self, source_rel_path: str) -> Optional[str]:
        manifest = self._load_pipeline_manifest()
        if not manifest:
            return None

        for item in manifest.get("contents", {}).values():
            if item.get("source_rel_path") != source_rel_path:
                continue
            converted_path = item.get("converted_path")
            if not converted_path:
                return None
            converted_file = self._resolve_manifest_path(converted_path)
            if converted_file.exists():
                return converted_file.read_text(encoding='utf-8')
        return None

    def _resolve_manifest_path(self, raw_path: str) -> Path:
        path = Path(raw_path)
        if not raw_path:
            return path
        if not path.is_absolute():
            if raw_path.startswith(("KB/", "APP/", "data/")):
                return self.kb_dir.parent / raw_path
            return self.kb_dir / raw_path
        normalized = raw_path.replace("\\", "/")
        rerooted_path: Optional[Path] = None
        for root_name in ("KB", "APP", "data"):
            marker = f"/{root_name}/"
            if marker in normalized:
                suffix = normalized.split(marker, 1)[1]
                rerooted_path = self.kb_dir.parent / root_name / suffix
                if rerooted_path.exists():
                    return rerooted_path
                break
        if path.exists():
            return path
        if rerooted_path is not None:
            return rerooted_path
        return path

    def _infer_source_kind(self, item: Dict) -> str:
        source_rel_path = item.get("source_rel_path", "")
        if source_rel_path.startswith("APP/"):
            return "app_docs"
        return "apcom_docs"

    def _build_topic_entries(self, contents: Dict[str, Dict]) -> List[Dict]:
        seen_topic_ids = set()
        topics = []

        for item in contents.values():
            topic_id = item.get("l2_index_id") or item.get("l1_index_id") or item.get("index_id")
            if not topic_id or topic_id in seen_topic_ids:
                continue
            seen_topic_ids.add(topic_id)
            topics.append(
                {
                    "topic_id": topic_id,
                    "name": item.get("l2_name") or item.get("l1_name") or item.get("name") or topic_id,
                }
            )

        return topics

    def _build_topic_names(self, item: Dict) -> List[str]:
        return [
            topic_name
            for topic_name in [
                item.get("l1_name"),
                item.get("l2_name"),
                item.get("name"),
            ]
            if topic_name
        ]

    def _build_content_url(self, content_id: str) -> str:
        return f"/api/kb/content/{content_id}"

    def _build_metadata_url(self, content_id: str) -> str:
        return f"/api/kb/metadata/{content_id}"

    def get_manifest(self) -> Dict:
        pipeline_manifest = self._load_pipeline_manifest()
        if pipeline_manifest:
            contents = pipeline_manifest.get("contents", {})
            sources: Dict[str, Dict[str, int]] = {}
            for item in contents.values():
                source_kind = self._infer_source_kind(item)
                if source_kind not in sources:
                    sources[source_kind] = {"count": 0}
                sources[source_kind]["count"] += 1

            return {
                **pipeline_manifest,
                "total_count": len(contents),
                "sources": sources,
                "topics": self._build_topic_entries(contents),
            }

        files = self.list_files()
        return {
            "generated_at": datetime.now().isoformat(),
            "total_count": len(files),
            "sources": {"legacy_files": {"count": len(files)}},
            "topics": [],
            "contents": {},
        }

    def _build_search_text(self, item: Dict) -> str:
        fields = [
            item.get("name", ""),
            item.get("summary", ""),
            " ".join(item.get("keywords", [])),
            item.get("source_rel_path", ""),
            item.get("l1_name", ""),
            item.get("l2_name", ""),
        ]
        converted_path = item.get("converted_path")
        if converted_path:
            converted_file = self._resolve_manifest_path(converted_path)
            if converted_file.exists():
                fields.append(converted_file.read_text(encoding='utf-8')[:4000])
        return " ".join(fields).lower()

    def _calculate_relevance(self, query: str, item: Dict, haystack: str) -> int:
        keyword = (query or "").strip().lower()
        if not keyword:
            return 1

        relevance = 0
        if keyword in haystack:
            relevance += haystack.count(keyword) * 10

        query_terms = re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9_-]{2,}", keyword)

        for field in [item.get("name", ""), item.get("summary", ""), item.get("source_rel_path", "")]:
            field_lower = field.lower()
            if field_lower and (field_lower in keyword or keyword in field_lower):
                relevance += 5
            field_terms = re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9_-]{2,}", field_lower)
            for term in field_terms:
                if term and term in keyword:
                    relevance += 4

        for token in item.get("keywords", []):
            token_lower = str(token).lower()
            if token_lower and token_lower in keyword:
                relevance += 8

        for term in query_terms:
            if term in haystack:
                relevance += 2

        return relevance

    def _format_manifest_result(self, item: Dict, relevance: int) -> Dict:
        content_id = item.get("content_id", "")
        return {
            "content_id": content_id,
            "name": item.get("name", ""),
            "citation_label": item.get("source_rel_path", ""),
            "source_rel_path": item.get("source_rel_path", ""),
            "summary": item.get("summary", ""),
            "keywords": item.get("keywords", []),
            "source_kind": self._infer_source_kind(item),
            "topic_names": self._build_topic_names(item),
            "content_url": self._build_content_url(content_id),
            "metadata_url": self._build_metadata_url(content_id),
            "topic_ids": [
                topic_id
                for topic_id in [
                    item.get("l1_index_id"),
                    item.get("l2_index_id"),
                    item.get("index_id"),
                ]
                if topic_id
            ],
            "relevance": relevance,
        }

    def get_content(self, content_id: str) -> Optional[Dict]:
        manifest = self._load_pipeline_manifest()
        if not manifest:
            return None

        item = manifest.get("contents", {}).get(content_id)
        if not item:
            return None

        raw_content = ""
        converted_path = item.get("converted_path")
        if converted_path:
            converted_file = self._resolve_manifest_path(converted_path)
            if converted_file.exists():
                raw_content = converted_file.read_text(encoding='utf-8')

        content_id = item.get("content_id", "")
        return {
            **item,
            "citation_label": item.get("source_rel_path", ""),
            "l1_module": item.get("l1_name", ""),
            "l2_module": item.get("l2_name", ""),
            "doc_type": item.get("ext", ""),
            "source_kind": self._infer_source_kind(item),
            "topic_names": self._build_topic_names(item),
            "content_url": self._build_content_url(content_id),
            "metadata_url": self._build_metadata_url(content_id),
            "topic_ids": [
                topic_id
                for topic_id in [
                    item.get("l1_index_id"),
                    item.get("l2_index_id"),
                    item.get("index_id"),
                ]
                if topic_id
            ],
            "raw_content": raw_content,
        }

    def get_metadata(self, content_id: str) -> Optional[Dict]:
        manifest = self._load_pipeline_manifest()
        if not manifest:
            return None

        item = manifest.get("contents", {}).get(content_id)
        if not item:
            return None

        return {
            **item,
            "topic_names": self._build_topic_names(item),
            "content_url": self._build_content_url(content_id),
            "metadata_url": self._build_metadata_url(content_id),
            "source_kind": self._infer_source_kind(item),
        }

    def _render_answer_html(self, answer_text: str) -> str:
        blocks = [block.strip() for block in answer_text.split("\n\n") if block.strip()]
        html_blocks = []
        for block in blocks:
            if block.startswith("## "):
                html_blocks.append(f"<h2>{html.escape(block[3:])}</h2>")
            elif block.startswith("- "):
                html_blocks.append(f"<p>{html.escape(block)}</p>")
            else:
                html_blocks.append(f"<p>{html.escape(block)}</p>")
        return "".join(html_blocks) if html_blocks else "<p></p>"

    def _truncate_text(self, text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "…"

    def _build_fallback_short_answer(self, query: str, results: List[Dict]) -> str:
        if not results:
            return "未检索到相关知识库内容"

        top_result = results[0]
        base_text = (
            f"知识库显示“{top_result.get('name', '相关资料')}”与“{query}”最相关。"
            f"建议先按文档中的关键步骤处理：{top_result.get('summary', '')}"
        )
        if len(results) > 1:
            base_text += f" 如需交叉验证，可继续参考 {results[1].get('name', '其他资料')}。"
        return self._truncate_text(base_text, 300)

    def _build_fallback_long_answer(self, query: str, results: List[Dict]) -> str:
        if not results:
            return "## 问题理解\n未检索到相关知识库内容。\n\n## 处理建议\n建议尝试更换关键词，或补充更明确的业务场景后再次搜索。"

        source_texts = []
        for result in results[:3]:
            detail = self.get_content(result["content_id"]) or {}
            raw_content = detail.get("raw_content", "")
            doc_name = result.get("name", "未命名文档")
            summary = result.get("summary", "")
            source_texts.append(
                f"资料《{doc_name}》\n摘要：{summary}\n{raw_content[:900]}"
            )

        combined_sources = "\n\n".join(source_texts)
        top_name = results[0].get("name", "知识库文档")
        top_summary = results[0].get("summary", "")

        answer = (
            f"## 问题理解\n"
            f"针对“{query}”，知识库命中了 {len(results[:3])} 篇相关文档，"
            f"最相关文档为《{top_name}》。\n\n"
            f"## 知识库结论\n"
            f"{top_summary if top_summary else '请参考下方引用文档获取详细信息。'}\n\n"
            f"## 处理建议\n"
            f"建议按照知识库文档中的步骤操作。如未解决，可联系相关支持团队。\n\n"
            f"## 参考资料\n"
            f"{combined_sources}"
        )
        return answer

    def answer_question(
        self,
        query: str,
        mode: str = "short",
        api_key: str = "",
        provider: str = "gemini",
        model_name: str = "",
        base_url: str = "",
    ) -> Dict:
        results = self.search_knowledge(query, top_k=5)

        if not results:
            empty_text = "未检索到相关知识库内容"
            return {
                "answer_text": empty_text,
                "answer_html": self._render_answer_html(empty_text),
                "used_llm": False,
                "fallback_used": True,
                "sources": [],
                "topics": [],
            }

        used_llm = bool(self.llm_service and api_key)
        fallback_used = not used_llm

        if used_llm:
            # 加载每篇匹配文档的真实正文
            context_parts = []
            for i, result in enumerate(results[:3], 1):
                detail = self.get_content(result["content_id"]) or {}
                raw = detail.get("raw_content", "").strip()
                doc_name = result.get("name", f"文档{i}")
                summary = result.get("summary", "")
                context_parts.append(
                    f"【文档{i}】{doc_name}\n"
                    f"摘要：{summary}\n"
                    f"正文：{raw[:2000] if raw else '（无正文）'}"
                )
            context_text = "\n\n---\n\n".join(context_parts)

            prompt = (
                f"你是企业软件产品知识库的智能问答助手。"
                f"请严格基于下方知识库文档内容回答用户问题，不得编造文档中未提到的信息。\n\n"
                f"用户问题：{query}\n\n"
                f"输出要求：{'simple模式，300字以内，直接给出核心结论' if mode == 'short' else 'detailed模式，500字以上，分节说明（## 问题理解 / ## 知识库结论 / ## 处理建议）'}\n\n"
                f"知识库文档内容：\n{context_text}"
            )
            answer_text = self.llm_service.call_llm(
                prompt=prompt,
                api_key=api_key,
                provider=provider,
                model_name=model_name,
                base_url=base_url,
            )
            if mode == "short":
                answer_text = self._truncate_text(answer_text.strip(), 300)
            else:
                answer_text = answer_text.strip()
        else:
            if mode == "short":
                answer_text = self._build_fallback_short_answer(query, results)
            else:
                answer_text = self._build_fallback_long_answer(query, results)

        topics = []
        for result in results[:3]:
            for topic_name in result.get("topic_names", []):
                if topic_name not in topics:
                    topics.append(topic_name)

        return {
            "answer_text": answer_text,
            "answer_html": self._render_answer_html(answer_text),
            "used_llm": used_llm,
            "fallback_used": fallback_used,
            "sources": results[:3],
            "topics": topics,
        }

    def sync(self) -> Dict:
        manifest = self.get_manifest()
        return {
            "status": "success",
            "message": "Knowledge base manifest refreshed",
            "total_count": manifest.get("total_count", 0),
            "sources": manifest.get("sources", {}),
        }
    
    def _read_docx(self, filepath: str) -> str:
        """Read content from DOCX file"""
        try:
            from docx import Document
            doc = Document(filepath)
            content = []
            for para in doc.paragraphs:
                if para.text.strip():
                    content.append(para.text)
            return "\n\n".join(content)
        except ImportError:
            return "Error: python-docx not installed"
        except Exception as e:
            return f"Error reading DOCX: {str(e)}"
    
    def _read_pdf(self, filepath: str) -> str:
        """Read content from PDF file"""
        try:
            import PyPDF2
            with open(filepath, 'rb') as f:
                reader = PyPDF2.PdfReader(f)
                content = []
                for page in reader.pages:
                    text = page.extract_text()
                    if text:
                        content.append(text)
                return "\n\n".join(content)
        except ImportError:
            return "Error: PyPDF2 not installed"
        except Exception as e:
            return f"Error reading PDF: {str(e)}"
    
    def analyze_file(self, filename: str, api_key: str = "", provider: str = "gemini",
                     model_name: str = "", base_url: str = "") -> Tuple[bool, str]:
        """
        Analyze a KB file and generate structured knowledge summary
        Returns (success, message)
        """
        if not self.llm_service:
            return False, "LLM service not configured"
        
        if not api_key:
            return False, "API key required for analysis"
        
        # Read source content
        content = self.get_file_content(filename)
        if not content:
            return False, f"Cannot read file: {filename}"
        
        # Truncate if too long
        if len(content) > 15000:
            content = content[:15000] + "\n\n[...内容已截断...]"
        
        # Build analysis prompt
        prompt = f"""请分析以下知识库文档，提取关键知识要点并生成结构化摘要。

## 源文件: {filename}

## 文档内容:
{content}

## 请按以下格式输出:

# 知识摘要: [主题名称]

## 概述
[一句话概述这篇文档的核心内容]

## 关键要点
1. [要点1]
2. [要点2]
3. [要点3...]

## 应用场景
- [场景1]
- [场景2]

## 相关标签
`标签1` `标签2` `标签3`

## 原文引用
> [如有重要的原文引用，在此列出]

---
*分析时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}*
*源文件: {filename}*
"""
        
        # Call LLM
        try:
            result = self.llm_service.call_llm(
                prompt=prompt,
                api_key=api_key,
                provider=provider,
                model_name=model_name,
                base_url=base_url
            )
            
            if result.startswith("Error"):
                return False, result
            
            # Save analysis result
            base_name = os.path.splitext(filename)[0]
            analysis_filename = f"{base_name}_analysis.md"
            analysis_path = self.kb_dir / analysis_filename

            with open(analysis_path, 'w', encoding='utf-8') as f:
                f.write(result)
            
            # Update index
            self._update_index(filename, analysis_filename)
            
            return True, f"Analysis saved to {analysis_filename}"
            
        except Exception as e:
            return False, f"Analysis failed: {str(e)}"
    
    def _update_index(self, source_file: str, analysis_file: str):
        """Update KB index with analysis record"""
        index = {}
        if self.kb_index_file.exists():
            try:
                with open(self.kb_index_file, 'r', encoding='utf-8') as f:
                    index = json.load(f)
            except:
                index = {}
        
        if 'files' not in index:
            index['files'] = {}
        
        index['files'][source_file] = {
            'analysis_file': analysis_file,
            'analyzed_at': datetime.now().isoformat()
        }
        index['last_updated'] = datetime.now().isoformat()
        
        with open(self.kb_index_file, 'w', encoding='utf-8') as f:
            json.dump(index, f, ensure_ascii=False, indent=2)

    def search_knowledge(self, query: str, top_k: int = 10) -> List[Dict]:
        """Search analyzed knowledge files for relevant content"""
        manifest = self._load_pipeline_manifest()
        if manifest:
            keyword = (query or "").strip().lower()
            results = []

            for item in manifest.get("contents", {}).values():
                haystack = self._build_search_text(item)
                relevance = self._calculate_relevance(keyword, item, haystack)
                if keyword and relevance <= 0:
                    continue
                results.append(self._format_manifest_result(item, relevance))

            results.sort(key=lambda item: item["relevance"], reverse=True)
            return results[:top_k]

        results = []

        for filename in os.listdir(self.kb_dir):
            if filename.endswith('_analysis.md'):
                filepath = self.kb_dir / filename
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        content = f.read().lower()
                        query_lower = query.lower()
                        
                        # Simple keyword matching (can be enhanced with embeddings)
                        if query_lower in content:
                            # Extract title
                            lines = content.split('\n')
                            title = filename
                            for line in lines:
                                if line.startswith('# '):
                                    title = line[2:].strip()
                                    break
                            
                            results.append(
                                {
                                    "content_id": filename,
                                    "name": title,
                                    "citation_label": filename,
                                    "source_rel_path": filename,
                                    "summary": title,
                                    "keywords": [query],
                                    "source_kind": "legacy_files",
                                    "topic_ids": [],
                                    "relevance": content.count(query_lower),
                                }
                            )
                except:
                    continue

        # Sort by relevance
        results.sort(key=lambda x: x['relevance'], reverse=True)
        return results[:top_k]


# Singleton instance
_kb_analyzer = None

def get_kb_analyzer(llm_service=None) -> KBAnalyzer:
    """Get or create KB analyzer singleton"""
    global _kb_analyzer
    if _kb_analyzer is None:
        _kb_analyzer = KBAnalyzer(llm_service)
    elif llm_service is not None:
        _kb_analyzer.llm_service = llm_service
    return _kb_analyzer
