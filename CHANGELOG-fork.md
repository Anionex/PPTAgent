# Anionex Fork 变更报告

> 基于 `icip-cas/pptagent` 的 fork 改进，目标：让模板管线在真实 PPTX 模板上端到端可用。

## 一、PPTX 解析能力补全

**出发点**：真实模板比测试模板复杂得多——多个 slide master、嵌套 group shape、空布局名、图片翻转属性。原有解析器在这些情况下要么丢数据要么直接崩溃。

| Commit | 问题 | 解法 |
|--------|------|------|
| `ea6b4ee` | 只扫描第一个 slide master，多 master 模板解析出 0 个 slide | 遍历所有 slide master |
| `0abc67a` | 空布局名导致 `layout_mapping` KeyError | 自动命名为 `layout_N` |
| `37a51ff` | `shape_idx > 100` 的硬限制拒绝嵌套 group shape | 移除这个人为限制 |
| `9d070ac` | 嵌套 group 只做了一层坐标变换，深层子形状飞到左上角 | 递归仿射坐标变换 `_remap_descendants()` |
| `4907f66` | `fill.build()` 重建整个 `spPr` 元素，覆盖了刚写入的坐标 | 把坐标写入放到 `fill.build()` 之后 |
| `e6c3b14` | 图片裁剪属性写到 Image 对象而非 Shape 对象，静默无效 | 写到正确的 `shape` 上 |
| `7d15a6c` | `add_picture()` 创建新 XML 时丢失 `flipH/flipV` | 构建后显式回写翻转属性 |

**涉及文件**：`pptagent/presentation/presentation.py`, `pptagent/presentation/shapes.py`

## 二、环境兼容性

**出发点**：原始代码假设环境有 Docker、GPU、PyTorch、特定 API 格式，缺任何一个就崩溃。需要在无 GPU 的 macOS 和 API 代理（aihubmix）环境下工作。

| Commit | 问题 | 解法 |
|--------|------|------|
| `5b903d0` | Docker 不可用时 `sys.exit(1)` | 改为 warning，sandbox 工具不可用但不阻塞启动 |
| `8daeacd` `8f3cf0a` | 没有 PyTorch/ViT 时引导崩溃 | `image_model` 返回空列表；引导回退到每个内容 slide 独立成一个 layout |
| `9291a30` | API 代理返回 JSON 被 markdown 代码块包裹，`parse()` 失败 | 改用 `create()` + 手动剥离 markdown 包装 + Pydantic 验证 |
| `1342ee4` | 代理不强制 schema，模型返回 list 而非 dict、可选字段格式错误 | `_validate_with_coercion()` 做类型强制转换 |
| `f234cee` | Claude API 严格校验 MIME type，硬编码 `image/jpeg` 发送 PNG 时 400 | 根据实际文件类型设置 MIME |

**涉及文件**：`deeppresenter/agents/env.py`, `pptagent/llms.py`, `pptagent/model_utils.py`, `pptagent/induct.py`

## 三、CLI 与架构整理

**出发点**：pptagent 模板管线没有方便的入口。最初尝试通过 MCP agent loop 路由，但 LLM 中介引入了不必要的错误。需要一个直接、确定性的调用路径。

| Commit | 做了什么 |
|--------|---------|
| `8bf0207` | CLI 加 `--template/-t` 参数，指定模板名走 pptagent 管线 |
| `d872725` | `--template` 直接调用 `PPTAgent.generate_pres()` Python API，绕过 MCP agent loop |
| `45836ab` | 更新 CLAUDE.md 文档，明确两条管线的边界和 `--template` 的工作方式 |

**使用方式**：

```bash
# 自由生成（deeppresenter HTML 管线）
uv run pptagent generate "AI发展史"

# 模板生成（pptagent 模板管线，直接 Python API）
uv run pptagent generate "AI发展史" --template brand -o out.pptx
```

**涉及文件**：`deeppresenter/cli/commands.py`, `deeppresenter/main.py`

## 四、模板引导质量

**出发点**：引导（induction）是从原始 PPTX 提取结构化布局定义的过程。引导质量差 → 生成内容填错位置、用错格式。

| Commit | 问题 | 解法 |
|--------|------|------|
| `b607ed2` | `ContextVar` 存放文本池，并发 asyncio 任务全部读到最后一个池的值 | 改用闭包捕获的 Pydantic 动态子类，每个 slide 独立池 |
| `b607ed2` | 文本池混入图片 caption，`edit_distance` 匹配到错误内容 | 分离 `text_contents` 和 `image_contents` 两个独立池 |
| `eb0a216` | 编辑器 LLM 不知道文本框应该填什么格式（写"章节"而非"ONE"） | `get_schema()` 加入 `example: [data]` 展示模板原始内容 |

