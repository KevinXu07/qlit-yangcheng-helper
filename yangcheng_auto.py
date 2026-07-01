#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
yangcheng_auto.py —— 养成教育自动填写

用 student JSESSIONID 走完整鉴权链，查询已填记录，
对未填的日期用 AI 生成"行为记录+总结反思"并提交。

命令行：python3 yangcheng_auto.py [开始日期] [结束日期]
GUI：    import yangcheng_auto; yangcheng_auto.run_fill(...)
"""

import os
import re
import sys
import json
import time
import hashlib
import urllib3
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Callable, Optional, Tuple

import requests

urllib3.disable_warnings()

# ──────────────────────── 配置 ────────────────────────

BASE = "https://pass.qlit.edu.cn"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) MacWechat/3.8.7")

LK = {
    "refresh": "15461423", "stu": "73289327", "hide": "46460068",
    "term": "36335074", "termDate": "18059799", "zb": "07605502",
    "records": "99877259", "submit": "01321027",
    "recordDetail": "68991300", "update": "03180205",
}

APP_DIR = Path.home() / ".campus_auth"
SESSION_FILE = APP_DIR / "session.txt"
OLD_SESSION_FILE = Path(__file__).resolve().parent / "session.txt"


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_saved_session() -> str:
    for path in (SESSION_FILE, OLD_SESSION_FILE):
        if path.exists():
            try:
                return path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
    return ""


def sign(*parts):
    return hashlib.md5(("".join(parts) + "md5").encode()).hexdigest()


def _preview_text(text, limit=180):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def default_prompt_template() -> str:
    return (
        "请为大学生填写一份养成教育记录，主题是「{category}」，日期 {rq}。\n"
        "语言风格：{style}。\n"
        "生成两段中文：\n"
        "1. 行为记录({xwjl_min}-{xwjl_max}字，第一人称，当天围绕该主题的具体行为，要具体自然)；\n"
        "2. 总结反思({zjfs_min}-{zjfs_max}字，第一人称，对表现的总结与不足)。\n"
        "严格只输出 JSON，对应字段必须填写完整中文内容，不能输出省略号、占位符或说明文字。\n"
        "输出格式示例：{\"XWJL\":\"今天我在课堂和宿舍里主动整理学习资料，并帮助同学完成值日。\",\"ZJFS\":\"我意识到坚持从小事做起更能体现良好习惯，后续还要继续保持主动性。\"}"
    )


AI_SYSTEM_PROMPT = (
    "你是中文校园记录写作助手。输出内容必须真实自然、贴近日常学生生活。"
    "除非用户明确要求，否则不要解释、不要道歉、不要写免责声明。"
    "禁止输出思考过程、<think>标签、英文自评、字数说明、改写说明或任何元评论。"
    "优先返回合法 JSON；如果做不到，也必须清楚给出“行为记录”和“总结反思”两段完整中文内容。"
)


def _flatten_ai_text(value) -> str:
    """兼容字符串、OpenAI content 数组、嵌套 text 块。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            part = _flatten_ai_text(item).strip()
            if part:
                parts.append(part)
        return "\n".join(parts)
    if isinstance(value, dict):
        for key in ("text", "content", "value", "output_text"):
            if key in value:
                part = _flatten_ai_text(value.get(key)).strip()
                if part:
                    return part
        return ""
    return str(value)


