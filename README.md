# script-doctor-mcp

一个**短视频脚本诊断** MCP Server：把脚本原文丢给它，返回 **钩子强度 / 信息密度 / 转化自然度** 三项评分（1-10）、具体问题和可执行的改进建议。

本地 stdio 运行、无服务器、无状态、无数据库，依赖只有 `mcp` 官方 SDK 一个包。

> 典型用法：在 Cherry Studio / Claude Desktop / Cursor 等支持 MCP 的客户端里接入本服务，写脚本时直接说「帮我诊断这段脚本」。

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [评分标准](#评分标准)
- [项目结构](#项目结构)
- [环境要求](#环境要求)
- [安装](#安装)
- [配置环境变量](#配置环境变量)
- [接入 Cherry Studio](#接入-cherry-studio)
- [接入其他 MCP 客户端](#接入其他-mcp-客户端)
- [工具定义](#工具定义)
- [使用示例](#使用示例)
- [错误处理与容错](#错误处理与容错)
- [离线自检](#离线自检)
- [常见问题](#常见问题)
- [自定义诊断口径](#自定义诊断口径)
- [License](#license)

---

## 它解决什么问题

写完一条短视频脚本，最大的问题是**自己看不出毛病**：开场啰嗦、信息重复、广告插得生硬。

这个工具把「编导审片」的标准固化成三个可打分的维度，让模型逐条挑毛病并给出可落地的改法，输出是给人看的 Markdown 报告，而不是一段需要再解读的 JSON。

- 只做诊断，不改写全文，不替你写脚本
- 结果稳定可比较：同一套标准，跨脚本、跨版本对比分数
- 平台自适应：抖音 / B 站 / 小红书用不同的尺度打分

## 评分标准

三项各 1-10 分，评分标准写死在提示词里（见 [`prompts.py`](prompts.py)），保证每次打分口径一致。

| 维度 | 字段 | 看什么 |
| :-- | :-- | :-- |
| 钩子强度 | `hook_score` | 前 3 秒有没有冲突、悬念、利益点 |
| 信息密度 | `density_score` | 每 5 秒有没有新信息，有没有废话 |
| 转化自然度 | `conversion_score` | 引导关注/下单的话术是否生硬，是否和内容衔接 |

各档位的大致含义：

- **9-10**：几乎无可挑剔
- **6-8**：合格，但有明显可优化的地方
- **3-5**：问题突出，需要重写这一段
- **1-2**：基本没有做到

**平台差异**（会随 `platform` 参数写进提示词，影响打分尺度）：

| platform | 平台 | 侧重 |
| :-- | :-- | :-- |
| `douyin`（默认） | 抖音 | 重钩子：前 3 秒必须抓人，节奏快、口语化，铺垫越长越吃亏 |
| `bilibili` | B 站 | 重信息量：允许较长铺垫，但要持续给新信息、逻辑完整 |
| `xiaohongshu` | 小红书 | 重真实感：第一人称、像朋友安利，避免硬广腔和绝对化用词 |

## 项目结构

```
script-doctor-mcp/
├── server.py          # MCP Server 主文件：工具定义、模型调用、容错解析、Markdown 渲染
├── prompts.py         # 提示词模板：评分标准、平台差异、JSON 输出约束
├── requirements.txt   # 依赖清单（只有 mcp）
├── .env.example       # 环境变量示例，复制成 .env 使用
├── .gitignore
├── LICENSE            # MIT
└── README.md
```

## 环境要求

- Python **3.10+**（用到 `X | None` 类型标注；实测 3.12 正常）
- 一个 OpenAI 兼容的模型接口和 API Key（默认按 DeepSeek 配置）
- 可选：`uv`（比 pip 更快，Cherry Studio 的 `uv` 配置方式需要它）

## 安装

先拿到代码：

```bash
git clone https://github.com/<你的用户名>/script-doctor-mcp.git
cd script-doctor-mcp
```

### 方式 A：uv（推荐）

```bash
# 1. 创建虚拟环境
uv venv

# 2. 安装依赖
uv pip install -r requirements.txt

# 3. 启动（stdio 方式，供 MCP 客户端调用）
uv run server.py
```

### 方式 B：pip

```bash
# 1. 创建虚拟环境
python -m venv .venv

# 2. 激活虚拟环境
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
# Windows (cmd)
.venv\Scripts\activate.bat
# macOS / Linux
source .venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 启动
python server.py
```

> ⚠️ **`python server.py` 不是命令行工具，不会打印诊断结果。**
> 它是一个 stdio MCP Server：启动后会等待客户端通过标准输入发送 JSON-RPC 请求。
> 直接在终端里运行只会看到进程挂住不动，这是正常的 —— 请把它配置到 MCP 客户端里使用。
> 想在终端里验证代码逻辑，用后面的[离线自检](#离线自检)。

## 配置环境变量

复制示例文件，填上自己的 Key：

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

| 环境变量 | 必填 | 默认值 | 说明 |
| :-- | :--: | :-- | :-- |
| `SCRIPT_DOCTOR_API_KEY` | ✅ | 无 | 模型服务 API Key。缺失时工具不会崩，而是返回配置引导 |
| `SCRIPT_DOCTOR_BASE_URL` | | `https://api.deepseek.com/v1` | OpenAI 兼容接口地址。写 `https://api.deepseek.com` 也可以，会自动补 `/v1` 和 `/chat/completions` |
| `SCRIPT_DOCTOR_MODEL` | | `deepseek-chat` | 模型名 |
| `SCRIPT_DOCTOR_TIMEOUT` | | `60` | 单次请求超时（秒），有效范围 1~600 |
| `SCRIPT_DOCTOR_JSON_MODE` | | 自动 | `1` 强制开启 JSON 模式；`0` 关闭。留空时只在 DeepSeek 官方地址上自动开启 |

兼容的别名（设置了上面的变量就不用管）：`DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL`，以及 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`。

读取优先级：**进程环境变量 > `.env` 文件**。`.env` 只会补位，不会覆盖已存在的环境变量，所以把 Key 写在客户端的 `env` 里也完全可行。

### 换成其他模型服务

任何 OpenAI 兼容接口都可以，改两个变量即可：

```bash
# 硅基流动
SCRIPT_DOCTOR_BASE_URL=https://api.siliconflow.cn/v1
SCRIPT_DOCTOR_MODEL=deepseek-ai/DeepSeek-V3
SCRIPT_DOCTOR_API_KEY=sk-xxx
```

## 接入 Cherry Studio

**设置 → MCP 服务器 → 添加服务器 → 从 JSON 导入**，粘贴下面的配置。

### 配置 1：uv 方式（先按上文装好依赖）

```json
{
  "mcpServers": {
    "script-doctor": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "D:/path/to/script-doctor-mcp",
        "server.py"
      ],
      "env": {
        "SCRIPT_DOCTOR_API_KEY": "sk-你的key",
        "SCRIPT_DOCTOR_BASE_URL": "https://api.deepseek.com/v1",
        "SCRIPT_DOCTOR_MODEL": "deepseek-chat"
      }
    }
  }
}
```

### 配置 2：python 方式（最稳，不依赖 uv）

把 `command` 指向虚拟环境里的 python，Windows 用反斜杠转义或正斜杠都可以：

```json
{
  "mcpServers": {
    "script-doctor": {
      "command": "D:/path/to/script-doctor-mcp/.venv/Scripts/python.exe",
      "args": [
        "D:/path/to/script-doctor-mcp/server.py"
      ],
      "env": {
        "SCRIPT_DOCTOR_API_KEY": "sk-你的key",
        "SCRIPT_DOCTOR_BASE_URL": "https://api.deepseek.com/v1",
        "SCRIPT_DOCTOR_MODEL": "deepseek-chat"
      }
    }
  }
}
```

macOS / Linux 把路径换成：

```json
{
  "mcpServers": {
    "script-doctor": {
      "command": "/path/to/script-doctor-mcp/.venv/bin/python",
      "args": ["/path/to/script-doctor-mcp/server.py"],
      "env": {
        "SCRIPT_DOCTOR_API_KEY": "sk-你的key",
        "SCRIPT_DOCTOR_BASE_URL": "https://api.deepseek.com/v1",
        "SCRIPT_DOCTOR_MODEL": "deepseek-chat"
      }
    }
  }
}
```

几点说明：

- **路径要写绝对路径**，或相对于客户端的工作目录；`D:/path/to/...` 请替换成你机器上的真实路径。
- 保存后在 MCP 服务器列表里应该能看到 `script-doctor`，并带有 `diagnose_script` 工具；如果显示红色，点开看错误日志，多半是路径或依赖没装。
- Key 放在 `env` 里和放在 `.env` 里效果一样，`env` 优先级更高。**不要把真实 Key 提交到 GitHub**（`.env` 已被 `.gitignore` 忽略）。
- 想省掉 `env`，也可以提前在项目目录建好 `.env`，配置里只写 `"env": {}`。

## 接入其他 MCP 客户端

Claude Desktop（`claude_desktop_config.json`）、Cursor、Cline 等用的都是同一套 `mcpServers` 格式，把上面的 JSON 复制进对应配置文件的 `mcpServers` 节点即可。区别只是：

- Claude Desktop 需要**完全退出后重启**才会加载新的 MCP 配置
- Cursor 在 **Settings → MCP** 里配置，改完点刷新

## 工具定义

本服务只暴露一个工具：

### `diagnose_script`

| 参数 | 类型 | 必填 | 默认 | 说明 |
| :-- | :-- | :--: | :-- | :-- |
| `script` | string | ✅ | — | 脚本原文。口播文案或分镜文案均可，建议 30~3000 字 |
| `platform` | string | | `douyin` | 目标平台，可选 `douyin` / `bilibili` / `xiaohongshu`；也认「抖音」「B站」「小红书」等写法 |

返回：一段 Markdown 报告（字符串）。

内部流程：接收参数 → 构造提示词（脚本 + 评分标准 + 平台侧重）→ 调用模型 → 要求返回固定 JSON → 容错解析 → 格式化成 Markdown。

模型被要求的输出格式：

```json
{
  "hook_score": 1,
  "density_score": 1,
  "conversion_score": 1,
  "problems": ["具体问题1", "具体问题2"],
  "suggestions": ["改进建议1", "改进建议2"]
}
```

## 使用示例

### 输入

在客户端里这样说：

> 用 script-doctor 诊断一下这段抖音脚本：
>
> 大家好，我是小李，一个在护肤坑里爬了五年的普通人。今天想跟大家聊聊洗面奶到底应该怎么选。
> 第一点，看成分表，氨基酸表活的相对温和。第二点，看肤质，油皮可以适当用清洁力强一点的。
> 第三点，不要迷信大牌，很多平价氨基酸洗面奶成分很干净。我自己现在用的是氨基酸洁面，一支不到五十块。
> 如果这期对你有帮助，记得点个关注，我们下期见。

对应工具调用：

```json
{
  "name": "diagnose_script",
  "arguments": {
    "script": "大家好，我是小李……我们下期见。",
    "platform": "douyin"
  }
}
```

### 返回（Markdown 报告）

```markdown
## 🩺 脚本诊断报告

- **目标平台**：抖音
- **脚本字数**：162 字
- **综合得分**：17/30（平均 5.7/10，一般）

### 评分总览

| 维度 | 得分 | 强度 | 判断 |
| :-- | :--: | :-- | :-- |
| 钩子强度 | 4/10 | `████░░░░░░` | 偏弱 |
| 信息密度 | 6/10 | `██████░░░░` | 一般 |
| 转化自然度 | 7/10 | `███████░░░` | 良好 |

### ⚠️ 主要问题

1. 开场两句是自我介绍加预告（"我是小李……今天想跟大家聊聊"），前 3 秒没有冲突、悬念或利益点，是典型的无效开场。
2. 三点建议是并列罗列，每点只有一句结论、没有细节，第 2 点"看肤质"和第 3 点"不要迷信大牌"其实都在说"别只看牌子"，信息重复。
3. 结尾"记得点个关注，我们下期见"与前面的内容没有衔接，任何一条视频都能套用，观众没有关注的动机。

### ✅ 改进建议

1. 把结论提到第一句，例如："洗面奶别乱买，我踩了五年坑才发现，看这三点就够了。"用"五年坑"制造反差，直接给出利益点。
2. 三点各补一个具体细节或数字，例如把"氨基酸表活相对温和"改成"成分表里出现'氨基酸'字样的，pH 一般在 5.5 左右，洗完不紧绷"，避免并列空话。
3. 把第 2、3 点合并成"看肤质定清洁力，看成分表避开皂基"，从三点压缩成两点，腾出的时间给细节。
4. 关注引导改成和内容绑定的说法，例如："想要我整理好的平价氨基酸洁面清单，评论区扣'清单'，我私发你。"用具体收益换关注。

> 评分口径：钩子强度看前 3 秒有没有冲突/悬念/利益点；信息密度看每 5 秒有没有新信息；转化自然度看引导话术是否与内容衔接。当前按 **抖音** 的偏好打分。
```

> 上面的分数和文字是示例，实际结果由模型给出。

## 错误处理与容错

这个 Server 的设计目标是**任何情况下都不崩、不返回堆栈**，所有失败都会变成一段能看懂的 Markdown。

| 情况 | 表现 |
| :-- | :-- |
| 没配置 API Key | 返回配置引导（两种配置方式 + 示例），不报错 |
| `script` 为空 | 返回「script 参数是空的，没法诊断」并说明怎么填 |
| 平台名不认识（如 `kuaishou`） | 按抖音标准诊断，并在报告末尾提示「未识别的平台，已按抖音标准诊断」 |
| 401 / 403 | 提示检查 API Key，以及 Key 与 base_url 是否属于同一家服务商 |
| 404 | 提示检查 base_url，通常需要形如 `https://api.deepseek.com/v1` |
| 400 / 422 | 提示可能是模型名不对或网关不支持 JSON 模式，建议 `SCRIPT_DOCTOR_JSON_MODE=0` |
| 429 / 余额不足 | 提示频率或额度问题 |
| 5xx | 提示是模型服务端故障，不是脚本的问题，稍后重试 |
| 网络不通 / 超时 | 提示检查网络、代理、base_url，或调大 `SCRIPT_DOCTOR_TIMEOUT` |
| 模型返回非法 JSON | 见下 |

**非法 JSON 的容错是分层的**，模型输出再野也尽量榨出分数：

1. 直接 `json.loads`
2. 去掉 ` ```json ` 代码块围栏再解析
3. 用括号配平扫描，从一堆解释文字里截出 JSON 对象
4. 修复常见瑕疵：中文引号 `“ ”`、尾随逗号、两端多余文字
5. 用正则逐个字段兜底捞 `hook_score` / `problems` 等
6. 全都失败 → 输出降级报告：明确说明「模型没按格式返回」，并**附上模型原始输出**，同时给出重试/换模型的建议

分数本身也做了归一化，兼容模型的各种偷懒写法：

| 模型返回 | 归一化结果 |
| :-- | :-- |
| `8` | 8 |
| `"8/10"`、`"8分"` | 8 |
| `0.8` | 8 |
| `80` | 8 |
| `11` | 10（轻微越界直接夹取） |
| `-3` | 1 |
| `null` / 缺字段 / 文字 | 显示 `—`，并在报告里提示缺字段 |

## 离线自检

不想配 Key，只想确认代码本身没坏：

```bash
python server.py --selftest
```

它会跑 15 项离线检查（不联网）：JSON 容错解析、分数归一化、列表归一化、Markdown 渲染、平台别名、base_url 补全、空输入处理，输出形如：

```
[PASS] 解析标准 JSON
[PASS] 解析代码块 JSON
...
自检结果：15/15 通过
全部通过：JSON 容错解析、打分归一化、Markdown 渲染、平台归一化均正常。
```

## 常见问题

**Q：客户端里工具调用失败 / 看不到 `diagnose_script`？**
A：先单独验证服务能不能起来：`python server.py --selftest`。能通过说明代码没问题，再检查客户端配置的 `command` 路径是否为绝对路径、虚拟环境里是否装了 `mcp`。

**Q：在终端运行 `python server.py` 一片空白，是坏了吗？**
A：没坏。stdio MCP Server 在等客户端发请求，这是预期行为。要手动体验，请用支持 MCP 的客户端，或自己写 JSON-RPC 请求走标准输入。

**Q：报 `SSL: CERTIFICATE_VERIFY_FAILED` 或连不上接口？**
A：公司网络、代理或证书问题是常见原因。确认 `SCRIPT_DOCTOR_BASE_URL` 能在浏览器/curl 里访问通，必要时配置系统代理。

**Q：分数每次都不一样？**
A：模型有随机性。`temperature` 已固定为 0.3，仍会有小幅波动；同一份脚本多跑两次取平均更稳。要更稳定的判断可以换更强的模型。

**Q：脚本很长会怎么样？**
A：不做截断，整段发给模型。但超长脚本可能撞上模型的上下文上限并返回空内容，此时工具会提示「输入过长被截断」——建议把脚本拆成分段分别诊断。

**Q：支持流式输出吗？**
A：MCP 工具调用返回的是一次性结果，不走流式；`stream` 固定为 `false`。

## 自定义诊断口径

改 [`prompts.py`](prompts.py) 即可，不用动 `server.py`：

- `SYSTEM_PROMPT`：评分标准、档位描述、输出格式要求
- `PLATFORMS`：每个平台的侧重说明和别名；新增平台只要加一个键，并同步更新工具参数说明
- `SCORE_DIMENSIONS`：评分维度的字段名和中文名（改字段名时记得同步 `server.py` 里的 `SCORE_KEYS`）

改完记得跑一遍 `python server.py --selftest`。

## License

[MIT](LICENSE)
