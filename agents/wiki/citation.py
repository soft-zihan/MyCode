"""Wiki citation — 使用闭环标记（Phase 4，仿 Codex citation 低成本版）。

模型实际使用某条注入的 wiki 条目时，在回复末尾输出：
    <wiki-citation>{rel_path}</wiki-citation>

本模块负责：
- CitationStripper：流式增量剥离器（逐 token 转发场景，hold 疑似标签前缀）
- strip_citations：一次性剥离（落盘 assistant_message 前，防 citation 进
  events.jsonl 被下次提取回灌）
- 解析出的 path → wiki_manager.increment_usage（usage_count+1、last_used，
  不单独 git commit）
"""

from __future__ import annotations

CITATION_OPEN = "<wiki-citation>"
CITATION_CLOSE = "</wiki-citation>"


def _partial_suffix_len(buf: str, tag: str) -> int:
    """buf 尾部与 tag 前缀的最长重叠长度（可能是尚未收全的标签开头）。"""
    for k in range(min(len(buf), len(tag) - 1), 0, -1):
        if buf.endswith(tag[:k]):
            return k
    return 0


class CitationStripper:
    """增量剥离器：正常文本透传；疑似标签前缀先 hold；确认标签则吞到闭合。

    用法：
        对每个流式 chunk 调 feed()，把返回值发给用户；
        流结束后调 flush() 把 hold 的残余发出去；
        paths 为本次流中剥离出的完整 citation 路径。
    """

    def __init__(self) -> None:
        self._buf = ""
        self._in_citation = False
        self._citation = ""
        self.paths: list[str] = []

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self._buf += chunk
        out: list[str] = []

        while self._buf:
            if not self._in_citation:
                idx = self._buf.find(CITATION_OPEN)
                if idx >= 0:
                    out.append(self._buf[:idx])
                    self._buf = self._buf[idx + len(CITATION_OPEN):]
                    self._in_citation = True
                    continue
                keep = _partial_suffix_len(self._buf, CITATION_OPEN)
                cut = len(self._buf) - keep
                if cut > 0:
                    out.append(self._buf[:cut])
                self._buf = self._buf[cut:]
                break
            else:
                idx = self._buf.find(CITATION_CLOSE)
                if idx >= 0:
                    self._citation += self._buf[:idx]
                    self._buf = self._buf[idx + len(CITATION_CLOSE):]
                    self._in_citation = False
                    path = self._citation.strip()
                    if path:
                        self.paths.append(path)
                    self._citation = ""
                    continue
                keep = _partial_suffix_len(self._buf, CITATION_CLOSE)
                cut = len(self._buf) - keep
                if cut > 0:
                    self._citation += self._buf[:cut]
                self._buf = self._buf[cut:]
                break

        return "".join(out)

    def flush(self) -> str:
        """流结束：hold 的残余若不是完整 citation，按可见文本补发。"""
        if self._in_citation:
            # 未闭合的 citation 当普通文本还回去（模型输出被截断等场景）
            rest = CITATION_OPEN + self._citation + self._buf
            self._citation = ""
            self._buf = ""
            self._in_citation = False
            return rest
        rest = self._buf
        self._buf = ""
        return rest


def strip_citations(text: str) -> tuple[str, list[str]]:
    """一次性剥离全部 citation，返回 (干净文本, 路径列表)。"""
    if not text or CITATION_OPEN not in text:
        return text, []
    stripper = CitationStripper()
    clean = stripper.feed(text) + stripper.flush()
    return clean, stripper.paths
