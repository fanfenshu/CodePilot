#!/usr/bin/env python3
"""
Token Usage — 跨 LLM Token 计量与查询工具

数据源:
  1. Claude Code JSONL (本地 ~/.claude/projects/)
  2. Codex JSONL (本地 ~/.codex/sessions/)
  3. LLM Gateway SQLite (线上 47.107.157.5)

用法:
  python3 token_usage.py daily [N]              # 按天汇总, 默认7天
  python3 token_usage.py top [days|models] [N]  # 排行榜, 默认Top5
  python3 token_usage.py model                  # 按模型对比
  python3 token_usage.py system                 # 按业务系统(仅Gateway)
  python3 token_usage.py cost [daily|monthly]   # 费用分析
  python3 token_usage.py detail [YYYY-MM-DD]    # 指定日期详情
  python3 token_usage.py sources                # 数据源状态
"""

import base64
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# ─── 配置 ───

CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
CODEX_SESSIONS_DIR = os.path.expanduser("~/.codex/sessions")
CODEX_AUTH_FILE = os.path.expanduser("~/.codex/auth.json")
GATEWAY_HOST = "deploy@47.107.157.5"
GATEWAY_DB = "/home/deploy/data/llm-gateway/llm_gateway.db"

# Claude 模型价格 ($/M tokens)
CLAUDE_PRICING = {
    "claude-opus-4-6": {
        "input": 15.0,
        "output": 75.0,
        "cache_creation": 18.75,
        "cache_read": 1.50,
    },
    "claude-sonnet-4-6": {
        "input": 3.0,
        "output": 15.0,
        "cache_creation": 3.75,
        "cache_read": 0.30,
    },
    "claude-haiku-4-5-20251001": {
        "input": 0.80,
        "output": 4.0,
        "cache_creation": 1.0,
        "cache_read": 0.08,
    },
}

# OpenAI / Codex 等效 API 价格 ($/M tokens)
# 说明:
# - gpt-5.4 / gpt-5.4-mini 为 Codex 当前日志中常见模型名
# - OpenAI 官方公开价目前展示的是 GPT-5 / GPT-5.2 / GPT-5.2-codex 等系列
# - 这里将 gpt-5.4 家族映射到最新公开的 GPT-5 Codex 同档价格，用于“等效 API 费用”估算
OPENAI_PRICING = {
    "gpt-5.4": {"input": 1.75, "cached_input": 0.175, "output": 14.0},
    "gpt-5.4-mini": {"input": 0.25, "cached_input": 0.025, "output": 2.0},
    "gpt-5.4-nano": {"input": 0.05, "cached_input": 0.005, "output": 0.40},
    "gpt-5.3-codex": {"input": 1.25, "cached_input": 0.125, "output": 10.0},
    "gpt-5.2-codex": {"input": 1.75, "cached_input": 0.175, "output": 14.0},
    "gpt-5.1-codex": {"input": 1.25, "cached_input": 0.125, "output": 10.0},
    "gpt-5.1-codex-max": {"input": 1.25, "cached_input": 0.125, "output": 10.0},
    "gpt-5.1-codex-mini": {"input": 0.25, "cached_input": 0.025, "output": 2.0},
    "gpt-5-codex": {"input": 1.25, "cached_input": 0.125, "output": 10.0},
    "codex-mini-latest": {"input": 1.50, "cached_input": 0.375, "output": 6.0},
    "gpt-5.2": {"input": 1.75, "cached_input": 0.175, "output": 14.0},
    "gpt-5.1": {"input": 1.25, "cached_input": 0.125, "output": 10.0},
    "gpt-5": {"input": 1.25, "cached_input": 0.125, "output": 10.0},
    "gpt-5-mini": {"input": 0.25, "cached_input": 0.025, "output": 2.0},
    "gpt-5-nano": {"input": 0.05, "cached_input": 0.005, "output": 0.40},
}

USD_TO_CNY = 7.2

# Claude Code 订阅价格 (月付, 人民币)
CLAUDE_CODE_SUBSCRIPTION = {
    "max": {"price_yuan": 720, "label": "Max ($100/月)"},
    "max_plus": {"price_yuan": 1440, "label": "Max+ ($200/月)"},
}
# 当前订阅套餐
CURRENT_PLAN = "max_plus"

# Codex / ChatGPT 套餐信息
CODEX_SUBSCRIPTION = {
    "free": {"price_yuan": 0, "label": "Free"},
    "plus": {"price_yuan": 144, "label": "Plus ($20/月)"},
    "prolite": {"price_yuan": 720, "label": "Pro Lite ($100/月)"},
    "pro": {"price_yuan": 1440, "label": "Pro ($200/月)"},
    "team": {"price_yuan": 0, "label": "Team"},
    "enterprise": {"price_yuan": 0, "label": "Enterprise"},
}


# ─── 通用辅助 ───

def _safe_int(value):
    try:
        return int(value or 0)
    except Exception:
        return 0


def _parse_day(ts):
    """兼容 ISO 字符串 / Unix 时间戳，统一转本地 YYYY-MM-DD。"""
    if not ts:
        return ""
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
        except Exception:
            return ts[:10]
    return ""


def _decode_jwt_payload(token):
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def get_claude_plan_info():
    plan = CLAUDE_CODE_SUBSCRIPTION.get(CURRENT_PLAN, {})
    return {
        "billing": "subscription",
        "plan_type": CURRENT_PLAN,
        "label": plan.get("label", "Claude Code 订阅"),
        "price_yuan": plan.get("price_yuan", 0),
    }


