"""
knowledge_retriever.py — 关键词驱动的分层知识检索模块

通用模块，可集成到任何需要从本地知识库提取相关内容的 AI 系统中。

原理：
  传统知识检索按目录顺序读取固定数量文件，不考虑与用户输入的相关性。
  本模块通过关键词匹配实现精准检索：
  1. 搜索阶段：扫描所有文件，按关键词相关度评分排序
  2. 分层读取：所有匹配文件按相关度分层，高相关读深、低相关读浅
  3. 全覆盖：不限制文件数量，通过总预算控制输出量

使用方式：
  1. 独立使用：
     from knowledge_retriever import KnowledgeRetriever
     retriever = KnowledgeRetriever(base_dirs=['/path/to/knowledge'])
     context = retriever.retrieve('二码合一')

  2. 与 AI 生成集成：
     context = retriever.retrieve(user_keyword, total_budget=12000)
     prompt = f"基于以下知识：{context}，生成..."

配置参数：
  base_dirs: 知识库根目录列表
  total_budget: 总字符预算（默认 12000）
  file_extensions: 扫描的文件类型（默认 ['.md']）
  exclude_dirs: 排除的目录名（默认排除 .git, node_modules 等）
"""

import os
import re
from pathlib import Path


# 排除的目录和文件模式
DEFAULT_EXCLUDE_DIRS = {
    '.git', '.obsidian', '.stfolder', '.claude', '.agents',
    'node_modules', '__pycache__', '.next', 'release',
    'docker-data', 'env', 'venv',
}

DEFAULT_EXCLUDE_FILES = {
    '.DS_Store', 'package-lock.json', 'yarn.lock',
}


