"""orchestrator.core.llm
统一 LLMClient：OpenAI / Anthropic 兼容，密钥一律走环境变量；仅用标准库 urllib。

config.yaml llm 段（本批基础段）：
    llm:
      strong: {model: gpt-5,      base_url: env:OPENAI_BASE_URL, api_key: env:OPENAI_API_KEY}
      fast:   {model: gpt-5-mini, base_url: env:OPENAI_BASE_URL, api_key: env:OPENAI_API_KEY}

约定：
- `env:VAR` 前缀 → 运行时从环境变量解析；未设置则抛 LLMError 清晰提示。
- provider 判定：spec 显式 provider > 模型名以 claude 开头 → anthropic > 其余 openai。
- model 设为 `mock` → 离线确定性桩（无 key/无网跑通流水线，验收批用此）。

计费（审计 R3，预算门真实化）：
- 构造时可注入 on_cost 回调（callable(dict)->None）；每次 chat 后记账一笔：
  {"tier", "llm_calls": 1, "tokens", "usd", "estimated"}。
- 真实 provider 解析响应 usage（openai: prompt/completion_tokens；
  anthropic: input/output_tokens）；兼容端点缺 usage 时按字符估算（~4 字符/token）
  并打 [billing] 警告，**不静默按 0 计**；mock 走同一记账函数（估算口径、不告警）。
- 单价取 llm.<tier>.price_per_1k_in / price_per_1k_out（USD/1k token，缺省 0）。
- 无全局状态：并发安全由调用方各自传入回调（审计 R6/R7 需要）。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from typing import Any, Callable


class LLMError(RuntimeError):
    """LLM 调用失败（缺密钥、HTTP 错误、响应解析失败等）。"""


def _resolve_ref(value: Any, *, required_for: str) -> str | None:
    """解析 `env:VAR` 引用或字面量；env 未设置返回 None。"""
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("env:"):
        var = value[4:]
        return os.environ.get(var, "").strip() or None
    return value


class LLMClient:
    def __init__(self, config: dict[str, Any],
                 on_cost: Callable[[dict[str, Any]], None] | None = None) -> None:
        llm = config.get("llm", {}) or {}
        self.strong: dict[str, Any] = llm.get("strong", {}) or {}
        self.fast: dict[str, Any] = llm.get("fast", {}) or {}
        self.temperature: float = llm.get("temperature", 0.2)
        self.max_tokens: int = llm.get("max_output_tokens", 4096)
        self.timeout: int = llm.get("request_timeout_seconds", 120)
        self.on_cost = on_cost      # 每调用一次回调一条计费记录（无全局账本）

    # ---- 对外主接口 -------------------------------------------------
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        tier: str = "fast",
        json_mode: bool = False,
        purpose: str = "",
    ) -> str:
        """发送对话消息，返回文本响应。

        tier: "fast"（explore 等轻任务）| "strong"（compile 等重任务）。
        purpose: mock 桩用来选择确定性输出，真实 provider 忽略。
        """
        spec = self.strong if tier == "strong" else self.fast
        model = spec.get("model", "")

        provider = spec.get("provider") or ("anthropic" if model.startswith("claude") else "openai")
        if model == "mock" or provider == "mock":
            text = self._mock(purpose, json_mode)
            # mock 走同一记账函数（估算口径，但属设计内行为，不打缺失警告）
            self._bill(tier, messages, text, usage=None, warn_missing=False)
            return text

        if provider == "anthropic":
            text, usage = self._call_anthropic(messages, spec)
        else:
            text, usage = self._call_openai(messages, spec, json_mode)
        self._bill(tier, messages, text, usage=usage)
        return text

    def chat_json(
        self, messages: list[dict[str, str]], *, tier: str = "fast", purpose: str = ""
    ) -> dict[str, Any]:
        """要求结构化 JSON 输出并解析为 dict。"""
        raw = self.chat(messages, tier=tier, json_mode=True, purpose=purpose)
        return _extract_json(raw)

    # ---- 密钥 / 端点解析 -------------------------------------------
    @staticmethod
    def _key_of(spec: dict[str, Any], provider: str) -> str:
        raw = spec.get("api_key", "env:OPENAI_API_KEY" if provider == "openai" else "env:ANTHROPIC_API_KEY")
        key = _resolve_ref(raw, required_for="api_key")
        if not key:
            var = raw[4:] if isinstance(raw, str) and raw.startswith("env:") else str(raw)
            raise LLMError(
                f"未找到 API 密钥（{var or raw}）。请设置该环境变量，"
                f"或将 config.llm.strong/fast 的 model 设为 mock 以离线试跑。"
            )
        return key

    @staticmethod
    def _base_url_of(spec: dict[str, Any]) -> str:
        raw = spec.get("base_url", "env:OPENAI_BASE_URL")
        return _resolve_ref(raw, required_for="base_url") or "https://api.openai.com/v1"

    # ---- 计费（审计 R3）-------------------------------------------
    def _bill(self, tier: str, messages: list[dict[str, str]],
              output_text: str, usage: dict[str, Any] | None,
              warn_missing: bool = True) -> None:
        """一次调用的用量入账：真实 usage 优先，缺失按字符估算（不静默按 0）。"""
        prompt_text = "\n".join(m.get("content", "") or "" for m in messages)
        estimated = False
        t_in = t_out = 0
        if isinstance(usage, dict):
            t_in = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            t_out = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        if not (t_in or t_out):
            estimated = True
            if warn_missing:
                print("[billing] usage 缺失（兼容端点未返回），按字符估算（约 4 字符/token）",
                      file=sys.stderr)
            t_in = max(1, len(prompt_text) // 4)
            t_out = max(1, len(output_text) // 4)
        spec = self.strong if tier == "strong" else self.fast
        price_in = float(spec.get("price_per_1k_in", 0) or 0)
        price_out = float(spec.get("price_per_1k_out", 0) or 0)
        usd = round(t_in / 1000 * price_in + t_out / 1000 * price_out, 6)
        if self.on_cost:
            self.on_cost({"tier": tier, "llm_calls": 1, "tokens": t_in + t_out,
                         "usd": usd, "estimated": estimated})

    # ---- 真实 provider ----------------------------------------------
    def _call_openai(self, messages: list[dict[str, str]], spec: dict[str, Any],
                     json_mode: bool) -> tuple[str, dict[str, Any] | None]:
        payload: dict[str, Any] = {
            "model": spec.get("model"),
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            f"{self._base_url_of(spec).rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._key_of(spec, 'openai')}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        try:
            return data["choices"][0]["message"]["content"], data.get("usage")
        except (KeyError, IndexError) as exc:
            raise LLMError(f"OpenAI 响应解析失败: {data}") from exc

    def _call_anthropic(self, messages: list[dict[str, str]],
                        spec: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        chat = [m for m in messages if m["role"] != "system"]
        payload = {
            "model": spec.get("model"),
            "system": system,
            "messages": chat,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": self._key_of(spec, "anthropic"),
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        try:
            return "".join(b.get("text", "") for b in data["content"]), data.get("usage")
        except (KeyError, TypeError) as exc:
            raise LLMError(f"Anthropic 响应解析失败: {data}") from exc

    # ---- 离线 mock（无 agent / 无 key 也能跑通流水线）----------------
    def _mock(self, purpose: str, json_mode: bool) -> str:  # noqa: ARG002
        if purpose == "explore":
            return json.dumps(
                {
                    "context": {
                        "goal": "（mock）澄清后的需求目标",
                        "background": "（mock）需求背景",
                        "constraints": ["不直推 main", "改动走 PR"],
                        "out_of_scope": ["（mock）超范围项"],
                    },
                    "questions": [
                        {
                            "id": "q1",
                            "type": "choice",
                            "question": "（mock）目标语言？",
                            "options": ["Python", "TypeScript"],
                            "default": "Python",
                        },
                        {
                            "id": "q2",
                            "type": "yesno",
                            "question": "（mock）是否需要补单元测试？",
                            "default": True,
                        },
                    ],
                },
                ensure_ascii=False,
            )
        if purpose == "compile":
            return (
                "# 交付提示词（mock 生成）\n\n"
                "## SEGMENT 1\n"
                "目标：实现需求。约束：不直推 main；改动经权限闸门；补单元测试。\n\n"
                "## SEGMENT 2\n"
                "共享上段产出，完善边界与错误处理，保持改动最小、不扩大范围。\n"
            )
        return "（mock）LLM 输出"


def _extract_json(raw: str) -> dict[str, Any]:
    """从可能带 ```json 围栏或前后文本的响应中抽取 JSON 对象。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise LLMError(f"响应中未找到 JSON 对象: {raw[:200]}")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"JSON 解析失败: {exc}") from exc