def get_codex_plan_info():
    info = {
        "billing": "subscription",
        "plan_type": "unknown",
        "label": "Codex 订阅",
        "price_yuan": 0,
    }
    auth_path = Path(CODEX_AUTH_FILE)
    if not auth_path.exists():
        return info
    try:
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
        tokens = auth.get("tokens", {}) or {}
        claims = _decode_jwt_payload(tokens.get("id_token", ""))
        auth_claim = claims.get("https://api.openai.com/auth", {}) or {}
        plan_type = auth_claim.get("chatgpt_plan_type") or auth.get("auth_mode") or "unknown"
        info["plan_type"] = plan_type
        meta = CODEX_SUBSCRIPTION.get(plan_type, {})
        if meta:
            info["label"] = meta.get("label", info["label"])
            info["price_yuan"] = meta.get("price_yuan", 0)
        elif plan_type and plan_type != "unknown":
            info["label"] = plan_type
    except Exception:
        pass
    return info


def get_source_subscription_meta(source):
    if source == "claude-code":
        return get_claude_plan_info()
    if source == "codex":
        return get_codex_plan_info()
    return {
        "billing": "pay-per-use",
        "plan_type": "",
        "label": "",
        "price_yuan": 0,
    }


def _get_openai_pricing(model):
    if model in OPENAI_PRICING:
        return OPENAI_PRICING[model]
    if model.startswith("gpt-5.4"):
        if "mini" in model:
            return OPENAI_PRICING["gpt-5.4-mini"]
        if "nano" in model:
            return OPENAI_PRICING["gpt-5.4-nano"]
        return OPENAI_PRICING["gpt-5.4"]
    if model.startswith("gpt-5.2"):
        return OPENAI_PRICING["gpt-5.2-codex"] if "codex" in model else OPENAI_PRICING["gpt-5.2"]
    if model.startswith("gpt-5.1"):
        if "mini" in model:
            return OPENAI_PRICING["gpt-5.1-codex-mini"]
        return OPENAI_PRICING["gpt-5.1-codex"] if "codex" in model else OPENAI_PRICING["gpt-5.1"]
    if model.startswith("gpt-5"):
        if "mini" in model:
            return OPENAI_PRICING["gpt-5-mini"]
        if "nano" in model:
            return OPENAI_PRICING["gpt-5-nano"]
        return OPENAI_PRICING["gpt-5-codex"] if "codex" in model else OPENAI_PRICING["gpt-5"]
    return None


def _aggregate_daily_records(records, since):
    daily = defaultdict(lambda: {
        "input": 0,
        "output": 0,
        "cache_creation": 0,
        "cache_read": 0,
        "total": 0,
        "cost": 0.0,
        "count": 0,
        "latency_sum": 0,
    })
    for r in records:
        if r["date"] < since:
            continue
        d = daily[r["date"]]
        d["input"] += r.get("input_tokens", 0)
        d["output"] += r.get("output_tokens", 0)
        d["cache_creation"] += r.get("cache_creation_tokens", 0)
        d["cache_read"] += r.get("cache_read_tokens", 0)
        d["total"] += r.get("total_tokens", 0)
        d["cost"] += r.get("cost_yuan", 0.0)
        d["count"] += 1
        d["latency_sum"] += r.get("latency_ms", 0)
    return daily


# ─── 数字格式化 ───

def fmt_tokens(n):
    """格式化 token 数量: 亿/万/千位分隔"""
    if n >= 1_0000_0000:
        return f"{n / 1_0000_0000:.2f}亿"
    elif n >= 1_0000:
        return f"{n / 1_0000:.1f}万"
    else:
        return f"{n:,}"


def fmt_cost(yuan):
    """格式化费用"""
    return f"¥{yuan:.2f}"


def fmt_pct(p):
    """格式化百分比"""
    return f"{p:.1f}%"


# ─── 数据采集: Claude Code JSONL ───

