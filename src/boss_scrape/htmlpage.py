"""在 Python 侧按容器取值 —— 刷新链路判定链路的第一个环节。

## 为什么不在浏览器里用 DOMParser

原先取值脚本在页面里 `fetch` 完就地解析、只回几个字符串。省带宽，但把**判定链路最要紧
的一段**放进了浏览器：那段代码只能用真浏览器跑，端到端测试覆盖不到。
现在 `fetch` 只负责把字节拿回来，解析与判定都在 Python 侧 —— 整条链路可脱网测试。

## 为什么自己写解析器

只为取 4 个容器引入 `bs4`/`lxml` 不划算，而项目当前一个 HTML 解析依赖都没有。
`html.parser` 是标准库，且岗位页是服务端渲染的规整 HTML，不需要容错解析。

只支持三种写法，够用就不再加：`tag`、`.class`、`#id`，以及它们用空格连成的
**后代链**（`.job-banner .job-status`）和逗号分组。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# 这些标签不会闭合，不能压栈。
_VOID = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})

_WS = re.compile(r"\s+")
_TAG = re.compile(r"^[A-Za-z][\w-]*")
_CLASS = re.compile(r"\.([\w-]+)")
_ID = re.compile(r"#([\w-]+)")


class _Part:
    """选择器的一段：`tag.class#id` 的任意组合。"""

    __slots__ = ("tag", "classes", "id")

    def __init__(self, text: str) -> None:
        tag = _TAG.match(text)
        ident = _ID.search(text)
        self.tag = tag.group(0).lower() if tag else ""
        self.classes = frozenset(_CLASS.findall(text))
        self.id = ident.group(1) if ident else ""

    def matches(self, tag: str, classes: frozenset[str], node_id: str) -> bool:
        return ((not self.tag or self.tag == tag)
                and (not self.id or self.id == node_id)
                and self.classes <= classes)


class _Selector:
    """后代链：`p1 p2 p3` 要求 p3 是当前节点、p2 是它的祖先、p1 是更外层的祖先。"""

    __slots__ = ("parts",)

    def __init__(self, text: str) -> None:
        self.parts = [_Part(chunk) for chunk in text.split() if chunk]

    def matches(self, stack: list[tuple[str, frozenset[str], str]]) -> bool:
        """`stack` 是当前打开元素的自外向内列表，末位是刚打开的那个。"""
        if not self.parts or not stack:
            return False
        index = len(stack) - 1
        if not self.parts[-1].matches(*stack[index]):
            return False
        # 剩下的段从内向外找祖先，顺序不能乱（`a b` 与 `b a` 不是一回事）。
        part, index = len(self.parts) - 2, index - 1
        while part >= 0 and index >= 0:
            if self.parts[part].matches(*stack[index]):
                part -= 1
            index -= 1
        return part < 0


class _Indexer(HTMLParser):
    """一遍扫描：记录每个目标选择器的**首个**命中元素的文本，以及是否出现过。"""

    def __init__(self, selectors: dict[str, list[_Selector]]) -> None:
        super().__init__(convert_charrefs=True)
        self.selectors = selectors
        self.stack: list[tuple[str, frozenset[str], str]] = []
        self.found = {key: False for key in selectors}
        self.text = {key: "" for key in selectors}
        self._capturing: list[tuple[str, int]] = []   # [(键, 压栈后的深度)]

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        classes = frozenset((values.get("class") or "").split())
        lowered = tag.lower()
        self.stack.append((lowered, classes, values.get("id") or ""))
        if lowered in _VOID:
            # ⚠️ 空元素（`<br/>` 最常见）**不能**走 handle_endtag：那会把当前正在收集的
            # 那个元素的深度误当成"它结束了"，于是收集被截断。
            # 实测代价：岗位正文用 `<br/>` 分段，第一版解析器在第一个 `<br/>` 就停，
            # 一段 743 字的正文只取到「岗位职责：」5 个字。
            self.stack.pop()
            return
        depth = len(self.stack)
        for key, group in self.selectors.items():
            if not self.found[key] and any(s.matches(self.stack) for s in group):
                self.found[key] = True
                self._capturing.append((key, depth))

    def handle_startendtag(self, tag, attrs):
        """`<br/>` 这类自闭合标签。**必须单独处理**：`HTMLParser` 的默认实现会
        依次调用 `handle_starttag` 和 `handle_endtag`，而后者会把当前正在收集的
        那个元素的深度当成"它结束了"，于是收集被截断。

        实测代价：岗位正文用 `<br/>` 分段，第一版在第一个 `<br/>` 就停——
        一段 743 字的正文只取到「岗位职责：」5 个字。
        """
        return None

    def handle_endtag(self, tag):
        if tag.lower() in _VOID:
            return
        if not self.stack:
            return
        depth = len(self.stack)
        self._capturing = [(k, d) for k, d in self._capturing if d != depth]
        lowered = tag.lower()
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == lowered:
                del self.stack[index:]
                return

    def handle_data(self, data):
        for key, _depth in self._capturing:
            self.text[key] += data

    def result(self) -> dict[str, dict]:
        return {key: {"present": self.found[key],
                      "text": _WS.sub(" ", self.text[key]).strip() or None}
                for key in self.selectors}


def query(html: str, selectors: dict[str, str]) -> dict[str, dict]:
    """按 `{键: 选择器}` 取值，返回 `{键: {present, text}}`。"""
    indexer = _Indexer({
        key: [_Selector(chunk) for chunk in value.split(",") if chunk.strip()]
        for key, value in selectors.items()})
    indexer.feed(html or "")
    indexer.close()
    return indexer.result()
