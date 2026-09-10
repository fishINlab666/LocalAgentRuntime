#!/usr/bin/env python3
"""Assemble a standalone workbook from reviewed teaching copy and unmodified sources."""
from hashlib import sha256
from html import escape
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
TARGET = ROOT / "Agent-Tools-工具调用层学习与开发手册.html"
OLD = ROOT / "Local-Agent-Runtime-小白入门与开发防跑偏手册.html"
SOURCES = {
    "D": ("项目设计", "0-个人本地智能助手-Agent项目设计方案.md", "3c1c322affc8056295a6810f4f6db6c6c0a208a9b9ea163564856e105e2f3779"),
    "T": ("工具设计", "1-工具调用层设计.md", "fa51e6da4803e00097c6e9ec5cd38fa6ca12b27b80acfbf864c06a9ed950872d"),
    "M": ("会议 0910", "meeting/0910/会议录制：agent-meeting 0910-1.md", "06f2661816046ca0e6153e876ad63abbed0810ae2f23564ad84b3f682c538adb"),
}
NAV = [
    ("60–90 分钟主线", [
        ("start", "00 怎么读这份手册"), ("why-tools", "01 为什么现在做工具层"),
        ("one-task", "02 跟着一次任务走"), ("model-input", "03 模型这一轮看见什么"),
        ("tool-contract", "04 工具说明与参数规则"), ("three-roles", "05 三个角色怎样分工"),
        ("five-gates", "06 请求怎样通过五道关"),
        ("results", "07 成功和错误怎样回填"), ("approval", "08 用户何时确认什么"),
        ("first-unit", "09 三步改造与最小验收"), ("guardrails", "10 开发前的对齐清单")]),
    ("2–3 小时完整学习 · 选读", [
        ("context-storage", "11 会话、上下文与文件"),
        ("interfaces", "12 Provider 与 MCP"),
        ("project-map", "13 工具层在全项目的位置"),
        ("roadmap", "14 完整路线与停止边界")]),
    ("附录与原文", [
        ("decisions", "A 口径差异与未决事项"), ("glossary", "B 一句话术语表"),
        ("examples", "C 原文例子与转写疑点"),
        ("coverage-matrix", "D 全部内容的覆盖去向"),
        ("source-snapshot", "E 版本与全文核对"),
        ("source-D", "原文 1 · 项目设计"),
        ("source-T", "原文 2 · 工具设计"),
        ("source-M", "原文 3 · 0910 会议")]),
]
TITLES = dict(item for _, group in NAV for item in group)
MEETING = [
    (1, 26, "00:00:02–00:00:51｜开场、共享屏幕；工程师评价 Loop 没问题", "why-tools", "当时判断 / 非实质操作"),
    (27, 38, "00:00:58–00:01:35｜实际动作最终落到工具", "why-tools", "工程师解释"),
    (39, 59, "00:01:45–00:02:31｜模型视角、轨迹面板与共享操作", "tool-contract", "解释 / 演示操作"),
    (60, 74, "00:02:34–00:03:44｜Turn、每日金价、周报与 Memory Recall", "one-task", "解释 / 示例"),
    (75, 125, "00:03:45–00:07:33｜Tool List、消息历史、read_skill 与下一次调用", "model-input", "解释 / 示例"),
    (126, 143, "00:07:38–00:08:13｜assistant 是谁；模型输出再拼回", "model-input", "用户疑问 / 解释"),
    (144, 182, "00:08:19–00:10:41｜Context/Message List/工具结果的关系", "model-input", "用户疑问 / 解释"),
    (183, 188, "00:11:05–00:11:12｜结束后的 Memory 写回", "context-storage", "演示 / 后续设计"),
    (189, 230, "00:11:28–00:13:36｜多 Session 隔离、CLI 和重启后的恢复", "context-storage", "用户疑问 / 解释"),
    (231, 266, "00:13:43–00:15:27｜日志文件、Workspace、PPT/Artifact、配置", "context-storage", "解释 / 示例 / 操作插话"),
    (267, 284, "00:15:30–00:16:42｜一次性 Loop 与程序的执行、时限、预算", "three-roles", "当时状态 / 解释"),
    (285, 332, "00:16:43–00:19:03｜动作文案；调用意图；现有还是临时写工具", "tool-contract", "演示 / 用户疑问"),
    (333, 395, "00:19:07–00:21:13｜固定 Schema、动态参数；URL 与关键词能力", "tool-contract", "解释 / 假设示例"),
    (396, 416, "00:21:18–00:22:13｜统一填写调用意图以便用户理解", "approval", "工程师建议"),
    (417, 431, "00:22:35–00:23:22｜工具声明、风险、模型判断中风险", "approval", "解释 / 可选策略"),
    (432, 452, "00:23:35–00:25:06｜用户工具、MCP、GitHub 30 个工具例子", "interfaces", "解释 / 假设数量"),
    (453, 536, "00:25:07–00:30:21｜供应商与 Provider 适配、参数差异、先接一家", "interfaces", "用户疑问 / 假设 / 阶段建议"),
    (537, 578, "00:30:35–00:32:42｜模型可能写错工具名、content 字段", "five-gates", "解释 / 假设 / 演示判断"),
    (579, 605, "00:32:48–00:34:28｜信封是返回报告；未知工具结果回填", "results", "用户困惑 / 解释"),
    (606, 611, "00:34:34–00:34:46｜参数缺失、拼写、非法值", "five-gates", "校验示例"),
    (612, 629, "00:34:47–00:35:32｜低中高风险、强制审批与可选放行", "approval", "工程师可选策略"),
    (630, 644, "00:35:52–00:36:24｜网络等待、工具时限、次数和等待参数", "five-gates", "示例 / 可选设计"),
    (645, 659, "00:36:54–00:37:44｜记账、Tool Runtime、轨迹反馈帮助调整", "three-roles", "解释 / 窄义说明"),
    (660, 668, "00:38:07–00:38:33｜成功失败报告、后续 Skill/MCP 关联", "results", "归纳 / 下一阶段"),
    (669, 689, "00:38:37–00:39:09｜先做工具；有空并行会话；用户回应一起做", "first-unit", "阶段对齐 / 用户回应 / 告别"),
]


