try:
    from google import genai as _genai_module
except ImportError:
    _genai_module = None
from openai import OpenAI
import base64
import concurrent.futures as _cf
import os
import re
import time
from typing import List, Dict, Generator

# Retry configuration
MAX_RETRIES = 3
INITIAL_DELAY = 1.0  # seconds
MAX_DELAY = 10.0  # seconds
DELAY_MULTIPLIER = 2.0
OPENAI_REQUEST_TIMEOUT = 30.0

class LLMService:
    def __init__(self):
        pass
    
    def _retry_with_backoff(self, operation, max_retries: int = MAX_RETRIES) -> Generator:
        """
        Execute an operation with exponential backoff retry.
        Yields results from the operation, retrying on transient errors.
        """
        last_error = None
        delay = INITIAL_DELAY

        for attempt in range(max_retries):
            try:
                yield from operation()
                return  # Success, exit retry loop
            except Exception as e:
                last_error = e
                error_str = str(e).lower()

                # Determine if error is retryable
                # NOTE: 'overloaded'/'529'/'resource exhausted' are NOT retried —
                # retrying a saturated upstream just wastes 45s per attempt and
                # blocks FastAPI thread pool threads, eventually freezing the service.
                retryable_errors = [
                    'rate limit', 'quota exceeded', 'timeout', 'connection',
                    'internal error', '503', '502', '429',
                    'temporarily unavailable'
                ]
                # Fail-fast on upstream saturation — do NOT retry
                fatal_errors = ['overloaded', 'resource exhausted', '529']
                if any(fe in error_str for fe in fatal_errors):
                    raise e
                is_retryable = any(err in error_str for err in retryable_errors)

                if not is_retryable or attempt == max_retries - 1:
                    # Not retryable or last attempt, raise
                    raise e

                # Wait before retry with exponential backoff
                print(f"[LLM Retry] Attempt {attempt + 1} failed: {e}. Retrying in {delay:.1f}s...")
                time.sleep(delay)
                delay = min(delay * DELAY_MULTIPLIER, MAX_DELAY)

        # Should not reach here, but just in case
        if last_error:
            raise last_error

    def _build_context_text(self, context_docs: List[Dict]) -> str:
        """
        Build context text from document list.

        Args:
            context_docs: List of document dictionaries with score, key, display_summary, full_text

        Returns:
            Formatted context string
        """
        context_text = ""
        for i, doc in enumerate(context_docs):
            context_text += f"{i+1}. [{doc.get('score', 0):.2f}] {doc.get('key')} - {doc.get('display_summary')}\n   Solution: {doc.get('full_text')[:500]}...\n\n"
        return context_text

    def _build_system_instruction(self, context_text: str) -> str:
        """
        Build system instruction with context.

        Args:
            context_text: Formatted context text

        Returns:
            Complete system instruction
        """
        return f"""你是工单智能助手。你的任务是根据用户的问题和提供的相关历史工单（Context），给出解决方案建议。

!!!important!!!注释（仅供程序阅读，不输出）：以下内容的样式请用富文本和自动换行，仅做内容结构规范，输出时请严格按照以下格式输出，不要输出其他无关内容。

1. ***推荐处理团队***：[根据工单上下文推断的团队名称，如'云平台-流程中心'，如果不确定则填'待确认']
2. ***推荐处理角色***：[根据上下文推断的角色，如'开发人员'或'产品经理']
3. ***推荐相关工单***:
[工单编号]-[工单描述]
[工单回复内容(摘录)]
[工单创建日期]-[工单回复人]
(如果有多个相关工单，请列出前3个；如果没有，请写'无')

4. ***智能建议***:
[在此处给出你的综合分析建议。如果找到了相关工单，请总结解决方案；如果没有找到，请根据通用知识给出建议。字数不超过500字。]

相关工单上下文：
{context_text}
"""

    def analyze_query(self, query: str, images: List[str], context_docs: List[Dict], api_key: str,
                      provider: str = "gemini", model_name: str = "", base_url: str = ""):
        if not api_key:
            yield "请先在设置中配置 API Key 以启用智能分析。"
            return

        # Build context and system instruction
        context_text = self._build_context_text(context_docs)
        system_instruction = self._build_system_instruction(context_text)

        if provider != "gemini":
            yield from self._call_openai_with_retry(api_key, model_name, base_url, system_instruction, query, images)
        else:
            yield from self._call_gemini_with_retry(api_key, model_name, system_instruction, query, images)

    def _call_gemini_with_retry(self, api_key: str, model_name: str, system_prompt: str, query: str, images: List[str], temperature: float = None):
        """Call Gemini API with retry logic"""
        def operation():
            yield from self._call_gemini(api_key, model_name, system_prompt, query, images, temperature=temperature)

        try:
            yield from self._retry_with_backoff(operation)
        except Exception as e:
            yield f"Gemini 分析失败: {str(e)}"

    def _call_openai_with_retry(self, api_key: str, model_name: str, base_url: str, system_prompt: str, query: str, images: List[str], temperature: float = None, max_tokens: int = None):
        """Call OpenAI API with retry logic"""
        def operation():
            yield from self._call_openai(api_key, model_name, base_url, system_prompt, query, images, temperature=temperature, max_tokens=max_tokens)

        try:
            yield from self._retry_with_backoff(operation)
        except Exception as e:
            yield f"模型调用失败: {str(e)}"

    def _call_gemini(self, api_key: str, model_name: str, system_prompt: str, query: str, images: List[str], temperature: float = None):
        """Call Gemini API using new google-genai SDK"""
        # Use provided model name, with fallback defaults
        if not model_name:
            model_name = "gemini-2.0-flash"
        
        # Create client with API key
        client = genai.Client(api_key=api_key)
        
        # Build content parts
        contents = []
        
        # Add system prompt and query as text
        contents.append(f"{system_prompt}\n\n用户问题: {query}")
        
        # Add images as inline data
        for b64_str in images:
            if "base64," in b64_str:
                b64_str = b64_str.split("base64,")[1]
            img_data = base64.b64decode(b64_str)
            contents.append(genai.types.Part.from_bytes(data=img_data, mime_type="image/jpeg"))
        
        # Stream response
        for chunk in client.models.generate_content_stream(
            model=model_name,
            contents=contents
        ):
            if chunk.text:
                yield chunk.text

    def _call_openai(self, api_key: str, model_name: str, base_url: str, system_prompt: str, query: str, images: List[str], temperature: float = None, max_tokens: int = None):
        if not base_url:
            base_url = "https://api.openai.com/v1" # Fallback

        import httpx as _httpx
        _is_local = bool(base_url) and any(h in base_url for h in ("localhost", "127.0.0.1"))
        _timeout = _httpx.Timeout(
            connect=5.0 if _is_local else 30.0,
            read=120.0, write=15.0, pool=5.0,
        )
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=_timeout, max_retries=0)

        if images:
            user_content: list | str = [{"type": "text", "text": f"用户问题: {query}"}]
            for b64_str in images:
                if not b64_str.startswith("data:"):
                    b64_str = "data:image/jpeg;base64," + b64_str
                user_content.append({"type": "image_url", "image_url": {"url": b64_str}})
        else:
            user_content = f"用户问题: {query}"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        _create_kwargs = {"model": model_name, "messages": messages, "stream": True}
        if temperature is not None:
            _create_kwargs["temperature"] = temperature
        if max_tokens is not None:
            _create_kwargs["max_tokens"] = max_tokens
        response = client.chat.completions.create(**_create_kwargs)
        for chunk in response:
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    def call_llm(self, prompt: str, api_key: str = None, provider: str = "gemini", model_name: str = "", base_url: str = "", temperature: float = None, max_tokens: int = None, timeout_s: float = 90.0) -> str:
        """Non-streaming LLM call for batch processing (e.g., weekly reports, reply generation)"""
        if not api_key:
             api_key = os.environ.get("LLM_API_KEY", "")

        if not api_key:
            return "Error: No API Key provided. Please set LLM_API_KEY."

        def _collect() -> str:
            chunks = []
            if provider != "gemini":
                for chunk in self._call_openai_with_retry(api_key, model_name, base_url, "You are a helpful analyst.", prompt, [], temperature=temperature, max_tokens=max_tokens):
                    chunks.append(chunk)
            else:
                for chunk in self._call_gemini_with_retry(api_key, model_name, "You are a helpful analyst.", prompt, [], temperature=temperature):
                    chunks.append(chunk)
            return "".join(chunks)

        # 用 ThreadPoolExecutor 做总时长兜底：防止流式响应极慢时永久阻塞
        # shutdown(wait=False) prevents blocking on the streaming thread after timeout
        _ex = _cf.ThreadPoolExecutor(max_workers=1)
        _fut = _ex.submit(_collect)
        try:
            result = _fut.result(timeout=timeout_s)
        except _cf.TimeoutError:
            _ex.shutdown(wait=False)
            return f"模型调用超时（>{timeout_s:.0f}s），请检查网络或稍后重试。"
        finally:
            _ex.shutdown(wait=False)

        # 过滤 <think>...</think> 标签（部分推理模型会输出思考过程）
        result = re.sub(r'<think>[\s\S]*?</think>', '', result, flags=re.DOTALL).strip()
        return result

    def analyze_query_stream(self, query: str, images: List[str], context_docs: List[Dict], api_key: str,
                           provider: str = "gemini", model_name: str = "", base_url: str = "") -> Generator[Dict, None, None]:
        """
        Stream analysis of query with event-based responses.

        Yields events in format:
        {"event": "start", "data": {"status": "searching", "message": "..."}}
        {"event": "search_results", "data": {"results": [...], "count": 5}}
        {"event": "analyzing", "data": {"message": "AI正在分析..."}}
        {"event": "content", "data": {"chunk": "文字片段", "index": 0}}
        {"event": "done", "data": {"status": "complete", "total_chunks": 45, "duration_ms": 3200, "content": "完整内容"}}
        {"event": "error", "data": {"code": "LLM_ERROR", "message": "..."}}
        """
        start_time = time.time()

        # Check API key
        if not api_key:
            yield {"event": "error", "data": {"code": "NO_API_KEY", "message": "请先在设置中配置 API Key 以启用智能分析。"}}
            return

        # Yield start event
        yield {"event": "start", "data": {"status": "searching", "message": "正在搜索相关知识库..."}}

        # Yield search results
        yield {"event": "search_results", "data": {"results": context_docs, "count": len(context_docs)}}

        # Yield analyzing event
        yield {"event": "analyzing", "data": {"message": "AI正在分析..."}}

        # Build context and system instruction
        context_text = self._build_context_text(context_docs)
        system_instruction = self._build_system_instruction(context_text)

        # Stream content
        full_content = ""
        chunk_index = 0

        try:
            if provider != "gemini":
                content_generator = self._call_openai_stream(
                    system_instruction, query, api_key, model_name, base_url, images
                )
            else:
                content_generator = self._call_gemini_stream(
                    system_instruction, query, api_key, model_name, images
                )

            for chunk in content_generator:
                full_content += chunk
                yield {"event": "content", "data": {"chunk": chunk, "index": chunk_index}}
                chunk_index += 1

            # Calculate duration
            duration_ms = (time.time() - start_time) * 1000

            # Yield done event
            yield {
                "event": "done",
                "data": {
                    "status": "complete",
                    "total_chunks": chunk_index,
                    "duration_ms": round(duration_ms, 2),
                    "content": full_content
                }
            }

        except Exception as e:
            # Yield error event
            yield {"event": "error", "data": {"code": "LLM_ERROR", "message": str(e)}}

    def _call_gemini_stream(self, system_prompt: str, query: str, api_key: str, model_name: str, images: List[str]) -> Generator[str, None, None]:
        """Call Gemini API using streaming with retries."""
        def operation():
            yield from self._call_gemini(api_key, model_name, system_prompt, query, images)

        try:
            yield from self._retry_with_backoff(operation)
        except Exception as e:
            raise Exception(f"Gemini 分析失败: {str(e)}")

    def _call_openai_stream(self, system_prompt: str, query: str, api_key: str, model_name: str, base_url: str, images: List[str]) -> Generator[str, None, None]:
        """Call OpenAI API using streaming with retries."""
        def operation():
            yield from self._call_openai(api_key, model_name, base_url, system_prompt, query, images)

        try:
            yield from self._retry_with_backoff(operation)
        except Exception as e:
            raise Exception(f"模型调用失败: {str(e)}")


llm_service = LLMService()
