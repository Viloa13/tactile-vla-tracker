"""
触觉 x VLA 论文追踪器 v4
增强推送格式：中文标题 | 作者机构 | 论文贡献 | 核心创新

新增功能:
- 中文标题: Google Translate API 翻译
- 作者机构: Semantic Scholar 补充 affiliation
- 论文贡献: 结构化提取（问题动机 + 核心方法 + 实验验证）
- 推送格式重设计
"""

import json
import os
import sys
import time
import logging
import io
import re
from datetime import datetime, timedelta
from pathlib import Path

# Windows 控制台 UTF-8 输出支持
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import arxiv
import requests

# ─── 配置 ───────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
HISTORY_FILE = SCRIPT_DIR / "sent_papers.json"
LOG_FILE = SCRIPT_DIR / "tracker.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# ─── 翻译缓存 ────────────────────────────────────────────
_translation_cache: dict[str, str] = {}


def translate_to_chinese(text: str) -> str:
    """使用 Google Translate API 将英文翻译为中文，带缓存"""
    if not text or len(text.strip()) < 3:
        return ""
    if text in _translation_cache:
        return _translation_cache[text]

    try:
        encoded = requests.utils.quote(text[:500])
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=zh&dt=t&q={encoded}"
        resp = requests.get(url, timeout=6)
        if resp.status_code == 200:
            data = resp.json()
            if data and data[0]:
                chinese = "".join(item[0] for item in data[0] if item[0])
                _translation_cache[text] = chinese
                return chinese
    except Exception as e:
        logger.debug(f"翻译失败: {e}")
    return ""


# ─── 辅助函数 ────────────────────────────────────────────

def load_json(path: Path) -> dict:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


# ─── 相关性评分 ──────────────────────────────────────────

CORE_TACTILE = ["tactile", "haptic", "force", "touch"]
CORE_ROBOT = ["robot", "manipulation", "arm", "gripper", "grasping", "dexterous", "dexterity"]
VLA_TERMS = [
    "vision language", "vla", "vision-language", "vlm",
    "large language model", "llm", "multimodal",
    "action", "policy", "control",
]
BONUS_TERMS = [
    "foundation model", "pretrain", "pre-train",
    "zero-shot", "zero shot", "generaliz",
    "real-world", "real world", "real robot",
    "grasp", "closed-loop", "contact-rich",
    "sim-to-real", "sim2real",
    "diffusion", "transformer", "language instruction",
]
EXCLUDE_TERMS = [
    "fundus", "retinal", "retina", "eye", "ophthalm",
    "skin disease", "dermatol", "biomedical", "medical",
    "covid", "ct scan", "mri", "x-ray", "xray",
    "electrocardi", "eeg", "fmri",
    "acoustic sensing", "audio", "speech", "voice",
]


def score_relevance(title: str, abstract: str) -> tuple[int, str]:
    text = (title + " " + abstract).lower()

    for excl in EXCLUDE_TERMS:
        if excl in text:
            return 0, f"排除领域: {excl}"

    tactile_hits = sum(1 for t in CORE_TACTILE if t in text)
    if tactile_hits == 0:
        return 0, "无触觉/力感知词"

    robot_hits = sum(1 for t in CORE_ROBOT if t in text)
    if robot_hits == 0:
        return 0, "无机器人相关词"

    score = 0
    score += min(tactile_hits * 12, 35)
    score += min(robot_hits * 10, 25)
    vla_hits = sum(1 for t in VLA_TERMS if t in text)
    score += min(vla_hits * 8, 25)
    bonus_hits = sum(1 for t in BONUS_TERMS if t in text)
    score += min(bonus_hits * 3, 15)

    return score, ""


# ─── 来源标签 ─────────────────────────────────────────────