def _parse_ai_json_content(content: str) -> dict:
    """尽量从 AI 文本中提取首个 JSON 对象。"""
    raw = str(content or "").strip()
    if not raw:
        raise ValueError("AI 返回为空")

    # 先去掉常见 markdown 代码块围栏。
    cleaned = re.sub(r"```(?:json)?", "", raw, flags=re.I).replace("```", "").strip()

    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(cleaned):
        if ch != "{":
            continue
        try:
            data, _ = decoder.raw_decode(cleaned[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data

    raise ValueError(f"AI 返回不是有效 JSON：{_preview_text(raw, 120)}")


def _chat_completions_url(base: str) -> str:
    """兼容用户填写 base URL 或完整 chat/completions URL。"""
    endpoint = str(base or "").strip().rstrip("/")
    if not endpoint:
        raise ValueError("未填写 API Base")
    if endpoint.endswith("/chat/completions"):
        return endpoint
    # 常见 OpenAI 兼容服务的根域名需要 /v1，用户容易只填到域名。
    if re.match(r"^https?://api\.(?:deepseek|minimaxi)\.com$", endpoint, flags=re.I):
        endpoint += "/v1"
    return endpoint + "/chat/completions"


def sanitize_prompt_template(template: str) -> str:
    """兼容旧版默认 Prompt，把占位式 JSON 示例升级为完整示例。"""
    text = str(template or "").strip()
    if not text:
        return default_prompt_template()
    text = text.replace(
        "严格只输出 JSON：{\"XWJL\":\"...\",\"ZJFS\":\"...\"}",
        "严格只输出 JSON，对应字段必须填写完整中文内容，不能输出省略号、占位符或说明文字。\n"
        "输出格式示例：{\"XWJL\":\"今天我在课堂和宿舍里主动整理学习资料，并帮助同学完成值日。\",\"ZJFS\":\"我意识到坚持从小事做起更能体现良好习惯，后续还要继续保持主动性。\"}"
    )
    return text


def _is_placeholder_text(text: str) -> bool:
    normalized = re.sub(r"\s+", "", str(text or ""))
    if not normalized:
        return True
    placeholders = {
        "...", "......", "…", "……", "略", "省略", "占位", "待填写", "待补充",
        "内容略", "暂无", "无", "n/a", "null", "undefined",
    }
    lowered = normalized.lower()
    if lowered in placeholders:
        return True
    if re.fullmatch(r"[.\u2026·_\-~]{2,}", normalized):
        return True
    if len(normalized) <= 8 and ("占位" in normalized or "省略" in normalized):
        return True
    return False


def _is_refusal_text(text: str) -> bool:
    normalized = re.sub(r"\s+", "", str(text or "")).lower()
    if not normalized:
        return False
    starts = (
        "抱歉", "对不起", "很抱歉", "作为ai", "作为一个ai",
        "作为语言模型", "作为人工智能",
    )
    contains = (
        "无法回答", "不能回答", "无法提供", "不能提供", "无法协助",
        "不能协助", "无法满足", "不能满足", "无法完成", "不能完成",
        "不便提供", "无法生成", "不能生成",
    )
    return normalized.startswith(starts) or any(token in normalized for token in contains)


def _clean_generated_field(text: str) -> str:
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"<think\b[^>]*>.*?</think>", "", cleaned, flags=re.I | re.S)
    cleaned = re.sub(r"</think>.*$", "", cleaned, flags=re.I | re.S)
    cleaned = re.sub(r"<think\b[^>]*>.*$", "", cleaned, flags=re.I | re.S)
    cleaned = re.sub(r"\(\s*(?:about|around|approximately|roughly)?\s*\d+\s*(?:characters?|chars?|words?)\s*[-–—][^)]+\)", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\(\s*\d+\s*(?:characters?|chars?|words?)\s*[-–—][^)]+\)", "", cleaned, flags=re.I)
    cleaned = re.sub(r"(?im)^\s*(?:let me|i will|i'll|here(?:'s| is)|now I|we need to|the answer)\b.*$", "", cleaned)
    cleaned = re.sub(r"^(?:[-*]\s*)?(?:[12][\.\、\)]\s*)?", "", cleaned)
    cleaned = re.sub(r"^(?:XWJL|ZJFS|行为记录|总结反思)\s*[:：]\s*", "", cleaned, flags=re.I)
    return cleaned.strip(" \t\r\n\"'“”")


def _has_meta_leak(text: str) -> bool:
    cleaned = str(text or "").lower()
    patterns = (
        r"</?think\b",
        r"\b(?:let me|i will|i'll|we need to|the answer is|here is|here's)\b",
        r"\b(?:characters?|chars?|words?)\s*[-–—]\s*(?:good|ok|fine|enough)",
        r"\b(?:more formal|more natural|make it|rewrite|polish)\b",
        r"作为(?:ai|人工智能|语言模型)",
    )
    return any(re.search(pattern, cleaned, flags=re.I) for pattern in patterns)


def _extract_ai_sections(content) -> Tuple[str, str]:
    raw = _flatten_ai_text(content).strip()
    if not raw:
        raise ValueError("AI 返回为空")

    # 先尝试 JSON。
    try:
        data = _parse_ai_json_content(raw)
        xwjl = _clean_generated_field(
            data.get("XWJL") or data.get("xwjl") or data.get("行为记录") or data.get("record") or ""
        )
        zjfs = _clean_generated_field(
            data.get("ZJFS") or data.get("zjfs") or data.get("总结反思") or data.get("summary") or ""
        )
        if xwjl and zjfs:
            return xwjl, zjfs
    except ValueError:
        pass

    cleaned = re.sub(r"```(?:json|text)?", "", raw, flags=re.I).replace("```", "").strip()
    labeled = re.search(
        r"(?:^|\n)(?:[12][\.\、\)]\s*)?(?:行为记录|XWJL)\s*[:：]\s*(.+?)"
        r"(?:\n+(?:[12][\.\、\)]\s*)?(?:总结反思|ZJFS)\s*[:：]\s*)(.+)$",
        cleaned,
        flags=re.S | re.I,
    )
    if not labeled:
        labeled = re.search(
            r"(?:行为记录|XWJL)\s*[:：]\s*(.+?)(?:总结反思|ZJFS)\s*[:：]\s*(.+)$",
            cleaned,
            flags=re.S | re.I,
        )
    if labeled:
        xwjl = _clean_generated_field(labeled.group(1))
        zjfs = _clean_generated_field(labeled.group(2))
        if xwjl and zjfs:
            return xwjl, zjfs

    paragraphs = [_clean_generated_field(p) for p in re.split(r"\n{2,}", cleaned) if p.strip()]
    if len(paragraphs) >= 2:
        return paragraphs[0], "\n\n".join(paragraphs[1:])

    raise ValueError(f"AI 返回无法提取内容：{_preview_text(raw, 120)}")


def _post_ai_completion(ai: "AiConfig", messages: list, timeout: int, max_tokens: Optional[int] = None):
    endpoint = _chat_completions_url(ai.base)
    payload = {
        "model": ai.model,
        "messages": messages,
        "temperature": 0.9,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    payload_with_json = dict(payload)
    payload_with_json["response_format"] = {"type": "json_object"}

    session = requests.Session()
    session.trust_env = False
    headers = {"Authorization": f"Bearer {ai.key}", "Content-Type": "application/json"}

    r = session.post(
        endpoint,
        headers=headers,
        json=payload_with_json,
        timeout=timeout,
    )
    if r.status_code >= 400 and r.status_code < 500:
        body = _preview_text(r.text, 160).lower()
        if "response_format" in body or "json_object" in body:
            r = session.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=timeout,
            )
    return r


def _ai_error_message(e: Exception) -> str:
    if isinstance(e, requests.exceptions.ProxyError):
        return "AI 请求被代理拦截或代理不可用，请关闭系统代理/VPN后重试：" + _preview_text(e, 180)
    if isinstance(e, requests.exceptions.SSLError):
        return "AI HTTPS 证书校验失败，请检查代理/VPN或证书设置：" + _preview_text(e, 180)
    if isinstance(e, requests.exceptions.ConnectTimeout):
        return "AI 连接超时，请检查网络或 API Base：" + _preview_text(e, 180)
    if isinstance(e, requests.exceptions.ReadTimeout):
        return "AI 响应超时，请稍后重试或换模型：" + _preview_text(e, 180)
    if isinstance(e, requests.exceptions.ConnectionError):
        return "AI 网络连接失败，请检查 API Base、网络、代理/VPN：" + _preview_text(e, 220)
    return _preview_text(e, 180)


# ──────────────────────── AI 配置 ────────────────────────

class AiConfig:
    """AI API 配置（OpenAI 兼容接口）+ 内容生成参数。"""
    def __init__(self, base="", key="", model="",
                 style="正式", xwjl_min=150, xwjl_max=250,
                 zjfs_min=120, zjfs_max=200, prompt_template=""):
        self.base = base or os.environ.get("AI_BASE", "https://open.bigmodel.cn/api/paas/v4")
        self.key = key or os.environ.get("AI_KEY", "")
        self.model = model or os.environ.get("AI_MODEL", "glm-4-flash")
        # 内容参数
        self.style = style            # 风格：正式/活泼/朴素/感恩...
        self.xwjl_min = xwjl_min      # 行为记录字数下限
        self.xwjl_max = xwjl_max      # 行为记录字数上限
        self.zjfs_min = zjfs_min      # 总结反思字数下限
        self.zjfs_max = zjfs_max      # 总结反思字数上限
        self.prompt_template = prompt_template  # 自定义 prompt 模板（空则用默认）

    @property
    def enabled(self):
        return bool(self.key)

    def to_dict(self):
        return {"base": self.base, "key": self.key, "model": self.model,
                "style": self.style, "xwjl_min": self.xwjl_min, "xwjl_max": self.xwjl_max,
                "zjfs_min": self.zjfs_min, "zjfs_max": self.zjfs_max,
                "prompt_template": self.prompt_template}

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d.get(k) for k in
                      ("base", "key", "model", "style",
                       "xwjl_min", "xwjl_max", "zjfs_min", "zjfs_max", "prompt_template")
                      if d.get(k) not in (None, "")})


