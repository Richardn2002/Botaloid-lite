# Botaloid-lite

用曲库里的歌词原文来聊天：返回歌词、译文、在歌曲中的起止时间、曲名、署名和段内位置。
没有设置页面、没有持久化配置、没有 Web 服务，只有三个入口：`init`、`search`、`step`。

```python
from botaloid_lite import Library, Settings

library = Library("data/lyrics.sqlite3")

library.search("夜里散步", limit=3)          # 不调用模型，直接检索
library.step(history, settings=Settings())   # 追加一条助手回复，返回新的 history
```

## 安装

需要 Python 3.11+。

```sh
python -m venv .venv
.venv/bin/python -m pip install -e .
```

## 准备曲库

两个脚本，先取原始数据，再整理成曲库：

```sh
python -m pip install requests
python tools/fetch_netease.py 8285082830 --output data/raw.sqlite3
python tools/prepare.py data/raw.sqlite3 --output data/lyrics.sqlite3
```

- `tools/fetch_netease.py` 只支持网易云歌单，默认每秒 1 次请求，中断后重跑会继续。
- `tools/prepare.py` 合并重复版本、清理署名，并按 LRC 时间戳补上每行的起止毫秒数。
  报告写到同名 `.tsv`，`tools/cleaning_rules.json` 是人工修订规则。
- 两者都不覆盖已有输出文件。`data/` 已被 `.gitignore` 排除。

曲库结构：

```sql
songs(id, title, author, duration_ms)
lyric_lines(id, song_id, position, stanza, text, translation, start_ms, end_ms)
```

`translation` 只参与检索，不会被当成原文返回。LRC 只标注开始时间，所以每行的
`end_ms` 取下一行的开始时间；最后一行取歌曲时长。

## 三个入口

### `Library(path)`

只读打开曲库，把歌词读进内存，首次检索时建立字符 n-gram TF-IDF 索引。
曲库在进程内视为快照：重建曲库后需要重新构造 `Library`。

### `search(...)`

```python
search(query=None, *, queries=None, author=None, title=None,
       source=None, direction="after", count=1, limit=5, phrases=4,
       exclude=(), refine=None) -> list[Passage]
```

从不调用模型。

| 参数 | 作用 |
| --- | --- |
| `query` | 自由文本，会作为一条检索词 |
| `queries` | 额外的检索词组，`[["夜道", "夜里散步"], ["街灯"]]`，最多 4 组、每组 3 个词 |
| `author` / `title` | 按署名、曲名限定，规范化后做子串匹配；没有检索词时返回该范围内的开头片段 |
| `source` | 从已有片段导航而不是检索，配合 `direction` 使用 |
| `direction` | `at` 原片段、`before` 前文、`after` 后文、`around` 前后各 `count` 句 |
| `count` | 导航时扩展的句数 |
| `limit` | 最多返回几条 |
| `phrases` | 每条最多合并几小句（默认 4，总长不超过 140 字） |
| `exclude` | 不要返回的歌词文本，例如上一轮的回复 |
| `refine` | 可选回调 `refine(options) -> {"choices": [...]}`，用于语义重排，契约与 SELECT 阶段相同 |

### `step(history, settings=...)`

```python
step(history, *, settings=None) -> list[dict]
```

`history` 是 `{"role": "user"|"assistant", "text": str}` 组成的列表，最后一条必须是用户消息。
返回**新的**列表，原列表不会被修改。助手回复形如：

```python
{
  "role": "assistant",
  "text": "静かな夜を歩いた\n君の声を聞かせて",
  "status": "continued",          # continued / selected / no_match / ended
  "source": {"song_id": 1, "start": 0, "end": 1},
  "fragments": [
    {
      "text": "静かな夜を歩いた\n君の声を聞かせて",
      "translation": "走过安静的夜\n让我听听你的声音",
      "title": "夜", "author": "作者甲", "song_id": 1,
      "position": 1,               # 起始行在曲内的行号
      "start_ms": 1000, "end_ms": 5000,
      "source": {"song_id": 1, "start": 0, "end": 1},
      "lines": [
        {"position": 1, "text": "静かな夜を歩いた", "translation": "走过安静的夜",
         "start_ms": 1000, "end_ms": 3000},
        {"position": 2, "text": "君の声を聞かせて", "translation": "让我听听你的声音",
         "start_ms": 3000, "end_ms": 5000}
      ]
    }
  ]
}
```

`source` 是不透明的定位符，原样传回即可继续接龙或前后跳转。
把助手回复原样放回 `history`，下一轮就能从这句歌词之后接下去。

## 接龙不需要模型

如果最新一条用户消息本身就是整句歌词（规范化后与曲库一致、落在短语边界上），
`step` 直接返回下一句，不调用任何模型。前提是这句歌词在曲库里只有一种后续；
有歧义时退回模型挑选。用 `Settings(allow_continue=False)` 或自定义判断函数关闭：

```python
Settings(allow_continue=lambda history: False)
```

## 两个模型阶段

其余情况下 `step` 调用两次 `settings.model`：

1. **PLAN**：把对话变成检索词，以及用户明确点名的作者/曲名。
2. **SELECT**：从检索出的候选片段里挑 `{id, count}`，`count` 是连续引用几小句。

`settings.model` 的签名与原来一致：

```python
def call_model(system: str, prompt: str, temperature: float, schema: dict) -> dict:
    ...
```

`system` 是 `botaloid_lite.prompts.PLAN` 或 `SELECT`，`schema` 是给支持结构化输出的
接口用的 JSON Schema。返回解析后的 `dict`（也接受 JSON 字符串）。抛异常会变成
`botaloid_lite.ModelError`。

不传 `model` 时 `step` 完全确定性：直接返回检索排序最靠前的片段。

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class Settings:
    model: Callable | None = None
    plan_temperature: float = 0.2
    select_temperature: float = 0.55
    results: int = 3          # SELECT 最多选几段
    phrases: int = 4          # 每段最多几小句
    allow_continue: bool | Callable[[list[dict]], bool] = True
```

设置随调用传入，模块不保存任何东西。

## 和 Botaloid 的区别

保留：字符 n-gram 检索、译文并入索引、短语边界与软换行合并、候选交错与去重、
原文重读校验、精确接龙、PLAN/SELECT 两阶段。

去掉：15 种语气、5 种意图分类、曲库语言配置、历史引用编号与前后句解析、
推荐去重、兜底歌词、"这句在讨论还是接龙" 的正则判断（改由 `allow_continue` 决定）、
设置文件与系统凭据、Web 管理与预览页面。

## 测试

```sh
python -m unittest discover -s tests
```