class KnowledgeRetriever:
    """关键词驱动的分层知识检索器"""

    # 分层配置：(相关度阈值, 每文件最大读取字符数)
    TIERS = [
        ('tier1', 50, 2000),   # 文件名/目录名直接包含关键词 → 读 2000 字
        ('tier2', 10, 1000),   # 内容中关键词出现 3+ 次 → 读 1000 字
        ('tier3', 1,  400),    # 内容中关键词出现 1-2 次 → 读 400 字
    ]

    def __init__(self, base_dirs, file_extensions=None, exclude_dirs=None):
        """
        初始化检索器

        Args:
            base_dirs: 知识库根目录列表，如 ['/home/user/Vic-Copilot']
            file_extensions: 扫描的文件扩展名，默认 ['.md']
            exclude_dirs: 要排除的目录名集合
        """
        self.base_dirs = [os.path.expanduser(d) for d in base_dirs]
        self.extensions = file_extensions or ['.md']
        self.exclude_dirs = exclude_dirs or DEFAULT_EXCLUDE_DIRS

    def retrieve(self, keyword, total_budget=12000):
        """
        检索与关键词相关的知识内容

        Args:
            keyword: 用户输入的关键词/主题，如 "二码合一"、"私域复购"
            total_budget: 总字符预算，控制返回内容的总量

        Returns:
            str: 结构化的知识上下文文本，可直接拼入 AI prompt
        """
        if not keyword or not keyword.strip():
            return ''

        keywords = self._extract_keywords(keyword)

        # 第一阶段：扫描并评分
        scored_files = self._scan_and_score(keywords)

        if not scored_files:
            return f'（未找到与「{keyword}」相关的知识文件）'

        # 第二阶段：分层读取
        return self._tiered_read(scored_files, keywords, total_budget, keyword)

    def retrieve_with_meta(self, keyword, total_budget=12000):
        """
        检索知识内容，同时返回元数据（匹配文件数、各层文件数等）

        Returns:
            (str, dict): (知识上下文文本, 元数据字典)
        """
        if not keyword or not keyword.strip():
            return '', {'total_files': 0, 'matched_files': 0}

        keywords = self._extract_keywords(keyword)
        scored_files = self._scan_and_score(keywords)

        total_scanned = sum(
            sum(1 for _ in self._walk_files(d)) for d in self.base_dirs
        )

        if not scored_files:
            return (
                f'（未找到与「{keyword}」相关的知识文件）',
                {'total_files': total_scanned, 'matched_files': 0}
            )

        context = self._tiered_read(scored_files, keywords, total_budget, keyword)

        # 统计各层文件数
        tier_counts = {}
        for tier_name, threshold, _ in self.TIERS:
            tier_counts[tier_name] = 0
        for f in scored_files:
            for tier_name, threshold, _ in self.TIERS:
                if f['score'] >= threshold:
                    tier_counts[tier_name] = tier_counts.get(tier_name, 0) + 1
                    break

        meta = {
            'total_files': total_scanned,
            'matched_files': len(scored_files),
            'tier_counts': tier_counts,
            'top_files': [
                {'path': f['rel_path'], 'score': f['score']}
                for f in scored_files[:10]
            ],
        }
        return context, meta

    # 中文停用词（问句/语气/代词/连接词 — 这些词不应该参与知识库搜索）
    _CN_STOP_WORDS = {
        '什么', '怎么', '如何', '为什么', '是什么', '哪些', '哪个', '哪里',
        '请问', '可以', '能不能', '是不是', '有没有', '可不可以',
        '告诉', '知道', '了解', '介绍', '说说', '讲讲', '解释',
        '一下', '关于', '现在', '目前', '还是', '或者',
        '你们', '我们', '他们', '这个', '那个', '这些', '那些',
        '请', '吗', '呢', '啊', '吧', '的', '了', '是', '在', '有', '和', '与',
    }

    def _extract_keywords(self, user_input):
        """
        从用户输入提取检索关键词列表

        策略（中英文通用）：
        1. 清洗中文停用词（"是什么""怎么""吗"等），保留实义词
        2. 从清洗后的文本提取 2-6 字的中文短语和英文单词
        3. 同时保留原始输入作为完整短语匹配（最低优先）

        例如：
        - "二码合一是什么" → 清洗 "是什么" → 提取 "二码合一" → ["二码合一", "二码合一是什么"]
        - "防伪码是什么防伪码" → 清洗 "是什么" → 提取 "防伪码" → ["防伪码", ...]
        - "EMC product overview" → 分词 → ["EMC", "product", "overview", ...]
        """
        raw = user_input.strip()
        keywords = []

        # Step 1: 中文停用词清洗 — 从原文中移除问句/语气词，保留实义内容
        cleaned = raw
        for sw in sorted(self._CN_STOP_WORDS, key=len, reverse=True):  # 长词先替换避免部分匹配
            cleaned = cleaned.replace(sw, ' ')

        # Step 2: 从清洗后的文本提取关键词
        # 中文：连续 2 字以上的汉字片段
        cn_words = re.findall(r'[\u4e00-\u9fff]{2,}', cleaned)
        for w in cn_words:
            if w not in keywords:
                keywords.append(w)

        # 英文：3 字母以上的单词
        en_words = re.findall(r'[a-zA-Z]{3,}', cleaned)
        for w in en_words:
            if w not in keywords:
                keywords.append(w)

        # Step 3: 空格/逗号分词（兼容 "二码合一 产品方案" 这种带空格的输入）
        parts = re.split(r'[,，\s]+', raw)
        for p in parts:
            p = p.strip()
            if len(p) >= 2 and p not in keywords and p not in self._CN_STOP_WORDS:
                keywords.append(p)

        # Step 4: 原始输入作为最后的完整短语匹配（最低优先）
        if raw not in keywords:
            keywords.append(raw)

        return keywords

    def _walk_files(self, base_dir):
        """遍历目录下的所有目标文件，排除指定目录"""
        for root, dirs, files in os.walk(base_dir):
            # 过滤排除目录（原地修改 dirs 列表）
            dirs[:] = [
                d for d in dirs
                if d not in self.exclude_dirs and not d.startswith('.')
            ]
            for fn in files:
                if fn in DEFAULT_EXCLUDE_FILES:
                    continue
                if any(fn.endswith(ext) for ext in self.extensions):
                    yield os.path.join(root, fn)

    def _score_file(self, filepath, keywords):
        """
        对单个文件评分

        评分规则：
        - 文件名包含关键词：+50
        - 所在目录名包含关键词：+50
        - 文件内容中每出现一次关键词：+3（上限 30）
        - 关键词出现在标题行（# 开头）：额外 +10
        """
        score = 0
        filename = os.path.basename(filepath).lower()
        dirpath = os.path.dirname(filepath).lower()

        for kw in keywords:
            kw_lower = kw.lower()
            # 文件名匹配
            if kw_lower in filename:
                score += 50
            # 目录名匹配
            if kw_lower in dirpath:
                score += 50

        # 内容匹配（只读前 5000 字符用于评分，避免大文件耗时）
        content_score = 0
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read(5000)

            content_lower = content.lower()
            for kw in keywords:
                kw_lower = kw.lower()
                occurrences = content_lower.count(kw_lower)
                if occurrences > 0:
                    content_score += min(occurrences * 3, 30)

                    # 标题行中出现关键词额外加分
                    for line in content.split('\n')[:30]:
                        if line.strip().startswith('#') and kw_lower in line.lower():
                            content_score += 10
                            break

            score += content_score
        except Exception:
            pass

        return score

    def _scan_and_score(self, keywords):
        """扫描所有文件并评分，返回按分数降序排列的列表"""
        scored = []

        for base_dir in self.base_dirs:
            if not os.path.isdir(base_dir):
                continue
            for filepath in self._walk_files(base_dir):
                score = self._score_file(filepath, keywords)
                if score > 0:
                    # 计算相对路径
                    rel_path = os.path.relpath(filepath, base_dir)
                    scored.append({
                        'path': filepath,
                        'rel_path': rel_path,
                        'score': score,
                    })

        scored.sort(key=lambda x: x['score'], reverse=True)
        return scored

    def _tiered_read(self, scored_files, keywords, total_budget, original_keyword):
        """分层读取文件内容，构建结构化知识上下文"""
        parts = []
        budget = total_budget
        tier_files = {'tier1': [], 'tier2': [], 'tier3': []}

        # 分层
        for f in scored_files:
            assigned = False
            for tier_name, threshold, _ in self.TIERS:
                if f['score'] >= threshold:
                    tier_files[tier_name].append(f)
                    assigned = True
                    break
            if not assigned:
                tier_files['tier3'].append(f)

        # 按层读取
        for tier_name, threshold, max_chars in self.TIERS:
            files = tier_files[tier_name]
            if not files:
                continue

            tier_label = {
                'tier1': '核心相关',
                'tier2': '高度相关',
                'tier3': '参考相关',
            }[tier_name]

            section_parts = [f'\n## {tier_label}文档（{len(files)} 个）\n']

            for f in files:
                if budget <= 0:
                    section_parts.append(f'- **{f["rel_path"]}**（预算不足，跳过）')
                    continue

                read_chars = min(max_chars, budget)
                try:
                    with open(f['path'], 'r', encoding='utf-8', errors='ignore') as fh:
                        content = fh.read(read_chars)

                    # 对于 tier2/tier3，尝试提取关键词附近的上下文而非文件开头
                    if tier_name != 'tier1' and read_chars < 1500:
                        focused = self._extract_relevant_sections(
                            f['path'], keywords, read_chars
                        )
                        if focused:
                            content = focused

                    title = os.path.basename(f['path']).replace('.md', '')
                    entry = f'### {title}\n> 来源：{f["rel_path"]}（相关度：{f["score"]}）\n\n{content}\n'
                    section_parts.append(entry)
                    budget -= len(content)
                except Exception:
                    pass

            parts.append('\n'.join(section_parts))

        # 组装最终上下文
        header = f'# 知识检索结果：「{original_keyword}」\n'
        header += f'共匹配 {len(scored_files)} 个文件\n'
        return header + '\n'.join(parts)

    def _extract_relevant_sections(self, filepath, keywords, max_chars):
        """
        从文件中提取关键词附近的段落，而非简单截取文件开头。
        确保低相关度文件也能贡献最有价值的片段。
        """
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                full_content = f.read()
        except Exception:
            return None

        if len(full_content) <= max_chars:
            return full_content

        # 按段落分割
        paragraphs = re.split(r'\n\n+', full_content)
        if not paragraphs:
            return None

        # 评分每个段落
        scored_paras = []
        for i, para in enumerate(paragraphs):
            para_lower = para.lower()
            score = 0
            for kw in keywords:
                count = para_lower.count(kw.lower())
                score += count * 10
                # 标题段落加分
                if para.strip().startswith('#') and kw.lower() in para_lower:
                    score += 20
            if score > 0:
                scored_paras.append((score, i, para))

        if not scored_paras:
            # 没有匹配段落，返回文件开头
            return full_content[:max_chars]

        # 按得分排序，取最相关的段落
        scored_paras.sort(key=lambda x: x[0], reverse=True)

        result_parts = []
        chars_used = 0
        # 始终包含文件第一段（通常是标题/概述）
        first_para = paragraphs[0] if paragraphs else ''
        if first_para:
            result_parts.append(first_para)
            chars_used += len(first_para)

        for _, idx, para in scored_paras:
            if chars_used >= max_chars:
                break
            if para == first_para:
                continue
            result_parts.append(para)
            chars_used += len(para) + 2  # +2 for \n\n

        return '\n\n'.join(result_parts)