# ──────────────────────── 养成教育客户端 ────────────────────────

class YangchengClient:
    def __init__(self, student_jsession: str):
        self._student_jsession = student_jsession
        self.s = requests.Session()
        self.s.verify = False
        self.s.headers.update({"User-Agent": UA})
        self.jwt_short = ""
        self.jwt_long = ""
        self.access = ""
        self.ycj_jsession = ""
        self.xn = ""
        self.xn_name = ""
        self.student = {}
        self.categories = []

    def _json_response(self, response, label):
        try:
            response.raise_for_status()
        except requests.HTTPError as e:
            raise RuntimeError(f"{label} HTTP {response.status_code}：{_preview_text(response.text)}") from e
        try:
            return response.json()
        except ValueError as e:
            raise RuntimeError(f"{label} 返回非 JSON：{_preview_text(response.text)}") from e

    def _post(self, lk, body):
        url = f"{BASE}/yangchengjiaoyu/RemoteAnswer.do?lk={lk}"
        headers = {
            "User-Agent": UA,
            "Authorization": self.jwt_long,
            "Access": self.access,
            "Content-Type": "application/json",
            "Cookie": f"JSESSIONID={self.ycj_jsession}; HHMM=18",
            "Origin": BASE,
        }
        r = self.s.post(url, headers=headers, json=body, timeout=15)
        return self._json_response(r, f"接口 lk={lk}")

    def _resolve(self, resp):
        if not isinstance(resp, dict):
            raise RuntimeError(f"接口响应格式异常：{_preview_text(resp)}")
        if resp.get("success") is False and resp.get("message") not in ("", None):
            raise RuntimeError(f"接口返回失败：{_preview_text(resp.get('message'))}")
        msg = resp.get("message", "")
        try:
            return json.loads(msg)
        except (json.JSONDecodeError, TypeError):
            return msg

    def auth(self):
        r = self.s.get(f"{BASE}/student/mobile/sso_pt_yangchengjy/index.jsp",
                       headers={"Cookie": f"JSESSIONID={self._student_jsession}"},
                       timeout=15)
        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            raise RuntimeError(f"SSO 页面 HTTP {r.status_code}：{_preview_text(r.text)}") from e
        m = re.search(r'setItem\("Authorization",\s*"(eyJ[^"]+)"', r.text)
        if not m:
            raise RuntimeError(f"SSO 未返回短 JWT（session 可能过期）：{_preview_text(r.text)}")
        self.jwt_short = m.group(1)

        r = self.s.get(f"{BASE}/yangchengjiaoyu/RemoteAnswer.do?lk={LK['refresh']}",
                       headers={"Authorization": self.jwt_short, "Cookie": "HHMM=18"},
                       timeout=15)
        d = self._json_response(r, "长 JWT 获取")
        if not str(d.get("message", "")).startswith("eyJ"):
            raise RuntimeError(f"长 JWT 获取失败：{d}")
        self.jwt_long = d["message"]
        for c in r.cookies:
            if c.name == "JSESSIONID":
                self.ycj_jsession = c.value
        if not self.ycj_jsession:
            raise RuntimeError("长 JWT 获取成功，但响应未返回养成教育 JSESSIONID")

        r = self.s.get(f"{BASE}/yangchengjiaoyu/SSOServerHelper.do?FUNNAME=getAccess",
                       headers={"Authorization": self.jwt_short,
                                "Cookie": f"JSESSIONID={self.ycj_jsession}; HHMM=18"},
                       timeout=15)
        self.access = self._json_response(r, "Access 获取").get("message", "")
        if not self.access.startswith("Bearer"):
            raise RuntimeError(f"access 获取失败：{self.access[:80]}")

    def load_student(self):
        a = sign(now_str())
        self.student = self._resolve(self._post(LK["stu"], {"A": a}))

    def load_terms(self):
        a = sign(now_str())
        terms = self._resolve(self._post(LK["term"], {"A": a}))
        if not isinstance(terms, list):
            raise RuntimeError(f"学期列表格式异常：{_preview_text(terms)}")
        cur = [t for t in terms if t.get("ISDQ") == "T"]
        if cur:
            self.xn, self.xn_name = cur[0]["ID"], cur[0]["NAME"]
        elif terms:
            self.xn, self.xn_name = terms[0]["ID"], terms[0]["NAME"]
        else:
            raise RuntimeError("没有可用的学期")

    def load_categories(self):
        a = sign(self.xn, now_str())
        self.categories = self._resolve(self._post(LK["zb"], {"XNXQID": self.xn, "A": a}))
        if not isinstance(self.categories, list):
            raise RuntimeError(f"好习惯类别格式异常：{_preview_text(self.categories)}")
        self.categories = [str(c).strip() for c in self.categories if str(c).strip()]
        if not self.categories:
            raise RuntimeError("官方接口未返回好习惯类别，已中断。请稍后重试或重新抓取凭据。")

    def get_filled_dates(self):
        a = sign(self.xn, now_str())
        recs = self._resolve(self._post(LK["records"], {"XNXQID": self.xn, "A": a}))
        if not isinstance(recs, list):
            raise RuntimeError(f"已填记录格式异常：{_preview_text(recs)}")
        filled = set()
        for r in recs:
            rq = r.get("RQ", "")
            if rq:
                filled.add(rq[:10])
        return filled

    def get_records(self):
        a = sign(self.xn, now_str())
        recs = self._resolve(self._post(LK["records"], {"XNXQID": self.xn, "A": a}))
        if not isinstance(recs, list):
            raise RuntimeError(f"已填记录格式异常：{_preview_text(recs)}")
        return recs

    def get_record_detail(self, record_id):
        a = sign(record_id, now_str())
        detail = self._resolve(self._post(LK["recordDetail"], {"ID": record_id, "A": a}))
        if not isinstance(detail, dict):
            raise RuntimeError(f"记录详情格式异常：{_preview_text(detail)}")
        return detail

    def submit(self, rq, hxg, xwjl, zjfs):
        info = {"XNXQID": self.xn, "RQ": rq, "HXG": hxg, "XWJL": xwjl, "ZJFS": zjfs}
        a = sign(self.xn, now_str())
        resp = self._post(LK["submit"], {"INFO": info, "A": a, "z": self.xn})
        success = resp.get("success") is True or resp.get("success") == "true"
        if success:
            return True, ""
        reason = resp.get("message") or resp.get("remark") or resp
        return False, _preview_text(reason)

    def update_record(self, record_id, rq, hxg, xwjl, zjfs):
        info = {
            "ID": record_id,
            "XNXQID": self.xn,
            "RQ": rq,
            "HXG": hxg,
            "XWJL": xwjl,
            "ZJFS": zjfs,
        }
        a = sign(self.xn, now_str())
        resp = self._post(LK["update"], {"INFO": info, "A": a, "z": self.xn})
        success = resp.get("success") is True or resp.get("success") == "true"
        if success:
            return True, ""
        reason = resp.get("message") or resp.get("remark") or resp
        return False, _preview_text(reason)