def get_venue_label(venue_name: str) -> tuple[str, int]:
    if not venue_name:
        return "arXiv 预印本", 0

    v = venue_name.strip()
    v_lower = v.lower()

    journal_keywords = [
        "science robotics", "nature machine", "ieee transactions",
        "t-ro", "ra-l", "robotics and automation letters",
        "ijrr", "international journal of robotics",
        "autonomous robots", "journal",
    ]
    for jk in journal_keywords:
        if jk in v_lower:
            return f"期刊: {v}", 20

    top_conf_keywords = [
        "icra", "iros", "corl", "rss",
        "neurips", "iclr", "icml", "cvpr", "iccv", "eccv",
        "robotics: science", "robot learning",
    ]
    for ck in top_conf_keywords:
        if ck in v_lower:
            return f"顶会: {v}", 15

    return f"已发表: {v}", 10


def query_semantic_scholar(arxiv_id: str) -> dict:
    """
    查询 Semantic Scholar 获取发表场所 + 作者 + 机构信息。
    fields: venue, year, citationCount, authors(含affiliation)
    """
    url = f"https://api.semanticscholar.org/graph/v1/paper/arXiv:{arxiv_id}"
    params = {
        "fields": "venue,year,publicationVenue,citationCount,authors.name,authors.affiliation"
    }

    for attempt in range(2):
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                venue = data.get("venue", "") or ""
                pub_venue = data.get("publicationVenue") or {}
                if pub_venue.get("name"):
                    venue = pub_venue["name"]

                # 作者 + 机构
                authors_list = data.get("authors", []) or []
                authors_info = []
                for a in authors_list[:5]:  # 最多取前5位
                    name = a.get("name", "")
                    affil = a.get("affiliation", "") or ""
                    if name:
                        authors_info.append({"name": name, "affiliation": affil})

                return {
                    "venue": venue,
                    "year": data.get("year"),
                    "citation_count": data.get("citationCount", 0),
                    "authors_info": authors_info,
                }
            elif resp.status_code == 429:
                logger.warning(f"[S2] 请求频率限制 (429)，等待 {(attempt+1)*3}s 后重试...")
                time.sleep((attempt + 1) * 3)
            else:
                break
        except Exception as e:
            logger.debug(f"[S2] 查询失败 {arxiv_id}: {e}")
            time.sleep(2)

    return {}


# ─── 论文贡献提取（结构化） ────────────────────────────────

def extract_contributions(title: str, abstract: str) -> dict:
    """
    从摘要中结构化提取论文贡献，分为三个维度：
    1. 问题与动机 (problem)
    2. 核心方法 (method)
    3. 实验验证 (experiment)
    """
    text = (title + " " + abstract).lower()
    contributions = {
        "problem": [],
        "method": [],
        "experiment": []
    }

    # 问题与动机关键词
    problem_keywords = [
        (["lack", "limitation", "challenge", "problem"], "现有方法的局限或挑战"),
        (["existing methods", "traditional", "conventional", "current approaches"], "现有方法不足"),
        (["real-world", "real robot", "physical interaction"], "真实场景部署难题"),
        (["sim-to-real", "sim2real", "domain gap", "transfer"], "仿真-真实迁移难题"),
        (["zero-shot", "generaliz", "未见物体", "unseen"], "泛化/零样本能力受限"),
        (["data efficient", "data-hungry", "large data", "标注数据"], "数据效率问题"),
        (["contact-rich", "rich contact", "dexterous"], "灵巧操作难题"),
    ]
    for terms, desc in problem_keywords:
        if any(t in text for t in terms) and desc not in contributions["problem"]:
            contributions["problem"].append(desc)

    # 核心方法关键词
    method_keywords = [
        (["tactile", "haptic", "force", "touch"], "触觉/力感知建模"),
        (["vision language", "vla", "vlm", "multimodal"], "视觉-语言-动作多模态融合"),
        (["foundation model", "pretrain", "pre-train", "large language"], "预训练/基础模型"),
        (["diffusion", "ddpm", "score-based"], "Diffusion 策略/生成"),
        (["transformer", "attention"], "Transformer 架构"),
        (["reinforcement learning", "rl", "policy"], "强化学习策略优化"),
        (["imitation learning", "il", "demonstration"], "模仿学习/示教"),
        (["closed-loop", "feedback", "real-time"], "闭环控制与实时反馈"),
        (["sim-to-real", "domain randomization", "sim2real"], "仿真-真实迁移技术"),
        (["language instruction", "text condition", "natural language"], "语言指令跟随"),
        (["graph neural", "gnn"], "图神经网络建模"),
        (["propriocept", "视觉", "vision"], "本体感/视觉感知"),
    ]
    for terms, desc in method_keywords:
        if any(t in text for t in terms) and desc not in contributions["method"]:
            contributions["method"].append(desc)

    # 实验验证关键词
    exp_keywords = [
        (["real robot", "physical robot", "真实机器人"], "真实机器人平台验证"),
        (["grasping", "manipulation", "抓取", "操作"], "抓取/操作任务实验"),
        ([" dexterous", "dexterity", "灵巧手"], "灵巧操作任务"),
        (["benchmark", "comparison", "对比"], "基准数据集对比实验"),
        (["ablation", "消融", "ablation study"], "消融实验验证"),
        (["zero-shot", "generalization", "泛化"], "泛化能力验证"),
        (["humanoid", "四足", "quadruped", "mobile"], "多种机器人平台"),
    ]
    for terms, desc in exp_keywords:
        if any(t in text for t in terms) and desc not in contributions["experiment"]:
            contributions["experiment"].append(desc)

    # 兜底：如果提取为空，补充摘要首句
    if not contributions["problem"] and not contributions["method"]:
        first_sent = abstract[:150].strip()
        if first_sent:
            contributions["method"].append(first_sent.rstrip("."))

    # 限制每类最多3条
    for key in contributions:
        contributions[key] = contributions[key][:3]

    return contributions