def collect_claude_code():
    """从所有 JSONL 文件采集 Claude Code token 数据"""
    records = []
    projects_dir = Path(CLAUDE_PROJECTS_DIR)
    if not projects_dir.exists():
        return records

    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl_file in project_dir.glob("*.jsonl"):
            try:
                with open(jsonl_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        if obj.get("type") != "assistant":
                            continue
                        msg = obj.get("message", {})
                        usage = msg.get("usage")
                        if not usage:
                            continue

                        day = _parse_day(msg.get("timestamp") or obj.get("timestamp"))
                        if not day:
                            continue

                        model = msg.get("model", "unknown")
                        if not model or model.startswith("<") or model == "unknown":
                            continue

                        input_tokens = usage.get("input_tokens", 0)
                        output_tokens = usage.get("output_tokens", 0)
                        cache_creation_tokens = usage.get("cache_creation_input_tokens", 0)
                        cache_read_tokens = usage.get("cache_read_input_tokens", 0)

                        pricing = CLAUDE_PRICING.get(model)
                        if pricing:
                            cost_usd = (
                                input_tokens * pricing["input"]
                                + output_tokens * pricing["output"]
                                + cache_creation_tokens * pricing["cache_creation"]
                                + cache_read_tokens * pricing["cache_read"]
                            ) / 1_000_000
                            cost_yuan = round(cost_usd * USD_TO_CNY, 4)
                        else:
                            cost_yuan = 0.0

                        records.append({
                            "date": day,
                            "source": "claude-code",
                            "provider": "anthropic",
                            "model": model,
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "cache_creation_tokens": cache_creation_tokens,
                            "cache_read_tokens": cache_read_tokens,
                            "total_tokens": (
                                input_tokens
                                + output_tokens
                                + cache_creation_tokens
                                + cache_read_tokens
                            ),
                            "cost_yuan": cost_yuan,
                        })
            except Exception:
                continue

    return records


# ─── 数据采集: Codex JSONL ───

def collect_codex(days=31):
    """从 Codex 会话 JSONL 采集 token 数据。

    Codex 的 token_count 是会话累计值，且会重复广播同一累计值。
    因此这里按“累计值差分”计算真实新增用量，避免重复统计。
    """
    records = []
    sessions_dir = Path(CODEX_SESSIONS_DIR)
    if not sessions_dir.exists():
        return records

    cutoff_ts = (datetime.now() - timedelta(days=days)).timestamp()
    for jsonl_file in sessions_dir.rglob("*.jsonl"):
        try:
            if jsonl_file.stat().st_mtime < cutoff_ts:
                continue
        except Exception:
            continue

        current_model = "gpt-5.4"
        prev_totals = None

        try:
            with open(jsonl_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if obj.get("type") == "turn_context":
                        payload = obj.get("payload", {}) or {}
                        current_model = payload.get("model") or current_model
                        continue

                    if obj.get("type") != "event_msg":
                        continue

                    payload = obj.get("payload", {}) or {}
                    if payload.get("type") != "token_count":
                        continue

                    info = payload.get("info") or {}
                    totals = info.get("total_token_usage") or {}
                    if not totals:
                        continue

                    current_totals = {
                        "input": _safe_int(totals.get("input_tokens")),
                        "cached_input": _safe_int(totals.get("cached_input_tokens")),
                        "output": _safe_int(totals.get("output_tokens")),
                        "reasoning": _safe_int(totals.get("reasoning_output_tokens")),
                    }
                    if prev_totals is None:
                        delta = current_totals
                    else:
                        delta = {
                            key: max(0, current_totals[key] - prev_totals.get(key, 0))
                            for key in current_totals
                        }
                    prev_totals = current_totals

                    if not any(delta.values()):
                        continue

                    day = _parse_day(obj.get("timestamp"))
                    if not day:
                        continue

                    model = current_model or "gpt-5.4"
                    output_tokens = delta["output"] + delta["reasoning"]
                    total_tokens = delta["input"] + delta["cached_input"] + output_tokens

                    pricing = _get_openai_pricing(model)
                    if pricing:
                        cost_usd = (
                            delta["input"] * pricing["input"]
                            + delta["cached_input"] * pricing["cached_input"]
                            + output_tokens * pricing["output"]
                        ) / 1_000_000
                        cost_yuan = round(cost_usd * USD_TO_CNY, 4)
                    else:
                        cost_yuan = 0.0

                    records.append({
                        "date": day,
                        "source": "codex",
                        "provider": "openai",
                        "model": model,
                        "input_tokens": delta["input"],
                        "output_tokens": output_tokens,
                        "cache_creation_tokens": 0,
                        "cache_read_tokens": delta["cached_input"],
                        "reasoning_output_tokens": delta["reasoning"],
                        "total_tokens": total_tokens,
                        "cost_yuan": cost_yuan,
                    })
        except Exception:
            continue

    return records


# ─── 数据采集: LLM Gateway ───

def collect_gateway(days=30):
    """从 LLM Gateway SQLite 采集数据"""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    query = (
        f"SELECT DATE(created_at) as date, model_id, system_id, agent_id, work_type, "
        f"input_tokens, output_tokens, cost_yuan, latency_ms, status "
        f"FROM llm_call_logs WHERE created_at >= '{since}' ORDER BY created_at"
    )
    cmd = f'ssh -o ConnectTimeout=5 {GATEWAY_HOST} "sqlite3 -json {GATEWAY_DB} \\"{query}\\""'

    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return []
        output = result.stdout.strip()
        if not output:
            return []
        rows = json.loads(output)
    except Exception:
        return []

    records = []
    for row in rows:
        model_id = row.get("model_id", "unknown")
        provider = "unknown"
        if "claude" in model_id:
            provider = "anthropic"
        elif "glm" in model_id:
            provider = "zhipu"
        elif "gemini" in model_id:
            provider = "google"
        elif "deepseek" in model_id:
            provider = "deepseek"
        elif "moonshot" in model_id or "kimi" in model_id:
            provider = "moonshot"
        elif "gpt" in model_id or "o3" in model_id or "o4" in model_id:
            provider = "openai"

        input_t = row.get("input_tokens", 0) or 0
        output_t = row.get("output_tokens", 0) or 0

        records.append({
            "date": row.get("date", ""),
            "source": "llm-gateway",
            "provider": provider,
            "model": model_id,
            "system": row.get("system_id", ""),
            "agent_id": row.get("agent_id", ""),
            "work_type": row.get("work_type", ""),
            "input_tokens": input_t,
            "output_tokens": output_t,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "total_tokens": input_t + output_t,
            "cost_yuan": float(row.get("cost_yuan", 0) or 0),
            "latency_ms": int(row.get("latency_ms", 0) or 0),
            "status": row.get("status", ""),
        })

    return records


# ─── 数据源状态检查 ───

def check_sources():
    """检查所有数据源连接状态"""
    print("## 数据源状态\n")

    projects_dir = Path(CLAUDE_PROJECTS_DIR)
    jsonl_count = 0
    if projects_dir.exists():
        for project_dir in projects_dir.iterdir():
            if project_dir.is_dir():
                jsonl_count += len(list(project_dir.glob("*.jsonl")))

    if jsonl_count > 0:
        plan = get_claude_plan_info()
        print(f"✅ Claude Code JSONL — {jsonl_count} 个会话文件（{plan['label']}）")
    else:
        print("❌ Claude Code JSONL — 未找到会话文件")

    codex_sessions_dir = Path(CODEX_SESSIONS_DIR)
    codex_count = len(list(codex_sessions_dir.rglob("*.jsonl"))) if codex_sessions_dir.exists() else 0
    if codex_count > 0:
        plan = get_codex_plan_info()
        suffix = f"（{plan['label']}）" if plan.get("label") else ""
        print(f"✅ Codex JSONL — {codex_count} 个会话文件{suffix}")
    else:
        print("❌ Codex JSONL — 未找到会话文件")

    cmd = f'ssh -o ConnectTimeout=5 {GATEWAY_HOST} "sqlite3 {GATEWAY_DB} \\"SELECT COUNT(*) FROM llm_call_logs\\""'
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            count = result.stdout.strip()
            print(f"✅ LLM Gateway — {count} 条调用记录")
        else:
            print("❌ LLM Gateway — 连接失败或表为空")
    except Exception as e:
        print(f"❌ LLM Gateway — {e}")


# ─── 查询: daily ───

def query_daily(days=7):
    """按天汇总"""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    print(f"## Token 用量日报（最近 {days} 天）\n")

    cc_records = collect_claude_code()
    codex_records = collect_codex(max(days, 31))
    gw_records = collect_gateway(days)

    cc_daily = _aggregate_daily_records(cc_records, since)
    codex_daily = _aggregate_daily_records(codex_records, since)
    gw_daily = _aggregate_daily_records(gw_records, since)

    if cc_daily:
        plan = get_claude_plan_info()
        print(f"### Claude Code（本地，订阅制 {plan['label']}）\n")
        print("| 日期 | 总Token | 输入 | 输出 | 缓存写入 | 缓存读取 | 等效API费用 | 消息数 |")
        print("|------|---------|------|------|---------|---------|-----------|--------|")
        for day in sorted(cc_daily.keys()):
            d = cc_daily[day]
            print(f"| {day} | {fmt_tokens(d['total'])} | {fmt_tokens(d['input'])} | "
                  f"{fmt_tokens(d['output'])} | {fmt_tokens(d['cache_creation'])} | "
                  f"{fmt_tokens(d['cache_read'])} | {fmt_cost(d['cost'])} | {d['count']:,} |")
        cc_total_tokens = sum(d["total"] for d in cc_daily.values())
        cc_total_cost = sum(d["cost"] for d in cc_daily.values())
        cc_total_cache_read = sum(d["cache_read"] for d in cc_daily.values())
        cc_total_all_input = (
            cc_total_cache_read
            + sum(d["input"] for d in cc_daily.values())
            + sum(d["cache_creation"] for d in cc_daily.values())
        )
        cache_ratio = (cc_total_cache_read / cc_total_all_input * 100) if cc_total_all_input > 0 else 0
        num_days = len(cc_daily)
        daily_sub_cost = plan["price_yuan"] / 30 if plan["price_yuan"] else 0
        sub_cost_period = daily_sub_cost * num_days
        print(f"\nClaude Code 小计: {fmt_tokens(cc_total_tokens)} token, "
              f"等效API费用 {fmt_cost(cc_total_cost)}, "
              f"实付订阅 ~{fmt_cost(sub_cost_period)}（{num_days}天 × {fmt_cost(daily_sub_cost)}/天）, "
              f"缓存占比 {fmt_pct(cache_ratio)}\n")

    if codex_daily:
        plan = get_codex_plan_info()
        print(f"### Codex（本地，订阅制 {plan['label']}）\n")
        print("| 日期 | 总Token | 输入 | 输出 | 缓存读取 | 等效API费用 | 响应数 |")
        print("|------|---------|------|------|---------|-----------|--------|")
        for day in sorted(codex_daily.keys()):
            d = codex_daily[day]
            print(f"| {day} | {fmt_tokens(d['total'])} | {fmt_tokens(d['input'])} | "
                  f"{fmt_tokens(d['output'])} | {fmt_tokens(d['cache_read'])} | "
                  f"{fmt_cost(d['cost'])} | {d['count']:,} |")
        codex_total_tokens = sum(d["total"] for d in codex_daily.values())
        codex_total_cost = sum(d["cost"] for d in codex_daily.values())
        codex_total_cache_read = sum(d["cache_read"] for d in codex_daily.values())
        codex_total_all_input = codex_total_cache_read + sum(d["input"] for d in codex_daily.values())
        cache_ratio = (codex_total_cache_read / codex_total_all_input * 100) if codex_total_all_input > 0 else 0
        num_days = len(codex_daily)
        daily_sub_cost = plan["price_yuan"] / 30 if plan["price_yuan"] else 0
        sub_cost_period = daily_sub_cost * num_days
        print(f"\nCodex 小计: {fmt_tokens(codex_total_tokens)} token, "
              f"等效API费用 {fmt_cost(codex_total_cost)}, "
              f"实付订阅 ~{fmt_cost(sub_cost_period)}（{num_days}天 × {fmt_cost(daily_sub_cost)}/天）, "
              f"缓存占比 {fmt_pct(cache_ratio)}\n")

    if gw_daily:
        print("### LLM Gateway（线上）\n")
        print("| 日期 | 总Token | 输入 | 输出 | 费用 | 调用次数 | 平均延迟 |")
        print("|------|---------|------|------|------|---------|---------|")
        for day in sorted(gw_daily.keys()):
            d = gw_daily[day]
            avg_lat = f"{d['latency_sum'] // d['count']}ms" if d["count"] > 0 else "-"
            print(f"| {day} | {fmt_tokens(d['total'])} | {fmt_tokens(d['input'])} | "
                  f"{fmt_tokens(d['output'])} | {fmt_cost(d['cost'])} | {d['count']:,} | {avg_lat} |")
        gw_total_tokens = sum(d["total"] for d in gw_daily.values())
        gw_total_cost = sum(d["cost"] for d in gw_daily.values())
        print(f"\nLLM Gateway 小计: {fmt_tokens(gw_total_tokens)} token, {fmt_cost(gw_total_cost)}\n")

    if cc_daily or codex_daily or gw_daily:
        print("### 汇总\n")
        sub_cost_total = 0.0
        if cc_daily:
            cc_plan = get_claude_plan_info()
            cc_sub_cost = cc_plan["price_yuan"] / 30 * len(cc_daily) if cc_plan["price_yuan"] else 0
            sub_cost_total += cc_sub_cost
            print(f"- Claude Code: 等效API费用 {fmt_cost(sum(d['cost'] for d in cc_daily.values()))}, 实付订阅 ~{fmt_cost(cc_sub_cost)}")
        if codex_daily:
            codex_plan = get_codex_plan_info()
            codex_sub_cost = codex_plan["price_yuan"] / 30 * len(codex_daily) if codex_plan["price_yuan"] else 0
            sub_cost_total += codex_sub_cost
            print(f"- Codex: 等效API费用 {fmt_cost(sum(d['cost'] for d in codex_daily.values()))}, 实付订阅 ~{fmt_cost(codex_sub_cost)}")
        if gw_daily:
            gw_cost = sum(d["cost"] for d in gw_daily.values())
            print(f"- LLM Gateway: 实际费用 {fmt_cost(gw_cost)}")
            print(f"- 合计实际支出: ~{fmt_cost(sub_cost_total + gw_cost)}")
        else:
            print(f"- 合计订阅支出: ~{fmt_cost(sub_cost_total)}")
        return

    print("无可用数据。请运行 `/token-usage sources` 检查数据源状态。")


# ─── 查询: top ───

def query_top(dimension="days", n=5):
    """排行榜"""
    cc_records = collect_claude_code()
    codex_records = collect_codex()
    gw_records = collect_gateway(90)
    all_records = cc_records + codex_records + gw_records

    if dimension == "days":
        print(f"## Token 消耗 Top {n}（按天）\n")
        daily = defaultdict(lambda: {"total": 0, "cost": 0.0, "count": 0})
        for r in all_records:
            d = daily[r["date"]]
            d["total"] += r["total_tokens"]
            d["cost"] += r["cost_yuan"]
            d["count"] += 1

        top = sorted(daily.items(), key=lambda x: x[1]["total"], reverse=True)[:n]
        print("| 排名 | 日期 | 总Token | 费用 | 消息/调用数 |")
        print("|------|------|---------|------|-----------|")
        for i, (day, d) in enumerate(top, 1):
            print(f"| {i} | {day} | {fmt_tokens(d['total'])} | {fmt_cost(d['cost'])} | {d['count']:,} |")

    elif dimension == "models":
        print(f"## Token 消耗 Top {n}（按模型）\n")
        models = defaultdict(lambda: {"total": 0, "cost": 0.0, "count": 0})
        for r in all_records:
            d = models[r["model"]]
            d["total"] += r["total_tokens"]
            d["cost"] += r["cost_yuan"]
            d["count"] += 1

        top = sorted(models.items(), key=lambda x: x[1]["total"], reverse=True)[:n]
        print("| 排名 | 模型 | 总Token | 费用 | 调用次数 |")
        print("|------|------|---------|------|---------|")
        for i, (model, d) in enumerate(top, 1):
            print(f"| {i} | {model} | {fmt_tokens(d['total'])} | {fmt_cost(d['cost'])} | {d['count']:,} |")

    elif dimension == "systems":
        print(f"## Token 消耗 Top {n}（按系统，仅 Gateway）\n")
        systems = defaultdict(lambda: {"total": 0, "cost": 0.0, "count": 0})
        for r in gw_records:
            sys_id = r.get("system", "") or "unknown"
            d = systems[sys_id]
            d["total"] += r["total_tokens"]
            d["cost"] += r["cost_yuan"]
            d["count"] += 1

        top = sorted(systems.items(), key=lambda x: x[1]["total"], reverse=True)[:n]
        print("| 排名 | 系统 | 总Token | 费用 | 调用次数 |")
        print("|------|------|---------|------|---------|")
        for i, (sys_id, d) in enumerate(top, 1):
            print(f"| {i} | {sys_id} | {fmt_tokens(d['total'])} | {fmt_cost(d['cost'])} | {d['count']:,} |")


# ─── 查询: model ───

def query_model():
    """按模型维度对比"""
    cc_records = collect_claude_code()
    codex_records = collect_codex()
    gw_records = collect_gateway(30)

    print("## 模型用量对比（最近 30 天）\n")

    models = defaultdict(lambda: {
        "provider": "",
        "source": set(),
        "total": 0,
        "input": 0,
        "output": 0,
        "cost": 0.0,
        "count": 0,
        "latency_sum": 0,
    })

    for r in cc_records + codex_records + gw_records:
        d = models[r["model"]]
        d["provider"] = r["provider"]
        d["source"].add(r["source"])
        d["total"] += r["total_tokens"]
        d["input"] += r["input_tokens"]
        d["output"] += r["output_tokens"]
        d["cost"] += r["cost_yuan"]
        d["count"] += 1
        d["latency_sum"] += r.get("latency_ms", 0)

    sorted_models = sorted(models.items(), key=lambda x: x[1]["cost"], reverse=True)

    print("| 模型 | Provider | 调用次数 | 输入Token | 输出Token | 等效API费用 | 来源 |")
    print("|------|----------|---------|----------|----------|-----------|------|")
    for model, d in sorted_models:
        sources = "+".join(sorted(d["source"]))
        print(f"| {model} | {d['provider']} | {d['count']:,} | "
              f"{fmt_tokens(d['input'])} | {fmt_tokens(d['output'])} | "
              f"{fmt_cost(d['cost'])} | {sources} |")

    total_cost = sum(d["cost"] for d in models.values())
    print(f"\n等效API费用合计: {fmt_cost(total_cost)}")
    print("（Claude Code / Codex 为订阅制，等效费用仅供参考，实付以订阅价为准）")


# ─── 查询: system ───

def query_system():
    """按业务系统维度（仅 Gateway）"""
    gw_records = collect_gateway(30)

    if not gw_records:
        print("## 业务系统用量\n\nLLM Gateway 无数据或连接失败。")
        return

    print("## 业务系统用量（最近 30 天，仅 LLM Gateway）\n")

    systems = defaultdict(lambda: {
        "total": 0,
        "input": 0,
        "output": 0,
        "cost": 0.0,
        "count": 0,
        "models": defaultdict(int),
    })

    for r in gw_records:
        sys_id = r.get("system", "") or "unknown"
        d = systems[sys_id]
        d["total"] += r["total_tokens"]
        d["input"] += r["input_tokens"]
        d["output"] += r["output_tokens"]
        d["cost"] += r["cost_yuan"]
        d["count"] += 1
        d["models"][r["model"]] += 1

    sorted_systems = sorted(systems.items(), key=lambda x: x[1]["cost"], reverse=True)

    print("| 系统 | 调用次数 | 输入Token | 输出Token | 费用 | 主要模型 |")
    print("|------|---------|----------|----------|------|---------|")
    for sys_id, d in sorted_systems:
        top_model = max(d["models"].items(), key=lambda x: x[1])[0] if d["models"] else "-"
        print(f"| {sys_id} | {d['count']:,} | {fmt_tokens(d['input'])} | "
              f"{fmt_tokens(d['output'])} | {fmt_cost(d['cost'])} | {top_model} |")


# ─── 查询: cost ───

def query_cost(period="monthly"):
    """费用深度分析"""
    cc_records = collect_claude_code()
    codex_records = collect_codex()
    gw_records = collect_gateway(90)

    print("## 费用分析\n")

    cc_total_cost = sum(r["cost_yuan"] for r in cc_records)
    cc_total_input = sum(r["input_tokens"] for r in cc_records)
    cc_total_output = sum(r["output_tokens"] for r in cc_records)
    cc_total_cache_creation = sum(r["cache_creation_tokens"] for r in cc_records)
    cc_total_cache_read = sum(r["cache_read_tokens"] for r in cc_records)
    cc_total_tokens = sum(r["total_tokens"] for r in cc_records)
    cc_msg_count = len(cc_records)
    cc_nocache_cost = 0.0
    for r in cc_records:
        pricing = CLAUDE_PRICING.get(r["model"])
        if pricing:
            nocache = (
                (r["input_tokens"] + r["cache_read_tokens"] + r["cache_creation_tokens"]) * pricing["input"]
                + r["output_tokens"] * pricing["output"]
            ) / 1_000_000 * USD_TO_CNY
            cc_nocache_cost += nocache
    cc_saved = cc_nocache_cost - cc_total_cost

    codex_total_cost = sum(r["cost_yuan"] for r in codex_records)
    codex_total_input = sum(r["input_tokens"] for r in codex_records)
    codex_total_output = sum(r["output_tokens"] for r in codex_records)
    codex_total_cache_read = sum(r["cache_read_tokens"] for r in codex_records)
    codex_total_tokens = sum(r["total_tokens"] for r in codex_records)
    codex_msg_count = len(codex_records)
    codex_nocache_cost = 0.0
    for r in codex_records:
        pricing = _get_openai_pricing(r["model"])
        if pricing:
            nocache = (
                (r["input_tokens"] + r["cache_read_tokens"]) * pricing["input"]
                + r["output_tokens"] * pricing["output"]
            ) / 1_000_000 * USD_TO_CNY
            codex_nocache_cost += nocache
    codex_saved = codex_nocache_cost - codex_total_cost

    gw_total_cost = sum(r["cost_yuan"] for r in gw_records)
    gw_total_tokens = sum(r["total_tokens"] for r in gw_records)
    gw_call_count = len(gw_records)

    print("### 总览\n")
    print("| 来源 | 计费方式 | Token总量 | 消息数 | 等效API费用 | 无缓存等效 | 缓存节省率 |")
    print("|------|---------|----------|--------|-----------|----------|-----------|")

    if cc_records:
        plan = get_claude_plan_info()
        save_rate = (cc_saved / cc_nocache_cost * 100) if cc_nocache_cost > 0 else 0
        print(f"| Claude Code | 订阅 {plan['label']} | {fmt_tokens(cc_total_tokens)} | {cc_msg_count:,} | "
              f"{fmt_cost(cc_total_cost)} | {fmt_cost(cc_nocache_cost)} | {fmt_pct(save_rate)} |")

    if codex_records:
        plan = get_codex_plan_info()
        save_rate = (codex_saved / codex_nocache_cost * 100) if codex_nocache_cost > 0 else 0
        print(f"| Codex | 订阅 {plan['label']} | {fmt_tokens(codex_total_tokens)} | {codex_msg_count:,} | "
              f"{fmt_cost(codex_total_cost)} | {fmt_cost(codex_nocache_cost)} | {fmt_pct(save_rate)} |")

    if gw_records:
        print(f"| LLM Gateway | 按量付费 | {fmt_tokens(gw_total_tokens)} | {gw_call_count:,} | "
              f"{fmt_cost(gw_total_cost)} | - | - |")

    print("\n### 实际支出估算\n")
    print("| 项目 | 金额 | 说明 |")
    print("|------|------|------|")
    if cc_records:
        plan = get_claude_plan_info()
        print(f"| Claude Code 订阅 | {fmt_cost(plan['price_yuan'])}/月 | 固定月费，不按 token 计费 |")
        print(f"| → 等效API费用 | {fmt_cost(cc_total_cost)} | 如果按 API 计费需要这么多（已含缓存折扣） |")
        print(f"| → 订阅赚到 | {fmt_cost(cc_total_cost - plan['price_yuan'])} | 等效API费 - 订阅费 |")
    if codex_records:
        plan = get_codex_plan_info()
        print(f"| Codex 订阅 | {fmt_cost(plan['price_yuan'])}/月 | 固定月费，不按 token 计费 |")
        print(f"| → 等效API费用 | {fmt_cost(codex_total_cost)} | 按 OpenAI API 公开价估算 |")
        print(f"| → 订阅赚到 | {fmt_cost(codex_total_cost - plan['price_yuan'])} | 等效API费 - 订阅费 |")
    if gw_records:
        print(f"| LLM Gateway | {fmt_cost(gw_total_cost)} | 按量，实际支出 |")
    print()

    total_api_cost = cc_total_cost + codex_total_cost + gw_total_cost
    print("### 单位成本（按等效API费用）\n")
    print("| 指标 | 值 |")
    print("|------|-----|")

    total_msgs = cc_msg_count + codex_msg_count + gw_call_count
    if total_msgs > 0:
        print(f"| 每条消息/调用平均费用 | {fmt_cost(total_api_cost / total_msgs)} |")

    all_tokens = cc_total_tokens + codex_total_tokens + gw_total_tokens
    if all_tokens > 0:
        cost_per_m = total_api_cost / all_tokens * 1_000_000
        print(f"| 每百万 token 平均费用 | {fmt_cost(cost_per_m)} |")

    daily_cost = defaultdict(float)
    for r in cc_records + codex_records + gw_records:
        daily_cost[r["date"]] += r["cost_yuan"]
    if daily_cost:
        max_day = max(daily_cost.items(), key=lambda x: x[1])
        print(f"| 最贵单日 | {max_day[0]} {fmt_cost(max_day[1])} |")

    if cc_records:
        print("\n### Claude Code Prompt Cache 效率\n")
        print("| 指标 | 值 |")
        print("|------|-----|")
        all_input_side = cc_total_input + cc_total_cache_creation + cc_total_cache_read
        if all_input_side > 0:
            print(f"| 缓存读取占比 | {fmt_pct(cc_total_cache_read / all_input_side * 100)} |")
            print(f"| 缓存写入占比 | {fmt_pct(cc_total_cache_creation / all_input_side * 100)} |")
            print(f"| 原始输入占比 | {fmt_pct(cc_total_input / all_input_side * 100)} |")
        print(f"| 实际花费 | {fmt_cost(cc_total_cost)} |")
        print(f"| 无缓存等效花费 | {fmt_cost(cc_nocache_cost)} |")
        print(f"| 缓存节省 | {fmt_cost(cc_saved)} ({fmt_pct(cc_saved / cc_nocache_cost * 100 if cc_nocache_cost > 0 else 0)}) |")

    if codex_records:
        print("\n### Codex Prompt Cache 效率\n")
        print("| 指标 | 值 |")
        print("|------|-----|")
        all_input_side = codex_total_input + codex_total_cache_read
        if all_input_side > 0:
            print(f"| 缓存读取占比 | {fmt_pct(codex_total_cache_read / all_input_side * 100)} |")
            print(f"| 原始输入占比 | {fmt_pct(codex_total_input / all_input_side * 100)} |")
        print(f"| 实际花费 | {fmt_cost(codex_total_cost)} |")
        print(f"| 无缓存等效花费 | {fmt_cost(codex_nocache_cost)} |")
        print(f"| 缓存节省 | {fmt_cost(codex_saved)} ({fmt_pct(codex_saved / codex_nocache_cost * 100 if codex_nocache_cost > 0 else 0)}) |")


# ─── 查询: detail ───

def query_detail(target_date=None):
    """指定日期的详细记录"""
    if not target_date:
        target_date = datetime.now().strftime("%Y-%m-%d")

    print(f"## Token 详情 | {target_date}\n")

    cc_records = [r for r in collect_claude_code() if r["date"] == target_date]
    if cc_records:
        by_model = defaultdict(lambda: {
            "input": 0,
            "output": 0,
            "cache_creation": 0,
            "cache_read": 0,
            "total": 0,
            "cost": 0.0,
            "count": 0,
        })
        for r in cc_records:
            d = by_model[r["model"]]
            d["input"] += r["input_tokens"]
            d["output"] += r["output_tokens"]
            d["cache_creation"] += r["cache_creation_tokens"]
            d["cache_read"] += r["cache_read_tokens"]
            d["total"] += r["total_tokens"]
            d["cost"] += r["cost_yuan"]
            d["count"] += 1

        print("### Claude Code\n")
        print("| 模型 | 消息数 | 输入 | 输出 | 缓存写入 | 缓存读取 | 总Token | 费用 |")
        print("|------|--------|------|------|---------|---------|---------|------|")
        for model, d in sorted(by_model.items(), key=lambda x: x[1]["cost"], reverse=True):
            print(f"| {model} | {d['count']:,} | {fmt_tokens(d['input'])} | "
                  f"{fmt_tokens(d['output'])} | {fmt_tokens(d['cache_creation'])} | "
                  f"{fmt_tokens(d['cache_read'])} | {fmt_tokens(d['total'])} | {fmt_cost(d['cost'])} |")
        print()

    codex_records = [r for r in collect_codex() if r["date"] == target_date]
    if codex_records:
        by_model = defaultdict(lambda: {
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "total": 0,
            "cost": 0.0,
            "count": 0,
        })
        for r in codex_records:
            d = by_model[r["model"]]
            d["input"] += r["input_tokens"]
            d["output"] += r["output_tokens"]
            d["cache_read"] += r["cache_read_tokens"]
            d["total"] += r["total_tokens"]
            d["cost"] += r["cost_yuan"]
            d["count"] += 1

        print("### Codex\n")
        print("| 模型 | 响应数 | 输入 | 输出 | 缓存读取 | 总Token | 费用 |")
        print("|------|--------|------|------|---------|---------|------|")
        for model, d in sorted(by_model.items(), key=lambda x: x[1]["cost"], reverse=True):
            print(f"| {model} | {d['count']:,} | {fmt_tokens(d['input'])} | "
                  f"{fmt_tokens(d['output'])} | {fmt_tokens(d['cache_read'])} | "
                  f"{fmt_tokens(d['total'])} | {fmt_cost(d['cost'])} |")
        print()

    gw_records = [r for r in collect_gateway(30) if r["date"] == target_date]
    if gw_records:
        by_model = defaultdict(lambda: {
            "input": 0,
            "output": 0,
            "total": 0,
            "cost": 0.0,
            "count": 0,
            "latency_sum": 0,
            "systems": set(),
        })
        for r in gw_records:
            d = by_model[r["model"]]
            d["input"] += r["input_tokens"]
            d["output"] += r["output_tokens"]
            d["total"] += r["total_tokens"]
            d["cost"] += r["cost_yuan"]
            d["count"] += 1
            d["latency_sum"] += r.get("latency_ms", 0)
            if r.get("system"):
                d["systems"].add(r["system"])

        print("### LLM Gateway\n")
        print("| 模型 | 调用次数 | 输入 | 输出 | 费用 | 平均延迟 | 系统 |")
        print("|------|---------|------|------|------|---------|------|")
        for model, d in sorted(by_model.items(), key=lambda x: x[1]["cost"], reverse=True):
            avg_lat = f"{d['latency_sum'] // d['count']}ms" if d["count"] > 0 else "-"
            systems = ", ".join(sorted(d["systems"])) if d["systems"] else "-"
            print(f"| {model} | {d['count']:,} | {fmt_tokens(d['input'])} | "
                  f"{fmt_tokens(d['output'])} | {fmt_cost(d['cost'])} | {avg_lat} | {systems} |")
        print()

    if not cc_records and not codex_records and not gw_records:
        print(f"{target_date} 无数据。")


# ─── 主入口 ───

def main():
    args = sys.argv[1:]

    if not args:
        query_daily(7)
        return

    cmd = args[0]

    if cmd == "daily":
        days = int(args[1]) if len(args) > 1 else 7
        query_daily(days)

    elif cmd == "top":
        dim = args[1] if len(args) > 1 else "days"
        n = int(args[2]) if len(args) > 2 else 5
        query_top(dim, n)

    elif cmd == "model":
        query_model()

    elif cmd == "system":
        query_system()

    elif cmd == "cost":
        period = args[1] if len(args) > 1 else "monthly"
        query_cost(period)

    elif cmd == "detail":
        target = args[1] if len(args) > 1 else None
        query_detail(target)

    elif cmd == "sources":
        check_sources()

    else:
        print(f"未知命令: {cmd}")
        print("可用命令: daily, top, model, system, cost, detail, sources")
        sys.exit(1)


if __name__ == "__main__":
    main()