def route(key, line):
    if key == "D":
        intervals = [
            (42, "why-tools"), (66, "guardrails"), (204, "project-map"),
            (219, "project-map"), (303, "one-task"), (329, "interfaces"),
            (361, "three-roles"), (398, "context-storage"), (424, "interfaces"),
            (479, "project-map"), (504, "approval"), (534, "guardrails"),
            (563, "project-map"), (724, "roadmap"), (751, "guardrails"),
            (769, "guardrails")]
    else:
        intervals = [
            (18, "why-tools"), (55, "tool-contract"), (73, "three-roles"),
            (95, "five-gates"), (113, "results"), (155, "approval"),
            (168, "three-roles"), (190, "first-unit"), (200, "first-unit"),
            (207, "guardrails")]
    return next(target for end, target in intervals if line < end)


def load_sources():
    result = {}
    for key, (label, name, expected) in SOURCES.items():
        data = (ROOT / name).read_bytes()
        actual = sha256(data).hexdigest()
        if actual != expected:
            raise ValueError(f"{name} 的版本已变化，须先重新阅读并调整覆盖索引")
        result[key] = (label, name, data.decode("utf-8"), actual, len(data))
    return result


def coverage(sources):
    result = ['<section id="coverage-matrix" class="chapter appendix" data-searchable data-nav-target><h2>D · 全部内容的覆盖去向</h2>',
              '<p>以下区间从第一行连续覆盖到末行，不跳过空行、重复、图代码或会议插话。每段的主题在正文讲解，逐字文本在原文附录保留。<strong>机械全覆盖不等于理解准确</strong>：另以独立内容审查核对主题与不同口径。</p>']
    for key, (label, _, raw, _, _) in sources.items():
        lines = raw.splitlines()
        if key == "M":
            rows = MEETING
        else:
            headings = [(i, line.lstrip("#").strip()) for i, line in enumerate(lines, 1) if re.match(r"^#{1,4} ", line)]
            rows = [(start, headings[idx + 1][0] - 1 if idx + 1 < len(headings) else len(lines), title, route(key, start), "设计原文 / 含原始图表与列表") for idx, (start, title) in enumerate(headings)]
        result.append(f'<h3>{escape(label)}</h3><div class="table-wrap" tabindex="0"><table><thead><tr><th>源行与主题</th><th>信息性质</th><th>教学去向</th></tr></thead><tbody>')
        for start, end, title, dest, kind in rows:
            result.append(f'<tr data-coverage-source="{key}" data-start="{start}" data-end="{end}"><td><a href="#{key}-L{start}">{key} {start}–{end}</a><br>{escape(title)}</td><td>{escape(kind)}</td><td><a href="#{dest}">{TITLES[dest]}</a></td></tr>')
        result.append("</tbody></table></div>")
    result.append("</section>")
    return "\n".join(result)


