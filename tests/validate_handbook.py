#!/usr/bin/env python3
"""Validate the self-contained Local Agent Runtime learning handbook."""

from __future__ import annotations

import hashlib
import re
import sys
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "Local-Agent-Runtime-小白入门与开发防跑偏手册.html"
SOURCES = {
    ROOT / "个人本地智能助手-Agent项目设计方案.md": (
        "3c1c322affc8056295a6810f4f6db6c6c0a208a9b9ea163564856e105e2f3779"
    ),
    ROOT / "meeting/0829/会议录制：agent-meeting01.md": (
        "6ba6fb17d6dc83ae5335c6af210b4b68df63154c1fe8c424add755bad2d8d670"
    ),
    ROOT / "meeting/0829/会议录制：agent-meeting02.md": (
        "f33607087582a429eace0e19b919aa83a7911befc398b7e33b0b295608fef4a5"
    ),
}
REQUIRED_COVERAGE = {
    *(f"D-{i:02d}" for i in range(1, 11)),
    *(f"M1-{i:02d}" for i in range(1, 8)),
    *(f"M2-{i:02d}" for i in range(1, 7)),
}
REQUIRED_IDS = {
    "reading-progress",
    "mobile-nav-toggle",
    "mobile-nav-close",
    "nav-scrim",
    "sidebar",
    "handbook-search",
    "search-status",
    "main-content",
    "start",
    "project",
    "model-to-agent",
    "context",
    "tool",
    "agent-loop",
    "runtime",
    "first-slice",
    "guardrails",
    "deep-dives",
    "session-memory",
    "context-compression",
    "long-term-memory",
    "tool-runtime",
    "completion-evaluation",
    "provider-models",
    "extensions",
    "product-enterprise",
    "roadmap",
    "collaboration-learning",
    "appendices",
    "glossary",
    "asr-corrections",
    "coverage-matrix",
    "back-to-top",
}
REQUIRED_PHRASES = {
    "60–90 分钟主线",
    "Agent Loop",
    "Tool Runtime",
    "Session、Context 与 Memory",
    "最大轮数",
    "取消",
    "工具结果回填",
    "read_file",
    "模型 API",
    "用户不偏好 CLI",
    "2–3 个工具",
    "7–8 个内置工具",
    "约 80%",
    "约 30%",
    "memory.md",
    "Cron",
    "GitHub",
    "Phase 0",
    "Phase 7",
    "M1",
    "M5",
    "设计文档",
    "会议 01",
    "会议 02",
    "教学解释",
}
REQUIRED_TERMS = {
    "Model",
    "Provider",
    "Agent",
    "AgentRun",
    "Runtime",
    "Harness",
    "Context",
    "Session",
    "Memory",
    "Tool",
    "ToolCall",
    "Skill",
    "MCP",
    "Trigger",
    "Artifact",
    "Event / Trace",
}


class HandbookParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.internal_hrefs: list[str] = []
        self.coverage: set[str] = set()
        self.external_dependencies: list[str] = []
        self.tags: Counter[str] = Counter()
        self.heading_levels: list[int] = []
        self.has_skip_link = False
        self.has_print_style = False
        self.has_reduced_motion = False
        self._in_style = False
        self._style_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags[tag] += 1
        attr = {key: value or "" for key, value in attrs}
        if attr.get("id"):
            self.ids.append(attr["id"])
        href = attr.get("href", "")
        if href.startswith("#") and len(href) > 1:
            self.internal_hrefs.append(href[1:])
        if tag == "a" and "skip-link" in attr.get("class", "").split():
            self.has_skip_link = href == "#main-content"
        coverage = attr.get("data-coverage", "")
        if coverage:
            self.coverage.update(coverage.split())
        if tag in {"script", "img", "iframe", "source", "video", "audio"}:
            src = attr.get("src", "")
            if src:
                self.external_dependencies.append(src)
        if tag == "link" and attr.get("rel", "").lower() in {
            "stylesheet",
            "preload",
            "modulepreload",
        }:
            self.external_dependencies.append(attr.get("href", ""))
        if re.fullmatch(r"h[1-6]", tag):
            self.heading_levels.append(int(tag[1]))
        if tag == "style":
            self._in_style = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self._style_parts.append(data)

    def finish(self) -> None:
        css = "\n".join(self._style_parts)
        self.has_print_style = "@media print" in css
        self.has_reduced_motion = "prefers-reduced-motion" in css


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def validate() -> list[str]:
    errors: list[str] = []

    for path, expected in SOURCES.items():
        check(path.exists(), f"源文件不存在：{path.relative_to(ROOT)}", errors)
        if path.exists():
            check(
                digest(path) == expected,
                f"源文件已变化：{path.relative_to(ROOT)}",
                errors,
            )

    if not TARGET.exists():
        errors.append(f"目标 HTML 尚不存在：{TARGET.name}")
        return errors

    raw = TARGET.read_text(encoding="utf-8")
    parser = HandbookParser()
    parser.feed(raw)
    parser.close()
    parser.finish()

    counts = Counter(parser.ids)
    duplicate_ids = sorted(item for item, count in counts.items() if count > 1)
    missing_anchor_targets = sorted(set(parser.internal_hrefs) - set(parser.ids))
    missing_ids = sorted(REQUIRED_IDS - set(parser.ids))
    missing_coverage = sorted(REQUIRED_COVERAGE - parser.coverage)
    missing_phrases = sorted(phrase for phrase in REQUIRED_PHRASES if phrase not in raw)
    missing_terms = sorted(term for term in REQUIRED_TERMS if term not in raw)
    forbidden_placeholders = sorted(
        token for token in ("TODO", "TBD", "FIXME") if token in raw
    )
    external_urls = re.findall(r"(?:https?:)?//[^\s'\"<>]+", raw, flags=re.I)

    check("<html lang=\"zh-CN\"" in raw, "html 缺少 lang=\"zh-CN\"", errors)
    check("name=\"viewport\"" in raw, "缺少 viewport 元信息", errors)
    check(parser.tags["main"] == 1, "必须且只能有一个 main", errors)
    check(parser.tags["h1"] == 1, "必须且只能有一个 h1", errors)
    check(parser.tags["section"] >= 20, "章节数量不足 20", errors)
    check(parser.tags["details"] >= 10, "深挖或自检折叠内容不足 10 组", errors)
    check(parser.tags["table"] >= 6, "证据与术语表格不足 6 张", errors)
    check(parser.has_skip_link, "缺少指向正文的跳过导航链接", errors)
    check(parser.has_print_style, "缺少 @media print 打印样式", errors)
    check(parser.has_reduced_motion, "缺少 prefers-reduced-motion 样式", errors)
    check(not duplicate_ids, f"存在重复 id：{duplicate_ids}", errors)
    check(not missing_anchor_targets, f"内部链接缺少目标：{missing_anchor_targets}", errors)
    check(not missing_ids, f"缺少必需 id：{missing_ids}", errors)
    check(not missing_coverage, f"缺少覆盖键：{missing_coverage}", errors)
    check(not missing_phrases, f"缺少关键内容：{missing_phrases}", errors)
    check(not missing_terms, f"缺少术语：{missing_terms}", errors)
    check(not forbidden_placeholders, f"存在占位符：{forbidden_placeholders}", errors)
    check(not parser.external_dependencies, f"存在外部依赖：{parser.external_dependencies}", errors)
    check(not external_urls, f"存在外部 URL：{external_urls[:5]}", errors)
    check("IntersectionObserver" in raw, "缺少 Scroll Spy", errors)
    check("requestAnimationFrame" in raw, "缺少滚动节流", errors)
    check("aria-current" in raw, "缺少当前章节可访问状态", errors)
    check("aria-expanded" in raw, "移动目录缺少 aria-expanded", errors)
    check("role=\"status\"" in raw, "搜索结果缺少状态播报", errors)
    check("window.print" in raw, "缺少打印入口", errors)
    check("max-width: 900px" in raw, "缺少 900px 移动断点", errors)
    check("min-height: 44px" in raw, "未声明 44px 触控高度", errors)
    check("overflow-x: auto" in raw, "表格缺少局部横向滚动", errors)
    check("data-source=\"设计文档\"" in raw, "缺少设计文档来源标记", errors)
    check("data-source=\"会议 01\"" in raw, "缺少会议 01 来源标记", errors)
    check("data-source=\"会议 02\"" in raw, "缺少会议 02 来源标记", errors)
    check("data-source=\"教学解释\"" in raw, "缺少教学解释来源标记", errors)

    return errors


def main() -> int:
    errors = validate()
    if errors:
        print("FAIL: handbook validation failed")
        for error in errors:
            print(f"- {error}")
        return 1
    print("PASS: handbook validation succeeded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