# ─── 核心创新提取 ─────────────────────────────────────────

def extract_innovations(title: str, abstract: str) -> list[str]:
    points = []
    text = (title + " " + abstract).lower()

    keyword_map = [
        (["tactile", "haptic", "force", "touch"], "触觉/力感知与控制"),
        (["vision language", "vla", "vision-language", "vlm"], "视觉-语言-动作对齐"),
        (["foundation model", "pretrain", "large language"], "预训练/基础模型方法"),
        (["zero-shot", "zero shot", "generaliz"], "泛化/零样本能力"),
        (["real-world", "real robot", "real tactile"], "真实机器人部署"),
        (["dexterous", "dexterity", "grasp"], "灵巧操作/抓取"),
        (["closed-loop", "feedback"], "闭环控制与反馈"),
        (["sim-to-real", "sim2real", "simulation"], "仿真到真实迁移"),
        (["diffusion", "transformer"], "Diffusion/Transformer架构"),
        (["contact-rich", "contact rich"], "接触密集型任务"),
        (["language instruction", "instruction"], "语言指令跟随"),
        (["multimodal"], "多模态感知融合"),
    ]

    for terms, desc in keyword_map:
        if any(t in text for t in terms) and desc not in points:
            points.append(desc)

    if not points:
        points.append(abstract[:120].strip().rstrip(".") + "...")
    else:
        points[:] = points[:4]

    return points


# ─── 论文搜索（arXiv） ───────────────────────────────────