def originals(sources):
    result = ['<section id="source-snapshot" class="chapter appendix" data-searchable data-nav-target><h2>E · 版本与全文核对</h2>',
              '<p>主代理已逐段阅读三份文档全部内容，独立审阅者另行复核。共 <strong>1,663 行、71,844 字节</strong>；行数按源文件换行统计，不等于中文字数。下方不仅存摘要：每个字符、标点、原始转写和空白都保留。行号是显示辅助，不属于原文。</p>',
              '<p>本册事实来源仅为这三份文件。旧手册只用于阅读与教学方式参照；本册不把 0829 会议、演示系统或设计提议冒充本项目已实现的能力。原始 Markdown 中的操作建议仅为资料，不会被此页面执行。</p>',
              '<p>校验方法：将下方内嵌原文还原为 UTF-8 后计算 SHA-256，与源文件逐字符、逐字节比较；生成器和静态检查均拒绝来源版本变化。正文例子、答案与审查限定另有“教学解释”标识。</p>']
    for key, (label, name, raw, digest, size) in sources.items():
        result.append(f'<p><strong>{key} · {escape(label)}</strong> · {len(raw.splitlines())} 行 / {size:,} 字节<br><a href="{escape(name, quote=True)}">{escape(name)}</a><br><code class="digest">{digest}</code></p>')
    result.append('<p>打印时会展开答案和三份原文，篇幅较长；只需教学正文时，可在系统打印窗口选择页码范围。完整原文无需联网或另开 Markdown 即可查阅。</p></section>')
    for key, (label, name, raw, _, _) in sources.items():
        # Entity-encode Markdown's trailing spaces without changing the DOM text.
        # This preserves the exact source while keeping generated HTML diff-clean.
        def encode_line(line):
            return re.sub(r"[ \t]+(?=\r?\n|$)", lambda match: "".join("&#32;" if char == " " else "&#9;" for char in match[0]), escape(line))
        line_spans = "".join(f'<span id="{key}-L{i}" class="source-line" data-line="{i}">{encode_line(line)}</span>' for i, line in enumerate(raw.splitlines(keepends=True), 1))
        result.append(f'<section id="source-{key}" class="chapter appendix" data-searchable data-nav-target><h2>原文 · {escape(label)}</h2><p>{escape(name)} · 保留原始转写及 Markdown 图表代码。<a href="#coverage-matrix">返回覆盖索引</a></p><details class="source-details"><summary>展开完整原文 · {len(raw.splitlines())} 行</summary><pre class="source-verbatim"><code id="raw-{key}" data-verbatim="{key}">{line_spans}</code></pre></details></section>')
    return "\n".join(result)


def build():
    sources = load_sources()
    old = OLD.read_text(encoding="utf-8")
    prefix = old.split('  <main id="main-content"', 1)[0]
    prefix = re.sub(r"<title>.*?</title>", "<title>Agent Tools｜工具调用层学习与开发手册</title>", prefix)
    prefix = prefix.replace("Local Agent Runtime 手册", "Agent Tools 学习手册")
    prefix = prefix.replace('<p class="sidebar-title">Local Agent Runtime</p>', '<p class="sidebar-title">Agent Tools</p>')
    # The no-script fallback uses immediate native jumps, so consecutive long
    # anchor/answer navigation does not compete with an unfinished smooth scroll.
    prefix = prefix.replace("<noscript>\n    <style>", "<noscript>\n    <style>\n      html { scroll-behavior: auto; }")
    nav = []
    for group_name, links in NAV:
        nav.append(f'<p class="toc-group">{group_name}</p>')
        nav.extend(f'<a href="#{key}">{label}</a>' for key, label in links)
    prefix = re.sub(r'(<nav class="toc"[^>]*>).*?(</nav>)', lambda m: m[1] + "\n".join(nav) + m[2], prefix, flags=re.S)
    extra_css = """
    .digest { overflow-wrap:anywhere; word-break:break-all; }
    .source-verbatim { background:var(--surface); color:var(--text); border-color:var(--border); white-space:pre-wrap; overflow-wrap:anywhere; word-break:break-word; font-size:.82rem; padding:.9rem; }
    .source-line { scroll-margin-top:5rem; }
    .source-line::before { content:attr(data-line); display:inline-block; width:3.3em; color:var(--text-muted); user-select:none; font-size:.8em; }
    .source-line:target { background:#fff0b3; outline:1px solid #b7791f; }
    .chapter pre:not(.source-verbatim) { white-space:pre-wrap; overflow-wrap:anywhere; }
    .mini-proof { border-top:1px solid var(--border); margin-top:1rem; padding-top:1rem; }
    .walkthrough { border-left:2px solid var(--border-strong); padding-left:1.1rem; margin-left:.25rem; }
    .walkthrough h3 { margin-top:1.8rem; }
    .chapter-pager { display:flex; flex-wrap:wrap; justify-content:space-between; gap:.5rem; margin-top:2rem; }
    .chapter-pager a { display:inline-flex; align-items:center; min-height:44px; }
    @media print {
      .source-details, .source-verbatim, .source-verbatim code { break-inside:auto; page-break-inside:auto; overflow:visible; }
      .source-verbatim { border:0; padding:0; font-size:8pt; }
    }
    """
    prefix = prefix.replace("  </style>", extra_css.rstrip() + "\n  </style>", 1)
    body = (HERE / "agent-tools-content.html").read_text(encoding="utf-8")
    body = body.replace("<!-- COVERAGE -->", coverage(sources)).replace("<!-- ORIGINALS -->", originals(sources))
    suffix = old.split("  </main>", 1)[1]
    # A source deep link may initially point inside a closed details element.
    suffix = suffix.replace("      runSearch();\n    })();", """      runSearch();
      const initialTarget = document.getElementById(decodeURIComponent(location.hash.slice(1)));
      if (initialTarget) {
        revealAnchor(initialTarget);
        requestAnimationFrame(() => initialTarget.scrollIntoView({ behavior: "auto", block: "start" }));
      }
    })();""")
    TARGET.write_text(prefix + '  <main id="main-content" tabindex="-1">\n' + body + "\n  </main>" + suffix, encoding="utf-8")
    print(f"Built {TARGET.name} ({TARGET.stat().st_size:,} bytes); three source hashes unchanged")


if __name__ == "__main__":
    build()
