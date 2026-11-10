# Botaloid-lite

用曲库里的歌词原文来回复：每条结果都带原文、译文、曲名、署名，以及这段歌词在歌曲中的起止时间；配合 `botaloid_lite.media`，还能把它变成从这一句开始播放的链接。

这是 [Botaloid](https://github.com/caipeilan/Botaloid) 的精简版，原项目作者是 [@caipeilan](https://github.com/caipeilan)。这里只保留核心功能，内存占用大幅降低，适合在小机器上长期运行。纯 Python 标准库实现，需要 Python 3.11+。

## 示例

用户发送「周末想出去玩」。`step()` 通过模型适配器（这里用 OpenRouter 上的 qwen3.8-flash）分两阶段工作：先把对话变成检索方向，再从检索到的候选里挑选片段。

```python
from botaloid_lite import Library, Settings

library = Library("data/lyrics.sqlite3")
history = library.step(
    [{"role": "user", "text": "周末想出去玩"}],
    settings=Settings(model=call_model),      # call_model 见「接入模型」
)
print(history[-1]["fragments"][0])
```

```jsonc
{
  "text": "遊びに行くはずだったのに\nどこにもいけない",
  "translation": "本来人家可以出去玩的\n现在却哪里也去不了",
  "title": "どこにもいかない",
  "author": "マサラダ / 雨衣",
  "song_id": 432,
  "position": 14,                          // 起始行在曲内的行号
  "start_ms": 48197,                       // 这段歌词在歌曲中的起止时间
  "end_ms": 55096,
  "source": {"song_id": 432, "start": 13, "end": 14},   // 小句序号
  "lines": [                               // 逐行原文、译文与时间
    {"position": 14, "text": "遊びに行くはずだったのに",
     "translation": "本来人家可以出去玩的", "start_ms": 48197, "end_ms": 52412},
    {"position": 15, "text": "どこにもいけない",
     "translation": "现在却哪里也去不了", "start_ms": 52412, "end_ms": 55096}
  ]
}
```

把这段元数据交给 `media`，就得到从这一句开始播放的链接：

```python
from botaloid_lite.media import Resolver

link = Resolver().find(history[-1]["fragments"][0])
print(link.url)
# https://www.youtube.com/watch?v=xJULqAHbst0&t=48
```

`t=48` 来自 `start_ms=48197` 向下取整。

## 安装

```sh
python -m venv .venv
.venv/bin/python -m pip install -e .
```

Windows 下把 `.venv/bin/python` 换成 `.venv\Scripts\python`。

下载网易云歌单的脚本额外需要 `requests`：

```sh
.venv/bin/python -m pip install requests
```

## 准备曲库

```sh
# 1. 下载曲名、署名、歌词与已有译文（默认每秒 1 次请求，可中断续跑）
.venv/bin/python tools/fetch_netease.py 8285082830 --output data/raw.sqlite3

# 2. 合并重复版本、整理署名，写出曲库并构建检索索引
.venv/bin/python tools/prepare.py data/raw.sqlite3 --output data/lyrics.sqlite3
```

- `fetch_netease.py` 接受歌单链接、`#/playlist?id=...` 链接或数字 ID。
- `prepare.py` 会跳过纯音乐和无歌词的曲目，并在同名 `.tsv` 里写出清洗报告；`tools/cleaning_rules.json` 是人工修订规则。
- 两个脚本都不覆盖已存在的输出文件。
- 已有符合格式的 SQLite 曲库时，用 `python -m botaloid_lite.build data/lyrics.sqlite3` 补建索引即可。

`data/` 已被 `.gitignore` 排除，曲库文件不会进版本库。

## 用法

### 打开曲库

```python
Library(db_path, *, cache_size=32, pool=40, weights=(1.0, 1.5, 2.0))
```

只读打开曲库，支持 `with Library(...) as library:`。重建曲库或索引后请重新构造实例；没有索引的文件会抛 `MissingIndexError`。

| 参数 | 作用 |
| --- | --- |
| `cache_size` | SQLite 页缓存，单位 MB |
| `pool` | 每个检索方向先取多少条召回结果 |
| `weights` | 召回时单字 / 双字 / 三字列的权重 |

### 检索

```python
search(query=None, *, queries=None, author=None, title=None,
       source=None, direction="after", count=1, limit=5, phrases=4,
       exclude=(), refine=None) -> list[Passage]
```

纯检索，不调用模型。

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| `query` | `None` | 自由文本，会作为一条检索词 |
| `queries` | `None` | 额外检索词，`[["夜道", "夜里散步"], ["街灯"]]`，最多 4 组、每组 3 个词 |
| `author` | `None` | 按署名限定，规范化后做子串匹配 |
| `title` | `None` | 按曲名限定；有完全同名时优先完全同名 |
| `source` | `None` | 从已有片段导航；直接用返回的 `passage.to_dict()["source"]` |
| `direction` | `"after"` | 导航方向：`at` 原片段、`before` 前文、`after` 后文、`around` 前后各 `count` 句 |
| `count` | `1` | 导航时扩展的句数 |
| `limit` | `5` | 最多返回几条片段 |
| `phrases` | `4` | 每条片段最多合并几小句（1–8，总长不超过 140 字） |
| `exclude` | `()` | 不要返回的歌词文本，例如上一轮的回复 |
| `refine` | `None` | 可选回调，见[只做一次调用](#只做一次调用refine) |

几条行为约定：

- 检索词会做规范化（全半角、大小写、标点不敏感），少于 2 个字符的词会被丢弃，用户消息本身始终作为一条检索词。
- 没有 `query` / `queries`、但给了 `author` 或 `title` 时，返回这些曲目的开头片段。
- 多个检索组之间轮流取结果，同一首歌最多 2 条，去重后最多 16 条候选；候选会向后合并 1–4 小句，要求同一段落、总长 ≤ 140 字。

```python
library.search("夜里散步")
library.search(author="いよわ", limit=10)
library.search(queries=[["夜道", "夜里散步"], ["街灯"]], limit=3)
library.search(source={"song_id": 12, "start": 4, "end": 4}, direction="after", count=2)
library.search(source={"song_id": 12, "start": 4, "end": 4}, direction="around", count=1)
```

### 对话

```python
step(history, *, settings=None) -> list[dict]
```

`history` 是对话列表，每条为 `{"role": "user"|"assistant", "text": str}`，**最后一条必须是用户消息**。返回**新的**列表（原列表不会被修改），末尾追加一条助手回复：

```jsonc
{
  "role": "assistant",
  "text": "遊びに行くはずだったのに\nどこにもいけない",
  "status": "selected",                     // selected / no_match
  "source": {"song_id": 432, "start": 13, "end": 14},
  "fragments": [ /* Passage.to_dict()，见下节 */ ]
}
```

| `status` | 含义 |
| --- | --- |
| `selected` | 模型（或确定性排序）从候选里选出了片段 |
| `no_match` | 没有可用片段，`text` 为空、`fragments` 为空 |

助手回复里的 `source` 是最后一段的定位符，可以交给 `search(source=..., direction=...)` 取前文、后文或上下文；把它视为不透明值。`step` 不会重复上一轮已经引用过的句子。

`step` 在没有候选时返回 `no_match`；模型调用失败时抛 `botaloid_lite.ModelError`。

### 片段

`search()` 返回 `Passage`，`step()` 返回的 `fragments` 是 `Passage.to_dict()`。

```python
@dataclass(frozen=True)
class Passage:
    song_id: int
    title: str
    author: str
    start: int                 # 起始小句序号（不透明）
    end: int                   # 结束小句序号（不透明，含）
    text: str                  # 原文，多句用 \n 连接
    translation: str | None    # 对应译文，只包含有译文的行
    lines: tuple[LineSpan, ...]

    position: int              # 起始行的曲内行号（= lines[0].position）
    start_ms: int | None       # 片段开始时间（= 第一行的 start_ms）
    end_ms: int | None         # 片段结束时间（= 最后一行的 end_ms）

@dataclass(frozen=True)
class LineSpan:
    position: int
    text: str
    translation: str | None
    start_ms: int | None
    end_ms: int | None
```

`text` 与 `lines` 不一一对应：一个小句可能由多行合并而成（日文歌词常在句中换行），`lines` 给出真正的逐行时间，适合做逐句高亮。

### 设置

设置随每次调用传入，模块本身是无状态的。

```python
@dataclass(frozen=True)
class Settings:
    model: Callable | None = None            # 见「接入模型」
    plan_temperature: float = 0.2
    select_temperature: float = 0.55
    results: int = 3                         # 一次回复最多选几段
    phrases: int = 4                         # 每段最多几小句
```

## 接入模型

`step()` 需要模型时调用 `settings.model`，一次用户消息最多两次。

### 签名与返回值

```python
def call_model(system: str, prompt: str, temperature: float, schema: dict) -> dict:
    ...
```

| 参数 | 内容 |
| --- | --- |
| `system` | 系统提示词，等于 `botaloid_lite.prompts.PLAN` 或 `prompts.SELECT`，可用来判断阶段 |
| `prompt` | 已经拼好的用户消息，原样作为 user 消息发送 |
| `temperature` | 本阶段建议温度 |
| `schema` | 本阶段的 JSON Schema，等于 `prompts.PLAN_SCHEMA` 或 `prompts.SELECT_SCHEMA` |

返回**解析后的 `dict`**，也接受 JSON 字符串（模块会自行 `json.loads`）。返回非 dict 或解析失败时按 `{}` 处理，这一阶段按空结果继续。

`prompt` 的形状是固定的，适配器原样转发即可：

```
前文（只用于参考）：
用户：……
机器人：……
候选歌词：                      ← 仅 SELECT 阶段
候选 1（《曲名》 / 署名）：1) 第一句 / 2) 第二句
【当前用户】
用户这次说的话
【当前用户结束】
```

### PLAN 阶段

把对话变成检索词，以及用户明确点名的作者 / 曲名。

```json
{
  "queries": [["夜道", "夜里散步"], ["街灯", "路灯"]],
  "author": "",
  "title": ""
}
```

| 字段 | 要求 | 缺省时 |
| --- | --- | --- |
| `queries` | 最多 4 组、每组最多 3 个短词；模块会规范化、去重、丢弃少于 2 字的词 | 只用用户消息本身检索 |
| `author` | 字符串，最多 100 字符；只在用户点名作者时填 | 不按署名过滤 |
| `title` | 字符串，最多 100 字符；只在用户点名曲名时填 | 不按曲名过滤 |

`author` / `title` 是规范化后的子串匹配，所以 `いよわ` 能匹配 `いよわ / 初音ミク`。

### SELECT 阶段

从候选片段里挑出可以当回复的部分。

```json
{"choices": [{"id": 2, "count": 1}]}
```

| 字段 | 要求 |
| --- | --- |
| `id` | 候选编号，从 1 开始，必须是整数且落在候选范围内，否则这一项被丢弃 |
| `count` | 从该候选第 1 句起连续引用几小句，整数，会夹到 `1..Settings.phrases` |

同一首歌只取一次、同一个曲名最多出现一次、最多取 `Settings.results` 段；`choices` 为空或全部无效时这一轮返回 `status: "no_match"`。`id` 是候选编号，不是 `song_id`。

### 容错与错误

- 适配器抛出的异常会被包装成 `botaloid_lite.ModelError` 再抛出（`ModelError` 原样透传）。
- 模型返回的多余字段会被忽略；`choices` 里类型不对的项会被跳过。
- 推理模型会把思考过程算进输出 token。`max_tokens` 设得太小会让 JSON 被截断，截断后模块按 `{}` 处理，表现为 `no_match`。OpenAI 兼容接口可用 `reasoning_effort: "none"` 关闭思考。

### 完整示例：OpenAI 兼容接口

适用于 OpenAI、OpenRouter、DeepSeek、LM Studio、llama.cpp、vLLM 等。

```python
import json
import requests
from botaloid_lite import Library, ModelError, Settings

def call_model(system, prompt, temperature, schema):
    body = {
        "model": "qwen/qwen3.8-flash",
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
        "temperature": temperature,
        "reasoning_effort": "none",
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=body,
            headers={"Authorization": "Bearer YOUR_API_KEY"},
            timeout=(5, 120),
        )
        response.raise_for_status()
        return json.loads(response.json()["choices"][0]["message"]["content"])
    except (requests.RequestException, KeyError, IndexError, ValueError) as error:
        raise ModelError(str(error)) from error

library = Library("data/lyrics.sqlite3")
history = library.step([{"role": "user", "text": "周末想出去玩"}],
                       settings=Settings(model=call_model))
```

接口支持结构化输出时，可以把 `response_format` 换成 `json_schema`，或直接使用 `schema` 参数。

### 完整示例：Ollama

```python
import json
import requests
from botaloid_lite import ModelError, prompts

def call_model(system, prompt, temperature, schema):
    body = {
        "model": "qwen3:1.7b",
        "stream": False,
        "think": False,                  # 关闭思考，回复更快
        "format": schema,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
        "options": {"temperature": temperature,
                    "num_predict": 320 if system == prompts.PLAN else 220,
                    "num_ctx": 8192},
    }
    try:
        response = requests.post("http://127.0.0.1:11434/api/chat",
                                 json=body, timeout=(5, 75))
        response.raise_for_status()
        return json.loads(response.json()["message"]["content"])
    except (requests.RequestException, KeyError, ValueError) as error:
        raise ModelError(str(error)) from error
```

### 不接模型

`Settings()` 不传 `model` 时 `step()` 完全确定性：跳过 PLAN，用用户消息直接检索，跳过 SELECT，返回排序最靠前的 `results` 条片段。

```python
history = library.step([{"role": "user", "text": "夜里散步"}])
```

### 只做一次调用：`refine`

如果只想要一次模型调用，可以让检索照常进行，再用 `refine` 做语义重排。`refine` 收到的参数与 SELECT 阶段的候选完全一样，返回同样的 `{"choices": [...]}`：

```python
from botaloid_lite import prompts

def render(options):
    # options: [{"id": 1, "title": "曲名", "author": "署名", "phrases": ["第一句", "第二句"]}]
    lines = ["候选歌词："]
    for item in options:
        phrases = " / ".join(f"{i}) {text}" for i, text in enumerate(item["phrases"], 1))
        lines.append(f'候选 {item["id"]}（《{item["title"]}》 / {item["author"]}）：{phrases}')
    return "\n".join(lines)

def refine(options):
    return call_model(prompts.SELECT, render(options), 0.55, prompts.SELECT_SCHEMA)

passages = library.search("夜里散步", refine=refine, limit=3)
```

也可以跳过模型，把自己的检索词直接交给 `search`：

```python
passages = library.search(queries=[["夜道", "夜里散步"], ["街灯"]], limit=3)
```

## 媒体链接

`botaloid_lite.media` 把歌词元数据换成可播放的链接。它接受 `Passage`、`Passage.to_dict()`，或任何带 `title` / `author` / `start_ms` 的对象，也可以直接传字符串。

### 抽象层

| 名称 | 作用 |
| --- | --- |
| `LyricMeta` | `title`、`author`、`start_ms`；`LyricMeta.coerce(...)` 接受映射、对象或字符串 |
| `MediaLink` | `url`、`provider`、`service`、`start_seconds`、`video_id`、`title`，可 `to_dict()` |
| `Querier` | 协议：`name` 加 `find(meta) -> MediaLink | None` |
| `Resolver` | 按顺序尝试 querier，返回第一个结果；单个 provider 抛异常时跳过它 |
| `register` / `available` | 注册、列出可用的 querier |
| `resolve(meta, queriers=None)` | 一次性调用 |

`Resolver()` 使用所有已注册的 querier；`Resolver(["vocadb"])` 按名字选择，`Resolver([MyQuerier()])` 直接传实例。名字不存在会抛 `ValueError`。

新增一个服务只要实现 `find` 并注册：

```python
from botaloid_lite.media import MediaLink, register

class BilibiliQuerier:
    name = "bilibili"

    def find(self, meta):
        ...
        return MediaLink(url="https://www.bilibili.com/video/BV...?t=59",
                         provider="bilibili", service="bilibili",
                         start_seconds=meta.start_seconds)

register(BilibiliQuerier())
```

### VocaDB（YouTube）

`VocadbQuerier` 用 [VocaDB 公共 API](https://wiki.vocadb.net/docs/public-api) 找到曲目，取其 YouTube PV，把 `start_ms` 向下取整成秒，拼成 `https://www.youtube.com/watch?v=<video_id>&t=<seconds>`。没有 `start_ms` 时不加时间参数；没有可播放的 YouTube PV 时返回 `None`。

```python
from botaloid_lite.media import VocadbQuerier

querier = VocadbQuerier(max_results=10, cache_size=256, timeout=10)
link = querier.find({"title": "千本桜", "author": "黒うさP / 初音ミク", "start_ms": 32500})
print(link.url)   # https://www.youtube.com/watch?v=shs0rAiwsGQ&t=32
```

网络或响应错误会抛 `botaloid_lite.media.QuerierError`；通过 `Resolver` 调用时该 provider 会被跳过。测试时传入自己的 `fetch(url, timeout=...)` 即可离线运行：

```python
VocadbQuerier(fetch=lambda url, timeout=15: {"items": []})
```

## 实现与限制

### 检索流程

1. **小句**是检索单位：原行按句末标点和句中停顿切成小句，再合并被换行切断的句子；构建时写进 `phrases` 表。
2. **召回**用 FTS5：每个检索词拆成三字、双字 n-gram 做 OR 查询，按 `bm25` 排序；只有结果不足 8 条时才补单字查询，避免常用字把候选撑爆。
3. **重排**用 TF-IDF 余弦：idf 取自 `grams` 表（`ln((1+N)/(1+df))+1`），文档向量长度在构建时算好存进 `phrases.norm`，查询时只查检索词的 df。查询词的字面命中再加 0.18。
4. 结果按歌曲去重、每首最多 2 条，再向后合并小句成片段；`Passage` 的逐行信息按需从 `lyric_lines` 读取。

### 曲库结构

```sql
songs(id, title, author, duration_ms)
lyric_lines(id, song_id, position, stanza, text, translation, start_ms, end_ms)
phrases(id, song_id, idx, stanza, line_first, line_last, text, translation,
        text_key, start_ms, end_ms, norm)
lyric_fts(u, b, t, content='')      -- contentless FTS5，rowid = phrases.id
grams(gram, df)
```

`lyric_lines` 是显示的来源：`end_ms` 取下一行的开始时间，最后一行用 `duration_ms`；没有时间戳的行两列都是 `NULL`。`phrases` 是检索单位，`line_first` / `line_last` 指向它覆盖的行号范围。

### 资源占用

本曲库（4591 首、215988 小句）的实测数据：

| | |
| --- | --- |
| 曲库文件 | 197 MB（含索引） |
| 构建索引 | 约 25 秒，峰值内存约 245 MB |
| 运行时常驻内存 | 35–45 MB |
| `search()` 耗时 | 约 7 ms（冷启动首查约 40 ms） |
| 冷启动 | 无需建索引，打开即可用 |

内存由 SQLite 页缓存（`cache_size`）和曲目表构成，不随歌词数量增长。

### 其他行为与限制

- **词面召回**：候选由 n-gram 召回决定，只能命中与查询共享 n-gram 的句子；语义完全不同的改写可能召回不到。
- **快照**：`Library` 只读打开文件，重建曲库或索引后需要重新构造实例。
- **去重**：规范化后完全相同的句子只出现一次；同一个曲名一次回复最多出现一段。
- **不重复**：`step()` 会排除上一轮助手已经引用过的句子。
- **媒体匹配是启发式的**：VocaDB 可能没有对应条目，或同名曲目有多个版本；匹配不到时返回 `None`。

## 测试

```sh
.venv/bin/python -m unittest discover -s tests
```

覆盖：索引构建与短语切分、返回字段与逐行时间、作者 / 曲名过滤、四向导航、`exclude`、`refine`、确定性排序与两阶段 `step`、模型错误包装、缺失索引报错；媒体链接的元数据转换、VocaDB 匹配与 PV 选择、URL 与时间戳、缓存、provider 选择与失败隔离。

## 数据与版权

歌词和译文的权利归各自权利人所有；本项目的 MIT 许可证不包含对第三方歌词内容的授权。采集、导入和后续使用应符合相应授权、平台规则和适用法律。`tools/fetch_netease.py` 依赖平台接口，接口和可获取范围可能变化。