def search_arxiv_papers(
    keywords: list[str],
    days: int = 7,
    max_pool: int = 60,
    min_score: int = 25,
) -> dict[str, dict]:
    cutoff = datetime.now() - timedelta(days=days)
    results = {}

    for kw in keywords:
        logger.info(f"[arXiv] Searching: {kw}")
        try:
            client = arxiv.Client()
            search = arxiv.Search(
                query=kw,
                max_results=50,
                sort_by=arxiv.SortCriterion.SubmittedDate,
                sort_order=arxiv.SortOrder.Descending,
            )
            papers = list(client.results(search))

            for p in papers:
                raw_id = p.entry_id.split("/")[-1]
                base_id = raw_id.split("v")[0] if "v" in raw_id else raw_id
                pub_date = p.published.date()

                if pub_date < cutoff.date():
                    continue
                if base_id in results:
                    continue

                title = p.title.strip()
                abstract = p.summary.strip().replace("\n", " ")
                score, reason = score_relevance(title, abstract)

                if score < min_score:
                    tag = reason if reason else f"score={score}"
                    logger.info(f"  [FILTER {tag}] {title[:65]}")
                    continue

                authors = [a.name for a in p.authors[:3]]
                if len(p.authors) > 3:
                    authors.append(f"et al. (+{len(p.authors) - 3})")

                results[base_id] = {
                    "base_id": base_id,
                    "title": title,
                    "authors": authors,
                    "published": pub_date.isoformat(),
                    "abstract": abstract,
                    "abs_url": p.entry_id,
                    "pdf_url": p.entry_id.replace("/abs/", "/pdf/"),
                    "score": score,
                    "venue": "",
                    "venue_label": "arXiv 预印本",
                    "quality_bonus": 0,
                    "citation_count": 0,
                    "authors_info": [],      # Semantic Scholar 补充
                    "zh_title": "",           # Google 翻译
                }
                logger.info(f"  [score={score}] {title[:70]}")

        except Exception as e:
            logger.warning(f"arXiv search error for '{kw}': {e}")

        time.sleep(0.5)

    logger.info(f"arXiv 相关性过滤后: {len(results)} 篇")
    return results


# ─── 来源信息补充（Semantic Scholar） ─────────────────────

def enrich_papers(papers: dict[str, dict]) -> dict[str, dict]:
    """
    批量查询 Semantic Scholar：
    1. 发表场所 + 引用量
    2. 作者 + 机构
    每篇间隔 1.2s，避免触发限速。
    """
    logger.info(f"[S2] 查询 {len(papers)} 篇论文详细信息...")
    enriched_venue = 0
    enriched_authors = 0

    for i, (base_id, paper) in enumerate(papers.items()):
        # 论文查询间隔 1.2s
        if i > 0:
            time.sleep(1.2)

        s2_data = query_semantic_scholar(base_id)
        if not s2_data:
            logger.debug(f"  [S2] 无数据: {paper['title'][:50]}")
            continue

        # 发表场所
        venue = s2_data.get("venue", "")
        if venue:
            label, bonus = get_venue_label(venue)
            paper["venue"] = venue
            paper["venue_label"] = label
            paper["quality_bonus"] = bonus
            paper["citation_count"] = s2_data.get("citation_count", 0)
            enriched_venue += 1
            logger.info(f"  [S2] 来源: {paper['title'][:45]} -> {label}")

        # 作者机构（优先用 S2 数据）
        authors_info = s2_data.get("authors_info", [])
        if authors_info:
            paper["authors_info"] = authors_info
            enriched_authors += 1

    logger.info(f"[S2] 发表信息: {enriched_venue} 篇 | 作者机构: {enriched_authors} 篇")
    return papers


# ─── Google 翻译标题 ─────────────────────────────────────

def translate_titles(papers: list[dict]) -> list[dict]:
    """
    对每篇论文标题进行中文翻译，每篇间隔 0.3s。
    """
    logger.info(f"[翻译] 翻译 {len(papers)} 篇论文标题...")
    for i, paper in enumerate(papers):
        if i > 0:
            time.sleep(0.3)
        zh = translate_to_chinese(paper["title"])
        paper["zh_title"] = zh
        if zh:
            logger.debug(f"  [翻译] {paper['title'][:40]} -> {zh[:40]}")
        else:
            logger.debug(f"  [翻译] 失败: {paper['title'][:40]}")

    logger.info(f"[翻译] 完成")
    return papers


# ─── 消息格式化 ─────────────────────────────────────────

