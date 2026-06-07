"""
URL 内容提取模块（可复用）

微信链接四层降级：
  Layer 0: URL 标准化（短链展开 + 参数清洗）
  Layer 1: wiki API DB 缓存（articles + url_cache 表，<100ms）
  Layer 2: Session 代理抓取（We-MP-RSS 登录态 cookies，2-5s）
  Layer 3: 直连 We-MP-RSS DB 文件（wiki 服务不可用时兜底）
  Layer 4: 模块内置微信 curl（大概率被反爬拦截，最后尝试）

非微信链接：通用网页提取（article/main 标签）

零外部依赖（仅 stdlib）。

用法:
    from url_extractor import extract_url_content, extract_urls_from_message, build_context_injection
    result = extract_url_content('https://mp.weixin.qq.com/s/xxx')
    injection = build_context_injection([result])
"""

import re
import subprocess
import time

# --- 内存缓存（LRU-like，TTL 30 分钟） ---
_url_cache = {}  # {url: (expire_ts, result_dict)}
_CACHE_TTL = 1800  # 30 minutes
_CACHE_MAX = 100


def _cache_get(url):
    cached = _url_cache.get(url)
    if cached and cached[0] > time.time():
        return cached[1]
    return None


def _cache_set(url, result):
    _url_cache[url] = (time.time() + _CACHE_TTL, result)
    # 超限清理过期条目
    if len(_url_cache) > _CACHE_MAX:
        now = time.time()
        expired = [k for k, v in _url_cache.items() if v[0] <= now]
        for k in expired:
            del _url_cache[k]


# --- HTML 清洗 ---

def _html_to_text(html, max_chars=3000):
    """HTML → 纯文本：去除 script/style → 去除标签 → 合并空白 → 截断"""
    if not html:
        return ''
    text = re.sub(r'<script[^>]*>[\s\S]*?</script>', '', html, flags=re.IGNORECASE)
    text = re.sub(r'<style[^>]*>[\s\S]*?</style>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:max_chars]


def _extract_title_from_html(html):
    """从 HTML 中提取标题：og:title → <title>"""
    if not html:
        return None
    m = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)', html, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r'<title[^>]*>([^<]+)</title>', html, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def _extract_source_from_html(html):
    """从微信 HTML 中提取公众号名称"""
    if not html:
        return None
    m = re.search(r'var\s+nickname\s*=\s*["\']([^"\']+)', html)
    if m:
        return m.group(1).strip()
    return None


# --- Layer 0: URL 标准化 ---

def _normalize_wx_url(url):
    """标准化微信文章 URL：短链展开 + 参数清洗 + 统一 https。
    避免同一篇文章因 URL 参数不同导致缓存 miss。"""
    try:
        # 短链检测：/s/ 后跟 Base64 字符（无 ? 参数）
        if re.match(r'https?://mp\.weixin\.qq\.com/s/[A-Za-z0-9_-]+$', url):
            try:
                cmd = ['curl', '-s', '-o', '/dev/null', '-w', '%{url_effective}',
                       '-L', '--max-time', '5', url]
                result = subprocess.run(cmd, capture_output=True, timeout=8)
                final_url = result.stdout.decode().strip()
                if final_url and 'mp.weixin.qq.com' in final_url and len(final_url) > len(url):
                    url = final_url
            except Exception:
                pass  # 展开失败用原 URL

        # 参数清洗：保留核心参数，去掉追踪参数
        from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
        parsed = urlparse(url)
        if parsed.query:
            params = parse_qs(parsed.query)
            core_keys = {'__biz', 'mid', 'idx', 'sn'}
            clean_params = {k: v[0] for k, v in params.items() if k in core_keys}
            if clean_params:
                url = urlunparse(parsed._replace(query=urlencode(clean_params), scheme='https'))
    except Exception:
        pass
    return url


# --- Layer 1: wiki API DB 缓存查询（articles + url_cache） ---

