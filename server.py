"""script-doctor-mcp —— 短视频脚本诊断 MCP Server。

运行方式：
    python server.py             # 以 stdio 方式启动 MCP Server，供 Cherry Studio / Claude Desktop 等客户端调用
    python server.py --selftest   # 离线自检：不联网，只验证 JSON 解析、容错与 Markdown 渲染逻辑

设计要点：
    1. 本地 stdio、无服务器、无状态：不写数据库、不落盘缓存，每次调用独立完成。
    2. 依赖极少：只依赖 mcp 官方 SDK；HTTP 用标准库 urllib，.env 用内置的极简解析器读取。
    3. 不崩：API 失败、返回非法 JSON、参数为空等情况，一律转成可读的 Markdown 提示返回。
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

import prompts

# ---------------------------------------------------------------------------
# 默认配置（都可以用环境变量覆盖，见 README「环境变量说明」）
# ---------------------------------------------------------------------------
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TIMEOUT = 60.0
MAX_TIMEOUT = 600.0

# 单条问题/建议的最大条数，避免模型刷屏
MAX_LIST_ITEMS = 6

mcp = FastMCP("script-doctor")


# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ApiConfig:
    """一次模型调用所需的配置快照。"""

    api_key: str
    base_url: str
    model: str
    timeout: float


class ModelCallError(Exception):
    """模型调用失败。

    message 是可以直接展示给用户的中文说明；detail 是给排查用的原始信息（HTTP 状态、响应片段等）。
    """

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


_DOTENV_LOADED = False


def _load_dotenv(path: Path) -> None:
    """极简 .env 读取器（避免引入 python-dotenv 依赖）。

    规则：按行解析 KEY=VALUE，忽略空行与 # 开头的注释，去掉值两侧的引号；
    已经存在的环境变量优先，不会被 .env 覆盖。任何读取异常都直接忽略。
    """
    if not path.is_file():
        return
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _first_env(*names: str) -> str:
    """按顺序取第一个非空环境变量。"""
    for name in names:
        value = os.environ.get(name, "")
        if value and value.strip():
            return value.strip()
    return ""


def _parse_timeout(raw: str) -> float:
    """解析超时秒数，非法值回退到默认值并被夹在 1~600 秒之间。"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    if value <= 0 or value != value:  # 包含 NaN
        return DEFAULT_TIMEOUT
    return min(max(value, 1.0), MAX_TIMEOUT)


def load_config() -> ApiConfig:
    """读取运行配置。每次调用时读取，保证进程内改环境变量也能生效，且保持无状态。"""
    global _DOTENV_LOADED
    if not _DOTENV_LOADED:
        _load_dotenv(Path(__file__).with_name(".env"))
        _DOTENV_LOADED = True

    return ApiConfig(
        api_key=_first_env("SCRIPT_DOCTOR_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"),
        base_url=_first_env("SCRIPT_DOCTOR_BASE_URL", "DEEPSEEK_BASE_URL", "OPENAI_BASE_URL")
        or DEFAULT_BASE_URL,
        model=_first_env("SCRIPT_DOCTOR_MODEL", "DEEPSEEK_MODEL", "OPENAI_MODEL") or DEFAULT_MODEL,
        timeout=_parse_timeout(os.environ.get("SCRIPT_DOCTOR_TIMEOUT", "")),
    )


def chat_completions_url(base_url: str) -> str:
    """把 base_url 补全成 chat/completions 接口地址。

    兼容三种写法：
        https://api.deepseek.com/v1      -> https://api.deepseek.com/v1/chat/completions
        https://api.deepseek.com         -> https://api.deepseek.com/v1/chat/completions
        https://xxx/v1/chat/completions  -> 原样使用
    """
    url = (base_url or "").strip().rstrip("/")
    if not url:
        url = DEFAULT_BASE_URL
    if url.endswith("/chat/completions"):
        return url
    if not re.search(r"/v\d+$", url):
        url = f"{url}/v1"
    return f"{url}/chat/completions"