def format_message(papers: list[dict], date_str: str) -> str:
    """
    v4 推送格式：
    原标题 | 中文标题 | 作者机构 | 发表时间 | 相关度 | 来源
    论文贡献 | 核心创新
    链接: 摘要 | PDF
    """
    if not papers:
        return (
            f"# 📚 触觉×VLA 最新论文\n"
            f"**追踪日期**: {date_str}\n\n"
            f"本周暂无新发表论文，继续关注中..."
        )

    # 统计
    top_conf_count = sum(1 for p in papers if p["venue_label"].startswith("顶会"))
    journal_count = sum(1 for p in papers if p["venue_label"].startswith("期刊"))
    arxiv_count = sum(1 for p in papers if p["venue_label"].startswith("arXiv"))

    lines = [
        f"# 📚 触觉×VLA 最新论文",
        f"**📅 追踪周期**: {date_str}（近7天） | **收录**: {len(papers)} 篇",
        f"**🏆 顶会**: {top_conf_count} 篇 | **📖 期刊**: {journal_count} 篇 | **📄 预印本**: {arxiv_count} 篇",
        "",
        "---",
    ]

    VENUE_EMOJI = {"顶会": "🏆", "期刊": "📖", "已发表": "✅", "arXiv": "📄"}

    for i, p in enumerate(papers, 1):
        emoji = VENUE_EMOJI.get(
            next((k for k in VENUE_EMOJI if p["venue_label"].startswith(k)), "arXiv"),
            "📄"
        )

        lines.append(f"## {i}. {p['title']}")

        # 中文标题
        if p.get("zh_title"):
            lines.append(f"**🇨🇳 中文标题**: {p['zh_title']}")

        # 作者 + 机构
        authors_display = []
        for a in p.get("authors_info", [])[:3]:
            name = a.get("name", "")
            affil = a.get("affiliation", "")
            if affil:
                authors_display.append(f"{name} ({affil})")
            else:
                authors_display.append(name)

        # 兜底：用原有作者列表
        if not authors_display:
            authors_display = p.get("authors", [])
        if len(authors_display) > 3:
            authors_display = authors_display[:3] + [f"et al."]

        if authors_display:
            lines.append(f"**👥 作者机构**: {', '.join(authors_display)}")

        # 发表时间 + 相关度 + 来源
        lines.append(
            f"**📅 发表**: {p['published']} | "
            f"**📊 相关度**: {p['score']}/100 | "
            f"**{emoji} 来源**: {p['venue_label']}"
        )

        # 引用量（仅当有引用时显示）
        if p.get("citation_count", 0) > 0:
            lines.append(f"**🔢 引用量**: {p['citation_count']}")

        lines.append("")

        # ── 论文贡献 ──
        contributions = extract_contributions(p["abstract"], p["title"])

        lines.append("**📌 论文贡献**:")
        has_contrib = False
        if contributions["problem"]:
            has_contrib = True
            lines.append(f"  • **问题动机**: {'; '.join(contributions['problem'])}")
        if contributions["method"]:
            has_contrib = True
            lines.append(f"  • **核心方法**: {'; '.join(contributions['method'])}")
        if contributions["experiment"]:
            has_contrib = True
            lines.append(f"  • **实验验证**: {'; '.join(contributions['experiment'])}")
        if not has_contrib:
            abstract_short = p["abstract"][:200].strip().rstrip(".")
            lines.append(f"  • {abstract_short}...")

        lines.append("")

        # ── 核心创新 ──
        innovations = extract_innovations(p["abstract"], p["title"])
        lines.append("**💡 核心创新**:")
        for ip in innovations:
            lines.append(f"  • {ip}")
        lines.append("")

        # ── 摘要 ──
        abstract = p["abstract"]
        if len(abstract) > 250:
            abstract = abstract[:250].strip().rstrip(".") + "..."
        lines.append(f"**📝 摘要**: {abstract}")
        lines.append("")

        # ── 链接 ──
        lines.append(
            f"**🔗 链接**: "
            f"[摘要]({p['abs_url']}) | "
            f"[PDF]({p['pdf_url']})"
        )
        lines.append("")
        lines.append("---")

    lines.extend([
        "",
        "> 🤖 TactileVLA Tracker v4 | arXiv + Semantic Scholar 双源追踪",
    ])

    return "\n".join(lines)


