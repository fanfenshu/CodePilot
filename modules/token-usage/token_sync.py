#!/usr/bin/env python3
"""
Token Sync — 将本地订阅型 LLM 日志推送到 VickClaw 线上服务

当前支持:
  - Claude Code JSONL
  - Codex JSONL

能力:
  1. 聚合推送: 按 source + date + model 聚合后 POST 到 VickClaw
  2. 原始日志同步: rsync 本地 Claude/Codex 日志到服务器本地缓存目录
  3. 本机刷新桥: 启动 localhost 服务，供 VickClaw 页面按钮直接触发“本机日志 -> 线上刷新”

用法:
  python3 token_sync.py                     # 原始日志同步 + 聚合推送
  python3 token_sync.py --dry-run          # 仅显示将推送的数据
  python3 token_sync.py --refresh-now      # 立即同步原始日志并触发线上 manual-refresh
  python3 token_sync.py --serve            # 启动 localhost 刷新桥
  python3 token_sync.py --url http://...   # 自定义目标 URL（跳过 SSH 隧道）

Cron 配置:
  0 */2 * * * /usr/bin/python3 /path/to/token_sync.py >> /tmp/token_sync.log 2>&1
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from token_usage import (
    collect_claude_code,
    collect_codex,
    get_source_subscription_meta,
)

# SSH 隧道配置（Cloudflare 拦截直接 POST，需要通过 SSH 隧道）
SSH_HOST = "deploy@47.107.157.5"
REMOTE_PORT = 9015
LOCAL_TUNNEL_PORT = 19015
DEFAULT_URL = f"http://127.0.0.1:{LOCAL_TUNNEL_PORT}/api/token-stats/sync"
SSH_CONNECT_OPTIONS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
]
RSYNC_RSH = "ssh -o BatchMode=yes -o ConnectTimeout=10"

# 原始日志同步
REMOTE_CC_LOG_DIR = "~/.claude/projects-local/"
REMOTE_CODEX_SESSIONS_DIR = "~/.codex/sessions-local/"
REMOTE_CODEX_AUTH_FILE = "~/.codex/auth-local.json"
LOCAL_CC_LOG_DIR = Path.home() / ".claude" / "projects"
LOCAL_CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
LOCAL_CODEX_AUTH_FILE = Path.home() / ".codex" / "auth.json"

# 本机刷新桥
LOCAL_BRIDGE_HOST = "127.0.0.1"
LOCAL_BRIDGE_PORT = 19016
ALLOWED_ORIGINS = {
    "https://vickclaw.flyranking.com",
    "https://vickclaw.com",
    "http://127.0.0.1:9015",
    "http://localhost:9015",
}
_REFRESH_LOCK = threading.Lock()


def setup_ssh_tunnel():
    """建立 SSH 隧道"""
    cmd = [
        "ssh", "-f", "-N",
        *SSH_CONNECT_OPTIONS,
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-L", f"{LOCAL_TUNNEL_PORT}:127.0.0.1:{REMOTE_PORT}",
        SSH_HOST,
    ]
    try:
        subprocess.run(cmd, check=True, timeout=15)
        time.sleep(1)
        return True
    except Exception as e:
        print(f"  SSH 隧道建立失败: {e}")
        return False


def teardown_ssh_tunnel():
    """关闭 SSH 隧道"""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{LOCAL_TUNNEL_PORT}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.stdout.strip():
            for pid in result.stdout.strip().split("\n"):
                os.kill(int(pid), signal.SIGTERM)
    except Exception:
        pass


def _run_cmd(cmd, timeout=60, check=True):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=check)


def _count_files(path):
    if not path.exists():
        return 0
    if path.is_file():
        return 1
    return sum(1 for p in path.rglob("*") if p.is_file())


def ensure_remote_token_dirs():
    cmd = [
        "ssh", *SSH_CONNECT_OPTIONS, SSH_HOST,
        "mkdir -p ~/.claude/projects-local ~/.codex/sessions-local ~/.codex",
    ]
    try:
        _run_cmd(cmd, timeout=20)
        return True
    except Exception as e:
        print(f"  远端目录创建失败: {e}")
        return False


def _rsync_to_server(local_path, remote_path, delete=False):
    if not local_path.exists():
        return {
            "ok": False,
            "skipped": True,
            "local_path": str(local_path),
            "remote_path": remote_path,
            "file_count": 0,
            "error": "local path missing",
        }

    cmd = ["rsync", "-az", "--contimeout=10", "--timeout=20", "-e", RSYNC_RSH]
    if delete and local_path.is_dir():
        cmd.append("--delete")
    src = str(local_path)
    if local_path.is_dir():
        src = src.rstrip("/") + "/"
    cmd.extend([src, f"{SSH_HOST}:{remote_path}"])

    try:
        _run_cmd(cmd, timeout=300)
        return {
            "ok": True,
            "skipped": False,
            "local_path": str(local_path),
            "remote_path": remote_path,
            "file_count": _count_files(local_path),
        }
    except Exception as e:
        return {
            "ok": False,
            "skipped": False,
            "local_path": str(local_path),
            "remote_path": remote_path,
            "file_count": _count_files(local_path),
            "error": str(e),
        }


def sync_raw_logs_to_server():
    """同步本地原始日志到服务器本地缓存目录。"""
    result = {
        "remote_dirs_ready": ensure_remote_token_dirs(),
        "claude": _rsync_to_server(LOCAL_CC_LOG_DIR, REMOTE_CC_LOG_DIR, delete=True),
        "codex_sessions": _rsync_to_server(LOCAL_CODEX_SESSIONS_DIR, REMOTE_CODEX_SESSIONS_DIR, delete=True),
        "codex_auth": _rsync_to_server(LOCAL_CODEX_AUTH_FILE, REMOTE_CODEX_AUTH_FILE, delete=False),
    }
    result["ok"] = all(
        item.get("ok") or item.get("skipped")
        for key, item in result.items()
        if isinstance(item, dict)
    ) and result["remote_dirs_ready"]
    return result


def aggregate_records(records):
    """按 source + date + model 聚合记录"""
    agg = defaultdict(lambda: {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 0,
        "cost_yuan": 0.0,
        "message_count": 0,
        "provider": "",
        "source": "",
    })

    for r in records:
        key = (r["source"], r["date"], r["model"])
        d = agg[key]
        d["input_tokens"] += r.get("input_tokens", 0)
        d["output_tokens"] += r.get("output_tokens", 0)
        d["cache_creation_tokens"] += r.get("cache_creation_tokens", 0)
        d["cache_read_tokens"] += r.get("cache_read_tokens", 0)
        d["total_tokens"] += r.get("total_tokens", 0)
        d["cost_yuan"] += r.get("cost_yuan", 0.0)
        d["message_count"] += 1
        d["provider"] = r.get("provider", "")
        d["source"] = r.get("source", "")

    result = []
    for (source, date, model), d in sorted(agg.items()):
        result.append({
            "source": source,
            "date": date,
            "model": model,
            "provider": d["provider"],
            "input_tokens": d["input_tokens"],
            "output_tokens": d["output_tokens"],
            "cache_creation_tokens": d["cache_creation_tokens"],
            "cache_read_tokens": d["cache_read_tokens"],
            "total_tokens": d["total_tokens"],
            "cost_yuan": round(d["cost_yuan"], 2),
            "message_count": d["message_count"],
        })
    return result


def split_by_source(records):
    grouped = defaultdict(list)
    for record in records:
        grouped[record["source"]].append(record)
    return grouped


def push_to_server(source, records, url, source_meta):
    """POST 单个来源的聚合数据到 VickClaw"""
    payload = json.dumps({
        "source": source,
        "source_meta": {
            "billing": source_meta.get("billing", ""),
            "plan_type": source_meta.get("plan_type", ""),
            "plan_label": source_meta.get("label", ""),
            "subscription_cost_yuan": source_meta.get("price_yuan", 0),
        },
        "records": records,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"HTTP Error {e.code}: {e.read().decode('utf-8', errors='replace')}")
        return None
    except Exception as e:
        print(f"Error: {e}")
        return None


def trigger_remote_manual_refresh(url):
    payload = json.dumps({
        "source": "manual-refresh",
        "records": [],
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def collect_local_records():
    print("  采集 Claude Code JSONL...")
    cc_records = collect_claude_code()
    print(f"  Claude Code 原始记录: {len(cc_records)} 条")

    print("  采集 Codex JSONL...")
    codex_records = collect_codex()
    print(f"  Codex 原始记录: {len(codex_records)} 条")

    return cc_records + codex_records


def push_aggregated_records(url, dry_run=False):
    raw_records = collect_local_records()
    if not raw_records:
        print("  无数据，跳过")
        return {"ok": True, "synced": 0, "gateway_refreshed": 0, "sources": {}}

    aggregated = aggregate_records(raw_records)
    grouped = split_by_source(aggregated)
    print(f"  聚合后来源数: {len(grouped)}，总记录数: {len(aggregated)}")

    if dry_run:
        print("\n  [DRY RUN] 将推送以下数据:")
        for source, rows in sorted(grouped.items()):
            meta = get_source_subscription_meta(source)
            print(f"    - {source} | {meta.get('label', source)} | {len(rows)} 条")
            for r in rows[-5:]:
                print(f"      {r['date']} | {r['model']} | {r['message_count']} msgs | "
                      f"{r['total_tokens']:,} tokens | ¥{r['cost_yuan']:.2f}")
            if len(rows) > 5:
                print(f"      ... 共 {len(rows)} 条")
        return {
            "ok": True,
            "dry_run": True,
            "source_counts": {k: len(v) for k, v in grouped.items()},
        }

    total_synced = 0
    total_gw = 0
    source_results = {}
    for source, rows in sorted(grouped.items()):
        meta = get_source_subscription_meta(source)
        label = meta.get("label") or source
        print(f"  推送 {source}（{label}）到 {url} ...")
        result = push_to_server(source, rows, url, meta)
        if result:
            synced = result.get("synced", 0)
            total_synced += synced
            total_gw += result.get("gateway_refreshed", 0)
            source_results[source] = result
            print(f"    成功: synced={synced}, gateway_refreshed={result.get('gateway_refreshed', 0)}")
        else:
            source_results[source] = {"ok": False}
            print(f"    推送失败: {source}")

    print(f"  汇总: synced={total_synced}, gateway_refreshed={total_gw}")
    return {
        "ok": True,
        "synced": total_synced,
        "gateway_refreshed": total_gw,
        "sources": source_results,
    }


def run_sync_job(url, dry_run=False, sync_raw=True):
    result = {
        "raw_log_sync": None,
        "push_result": None,
    }
    if sync_raw and not dry_run:
        print("  同步原始日志到服务器...")
        result["raw_log_sync"] = sync_raw_logs_to_server()
        print(f"  原始日志同步: {'成功' if result['raw_log_sync']['ok'] else '部分失败'}")
    result["push_result"] = push_aggregated_records(url, dry_run=dry_run)
    result["ok"] = True
    return result


def run_refresh_now(url, manage_tunnel=False, attempt_raw_sync=False):
    started_at = time.time()
    tunnel_ready = True
    try:
        if manage_tunnel:
            tunnel_ready = setup_ssh_tunnel()
            if not tunnel_ready:
                raise RuntimeError("无法建立 SSH 隧道，远端刷新未执行")

        with _REFRESH_LOCK:
            push_result = push_aggregated_records(url, dry_run=False)
            raw_log_sync = None
            remote_refresh = None

            if attempt_raw_sync:
                try:
                    raw_log_sync = sync_raw_logs_to_server()
                except Exception as e:
                    raw_log_sync = {"ok": False, "error": f"raw log sync failed: {e}"}

                if raw_log_sync and raw_log_sync.get("ok"):
                    try:
                        remote_refresh = trigger_remote_manual_refresh(url)
                    except Exception as e:
                        remote_refresh = {"ok": False, "error": str(e)}

        return {
            "ok": bool(push_result and push_result.get("ok")),
            "push_result": push_result,
            "raw_log_sync": raw_log_sync,
            "remote_refresh": remote_refresh,
            "duration_ms": int((time.time() - started_at) * 1000),
            "tunnel_managed": manage_tunnel,
            "refresh_mode": "direct-push" if not attempt_raw_sync else "direct-push+raw-sync",
        }
    finally:
        if manage_tunnel and tunnel_ready:
            teardown_ssh_tunnel()


class TokenRefreshBridgeHandler(BaseHTTPRequestHandler):
    server_version = "TokenSyncBridge/1.0"

    def _allow_origin(self):
        origin = self.headers.get("Origin", "")
        return origin if origin in ALLOWED_ORIGINS else "*"

    def _set_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", self._allow_origin())
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Cache-Control", "no-store")

    def do_OPTIONS(self):
        self.send_response(204)
        self._set_cors_headers()
        self.end_headers()

    def do_GET(self):
        if self.path != "/health":
            self.send_response(404)
            self._set_cors_headers()
            self.end_headers()
            return
        payload = json.dumps({
            "ok": True,
            "service": "token-sync-bridge",
            "port": LOCAL_BRIDGE_PORT,
            "time": datetime.now().isoformat(),
        }).encode("utf-8")
        self.send_response(200)
        self._set_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        if self.path != "/refresh":
            self.send_response(404)
            self._set_cors_headers()
            self.end_headers()
            return

        try:
            result = run_refresh_now(DEFAULT_URL, manage_tunnel=True, attempt_raw_sync=False)
            payload = json.dumps(result).encode("utf-8")
            self.send_response(200)
        except Exception as e:
            payload = json.dumps({"ok": False, "error": str(e)}).encode("utf-8")
            self.send_response(500)

        self._set_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        print(f"[token-sync-bridge] {self.address_string()} - {fmt % args}")


def serve_bridge():
    httpd = ThreadingHTTPServer((LOCAL_BRIDGE_HOST, LOCAL_BRIDGE_PORT), TokenRefreshBridgeHandler)
    print(f"[token-sync-bridge] listening on http://{LOCAL_BRIDGE_HOST}:{LOCAL_BRIDGE_PORT}")
    httpd.serve_forever()


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    serve_mode = "--serve" in args
    refresh_now = "--refresh-now" in args
    skip_raw_sync = "--skip-raw-sync" in args

    custom_url = None
    for i, a in enumerate(args):
        if a == "--url" and i + 1 < len(args):
            custom_url = args[i + 1]

    if serve_mode:
        serve_bridge()
        return

    use_tunnel = custom_url is None
    url = custom_url or DEFAULT_URL

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Token Sync 开始")

    if use_tunnel:
        print("  建立 SSH 隧道...")
        if not setup_ssh_tunnel():
            print("  无法建立隧道，中止")
            return

    try:
        if refresh_now:
            result = run_refresh_now(url, attempt_raw_sync=True)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        run_sync_job(url, dry_run=dry_run, sync_raw=not skip_raw_sync)
    finally:
        if use_tunnel:
            teardown_ssh_tunnel()

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Token Sync 完成\n")


if __name__ == "__main__":
    main()
