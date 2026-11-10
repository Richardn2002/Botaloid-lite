# Botaloid-lite

用曲库里的**歌词原文**来回复：返回歌词、译文、在歌曲中的起止时间、曲名、署名和段内位置。

它是一个库，不是服务：没有配置持久化、没有 Web 界面、没有账号体系。
所有设置随调用传入，所有结果都是一个普通的 `dict` 或 dataclass。
只有三个入口：构造 `Library`、`search()`、`step()`。

- `search()` 纯检索，**从不调用模型**，约 0.1–0.2 秒。
- `step()` 接收聊天历史、返回新的聊天历史，调用两次模型：一次生成检索词，一次从候选里挑选。
  不传 `model` 时完全确定性，不调用任何模型。
- 每条结果都带原文、译文、`start_ms` / `end_ms`、逐行时间、曲名、署名、曲内行号。
- `botaloid_lite.media` 是独立的可选模块，把歌词元数据换成带起播时间的播放链接（先支持 VocaDB → YouTube），
  不依赖检索或模型。

```python
from botaloid_lite import Library, Settings

library = Library("data/lyrics.sqlite3")

for passage in library.search("夜里散步", limit=3):      # 不需要模型
    print(passage.title, passage.author, passage.start_ms, passage.text)

history = library.step([{"role": "user", "text": "唱点写夜里散步的歌"}],
                       settings=Settings(model=call_model))
print(history[-1]["fragments"])
```