def _fetch_via_wiki_api(url, wiki_base='http://127.0.0.1:8082', timeout=5):
    """通过 wiki dashboard API 查询 DB 缓存（articles + url_cache 两张表）。
    返回 dict {content, title, source} 或 None。"""
    import urllib.request
    import urllib.parse
    import urllib.error
    import json as _json
    encoded_url = urllib.parse.quote(url, safe='')
    try:
        api_url = f'{wiki_base}/api/wx-db-content?url={encoded_url}'
        req = urllib.request.Request(api_url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = _json.loads(resp.read().decode('utf-8', errors='ignore'))
        content = data.get('content', '')
        if content and len(content) > 200:
            print(f'[url_extractor] Wiki API DB cache hit: {len(content)} bytes')
            return {'content': content, 'title': data.get('title'), 'source': data.get('source')}
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f'[url_extractor] Wiki API error {e.code}')
    except Exception as e:
        print(f'[url_extractor] Wiki API unreachable: {e}')
    return None


# --- Layer 2: Session 代理抓取（用 We-MP-RSS 登录态 cookies） ---

def _fetch_via_session_proxy(url, wiki_base='http://127.0.0.1:8082', timeout=15):
    """通过 Dashboard 的 session 代理 API 抓取微信文章。
    Dashboard 使用 We-MP-RSS 的登录态 cookies 请求，绕过 IP 级反爬。
    成功后自动缓存到 url_cache 表，下次 Layer 1 直接命中。
    返回 dict {content, title, source, error} 或 None。error 字段用于精确失败反馈。"""
    import urllib.request
    import json as _json

    try:
        api_url = f'{wiki_base}/api/fetch-wx-with-session'
        body = _json.dumps({'url': url}).encode('utf-8')
        req = urllib.request.Request(
            api_url, data=body,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = _json.loads(resp.read().decode('utf-8', errors='ignore'))

        if data.get('success') and data.get('content') and len(data['content']) > 200:
            print(f'[url_extractor] Session proxy hit: {len(data["content"])} bytes, method={data.get("method")}')
            return {
                'content': data['content'],
                'title': data.get('title'),
                'source': data.get('source'),
                'error': None
            }
        else:
            error = data.get('error', 'unknown')
            print(f'[url_extractor] Session proxy failed: {error} - {data.get("detail", "")}')
            return {'content': None, 'title': None, 'source': None, 'error': error}
    except Exception as e:
        print(f'[url_extractor] Session proxy unreachable: {e}')
    return None


# --- Layer 3: 直连 We-MP-RSS DB 文件 ---

def _fetch_from_db(url, db_path):
    """查询 We-MP-RSS SQLite DB，公众号文章大概率已采集。
    DB 路径从 config 读取，不硬编码。"""
    if not db_path:
        return None
    import sqlite3
    import os
    if not os.path.isfile(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        cursor = conn.execute("SELECT content FROM articles WHERE url = ? LIMIT 1", (url,))
        row = cursor.fetchone()
        conn.close()
        if row and row[0] and len(row[0]) > 100:
            return row[0]
    except Exception as e:
        print(f'[url_extractor] DB lookup failed: {e}')
    return None


# --- Layer 4: 微信专用 curl 提取 ---

# 反爬/无效页面关键词（所有 Layer 共用）
_BLOCK_KEYWORDS = ['环境异常', '请在微信客户端打开链接', 'verify_redirect', 'weixin110.qq.com',
                   '完成验证后即可继续访问', '请先登录', '访问受限', '404 Not Found']


def _fetch_wx_article(url, timeout=20):
    """curl + HTTP/2 + 微信移动端 UA (MicroMessenger 8.0.50)
    内容容器: id='js_content' → fallback class='rich_media_content'
    反爬检测: 环境异常、请在微信客户端打开链接等。"""
    try:
        cmd = [
            'curl', '-s', '-L', '--max-time', str(timeout), '--http2', '--compressed',
            '-H', 'User-Agent: Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
                  'AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.50',
            '-H', 'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            '-H', 'Accept-Language: zh-CN,zh;q=0.9,en;q=0.8',
            '-H', 'Accept-Encoding: gzip, deflate, br',
            '-H', 'Referer: https://mp.weixin.qq.com/',
            url
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
        if result.returncode != 0:
            return None
        html = result.stdout.decode('utf-8', errors='ignore')
        if not html or len(html) < 500:
            return None

        # 检测反爬拦截页
        for kw in _BLOCK_KEYWORDS:
            if kw in html:
                print(f'[url_extractor] WX anti-crawl detected: {kw}')
                return None

        # 提取 js_content 区块
        m = re.search(r'id=["\']js_content["\'][^>]*>(.*?)</div>\s*<div', html, re.DOTALL | re.IGNORECASE)
        if not m:
            m = re.search(r'id=["\']js_content["\'][^>]*>(.*)', html, re.DOTALL)
        if m:
            content = m.group(1)
            return content[:200000]

        # 备用：rich_media_content
        m2 = re.search(r'class=["\']rich_media_content["\'][^>]*>(.*?)</div>', html, re.DOTALL)
        if m2:
            return m2.group(1)

        return None
    except subprocess.TimeoutExpired:
        print(f'[url_extractor] WX curl timeout: {url}')
        return None
    except Exception as e:
        print(f'[url_extractor] WX fetch error: {e}')
        return None


# --- Layer 4: 通用网页提取 ---

def _fetch_generic(url, timeout=20):
    """curl + Chrome UA，提取 <article> → <main> → role='main' → <body>
    返回 (content_html, title, source)"""
    try:
        cmd = [
            'curl', '-s', '-L', '--max-time', str(timeout), '--compressed',
            '-H', 'User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            '-H', 'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            '-H', 'Accept-Language: zh-CN,zh;q=0.9,en;q=0.8',
            url
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
        if result.returncode != 0:
            return None, None, None
        html = result.stdout.decode('utf-8', errors='ignore')
        if not html or len(html) < 200:
            return None, None, None

        # 反爬/无效页面检测
        for kw in _BLOCK_KEYWORDS:
            if kw in html:
                print(f'[url_extractor] Generic anti-crawl detected: {kw}')
                return None, None, None

        title = _extract_title_from_html(html)
        source = _extract_source_from_html(html)

        # 提取正文：依次尝试 article / main / role=main / body
        content = None
        for pattern in [
            r'<article[^>]*>(.*?)</article>',
            r'<main[^>]*>(.*?)</main>',
            r'role=["\']main["\'][^>]*>(.*?)</div>',
        ]:
            m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
            if m and len(m.group(1)) > 200:
                content = m.group(1)
                break
        if not content:
            m = re.search(r'<body[^>]*>(.*?)</body>', html, re.DOTALL | re.IGNORECASE)
            if m:
                content = m.group(1)

        return content, title, source
    except subprocess.TimeoutExpired:
        print(f'[url_extractor] Generic curl timeout: {url}')
        return None, None, None
    except Exception as e:
        print(f'[url_extractor] Generic fetch error: {e}')
        return None, None, None


# --- 统一入口 ---

def extract_url_content(url, config=None):
    """
    统一 URL 内容提取入口。四层降级策略。

    参数:
      url: 目标 URL
      config: dict，可选字段：
        max_chars: 最大提取字符数（默认 3000）
        timeout: 超时秒数（默认 20）
        layers: 启用的提取层（默认全部）
        wemprss_db: We-MP-RSS 数据库路径
        wiki_api_base: wiki dashboard 地址

    返回:
      {
        'url': 原始 URL,
        'success': True/False,
        'title': 标题或 None,
        'source': 来源或 None,
        'content': 正文纯文本或 None,
        'method': 'wiki_api' | 'session_fetch' | 'db_cache' | 'wx_special' | 'generic' | 'cache_hit' | 'failed',
        'error': None | 'session_expired' | 'fetch_failed' | 'empty_content' | 'article_deleted' | str,
        'chars': 实际提取字符数
      }
    """
    config = config or {}
    max_chars = config.get('max_chars', config.get('max_chars_per_url', 3000))
    timeout = config.get('timeout', config.get('timeout_seconds', 20))
    layers = config.get('layers', config.get('extraction_layers', ['wiki_api', 'session_proxy', 'db_cache', 'wx_special', 'generic']))
    wemprss_db = config.get('wemprss_db', config.get('wemprss_db_path', ''))
    wiki_base = config.get('wiki_api_base', 'http://127.0.0.1:8082')

    _fail = lambda err: {'url': url, 'success': False, 'title': None, 'source': None,
                         'content': None, 'method': 'failed', 'error': err, 'chars': 0}

    # 内存缓存
    cached = _cache_get(url)
    if cached:
        cached_copy = dict(cached)
        cached_copy['method'] = 'cache_hit'
        return cached_copy

    is_wx = 'mp.weixin.qq.com' in url or 'weixin.qq.com' in url
    title, source = None, None

    # Layer 0: URL 标准化（微信链接）
    original_url = url
    if is_wx:
        url = _normalize_wx_url(url)

    # 辅助函数：从 HTML 构建成功结果
    def _make_result(html, method, error=None):
        nonlocal title, source
        title = _extract_title_from_html(html) or title
        source = _extract_source_from_html(html) or source
        text = _html_to_text(html, max_chars)
        if text and len(text) > 200:
            r = {'url': original_url, 'success': True, 'title': title, 'source': source,
                 'content': text, 'method': method, 'error': None, 'chars': len(text)}
            _cache_set(original_url, r)
            if url != original_url:
                _cache_set(url, r)
            return r
        return None

    # 记录 session proxy 返回的具体错误（用于最终精确反馈）
    last_session_error = None

    if is_wx:
        # Layer 1: wiki API DB 缓存（articles + url_cache 表）
        wiki_data = _fetch_via_wiki_api(url, wiki_base, timeout=5)
        if not wiki_data and url != original_url:
            wiki_data = _fetch_via_wiki_api(original_url, wiki_base, timeout=3)
        if wiki_data:
            title = wiki_data.get('title') or title
            source = wiki_data.get('source') or source
            r = _make_result(wiki_data['content'], 'wiki_api')
            if r:
                return r

        # Layer 2: Session 代理抓取（We-MP-RSS 登录态 cookies）
        if 'session_proxy' in layers:
            session_data = _fetch_via_session_proxy(url, wiki_base, timeout=15)
            if session_data:
                if session_data.get('content') and len(session_data['content']) > 200:
                    title = session_data.get('title') or title
                    source = session_data.get('source') or source
                    r = _make_result(session_data['content'], 'session_fetch')
                    if r:
                        return r
                else:
                    last_session_error = session_data.get('error')

        # Layer 3: 直连 DB（wiki 不可用时兜底）
        if 'db_cache' in layers:
            db_content = _fetch_from_db(url, wemprss_db)
            if not db_content and url != original_url:
                db_content = _fetch_from_db(original_url, wemprss_db)
            if db_content and len(db_content) > 200:
                r = _make_result(db_content, 'db_cache')
                if r:
                    return r

        # Layer 4: 模块内置微信 curl（大概率被反爬拦截）
        if 'wx_special' in layers:
            wx_html = _fetch_wx_article(url, timeout)
            if wx_html and len(wx_html) > 200:
                r = _make_result(wx_html, 'wx_special')
                if r:
                    return r
    else:
        # 非微信链接：直接走通用提取
        pass

    # Layer 4（通用）: 模块内置通用提取
    if 'generic' in layers:
        gen_html, gen_title, gen_source = _fetch_generic(url, timeout)
        if gen_html:
            title = gen_title or title
            source = gen_source or source
            text = _html_to_text(gen_html, max_chars)
            if text and len(text) > 200:
                r = {'url': original_url, 'success': True, 'title': title, 'source': source,
                     'content': text, 'method': 'generic', 'error': None, 'chars': len(text)}
                _cache_set(original_url, r)
                return r

    # 全部失败 — 使用精确错误信息
    if last_session_error:
        error_msg = last_session_error
    elif is_wx:
        error_msg = 'fetch_failed'
    else:
        error_msg = 'fetch_failed'
    result = _fail(error_msg)
    _cache_set(original_url, result)
    return result


def extract_urls_from_message(content, config=None):
    """
    从消息文本中提取所有 URL 并并行获取内容。

    返回: [(url, result_dict), ...]
    最多处理 config['max_urls'] 个（默认 3）。
    多链接并行抓取（ThreadPoolExecutor），总耗时约等于最慢的那条。
    """
    config = config or {}
    max_urls = config.get('max_urls', 3)
    urls = re.findall(r'https?://[^\s<>"\')\]]+', content)
    if not urls:
        return []

    urls = urls[:max_urls]

    # 单链接不用线程池
    if len(urls) == 1:
        result = extract_url_content(urls[0], config)
        return [(urls[0], result)]

    # 多链接并行
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = []
    try:
        with ThreadPoolExecutor(max_workers=min(len(urls), 3)) as pool:
            futures = {pool.submit(extract_url_content, url, config): url for url in urls}
            for future in as_completed(futures, timeout=25):
                url = futures[future]
                try:
                    result = future.result()
                    results.append((url, result))
                except Exception as e:
                    results.append((url, {'url': url, 'success': False, 'title': None, 'source': None,
                                          'content': None, 'method': 'failed', 'error': str(e), 'chars': 0}))
    except Exception:
        # timeout 或其他异常，返回已有结果
        pass

    # 按原始 URL 顺序排序
    url_order = {u: i for i, u in enumerate(urls)}
    results.sort(key=lambda x: url_order.get(x[0], 999))
    return results


def build_context_injection(results):
    """
    将提取结果构建为 extra_ctx 注入文本。

    参数: results — extract_urls_from_message 返回的 [(url, result_dict), ...] 列表
          或 [result_dict, ...] 列表（兼容两种格式）

    返回: 注入到 extra_ctx 的字符串
    - 成功: 链接内容
    - 失败: 精确失败原因的防幻觉指令
    - 部分: 内容 + 不完整标注
    """
    if not results:
        return ''

    parts = []
    for item in results:
        # 兼容 (url, result) 和 result 两种格式
        if isinstance(item, tuple):
            url, r = item
        else:
            r = item
            url = r.get('url', '')

        if r.get('success'):
            text = r['content'] or ''
            if len(text) < 200:
                # 部分提取
                section = f'\n\n用户分享的链接内容（可能不完整，{url}）：\n'
                if r.get('title'):
                    section += f'标题: {r["title"]}\n'
                if r.get('source'):
                    section += f'来源: {r["source"]}\n'
                section += f'正文:\n{text}\n'
                section += '[系统提示：以上内容可能不完整，请基于已有内容回答，不确定的部分请说明。]'
                parts.append(section)
            else:
                # 完整提取
                section = f'\n\n用户分享的链接内容（{url}）：\n'
                if r.get('title'):
                    section += f'标题: {r["title"]}\n'
                if r.get('source'):
                    section += f'来源: {r["source"]}\n'
                section += f'正文:\n{text}'
                parts.append(section)
        else:
            # 提取失败 — 精确失败原因的防幻觉指令
            error_code = r.get('error', '')
            friendly_msg = _get_friendly_error_msg(error_code, url)
            parts.append(
                f'\n\n[系统提示：用户分享了链接 {url}，但系统无法读取该链接内容。'
                f'{friendly_msg}]'
            )

    return ''.join(parts)


def _get_friendly_error_msg(error_code, url):
    """根据错误类型生成注入给 LLM 的精确提示"""
    if error_code == 'session_expired':
        return ('你的微信阅读能力暂时不可用（登录态已过期）。'
                '请如实告知用户，并建议用户把文章内容复制发给你。不要猜测或编造链接中的内容。')
    elif error_code == 'article_deleted':
        return ('该文章可能已被作者删除或设为私密。'
                '请告知用户文章可能已不可用。不要猜测或编造链接中的内容。')
    elif error_code == 'empty_content':
        return ('该文章内容为空（可能是纯图片或视频内容）。'
                '请告知用户，并建议用户截图或描述文章主要内容。不要猜测或编造链接中的内容。')
    else:
        return ('请如实告知用户你目前无法访问和读取该链接，不要猜测或编造链接中的内容。'
                '可以建议用户直接复制文章内容发给你。')