# ──────────────────────── AI 内容生成 ────────────────────────

def ai_generate(category, rq, ai: AiConfig) -> Tuple[str, str, str]:
    """返回 (xwjl, zjfs)。无 AI 时用模板兜底。"""
    if not ai.enabled:
        xwjl, zjfs = _fallback(category, rq, ai)
        return xwjl, zjfs, "未配置 AI，使用模板"

    prompt_template = sanitize_prompt_template(ai.prompt_template or default_prompt_template())
    # 用 replace 而非 str.format：避免 prompt 里 JSON 字面量的花括号 {X}
    # 被当成 format 占位符导致 KeyError。replace 对字面量花括号完全安全。
    prompt = (prompt_template
              .replace("{category}", str(category))
              .replace("{rq}", str(rq))
              .replace("{style}", str(ai.style))
              .replace("{xwjl_min}", str(ai.xwjl_min))
              .replace("{xwjl_max}", str(ai.xwjl_max))
              .replace("{zjfs_min}", str(ai.zjfs_min))
              .replace("{zjfs_max}", str(ai.zjfs_max)))
    try:
        r = _post_ai_completion(
            ai,
            [
                {"role": "system", "content": AI_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            timeout=120,
        )
        r.raise_for_status()
        payload = r.json()
        content = _flatten_ai_text(payload["choices"][0]["message"].get("content")).strip()
        xwjl, zjfs = _extract_ai_sections(content)
        if _is_placeholder_text(xwjl) or _is_placeholder_text(zjfs):
            raise ValueError("AI 返回了省略号或占位内容")
        if _has_meta_leak(xwjl) or _has_meta_leak(zjfs):
            raise ValueError("AI 返回混入了思考过程或说明文字")
        if _is_refusal_text(xwjl) or _is_refusal_text(zjfs) or _is_refusal_text(content):
            raise ValueError("AI 返回了拒答或说明文字")
        return xwjl, zjfs, ""
    except Exception as e:
        xwjl, zjfs = _fallback(category, rq, ai)
        return xwjl, zjfs, f"AI 生成失败，使用模板：{_ai_error_message(e)}"


def test_ai_connection(ai: AiConfig) -> Tuple[bool, str]:
    """测试 AI 连接。发一个极简请求，返回 (是否成功, 说明)。
    GUI 的"测试连接"按钮用。不依赖 session，纯测 AI API。"""
    if not ai.key:
        return False, "未填写 API Key"
    try:
        r = _post_ai_completion(
            ai,
            [
                {"role": "system", "content": "你是连通性测试助手，只返回OK。"},
                {"role": "user", "content": "请回复OK"},
            ],
            timeout=20,
            max_tokens=8,
        )
    except requests.exceptions.ConnectionError as e:
        endpoint = _chat_completions_url(ai.base)
        return False, f"无法连接 {endpoint}（{_preview_text(e, 80)}）"
    except requests.exceptions.Timeout:
        endpoint = _chat_completions_url(ai.base)
        return False, f"请求超时（{endpoint}）"
    except Exception as e:
        return False, f"请求异常：{_preview_text(e, 100)}"

    if r.status_code != 200:
        # 尝试从响应体抠错误信息
        try:
            err = r.json()
            msg = err.get("error", {}).get("message") or err.get("message") or _preview_text(r.text, 120)
        except Exception:
            msg = _preview_text(r.text, 120)
        return False, f"HTTP {r.status_code}：{msg}"

    try:
        content = _flatten_ai_text(r.json()["choices"][0]["message"].get("content")).strip()
        return True, f"连接正常（模型 {ai.model} 回复：{_preview_text(content, 30)}）"
    except Exception:
        return True, f"连接正常（HTTP 200，但响应结构非标准）"


def _fallback(category, rq, ai=None):
    style = (ai.style if ai else "正式")
    xwjl = (f"在{rq}当天，我坚持以「{category}」为指引，在学习生活中积极践行，"
            f"风格上做到{style}。认真完成专业课程学习，主动参与班级活动，"
            f"与同学团结协作，努力将这一理念落实到具体行动中，不断提升自身综合素质。")
    zjfs = (f"通过对「{category}」的{style}践行与反思，我认识到这不仅是一种理念，"
            f"更需要在日常中持续践行。虽然取得了一定进步，但仍有提升空间，"
            f"今后会继续努力，做到知行合一。")
    return xwjl, zjfs


# ──────────────────────── 编排（GUI 用） ────────────────────────

def run_fill(
    session: str,
    start_date: date,
    end_date: date,
    ai: Optional[AiConfig] = None,
    on_log: Optional[Callable[[str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    # —— 自定义选项 ——
    category: object = None,        # None=轮转 / str=固定 / list[str]=子集轮转
    skip_weekend: bool = False,     # 跳过周六日
    force_override: bool = False,   # 忽略已填记录，仍然提交覆盖
    exclude_dates: Optional[set] = None,  # 排除的日期集合 {'YYYY-MM-DD'}
    submit_interval: float = 0,     # 提交间隔秒（会加 ±20% 随机抖动）
    retry: int = 0,                 # 失败重试次数
    preview_callback: Optional[Callable[[dict], bool]] = None,  # 预览确认，返回 False 跳过
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """
    执行补填。阻塞调用，GUI 在后台线程跑。

    category:
        None → 在服务器返回的全部分类里轮转
        "爱党爱国" → 固定用这个
        ["爱党爱国","诚实守信"] → 在这两个里轮转
    preview_callback(info): info = {rq, category, xwjl, zjfs}，返回 True 提交/False 跳过
    progress_callback(done, total): 每处理完一条回调一次
    返回 {"ok": int, "fail": int, "skip": int, "total": int}
    """
    import random

    def log(m):
        if on_log:
            on_log(m)

    def progress(done, total):
        if progress_callback:
            try:
                progress_callback(done, total)
            except Exception:
                pass

    if ai is None:
        ai = AiConfig()

    # 计算待填日期
    days = []
    d = start_date
    while d <= end_date:
        days.append(d)
        d += timedelta(days=1)

    # 过滤：跳过周末
    if skip_weekend:
        before = len(days)
        days = [d for d in days if d.weekday() < 5]  # 0-4 = 周一到周五
        log(f"跳过周末，移除 {before - len(days)} 天")

    # 过滤：排除特定日期
    if exclude_dates:
        days = [d for d in days if d.isoformat() not in exclude_dates]

    c = YangchengClient(session)
    log("鉴权中...")
    c.auth()
    c.load_student()
    c.load_terms()
    c.load_categories()
    log(f"✓ 鉴权成功（学生：{c.student.get('XM','?')}）")
    log(f"学期：{c.xn_name}")

    # 确定可用的类别列表
    if category is None:
        cat_list = c.categories
        cat_mode = "轮转(全部)"
    elif isinstance(category, str):
        cat_list = [category]
        cat_mode = f"固定({category})"
    elif isinstance(category, list) and category:
        cat_list = category
        cat_mode = f"轮转({category})"
    else:
        cat_list = c.categories
        cat_mode = "轮转(全部)"
    log(f"好习惯类别：{cat_mode}")

    records = c.get_records()
    filled = set()
    record_by_date = {}
    for rec in records:
        rq = (rec.get("RQ") or "")[:10]
        if not rq:
            continue
        filled.add(rq)
        record_by_date[rq] = rec
    if force_override:
        todo = list(days)
        log(f"强制覆盖已开启：将尝试提交 {len(todo)} 天（包含已填 {len(filled & {d.isoformat() for d in days})} 天）")
    else:
        todo = [d for d in days if d.isoformat() not in filled]
        log(f"待填 {len(todo)} 天（跳过已填 {len(days) - len(todo)} 天）")
    progress(0, len(todo))

    if not todo:
        log("✓ 全部已填，无需补填")
        return {"ok": 0, "fail": 0, "skip": 0, "total": 0}

    if not ai.enabled:
        log("⚠️ 未配置 AI，用模板内容")

    ok = fail = skip = 0
    for i, d in enumerate(todo, 1):
        if should_stop and should_stop():
            log("已取消")
            break
        rq = d.isoformat()
        cat = cat_list[d.toordinal() % len(cat_list)]
        log(f"[{i}/{len(todo)}] {rq} 类别={cat}")
        xwjl, zjfs, fallback_reason = ai_generate(cat, rq, ai)
        if fallback_reason and ai.enabled:
            log(f"  ⚠ {fallback_reason}")
        log(f"  行为记录: {xwjl[:40]}...")
        log(f"  总结反思: {zjfs[:40]}...")

        # 预览确认
        if preview_callback:
            info = {"rq": rq, "category": cat, "xwjl": xwjl, "zjfs": zjfs}
            try:
                approved = preview_callback(info)
            except Exception:
                approved = True
            if not approved:
                log(f"  ⊘ 已跳过（用户取消）")
                skip += 1
                progress(ok + fail + skip, len(todo))
                continue

        # 提交（带重试）
        submitted = False
        existing = record_by_date.get(rq) if force_override else None
        for attempt in range(retry + 1):
            try:
                if existing:
                    record_id = existing.get("ID")
                    if not record_id:
                        raise RuntimeError("已填记录缺少 ID，无法更新")
                    success, reason = c.update_record(record_id, rq, cat, xwjl, zjfs)
                    action = "覆盖成功" if success else "覆盖失败"
                else:
                    success, reason = c.submit(rq, cat, xwjl, zjfs)
                    action = "提交成功" if success else "提交失败"
                if success:
                    log(f"  ✓ {action}")
                    ok += 1
                    record_by_date[rq] = {
                        "ID": existing.get("ID") if existing else "",
                        "RQ": rq,
                        "HXG": cat,
                        "XWJL": xwjl,
                        "ZJFS": zjfs,
                    }
                    submitted = True
                    break
                else:
                    if attempt < retry:
                        log(f"  ⚠ {action}：{reason or '无详情'}，重试({attempt+1}/{retry})...")
                        time.sleep(1)
                    else:
                        log(f"  ✗ {action}：{reason or '无详情'}")
            except Exception as e:
                if attempt < retry:
                    log(f"  ⚠ 异常({e})，重试({attempt+1}/{retry})...")
                    time.sleep(1)
                else:
                    log(f"  ✗ 提交异常：{e}")
        if not submitted:
            fail += 1
        progress(ok + fail + skip, len(todo))

        # 提交间隔（最后一条不用等）
        if submit_interval > 0 and i < len(todo):
            wait = submit_interval * (0.8 + random.random() * 0.4)  # ±20% 抖动
            time.sleep(wait)

    log(f"=== 完成：成功 {ok}，失败 {fail}，跳过 {skip} ===")
    return {"ok": ok, "fail": fail, "skip": skip, "total": len(todo)}


# ──────────────────────── 命令行入口 ────────────────────────

def main():
    if len(sys.argv) >= 3:
        start = date.fromisoformat(sys.argv[1])
        end = date.fromisoformat(sys.argv[2])
    else:
        end = date.today()
        start = end - timedelta(days=6)
    print(f"=== 养成教育自动填写 {start} ~ {end} ===\n")

    session = load_saved_session()
    if not session:
        print(f"✗ 找不到 {SESSION_FILE}，请先运行 qlit_auth_core.py")
        return 1

    try:
        result = run_fill(
            session, start, end,
            on_log=lambda m: print(m, flush=True),
        )
        return 0 if result["fail"] == 0 else 1
    except Exception as e:
        print(f"\n✗ 出错：{e}", flush=True)
        import traceback; traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