def retrieve_for_prompt(keyword, base_dirs=None, total_budget=12000):
    """
    便捷函数：一行调用完成知识检索

    Args:
        keyword: 搜索关键词
        base_dirs: 知识库目录列表，默认使用 Vic-Copilot 根目录
        total_budget: 总字符预算

    Returns:
        str: 可直接拼入 AI prompt 的知识上下文
    """
    if base_dirs is None:
        # 默认：从模块位置推算 Vic-Copilot 根目录
        module_dir = os.path.dirname(os.path.abspath(__file__))
        # modules/knowledge-retriever/ → CodePilot/source/ → CodePilot/ → AI学习实践/ → Vic-Copilot/
        project_root = os.path.abspath(os.path.join(module_dir, '..', '..', '..', '..'))
        base_dirs = [project_root]

    retriever = KnowledgeRetriever(base_dirs=base_dirs)
    return retriever.retrieve(keyword, total_budget=total_budget)


if __name__ == '__main__':
    import sys
    keyword = sys.argv[1] if len(sys.argv) > 1 else '二码合一'
    budget = int(sys.argv[2]) if len(sys.argv) > 2 else 12000

    retriever = KnowledgeRetriever(
        base_dirs=[os.path.expanduser('~/Vic-Copilot')]
    )
    context, meta = retriever.retrieve_with_meta(keyword, total_budget=budget)
    print(f"=== 检索结果：{keyword} ===")
    print(f"扫描文件数：{meta['total_files']}")
    print(f"匹配文件数：{meta['matched_files']}")
    print(f"各层文件数：{meta.get('tier_counts', {})}")
    print(f"Top 匹配：")
    for f in meta.get('top_files', []):
        print(f"  {f['score']:>4d} | {f['path']}")
    print(f"\n=== 知识上下文（{len(context)} 字）===\n")
    print(context[:3000] + ('...' if len(context) > 3000 else ''))
