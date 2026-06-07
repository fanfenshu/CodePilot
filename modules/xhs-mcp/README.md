# xhs-mcp — 小红书 MCP 集成模块

可复用的小红书 MCP 通信、登录管理、搜索与发布模块。

## 核心能力

| 功能 | 函数/方法 | 说明 |
|------|----------|------|
| MCP 通信 | `mcp_post()` | curl Popen 流式 SSE 读取，拿到结果立即返回 |
| Session 管理 | `mcp_ensure_session()` | 自动握手 + session 缓存 + 过期重试 |
| 工具调用 | `mcp_call()` | 调用任意 MCP 工具，自动握手和重试 |
| 健康检查 | `health_check()` | 快速检测 MCP 是否响应 |
| 进程管理 | `ensure_running()` | 自动下载二进制、启动进程 |
| 状态查询 | `check_status()` | 带缓存的登录状态查询，防轮询堆积 |
| 发布笔记 | `publish()` | 完整流程：启动/复用 MCP → 登录检查 → 发布 → 清理 |
| 搜索账号 | `search_accounts()` | 搜索 + 粉丝查询 + Markdown 表格生成 |

## 快速开始

### 面向对象 API

```python
from xhs_mcp import XhsMcp

mcp = XhsMcp(port=18060)

# 检查状态
status = mcp.status()
print(f"运行中: {status['running']}, 已登录: {status['loggedIn']}")

# 获取登录二维码
qr_data, mime = mcp.qrcode()

# 发布笔记
ok, msg, need_login = mcp.publish(
    title='测试标题',
    content='测试内容',
    images=['/path/to/image.jpg'],
    tags=['标签1', '标签2']
)

# 搜索账号
table_md, context = mcp.search_accounts(['跨境电商私域'])
```

### 函数式 API

```python
from xhs_mcp import mcp_call, mcp_text, publish, check_status

# 调用任意 MCP 工具
result = mcp_call('check_login_status', port=18060)
print(mcp_text(result))

# 发布
ok, msg, need_login = publish(
    title='标题', content='正文',
    images=['/path/to/img.jpg'], port=18060
)
```

### 在其他系统中集成

```python
import sys, os
_modules_path = os.path.join(os.path.dirname(__file__), '..', '..',
                             'CodePilot', 'source', 'modules', 'xhs-mcp')
sys.path.insert(0, _modules_path)

from xhs_mcp import XhsMcp
```

## 关键设计决策

1. **curl Popen 流式读取**：MCP 通过 SSE 返回结果但保持连接。用 Popen 读到第一个 `data:` 结果即杀掉 curl，避免等待连接关闭（原来延迟 ~1 分钟，现在秒级返回）
2. **Cookie 文件持久登录**：MCP 通过 `COOKIES_PATH` 环境变量管理登录态——登录成功后自动保存 cookies 到文件，重启时从文件加载恢复。`-rod dir=...` 仅影响 Chromium 用户数据目录，对登录持久化无作用
3. **状态缓存防堆积**：MCP 查询需 7-13 秒，前端 3-5 秒轮询会堆积请求。8 秒缓存 + 并发保护解决
4. **成功后才清理**：仅发布成功后清理 Chromium 释放内存（1.6GB 服务器友好），失败时保留登录会话便于重试
5. **Accept 双类型**：MCP 要求 `Accept: application/json, text/event-stream`，缺一不可
6. **防 LLM 数据篡改**：搜索表格由 Python 直接构建，LLM 只做策略分析不接触数字

## 创作者中心模块 (creator_center.py)

独立于 MCP 的创作者中心（creator.xiaohongshu.com）管理模块，提供 SMS 登录和笔记数据采集。

### 核心能力

| 功能 | 方法 | 说明 |
|------|------|------|
| Chrome 管理 | `ensure_chrome()` | 持久化 Chrome 实例，跨服务重启保持登录态 |
| SMS 登录 | `start_login(phone)` | 自动填手机号 + CDP 鼠标事件发送验证码（绕过反爬） |
| 提交验证码 | `submit_code(code)` | CDP 鼠标事件点击登录按钮 |
| 数据采集 | `fetch_views(note_ids)` | Vue DOM 提取 + Network 拦截双策略 |
| 登录状态 | `is_logged_in()` | 持久化标记，不启动 Chrome |

### 用法

```python
from creator_center import CreatorCenter

cc = CreatorCenter(user_data_dir='~/data/creator-center-chrome')

# SMS 登录
result, err = cc.start_login(phone='13800138000')
# 用户收到验证码后
result = cc.submit_code('123456')

# 获取笔记数据
data, err = cc.fetch_views(['noteId1', 'noteId2'])
# data = {'noteId1': {'views': 100, 'likes': 10, 'comments': 5, ...}}

# 检查登录状态（读本地标记，无网络请求）
logged_in = cc.is_logged_in()
```

### 关键设计

1. **持久化 Chrome**：`user-data-dir` 保存登录 cookies，`start_new_session=True` 脱离 systemd cgroup
2. **CDP 鼠标事件**：JS `.click()` 被反爬拦截，改用 `Input.dispatchMouseEvent` 模拟真实点击
3. **Network 拦截**：API 有签名校验，直接 `fetch()` 返回 `code:-1`。通过 CDP Network 域捕获页面自身的已签名请求
4. **进程扫描复用**：服务重启后通过 `pgrep` 找到已有 Chrome 进程，避免重复启动

### 命令行

```bash
python creator_center.py login 13800138000   # 发送验证码
python creator_center.py code 123456         # 提交验证码
python creator_center.py status              # 检查登录状态
python creator_center.py <noteId>            # 获取笔记数据
```

## 依赖

- `curl` 命令行工具（xhs_mcp.py）
- `xiaohongshu-mcp` Go 二进制（调用 `ensure_running()` 可自动下载）
- `websocket-client`（creator_center.py 的 Network 拦截备用方案）
- Python 3.8+ 标准库

## 时间预算

| 操作 | 正常耗时 | 最坏耗时 |
|-----|---------|---------|
| 发布笔记 | 10-30s | 180s |
| 搜索（2关键词并行） | 5-8s | 45s |
| 粉丝查询（7个串行） | 15-25s | 105s |
| 登录状态查询 | <1s（缓存命中） | 13s |