`call_model` 的写法见[接入模型](#接入模型call_model)；不想接模型时直接省略 `settings`。

---

## 目录

- [安装](#安装)
- [准备曲库](#准备曲库)
- [曲库结构](#曲库结构)
- [API](#api)
  - [`Library(path)`](#librarypath)
  - [`search()`](#search)
  - [`step()`](#step)
  - [`Passage` 与 `LineSpan`](#passage-与-linespan)
  - [`Settings`](#settings)
- [媒体链接](#媒体链接)
- [接入模型：`call_model`](#接入模型call_model)
  - [签名与返回值](#签名与返回值)
  - [PLAN 阶段](#plan-阶段)
  - [SELECT 阶段](#select-阶段)
  - [容错与错误](#容错与错误)
  - [完整示例：OpenAI 兼容接口](#完整示例openai-兼容接口)
  - [完整示例：Ollama](#完整示例ollama)
  - [不接模型](#不接模型)
  - [只做一次调用：`refine`](#只做一次调用refine)
- [设计要点与限制](#设计要点与限制)
- [测试](#测试)
- [数据与版权](#数据与版权)

---

## 安装

需要 Python 3.11+。没有第三方运行时依赖；`pip install -e .` 只是为了把包装进环境。

```sh
python -m venv .venv
.venv/bin/python -m pip install -e .
```

Windows 下把 `.venv/bin/python` 换成 `.venv\Scripts\python`。

`tools/fetch_netease.py` 额外需要 `requests`：

```sh
.venv/bin/python -m pip install requests
```

## 准备曲库

两个脚本：一个下载原始数据，一个整理成曲库。先有原始数据时可以只跑第二个。

```sh
# 1. 从网易云歌单下载曲名、署名、歌词与已有译文（默认每秒 1 次请求，可中断续跑）
.venv/bin/python tools/fetch_netease.py 8285082830 --output data/raw.sqlite3

# 2. 合并重复版本、清理署名，写出带时间戳的曲库
.venv/bin/python tools/prepare.py data/raw.sqlite3 --output data/lyrics.sqlite3
```

- `fetch_netease.py` 只支持网易云，接受歌单链接、`#/playlist?id=...` 链接或数字 ID。
- `prepare.py` 会跳过纯音乐、无歌词和获取失败的曲目，按曲名、署名、原曲引用、
  歌词指纹和相似度去重，并在同名 `.tsv` 里写出清洗报告。
  `tools/cleaning_rules.json` 是人工修订规则，`--rules` 可换一份，`[]` 表示不用。
- `prepare.py` 写完曲库后会顺带构建检索索引（见下节），本曲库约 25 秒、200 MB。
- 两者都不覆盖已存在的输出文件。
- 也可以完全不用这两个脚本：任何符合下面表结构的 SQLite 文件都能被读取，再用
  `python -m botaloid_lite.build data/lyrics.sqlite3` 补建索引。

`data/` 已在 `.gitignore` 中排除，曲库文件不会进版本库。

## 曲库结构

```sql
CREATE TABLE songs (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL,
    duration_ms INTEGER
);
CREATE TABLE lyric_lines (
    id INTEGER PRIMARY KEY,
    song_id INTEGER NOT NULL REFERENCES songs(id),
    position INTEGER NOT NULL,       -- 曲内行号，从 1 开始
    stanza INTEGER NOT NULL,         -- 段落编号，按出现顺序不减
    text TEXT NOT NULL,              -- 原文，回复里显示的就是它
    translation TEXT,                -- 译文，只参与检索，不会被当成原文返回
    start_ms INTEGER,                -- 这一行在歌曲中的开始时间
    end_ms INTEGER,                  -- 这一行在歌曲中的结束时间
    UNIQUE(song_id, position)
);
```

时间以毫秒计，从歌曲开头算起。LRC 只标注开始时间，所以 `prepare.py` 用
**下一行的开始时间**作为这一行的 `end_ms`，最后一行用 `duration_ms`；
没有时间戳的行两列都是 `NULL`（本曲库里只有 19 行）。

### 检索索引

索引也放在同一个文件里，由 `prepare.py` 或 `python -m botaloid_lite.build <db>` 生成：

```sql
CREATE TABLE phrases (
    id INTEGER PRIMARY KEY, song_id INTEGER NOT NULL, idx INTEGER NOT NULL,
    stanza INTEGER NOT NULL, line_first INTEGER NOT NULL, line_last INTEGER NOT NULL,
    text TEXT NOT NULL, translation TEXT, text_key TEXT NOT NULL,
    start_ms INTEGER, end_ms INTEGER, norm REAL NOT NULL
);
CREATE VIRTUAL TABLE lyric_fts USING fts5(
    u, b, t, content='', tokenize='unicode61'
);
CREATE TABLE grams (gram TEXT PRIMARY KEY, df INTEGER NOT NULL);
```

- `phrases` 是检索单位：小句（必要时合并了句中换行），`line_first`/`line_last`
  指向它覆盖的 `lyric_lines.position` 范围。`norm` 是构建时算好的向量长度。
- `lyric_fts` 是 contentless FTS5 表，`rowid` 就是 `phrases.id`；三列分别是
  单字、双字、三字 n-gram。
- `grams` 是 n-gram 的文档频率，重排时用来算 idf。

本曲库（4591 首、215988 短语）的索引约 197 MB，构建约 25 秒、峰值内存约 250 MB。
索引是离线构建的，运行时只读。

## API

### `Library(path)`

```python
Library(db_path, *, cache_size=32, pool=40, weights=(1.0, 1.5, 2.0))
```

只读打开曲库（`mode=ro`）。**索引在磁盘上**，Python 只保留曲目表（本曲库约 2 MB）
和 SQLite 页缓存，因此内存不随歌词数量增长：本曲库常驻约 35–45 MB。

| 参数 | 作用 |
| --- | --- |
| `cache_size` | SQLite 页缓存，单位 MB，默认 32；小机器可以调到 8 |
| `pool` | 每个检索方向先取多少条 FTS 结果 |
| `weights` | `bm25` 的单字 / 双字 / 三字列权重 |

曲库在进程内是**快照**：重建曲库或索引后请重新构造 `Library`。
支持 `with Library(...) as library:`。没有索引的文件会抛 `MissingIndexError`。

### `search()`

```python
search(query=None, *, queries=None, author=None, title=None,
       source=None, direction="after", count=1, limit=5, phrases=4,
       exclude=(), refine=None) -> list[Passage]
```

纯检索，从不调用模型。

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| `query` | `None` | 自由文本，会作为一条检索词 |
| `queries` | `None` | 额外检索词，`[["夜道", "夜里散步"], ["街灯"]]`，最多 4 组、每组 3 个词 |
| `author` | `None` | 按署名限定，规范化后做子串匹配 |
| `title` | `None` | 按曲名限定；有完全同名时优先完全同名 |
| `source` | `None` | 从已有片段导航，而不是检索；直接用返回的 `passage.to_dict()["source"]` |
| `direction` | `"after"` | 导航方向：`at` 原片段、`before` 前文、`after` 后文、`around` 前后各 `count` 句 |
| `count` | `1` | 导航时扩展的句数 |
| `limit` | `5` | 最多返回几条片段 |
| `phrases` | `4` | 每条片段最多合并几小句（1–8，总长不超过 140 字） |
| `exclude` | `()` | 不要返回的歌词文本，例如上一轮的回复 |
| `refine` | `None` | 可选回调，见[只做一次调用](#只做一次调用refine) |

行为细节：

- 没有 `query` / `queries`、但给了 `author` 或 `title` 时，返回这些曲目的开头片段。
- 检索词会做规范化（全半角、大小写、标点不敏感），少于 2 个字符的词会被丢弃，
  用户消息本身始终作为一条检索词。
- 每个检索方向先走 FTS5：词拆成三字、双字 n-gram 做 OR 查询，按 `bm25` 排序；
  只有结果不足 8 条时才补单字查询，避免常用字把候选撑爆。
- 召回结果再用 TF-IDF 余弦重排：idf 来自 `grams` 表，文档向量长度在构建时算好；
  查询词的字面命中再加 0.18。
- 多个检索组之间轮流取，同一首歌最多 2 条，去重后最多 16 条候选。
- 候选会向后合并 1–4 小句，但要求同一段落、总长 ≤ 140 字、不与已有小句重复，
  且不以 `－` `—` `―` 开头。

```python
library.search("夜里散步")
library.search(author="いよわ", limit=10)
library.search(queries=[["夜道", "夜里散步"], ["街灯"]], limit=3)
library.search(source={"song_id": 12, "start": 4, "end": 4}, direction="after", count=2)
library.search(source={"song_id": 12, "start": 4, "end": 4}, direction="around", count=1)
```

### `step()`

```python
step(history, *, settings=None) -> list[dict]
```

`history` 是对话列表，每条为 `{"role": "user"|"assistant", "text": str}`，
**最后一条必须是用户消息**。返回**新的**列表（原列表不会被修改），末尾追加一条助手回复：

```jsonc
{
  "role": "assistant",
  "text": "静かな夜を歩いた\n君の声を聞かせて",
  "status": "selected",                     // selected / no_match
  "source": {"song_id": 1, "start": 0, "end": 1},
  "fragments": [ /* Passage.to_dict()，见下文 */ ]
}
```

| `status` | 含义 |
| --- | --- |
| `selected` | 模型（或确定性排序）从候选里选出了片段 |
| `no_match` | 没有可用片段，`text` 为空、`fragments` 为空 |

助手回复里的 `source` 是最后一段的定位符，可以交给 `search(source=..., direction=...)`
取它的前文、后文或上下文；`source` 视为不透明值，不要自己拼接。

```python
history = [{"role": "user", "text": "唱点写夜里散步的歌"}]
history = library.step(history, settings=Settings(model=call_model))
history = library.step(history + [{"role": "user", "text": "再来几句"}], settings=Settings(model=call_model))
```

`step` 在以下顺序里做决定：

1. `history` 非空且最后一条是用户消息，否则抛 `ValueError`。
2. 调用 PLAN 生成检索词（`model is None` 时跳过）。
3. 检索候选；调用 SELECT 挑选（`model is None` 时直接取排序最前的 `results` 条）。

### `Passage` 与 `LineSpan`

`search()` 返回 `Passage`，`step()` 返回的 `fragments` 是 `Passage.to_dict()`。

```python
@dataclass(frozen=True)
class Passage:
    # 字段
    song_id: int
    title: str
    author: str
    start: int                 # 起始小句序号（不透明）
    end: int                   # 结束小句序号（不透明，含）
    text: str                  # 原文，多句用 \n 连接
    translation: str | None    # 对应译文，多句用 \n 连接；只包含有译文的行，全无则 None
    lines: tuple[LineSpan, ...]

    # 计算属性
    position: int              # 起始行在曲内的行号，从 1 开始（= lines[0].position）
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

`Passage.to_dict()` 的形状：

```json
{
  "text": "静かな夜を歩いた\n君の声を聞かせて",
  "translation": "走过安静的夜\n让我听听你的声音",
  "title": "夜",
  "author": "作者甲",
  "song_id": 1,
  "position": 1,
  "start_ms": 1000,
  "end_ms": 5000,
  "source": {"song_id": 1, "start": 0, "end": 1},
  "lines": [
    {"position": 1, "text": "静かな夜を歩いた", "translation": "走过安静的夜",
     "start_ms": 1000, "end_ms": 3000},
    {"position": 2, "text": "君の声を聞かせて", "translation": "让我听听你的声音",
     "start_ms": 3000, "end_ms": 5000}
  ]
}
```

注意 `text` 与 `lines` 不是一一对应：小句可能由多行合并而成（日文歌词常在句中换行），
`lines` 给出的是真正的逐行时间，适合做逐句高亮或滚动。

### `Settings`

设置随每次调用传入，模块不保存任何东西。

```python
@dataclass(frozen=True)
class Settings:
    model: Callable | None = None            # 见下文 call_model
    plan_temperature: float = 0.2            # PLAN 阶段
    select_temperature: float = 0.55         # SELECT 阶段
    results: int = 3                         # 一次回复最多选几段
    phrases: int = 4                         # 每段最多几小句
```

## 媒体链接

`botaloid_lite.media` 把一段歌词元数据换成可以播放的链接。它不依赖 `Library`，
只用标准库；`Passage`、`Passage.to_dict()` 或任何带
`title` / `author` / `start_ms` 的对象都能直接传进去。

```python
from botaloid_lite import Library
from botaloid_lite.media import Resolver

library = Library("data/lyrics.sqlite3")
passage = library.search("夜里散步", limit=1)[0]

link = Resolver().find(passage)
print(link.url)             # https://www.youtube.com/watch?v=...&t=59
print(link.start_seconds)   # 59
```

### 抽象层

| 名称 | 作用 |
| --- | --- |
| `LyricMeta` | `title`、`author`、`start_ms`；`LyricMeta.coerce(...)` 接受映射、对象或字符串 |
| `MediaLink` | `url`、`provider`、`service`、`start_seconds`、`video_id`、`title`，可 `to_dict()` |
| `Querier` | 协议：`name` 加 `find(meta) -> MediaLink | None` |
| `Resolver` | 按顺序尝试 querier，返回第一个结果；单个 provider 抛异常时跳过它 |
| `register` / `available` | 注册、列出可用的 querier |
| `resolve(meta, queriers=None)` | 一次性调用 |

`Resolver()` 使用所有已注册的 querier；`Resolver(["vocadb"])` 按名字选择，
`Resolver([MyQuerier()])` 直接传实例。名字不存在会抛 `ValueError`。

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

`VocadbQuerier` 用 [VocaDB 公共 API](https://wiki.vocadb.net/docs/public-api) 找到曲目，
取其 YouTube PV，把 `start_ms` 向下取整成秒，拼成
`https://www.youtube.com/watch?v=<video_id>&t=<seconds>`。
没有 `start_ms` 时不加时间参数；没有可播放的 YouTube PV 时返回 `None`。

匹配规则：

- 先用去掉结尾括号（`(feat. …)`、`[Remix]` 等）的曲名搜索，没有结果再用完整曲名，最多两次请求。
- `sort=FavoritedTimes` 让经典版本排在前面；候选取名与曲名的相似度、署名命中数、
  是否 `Original` 综合打分，完全没有名字相似度的结果直接丢弃。
- YouTube PV 优先 `pvType=Original`，其次发布更早的；跳过 `disabled` 的和非 YouTube 的 PV。
- 响应按 URL 缓存（`cache_size`）；默认 10 条结果、`lang="Japanese"`，请求带自定义 User-Agent。
  请遵守 VocaDB 的请求频率要求，不要高频调用。

```python
from botaloid_lite.media import VocadbQuerier

querier = VocadbQuerier(max_results=10, cache_size=256, timeout=10)
link = querier.find({"title": "千本桜", "author": "黒うさP / 初音ミク", "start_ms": 32500})
print(link.url)   # https://www.youtube.com/watch?v=shs0rAiwsGQ&t=32
```

网络或响应错误会抛 `botaloid_lite.media.QuerierError`；通过 `Resolver` 调用时
该 provider 会被跳过，不影响其他 querier。测试时可以传入自己的
`fetch(url, timeout=...)`，不需要联网：

```python
VocadbQuerier(fetch=lambda url, timeout=15: {"items": []})
```

## 接入模型：`call_model`

`step()` 需要模型时调用 `settings.model`，一次用户消息最多两次。

### 签名与返回值

```python
def call_model(system: str, prompt: str, temperature: float, schema: dict) -> dict:
    ...
```

| 参数 | 内容 |
| --- | --- |
| `system` | 系统提示词原文，等于 `botaloid_lite.prompts.PLAN` 或 `prompts.SELECT`，可用来判断阶段 |
| `prompt` | 已经拼好的用户消息，直接作为 user 消息发送，不需要再加工 |
| `temperature` | 本阶段建议温度，默认 PLAN 0.2、SELECT 0.55 |
| `schema` | 本阶段的 JSON Schema，等于 `prompts.PLAN_SCHEMA` 或 `prompts.SELECT_SCHEMA`；支持结构化输出的接口可以直接使用，不支持的忽略即可 |

返回值：**解析后的 `dict`**。也接受 JSON 字符串（模块会自行 `json.loads`）。
返回非 dict、或 JSON 解析失败时按 `{}` 处理——不会抛异常，但这一阶段不会选出内容。

`prompt` 的形状是固定的，适配器不需要理解它，原样转发即可：

```
前文（只用于参考）：
用户：……
机器人：……
候选歌词：
候选 1（《曲名》 / 署名）：1) 第一句 / 2) 第二句
候选 2（《曲名》 / 署名）：1) ……
【当前用户】
用户这次说的话
【当前用户结束】
```

“候选歌词”只在 SELECT 阶段出现。

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

`author` / `title` 是**子串匹配**（规范化后），所以写 `いよわ` 能匹配
`いよわ / 初音ミク`；写成别的写法就匹配不到。

### SELECT 阶段

从候选片段里挑出可以当回复的部分。

```json
{"choices": [{"id": 2, "count": 1}]}
```

| 字段 | 要求 |
| --- | --- |
| `id` | 候选编号，从 1 开始，必须是整数且落在候选范围内，否则这一项被丢弃 |
| `count` | 从该候选的第 1 句起连续引用几小句，整数，会夹到 `1..Settings.phrases` |

模块会再做这些校验：同一首歌只取一次、最多取 `Settings.results` 段、
`choices` 为空或全部无效时这一轮返回 `status: "no_match"`。
`id` 是候选的顺序编号，不是 `song_id`。

### 容错与错误

- 适配器抛出的任何异常都会被包装成 `botaloid_lite.ModelError` 再抛出（`ModelError` 原样透传），
  调用方可以用它区分“模型不可用”和“没找到歌词”。
- 模型返回多出来的字段会被忽略；`choices` 里类型不对的项会被跳过，不会影响其他项。
- 推理模型会把思考过程算进输出 token。给 `max_tokens` 设太小会让 JSON 被截断，
  截断后模块按 `{}` 处理，表现为 `no_match`。

### 完整示例：OpenAI 兼容接口

适用于 OpenAI、DeepSeek、LM Studio、llama.cpp、vLLM 等。

```python
import json
import requests

from botaloid_lite import Library, ModelError, Settings

def call_model(system, prompt, temperature, schema):
    body = {
        "model": "your-model",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    try:
        response = requests.post(
            "https://your-endpoint/v1/chat/completions",
            json=body,
            headers={"Authorization": "Bearer YOUR_API_KEY"},
            timeout=(5, 60),
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except (requests.RequestException, KeyError, IndexError, ValueError) as error:
        raise ModelError(str(error)) from error

library = Library("data/lyrics.sqlite3")
history = library.step([{"role": "user", "text": "想听几首写夜里散步的歌"}],
                       settings=Settings(model=call_model))
```

如果接口支持结构化输出，可以在 `body` 里再加 `"response_format": {"type": "json_schema", ...}`，
或者直接用 `schema` 参数。这个示例用 `json_object` 是因为它兼容性最好。

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
        "format": schema,                # Ollama 原生结构化输出
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "options": {
            "temperature": temperature,
            "num_predict": 320 if system == prompts.PLAN else 220,
            "num_ctx": 8192,
        },
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

`Settings()` 不传 `model` 时 `step()` 完全确定性：跳过 PLAN，用用户消息直接检索，
跳过 SELECT，返回排序最靠前的 `results` 条片段。适合离线、测试或自己做重排。

```python
history = library.step([{"role": "user", "text": "夜里散步"}])
```

### 只做一次调用：`refine`

如果只想要一次模型调用（不要 PLAN），可以让检索照常进行，再用 `refine` 做语义重排。
`refine` 收到的参数与 SELECT 阶段的候选完全一样，返回同样的 `{"choices": [...]}`：

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

渲染格式由你决定，模块只要求返回 `{"choices": [{"id": ..., "count": ...}]}`。
也可以跳过模型，把自己的检索词直接交给 `search`：

```python
passages = library.search(queries=[["夜道", "夜里散步"], ["街灯"]], limit=3)
```

## 设计要点与限制

- **模型只挑编号，不写歌词。** 两个阶段都只产出检索词和候选编号，歌词永远来自曲库。
- **译文只参与检索。** 中文提问能命中日文原文，但返回的 `text` 一定是原文，`translation` 单独给出。
- **短语边界是统一的单位。** 检索、合并和前后跳转都以小句为最小单位，
  跨行合并会修复“日文歌词在句中换行”的情况。
- **索引在磁盘上。** 4591 首 / 215988 短语的索引约 197 MB，常驻内存约 35–45 MB；
  曲库变大只影响磁盘和构建时间，不影响每进程内存。
- **只读。** 运行时不会写入曲库，也不会落任何配置。
- **同名歌词去重。** 检索候选按规范化后的短语文本去重，两首歌里完全相同的句子只出现一次。
- **词形召回有上限。** 候选由 n-gram 召回决定，只能命中与查询共享 n-gram 的句子；
  这一点和 TF-IDF 一样是词面的，不是语义的。

## 测试

```sh
.venv/bin/python -m unittest discover -s tests
```

测试覆盖：索引构建与短语切分、返回字段与逐行时间、作者 / 曲名过滤、四向导航、
`exclude`、`refine`、确定性排序与两阶段 `step`、模型错误包装、缺失索引报错；
媒体链接的元数据转换、VocaDB 匹配与 PV 选择、URL 与时间戳、缓存、provider
选择与失败隔离。

## 数据与版权

歌词和译文的权利归各自权利人所有；本项目的 MIT 许可证不包含对第三方歌词内容的授权。
采集、导入和后续使用应符合相应授权、平台规则和适用法律。
`tools/fetch_netease.py` 依赖平台接口，接口和可获取范围可能变化。