# ─── Server酱 推送 ──────────────────────────────────────

def send_serverchan(sendkey: str, content: str) -> bool:
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    title = f"📚 触觉×VLA 新论文 | {datetime.now().strftime('%Y-%m-%d')}"
    payload = {"title": title, "desp": content, "content": content}

    for attempt in range(3):
        try:
            resp = requests.post(url, data=payload, timeout=15)
            result = resp.json()
            logger.info(f"Server酱响应: {result}")
            if result.get("code") == 0 or result.get("data", {}).get("error") == "SUCCESS":
                logger.info("推送成功！")
                return True
            else:
                logger.warning(f"推送失败 (尝试 {attempt+1}/3): {result}")
        except Exception as e:
            logger.warning(f"网络错误 (尝试 {attempt+1}/3): {e}")

        if attempt < 2:
            wait = (attempt + 1) * 2
            logger.info(f"{wait}秒后重试...")
            time.sleep(wait)

    logger.error("推送失败，已达最大重试次数")
    return False


# ─── 主程序 ─────────────────────────────────────────────

def main():
    logger.info("=" * 50)
    logger.info("触觉×VLA 论文追踪器 v4 启动")
    logger.info("=" * 50)

    config = load_json(CONFIG_FILE)
    if not config.get("server_sendkey"):
        logger.error("错误：config.json 中未配置 server_sendkey！")
        sys.exit(1)

    keywords = config.get("keywords", [])
    days = config.get("days_back", 7)
    max_papers = config.get("max_papers", 20)
    min_score = config.get("min_score", 25)
    sendkey = config["server_sendkey"]

    logger.info(f"搜索策略: 近 {days} 天，最多 {max_papers} 篇，最低相关度 {min_score}/100")

    # 1. arXiv 搜索
    papers_dict = search_arxiv_papers(keywords, days=days, min_score=min_score)

    if not papers_dict:
        logger.info("无相关论文，退出")
        return 0

    # 2. Semantic Scholar 补充来源 + 作者机构
    papers_dict = enrich_papers(papers_dict)

    # 3. Google 翻译中文标题
    all_papers = sorted(
        papers_dict.values(),
        key=lambda x: (x["score"] + x["quality_bonus"], x["published"]),
        reverse=True,
    )

    # 翻译前50篇（按得分排序取前50）
    top_for_translate = all_papers[:50]
    translate_titles(top_for_translate)

    # 4. 历史去重
    history = load_json(HISTORY_FILE)
    sent_ids = {p["id"] for p in history.get("papers", [])}
    new_papers = [p for p in top_for_translate if p["base_id"] not in sent_ids][:max_papers]
    logger.info(f"去重后新增论文: {len(new_papers)} 篇（上限 {max_papers} 篇）")

    if not new_papers:
        logger.info("本周无新论文，跳过推送")
        return 0

    # 5. 格式化消息
    date_str = datetime.now().strftime("%Y-%m-%d")
    message = format_message(new_papers, date_str)

    preview = message[:2000]
    if len(message) > 2000:
        preview += f"\n... (共 {len(message)} 字符，已截断预览)"
    logger.info("\n" + "=" * 50 + "\n推送内容预览:\n" + "=" * 50
                + "\n" + preview + "\n" + "=" * 50 + "\n")

    # 6. 推送
    success = send_serverchan(sendkey, message)

    # 7. 更新历史
    if success:
        today = datetime.now().strftime("%Y-%m-%d")
        for p in new_papers:
            history.setdefault("papers", []).insert(0, {
                "id": p["base_id"],
                "title": p["title"],
                "sent_date": today,
                "venue_label": p["venue_label"],
            })
        history["papers"] = history["papers"][:200]
        save_json(HISTORY_FILE, history)
        logger.info("历史记录已更新")

    logger.info("运行完成！")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
