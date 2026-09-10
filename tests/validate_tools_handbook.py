#!/usr/bin/env python3
"""Check the three-source tools workbook, without running the Agent application."""
from collections import Counter
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "Agent-Tools-工具调用层学习与开发手册.html"
SOURCES = {
    "D": ("0-个人本地智能助手-Agent项目设计方案.md", "3c1c322affc8056295a6810f4f6db6c6c0a208a9b9ea163564856e105e2f3779"),
    "T": ("1-工具调用层设计.md", "fa51e6da4803e00097c6e9ec5cd38fa6ca12b27b80acfbf864c06a9ed950872d"),
    "M": ("meeting/0910/会议录制：agent-meeting 0910-1.md", "06f2661816046ca0e6153e876ad63abbed0810ae2f23564ad84b3f682c538adb"),
}
LESSONS = ["why-tools", "one-task", "model-input", "tool-contract", "three-roles", "five-gates", "results", "approval", "first-unit", "guardrails"]


class Book(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = []
        self.links = []
        self.external = []
        self.raw = {}
        self.raw_key = None
        self.coverage = {key: [] for key in SOURCES}
        self.sections = {}
        self.section = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "a" and "href" in attrs:
            self.links.append(attrs["href"])
        for key in ("src", "href"):
            if tag in ("script", "link", "img", "iframe", "video", "audio") and attrs.get(key, "").startswith(("http:", "https:", "//")):
                self.external.append(attrs[key])
        if tag == "code" and "data-verbatim" in attrs:
            self.raw_key = attrs["data-verbatim"]
            self.raw[self.raw_key] = ""
        if tag == "tr" and "data-coverage-source" in attrs:
            self.coverage[attrs["data-coverage-source"]].append((int(attrs["data-start"]), int(attrs["data-end"])))
        if tag == "section":
            self.section = attrs.get("id")
            self.sections[self.section] = Counter()
        if tag == "details" and self.section:
            for name in attrs.get("class", "").split():
                self.sections[self.section][name] += 1

    def handle_endtag(self, tag):
        if tag == "code":
            self.raw_key = None
        if tag == "section":
            self.section = None

    def handle_data(self, data):
        if self.raw_key:
            self.raw[self.raw_key] += data


def validate():
    assert TARGET.exists(), "最终 HTML 尚未生成"
    content = TARGET.read_text(encoding="utf-8")
    book = Book()
    book.feed(content)
    assert not [key for key, n in Counter(book.ids).items() if n > 1], "重复锚点"
    for href in book.links:
        parsed = urlsplit(href)
        if href.startswith("#"):
            assert unquote(href[1:]) in book.ids, f"无效站内锚点：{href}"
        elif not parsed.scheme and parsed.path:
            assert (ROOT / unquote(parsed.path)).is_file(), f"本地链接不存在：{href}"
    assert not book.external, f"存在外部依赖：{book.external}"
    for key, (name, expected) in SOURCES.items():
        data = (ROOT / name).read_bytes()
        assert sha256(data).hexdigest() == expected, f"原文版本发生变化：{name}"
        original = data.decode("utf-8")
        assert book.raw.get(key) == original, f"内嵌原文不逐字符一致：{key}"
        assert expected in content, f"未展示来源校验值：{key}"
        total = len(original.splitlines())
        next_line = 1
        for start, end in book.coverage[key]:
            assert start == next_line and end >= start, f"覆盖区间缺漏/重叠：{key} {start}-{end}"
            next_line = end + 1
        assert next_line == total + 1, f"覆盖未到原文末尾：{key}"
        for line in range(1, total + 1):
            assert f"{key}-L{line}" in book.ids, f"缺少原文行锚点：{key}:{line}"
    for lesson in LESSONS:
        assert book.sections.get(lesson, {}).get("self-check") == 1, f"缺少自检：{lesson}"
        assert book.sections[lesson].get("answer-key") == 1, f"缺少答案：{lesson}"
    for phrase in ("不是本次代码核验", "模型判断", "这俩东西一起搞", "实际后果", "200", "跨重启", "inputSchema", "调用 ID", "归谁", "教学解释", "prefers-reduced-motion", "beforeprint", "source-snapshot", "没有匹配章节"):
        assert phrase in content, f"缺少关键边界或功能：{phrase}"
    print("PASS: tools handbook — exact sources, complete line coverage, anchors, offline structure, 10 lesson answer pairs")


if __name__ == "__main__":
    validate()