**涉及文件**：`pptagent/response/induct.py`, `pptagent/induct.py`, `pptagent/presentation/layout.py`

### ContextVar 竞态问题详解

原代码用 `ContextVar` 存放当前 slide 的文本池，`SlideElement.model_post_init` 从中读取做 `edit_distance` 匹配。但 `asyncio.gather` 并发执行多个 slide 的引导时，所有 task 共享同一个 `ContextVar`，最后设置的值覆盖前面的——所有 slide 都用了最后一个 slide 的文本池。

修复方案：不用 `ContextVar`，改为 `SlideSchema.response_model(text_fields, image_fields)` 返回一个闭包捕获了池的动态 Pydantic 子类。每次调用创建独立的类，从根源消除竞态。

## 五、文本溢出与布局保真

**出发点**：LLM 生成的文本长度不可控，经常超出文本框边界。需要后处理机制保证文字不溢出，同时保持模板的视觉效果（如大号装饰文字的重叠效果）。

这是迭代最多的部分，经历了三次修正：

### 迭代 1：初版 autofit (`b607ed2`)

估算每段文字所需高度，等比缩小字体（最小 0.5 倍）。

盲区：
- 跳过字体大小全部继承的形状（`run.font.size=None`）
- 硬编码 1.3 倍行高，实际模板可能是 2.0 倍

### 迭代 2：跳过装饰文字 (`339e747`)

发现装饰性大字（94pt "BUSINESS"）被缩到 47pt。原因：`SHAPE_TO_FIT_TEXT` 的设计意图是形状适配文字，不是缩小字体。于是跳过这类形状。

副作用：首页几乎所有文本框都是 `SHAPE_TO_FIT_TEXT`，全部被跳过，PowerPoint 打开后形状自动变大，互相重叠。

### 迭代 3：最终方案 (`95ea36b`)

三个核心改动：

1. **不跳过任何形状** — 全部做 autofit
2. **读取实际行距** — 从 `<a:lnSpc><a:spcPct>` 取真实值（200% 等），替代硬编码 1.3
3. **处理完后设 `auto_size=NONE`** — 锁定形状大小，PowerPoint 打开时不再自动调整

同时降低非 CJK 字符宽度系数 0.55→0.50，避免英文文本行数高估。

### 布局文字处理 (`e817634` → `e6b7909`)

master/layout 上的 TEXT_BOX（如"企业品牌宣传"标题、"01" 水印）在 pptagent 解析管线中不可见。

- 第一版：提升（复制到 slide 并从 layout 删除）→ 导致与 master 水印重叠
- 最终版：直接从 layout/master 删除（只删 >4 字符的，保留 "01" 等装饰数字）

**涉及文件**：`pptagent/presentation/presentation.py`

## 六、编辑 API 健壮性

**出发点**：LLM 生成的 API 调用代码通过 `eval()` 执行，各种边界情况会导致崩溃。

| Commit | 问题 | 解法 |
|--------|------|------|
| `b607ed2` | 中文引号 `\u201c` `\u201d` 在 `eval()` 中导致 SyntaxError | 捕获 SyntaxError 后自动转义内部引号重试 |
| `5d48298` | 空/损坏图片直接传给 `PIL.open()` 导致未处理异常 | 先验证文件存在且非空 |
| `a49f6b3` | `remove_item` 对不存在的元素抛 ValueError | 改为静默跳过（幂等删除） |
| `b91cdf4` | `set_reference()` 的 `pop()` 修改了共享数据 | 先 deepcopy |
| `b3a1db6` | `mcp_slide_validate` 遇到第一个缺失元素就崩溃 | 跳过已缺失元素，收集所有错误 |
| `2ef5cfe` | 编辑器为装饰性元素硬编造内容 | prompt 明确允许空数组 `[]` |

**涉及文件**：`pptagent/apis.py`, `pptagent/pptgen.py`, `pptagent/mcp_server.py`, `pptagent/presentation/layout.py`, `pptagent/roles/editor.yaml`

## 七、新模板资源

| Commit | 内容 |
|--------|------|
| `b112351` `3b28873` `40a31dc` `74bd6e6` | **讯飞竞赛模板**（xunfei）：7 个布局，21 个图片位 |
| `1269e67` | **企业品牌宣传模板**（brand）：20 页原始模板，19 个布局，ViT 聚类 |
| `158b692` | 引导脚本改为接受文件夹路径参数，`uv run python -m pptagent.scripts.template_induct pptagent/templates/xunfei` |

模板路径：`pptagent/templates/<name>/`，包含 `source.pptx`、`slide_induction.json`、`image_stats.json`。

## 统计

- 39 个 commit，77 个文件变更，+4084 / -169 行
- 解析层 7 fix、兼容层 5 fix、引导质量 3 fix、布局保真 6 fix/迭代、健壮性 6 fix
- 新增 2 个模板（xunfei、brand）