def _json_mode_enabled(base_url: str) -> bool:
    """是否启用 OpenAI 兼容的 JSON 模式。

    默认只在 DeepSeek 官方地址上开启（该接口支持 response_format）；
    其他兼容网关可用 SCRIPT_DOCTOR_JSON_MODE=1/0 强制开关，避免个别网关不支持时直接 400。
    """
    flag = os.environ.get("SCRIPT_DOCTOR_JSON_MODE", "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return True
    if flag in {"0", "false", "no", "off"}:
        return False
    return "deepseek" in (base_url or "").lower()


# ---------------------------------------------------------------------------
# 调用模型
# ---------------------------------------------------------------------------
def _http_post_json(url: str, payload: dict[str, Any], api_key: str, timeout: float) -> str:
    """用标准库发一个 JSON POST 请求，返回响应文本。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "script-doctor-mcp/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 地址来自用户配置
        return response.read().decode("utf-8", errors="replace")


def _http_error_message(code: int) -> str:
    """把 HTTP 状态码翻译成用户能看懂的中文建议。"""
    mapping = {
        400: "模型拒绝了这次请求（HTTP 400）。常见原因是模型名不对，或该网关不支持 JSON 模式 —— 可设置 SCRIPT_DOCTOR_JSON_MODE=0 后重试。",
        401: "鉴权失败（HTTP 401）。请检查 SCRIPT_DOCTOR_API_KEY 是否正确、是否与 base_url 属于同一家服务商。",
        403: "没有访问权限（HTTP 403）。请确认该 API Key 已开通对应模型。",
        404: "接口地址不存在（HTTP 404）。请检查 SCRIPT_DOCTOR_BASE_URL，通常需要形如 https://api.deepseek.com/v1。",
        422: "请求参数不被接受（HTTP 422）。可尝试修改 SCRIPT_DOCTOR_MODEL 或关闭 JSON 模式。",
        429: "请求过于频繁或额度用尽（HTTP 429）。稍等片刻再试，或检查账户额度。",
    }
    if code in mapping:
        return mapping[code]
    if code == 402 or code == 4001:
        return "账户余额不足（HTTP 402）。请充值后再试。"
    if 500 <= code < 600:
        return f"模型服务端故障（HTTP {code}）。这不是脚本的问题，稍后重试即可。"
    return f"模型接口返回了异常状态码 HTTP {code}。"


def call_model(config: ApiConfig, messages: list[dict[str, str]]) -> str:
    """调用 OpenAI 兼容的 chat/completions 接口，返回模型输出的文本。

    所有异常都会被转换成 ModelCallError，保证调用方只需要处理一种错误类型。
    """
    if not config.api_key:
        raise ModelCallError(
            "没有找到 API Key，无法调用模型。",
            "请设置环境变量 SCRIPT_DOCTOR_API_KEY，或在项目目录下创建 .env 文件（可参考 .env.example）。",
        )

    payload: dict[str, Any] = {
        "model": config.model,
        "messages": messages,
        "temperature": 0.3,
        "stream": False,
    }
    if _json_mode_enabled(config.base_url):
        payload["response_format"] = {"type": "json_object"}

    url = chat_completions_url(config.base_url)

    try:
        raw = _http_post_json(url, payload, config.api_key, config.timeout)
    except urllib.error.HTTPError as err:
        # HTTPError 必须比 URLError 先捕获（它是 URLError 的子类）
        detail = ""
        try:
            detail = err.read().decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - 读取响应体失败时不影响主流程
            detail = ""
        raise ModelCallError(_http_error_message(int(getattr(err, "code", 0) or 0)), detail[:500]) from err
    except urllib.error.URLError as err:
        reason = getattr(err, "reason", err)
        message = f"无法连接模型接口（{url}）：{reason}。请检查网络、代理或 base_url 是否正确。"
        raise ModelCallError(message, f"{type(err).__name__}: {err}") from err
    except TimeoutError as err:
        raise ModelCallError(
            f"调用模型超时（超过 {config.timeout:.0f} 秒）。可以调大 SCRIPT_DOCTOR_TIMEOUT，或把脚本缩短后重试。",
            f"{type(err).__name__}: {err}",
        ) from err
    except OSError as err:
        raise ModelCallError(f"网络请求失败：{err}", f"{type(err).__name__}: {err}") from err

    # 解析外层响应体（OpenAI 兼容格式）
    try:
        envelope = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise ModelCallError(
            "模型接口返回的内容不是合法 JSON，无法解析。",
            raw[:500],
        ) from None

    if isinstance(envelope, dict) and envelope.get("error"):
        err_info = envelope["error"]
        text = err_info.get("message") if isinstance(err_info, dict) else str(err_info)
        raise ModelCallError(f"模型接口返回错误：{text}", json.dumps(err_info, ensure_ascii=False)[:500])

    try:
        choices = envelope["choices"]
        content = choices[0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ModelCallError(
            "模型返回结构不符合预期（缺少 choices[0].message.content）。",
            raw[:500],
        ) from None

    if not isinstance(content, str) or not content.strip():
        raise ModelCallError(
            "模型返回了空内容，可能是输入过长被截断或触发了安全策略。",
            raw[:500],
        )

    return content


# ---------------------------------------------------------------------------
# 容错解析：从模型输出里挖出 JSON
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


def _strip_code_fence(text: str) -> str:
    """如果模型用 ```json ... ``` 包住了结果，把代码块内容取出来。"""
    match = _FENCE_RE.search(text)
    if match and match.group(1).strip():
        return match.group(1).strip()
    return text.strip()


def _find_balanced_object(text: str) -> str:
    """从文本里找出第一个括号配平的 JSON 对象（忽略字符串里的花括号）。"""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        # 括号没配平，试试下一个 '{'
        start = text.find("{", start + 1)
    return ""


def _repair_json(text: str) -> str:
    """对常见的小瑕疵做修复：中文引号、尾随逗号、两头多余文字。"""
    fixed = (
        text.replace("“", '"')
        .replace("”", '"')
        .replace("‘", "'")
        .replace("’", "'")
    )
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)  # 去掉尾随逗号
    return fixed


def _unescape(raw: str) -> str:
    """把正则抓到的字符串片段还原成正常文本（处理 \\" \\n 等转义）。"""
    try:
        return json.loads(f'"{raw}"')
    except Exception:
        return raw.replace('\\"', '"').replace("\\n", " ").strip()


def _regex_extract(text: str) -> dict[str, Any] | None:
    """最后一道兜底：JSON 彻底解析失败时，用正则逐个字段捞。

    只捞到了部分字段也算成功 —— 有多少给多少，总比整段丢掉强。
    """
    result: dict[str, Any] = {}
    for key in ("hook_score", "density_score", "conversion_score"):
        # 允许 key 带引号或不带引号，允许半角/全角冒号
        match = re.search(rf'"?{key}"?\s*[:：]\s*"?(-?\d+(?:\.\d+)?)"?', text)
        if match:
            result[key] = match.group(1)

    for key in ("problems", "suggestions"):
        match = re.search(rf'"?{key}"?\s*[:：]\s*\[(.*?)\]', text, re.S)
        if match:
            items = re.findall(r'"((?:[^"\\]|\\.)*)"', match.group(1))
            if items:
                result[key] = [_unescape(item) for item in items]

    return result or None


def extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出中容错地提取 JSON 对象。

    依次尝试：原文 -> 去代码块 -> 括号配平截取 -> 小瑕疵修复 -> 正则逐字段兜底。
    全部失败返回 None，由调用方给出降级提示。
    """
    if not isinstance(text, str) or not text.strip():
        return None

    stripped = text.strip()
    fenced = _strip_code_fence(stripped)

    candidates: list[str] = []
    for source in (fenced, stripped):
        balanced = _find_balanced_object(source)
        if balanced:
            candidates.append(balanced)
        candidates.append(source)

    for candidate in candidates:
        for attempt in (candidate, _repair_json(candidate)):
            try:
                data = json.loads(attempt)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if isinstance(data, dict):
                return data
            # 少数模型会返回 [ {...} ] 这种数组包裹
            if isinstance(data, list) and data and isinstance(data[0], dict):
                return data[0]

    return _regex_extract(stripped)


# ---------------------------------------------------------------------------
# 结果归一化：不管模型给什么类型，都整理成稳定的结构
# ---------------------------------------------------------------------------
SCORE_KEYS = ("hook_score", "density_score", "conversion_score")


def _coerce_score(value: Any) -> float | None:
    """把模型的打分统一成 1~10 的数值；无法识别返回 None。

    兼容：8 / "8" / "8/10" / "8分" / 0.8（十分制小数） / 80（百分制）。
    """
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        if not match:
            return None
        number = float(match.group())
    else:
        return None

    if number != number or number in (float("inf"), float("-inf")):  # NaN / Inf
        return None

    if 0 < number < 1:
        number *= 10  # 0.8 -> 8
    elif 12 < number <= 100:
        number /= 10  # 80 -> 8（百分制）；10~12 视为轻微越界，交给下面的夹取处理
    number = min(max(number, 1.0), 10.0)

    # 整数值就返回整数，否则保留一位小数
    return float(round(number)) if abs(number - round(number)) < 0.05 else round(number, 1)


def _split_text_items(text: str) -> list[str]:
    """把模型返回的一整段字符串拆成条目列表。"""
    lines = re.split(r"[\r\n]+", text)
    items: list[str] = []
    for line in lines:
        cleaned = re.sub(r"^\s*(?:[-*•·]|\(?\d+[.)、]|[一二三四五六七八九十]+[、.)])\s*", "", line).strip()
        if cleaned:
            items.append(cleaned)
    return items or ([text.strip()] if text.strip() else [])


def _coerce_list(value: Any) -> list[str]:
    """把模型返回的任意类型整理成字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        items = _split_text_items(value)
    elif isinstance(value, (list, tuple)):
        items = []
        for item in value:
            if isinstance(item, dict):
                # 少数模型会返回 {"问题": "..."} 这种结构
                item = item.get("text") or item.get("content") or item.get("desc") or next(
                    (v for v in item.values() if isinstance(v, str)), ""
                )
            text = str(item).strip()
            if text:
                items.extend(_split_text_items(text) if "\n" in text else [text])
    elif isinstance(value, dict):
        items = [str(v).strip() for v in value.values() if str(v).strip()]
    else:
        items = [str(value).strip()]

    # 去重 + 限量，避免刷屏
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        item = re.sub(r"\s+", " ", item).strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result[:MAX_LIST_ITEMS]


def normalize_result(data: dict[str, Any]) -> dict[str, Any]:
    """把解析出来的字典整理成渲染层可以直接用的稳定结构。"""
    scores: dict[str, float | None] = {}
    for key in SCORE_KEYS:
        scores[key] = _coerce_score(data.get(key))

    known = sum(1 for value in scores.values() if value is not None)
    total = sum(value for value in scores.values() if value is not None)

    return {
        "scores": scores,
        "problems": _coerce_list(data.get("problems")),
        "suggestions": _coerce_list(data.get("suggestions")),
        "known_scores": known,
        "total": total,
        "average": (total / known) if known else None,
    }


# ---------------------------------------------------------------------------
# Markdown 渲染
# ---------------------------------------------------------------------------
def _grade(score: float | None) -> str:
    """把分数翻译成一句人话。"""
    if score is None:
        return "未评分"
    if score >= 9:
        return "优秀"
    if score >= 7:
        return "良好"
    if score >= 5:
        return "一般"
    if score >= 3:
        return "偏弱"
    return "严重不足"


def _format_score(score: float | None) -> str:
    """分数显示：整数不带小数点，小数保留一位。"""
    if score is None:
        return "—"
    return str(int(score)) if float(score).is_integer() else f"{score:.1f}"


def _bar(score: float | None, blocks: int = 10) -> str:
    """用方块画一个强度条。"""
    if score is None:
        return "░" * blocks
    filled = int(round(min(max(score, 0), 10) / 10 * blocks))
    return "█" * filled + "░" * (blocks - filled)


def _numbered(items: list[str], empty_hint: str) -> str:
    """渲染有序列表，空列表时给一句提示。"""
    if not items:
        return empty_hint
    return "\n".join(f"{index}. {item}" for index, item in enumerate(items, start=1))


def render_markdown(
    result: dict[str, Any],
    platform_key: str,
    script: str,
    *,
    extra_note: str = "",
) -> str:
    """把归一化后的诊断结果渲染成可读的 Markdown。"""
    label = prompts.platform_label(platform_key)
    lines: list[str] = ["## 🩺 脚本诊断报告", ""]

    lines.append(f"- **目标平台**：{label}")
    lines.append(f"- **脚本字数**：{len(script)} 字")

    known = result["known_scores"]
    if known == len(SCORE_KEYS):
        lines.append(f"- **综合得分**：{result['total']:g}/30（平均 {result['average']:.1f}/10，{_grade(result['average'])}）")
    elif known:
        lines.append(f"- **综合得分**：{result['total']:g}/{known * 10}（仅 {known} 项拿到了分数）")
    else:
        lines.append("- **综合得分**：模型没有返回可识别的分数")

    lines.append("")
    lines.append("### 评分总览")
    lines.append("")
    lines.append("| 维度 | 得分 | 强度 | 判断 |")
    lines.append("| :-- | :--: | :-- | :-- |")
    for key, dimension in prompts.SCORE_DIMENSIONS:
        score = result["scores"].get(key)
        lines.append(
            f"| {dimension} | {_format_score(score)}/10 | `{_bar(score)}` | {_grade(score)} |"
        )

    lines.append("")
    lines.append("### ⚠️ 主要问题")
    lines.append("")
    lines.append(_numbered(result["problems"], "（模型没有指出具体问题，可再跑一次或换用更强的模型）"))

    lines.append("")
    lines.append("### ✅ 改进建议")
    lines.append("")
    lines.append(_numbered(result["suggestions"], "（模型没有给出改进建议，可再跑一次或换用更强的模型）"))

    lines.append("")
    lines.append(f"> 评分口径：钩子强度看前 3 秒有没有冲突/悬念/利益点；信息密度看每 5 秒有没有新信息；"
                 f"转化自然度看引导话术是否与内容衔接。当前按 **{label}** 的偏好打分。")

    if extra_note:
        lines.append("")
        lines.append(extra_note)

    return "\n".join(lines)


def _warning_block(reason: str, detail: str = "") -> str:
    """统一的告警块。"""
    text = f"> ⚠️ **{reason}**"
    if detail:
        text += f"\n>\n> {detail}"
    return text


def render_setup_help() -> str:
    """未配置 API Key 时的引导信息。"""
    return "\n".join(
        [
            "## 🩺 脚本诊断报告",
            "",
            "> ⚠️ **还没有配置模型 API Key，暂时无法诊断脚本。**",
            "",
            "按下面任一种方式配置后重试即可（两种都不用改代码）：",
            "",
            "1. 设置环境变量（推荐写进 MCP 客户端配置的 `env` 里）：",
            "",
            "   ```",
            "   SCRIPT_DOCTOR_API_KEY=sk-你的key",
            "   SCRIPT_DOCTOR_BASE_URL=https://api.deepseek.com/v1",
            "   SCRIPT_DOCTOR_MODEL=deepseek-chat",
            "   ```",
            "",
            "2. 在项目目录下放一个 `.env` 文件（内容参考 `.env.example`）：",
            "",
            "   ```",
            "   SCRIPT_DOCTOR_API_KEY=sk-你的key",
            "   ```",
            "",
            "配置完成后重新启动 MCP Server（或重启客户端）再调用本工具。",
        ]
    )


def render_error(message: str, detail: str = "", config: ApiConfig | None = None) -> str:
    """调用失败时的友好提示（不抛异常，也不让 Server 崩）。"""
    lines = [
        "## 🩺 脚本诊断报告",
        "",
        f"> ❌ **诊断没有完成**：{message}",
        "",
        "可以这样排查：",
        "",
        "1. 确认 `SCRIPT_DOCTOR_API_KEY` 有效，且与 `SCRIPT_DOCTOR_BASE_URL` 属于同一家服务商。",
        "2. 确认 `SCRIPT_DOCTOR_BASE_URL` 形如 `https://api.deepseek.com/v1`（会自动补 `/chat/completions`）。",
        "3. 确认本机网络可以访问该接口（公司网络/代理常会拦截）。",
        "4. 如果是超时，调大 `SCRIPT_DOCTOR_TIMEOUT` 后重试，或把脚本拆短。",
    ]
    if config is not None:
        lines.append("")
        lines.append(f"> 当前配置：model=`{config.model}`，base_url=`{chat_completions_url(config.base_url)}`，"
                     f"timeout=`{config.timeout:.0f}s`。")
    if detail:
        lines.extend(["", "<details><summary>原始错误信息（排查用）</summary>", "", "```", detail, "```", "", "</details>"])
    return "\n".join(lines)


def render_degraded(raw_output: str, platform_key: str, script: str) -> str:
    """模型没有返回合法 JSON 时的降级输出：保留原文，并提示如何改进。"""
    result = {
        "scores": {key: None for key in SCORE_KEYS},
        "problems": [],
        "suggestions": [],
        "known_scores": 0,
        "total": 0,
        "average": None,
    }
    note = _warning_block(
        "模型这次没有按约定返回合法 JSON，无法解析出三项评分。",
        "这通常是模型没遵守格式，或输出被截断。重试一次，或把 `SCRIPT_DOCTOR_MODEL` 换成更强的模型（如 `deepseek-reasoner`）通常就好了。"
        "下面是模型的原始输出，内容本身仍然可以参考。",
    )
    markdown = render_markdown(result, platform_key, script, extra_note=note)
    return f"{markdown}\n\n### 📄 模型原始输出\n\n```text\n{raw_output.strip()}\n```"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run_diagnosis(script: str, platform: str = "douyin") -> str:
    """诊断主流程：校验参数 -> 构造提示词 -> 调用模型 -> 容错解析 -> 渲染 Markdown。

    任何情况下都返回字符串，不抛异常。
    """
    try:
        platform_key, platform_note = prompts.normalize_platform(platform)

        text = (script or "").strip()
        if not text:
            return "\n".join(
                [
                    "## 🩺 脚本诊断报告",
                    "",
                    "> ⚠️ **script 参数是空的，没法诊断。**",
                    "",
                    "请把要诊断的脚本原文（口播文案或分镜文案）填进 `script` 参数再试一次。",
                ]
            )

        config = load_config()
        if not config.api_key:
            return render_setup_help()

        messages = prompts.build_messages(text, platform_key)
        raw_output = call_model(config, messages)

        data = extract_json_object(raw_output)
        if data is None:
            markdown = render_degraded(raw_output, platform_key, text)
        else:
            result = normalize_result(data)
            note = ""
            if result["known_scores"] < len(SCORE_KEYS):
                note = _warning_block(
                    "模型返回的 JSON 缺少部分评分字段，已按能解析到的部分展示。",
                    "如果经常出现，建议换用 `deepseek-chat` 这类更稳定的模型。",
                )
            markdown = render_markdown(result, platform_key, text, extra_note=note)

        if platform_note:
            markdown += f"\n\n> ℹ️ {platform_note}"
        return markdown
    except ModelCallError as err:
        return render_error(err.message, err.detail, locals().get("config"))
    except Exception as err:  # 兜底：任何未预期异常都变成可读提示，绝不让 Server 崩
        return render_error(
            "诊断过程中出现了未预期的错误。",
            f"{type(err).__name__}: {err}",
            locals().get("config"),
        )


@mcp.tool()
def diagnose_script(script: str, platform: str = "douyin") -> str:
    """诊断一段短视频脚本，返回钩子强度、信息密度、转化自然度的评分和改进建议。

    Args:
        script: 脚本原文，必填。口播文案或分镜文案都可以，建议 30~3000 字。
        platform: 目标平台，可选 douyin / bilibili / xiaohongshu，默认 douyin。
    """
    return run_diagnosis(script, platform)


# ---------------------------------------------------------------------------
# 离线自检 & 入口
# ---------------------------------------------------------------------------
def _selftest() -> int:
    """离线自检：不联网，验证容错解析、归一化与渲染。返回进程退出码。"""
    cases: list[tuple[str, bool]] = []

    def check(name: str, condition: bool) -> None:
        cases.append((name, bool(condition)))

    # 1. 标准 JSON
    std = '{"hook_score": 7, "density_score": 6, "conversion_score": 8, "problems": ["开场是自我介绍"], "suggestions": ["把结论提到第一句"]}'
    check("解析标准 JSON", (extract_json_object(std) or {}).get("hook_score") == 7)

    # 2. 代码块包裹
    fenced = '好的，结果如下：\n```json\n{"hook_score": 9, "density_score": 9, "conversion_score": 9}\n```\n以上。'
    check("解析代码块 JSON", (extract_json_object(fenced) or {}).get("hook_score") == 9)

    # 3. 前后夹带解释文字
    noisy = '评分如下 {"hook_score": 5, "density_score": 4, "conversion_score": 3} 希望有帮助'
    check("解析夹带文字的 JSON", (extract_json_object(noisy) or {}).get("density_score") == 4)

    # 4. 尾随逗号 + 中文引号
    broken = '{“hook_score”: 6, “density_score”: 6, “conversion_score”: 6,}'
    check("修复尾随逗号/中文引号", (extract_json_object(broken) or {}).get("hook_score") == 6)

    # 5. 彻底不是 JSON -> 正则兜底
    salvaged = extract_json_object('hook_score: 8\ndensity_score: 7\nconversion_score: 6')
    check("非 JSON 文本正则兜底", (salvaged or {}).get("hook_score") == "8")

    # 6. 完全无信息 -> None
    check("无信息文本返回 None", extract_json_object("这次我不方便评分。") is None)

    # 7. 打分归一化
    normalized = normalize_result({"hook_score": "8/10", "density_score": 80, "conversion_score": 0.9})
    check(
        "打分归一化（字符串/百分制/小数）",
        normalized["scores"] == {"hook_score": 8.0, "density_score": 8.0, "conversion_score": 9.0},
    )

    # 8. 越界与非法值
    clamped = normalize_result({"hook_score": 99, "density_score": -3, "conversion_score": None})
    check(
        "分数越界与空值处理",
        clamped["scores"]["hook_score"] == 9.9
        and clamped["scores"]["density_score"] == 1.0
        and clamped["scores"]["conversion_score"] is None,
    )
    check("轻微越界夹取到 10", normalize_result({"hook_score": 11})["scores"]["hook_score"] == 10.0)

    # 9. 列表归一化（字符串 / 字典 / 编号行）
    lists = normalize_result({"problems": "- 开场太慢\n- 废话太多", "suggestions": {"a": "第一句给结论", "b": "删掉重复解释"}})
    check("列表归一化", lists["problems"] == ["开场太慢", "废话太多"] and lists["suggestions"] == ["第一句给结论", "删掉重复解释"])

    # 10. Markdown 渲染包含关键区块
    markdown = render_markdown(normalize_result(json.loads(std)), "douyin", "开场先自我介绍一下")
    check("Markdown 渲染三要素", all(text in markdown for text in ("评分总览", "主要问题", "改进建议", "钩子强度")))

    # 11. 平台归一化
    check("平台别名归一化", prompts.normalize_platform("B站")[0] == "bilibili" and prompts.normalize_platform("小红书")[0] == "xiaohongshu")
    check("未知平台回退抖音", prompts.normalize_platform("kuaishou")[0] == "douyin" and prompts.normalize_platform("kuaishou")[1] != "")

    # 12. 空脚本不崩
    check("空脚本友好提示", "script 参数是空的" in run_diagnosis("   ", "douyin"))

    # 13. 接口地址补全
    check(
        "base_url 补全",
        chat_completions_url("https://api.deepseek.com") == "https://api.deepseek.com/v1/chat/completions"
        and chat_completions_url("https://x/v1/") == "https://x/v1/chat/completions",
    )

    failed = [name for name, ok in cases if not ok]
    for name, ok in cases:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\n自检结果：{len(cases) - len(failed)}/{len(cases)} 通过")
    if failed:
        print("失败项：" + "、".join(failed))
        return 1
    print("全部通过：JSON 容错解析、打分归一化、Markdown 渲染、平台归一化均正常。")
    return 0


def main() -> None:
    """入口：默认以 stdio 方式启动 MCP Server。"""
    if "--selftest" in sys.argv[1:]:
        raise SystemExit(_selftest())
    # FastMCP 默认使用 stdio 传输，本地运行、无需服务器
    mcp.run()


if __name__ == "__main__":
    main()
