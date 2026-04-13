"""
触觉 x VLA 论文追踪器 v3
新增：Semantic Scholar API 来源识别，标注顶会/期刊/预印本
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

# 目标顶会列表（机器人/AI 领域核心会议）
TOP_VENUES = {
    # 机器人顶会
    "ICRA", "IROS", "CoRL", "RSS", "ICRA 2026", "IROS 2026", "CoRL 2026",
    "Robotics: Science and Systems",
    "International Conference on Robotics and Automation",
    "International Conference on Intelligent Robots and Systems",
    "Conference on Robot Learning",
    # AI/ML 顶会
    "NeurIPS", "ICLR", "ICML", "CVPR", "ICCV", "ECCV",
    "Neural Information Processing Systems",
    "International Conference on Learning Representations",
    # 顶级期刊
    "Science Robotics", "Nature Machine Intelligence",
    "IEEE Transactions on Robotics",
    "T-RO", "RA-L", "IEEE Robotics and Automation Letters",
    "IJRR", "International Journal of Robotics Research",
    "Autonomous Robots",
}

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
    """
    根据发表场所返回显示标签和质量权重。
    返回 (label, quality_bonus)
    quality_bonus 用于排序时给已发表论文加权。
    """
    if not venue_name:
        return "arXiv 预印本", 0

    v = venue_name.strip()
    v_upper = v.upper()

    # 期刊检测
    journal_keywords = [
        "science robotics", "nature machine", "ieee transactions",
        "t-ro", "ra-l", "robotics and automation letters",
        "ijrr", "international journal of robotics",
        "autonomous robots", "journal",
    ]
    for jk in journal_keywords:
        if jk in v.lower():
            return f"期刊: {v}", 20

    # 顶会检测
    top_conf_keywords = [
        "icra", "iros", "corl", "rss",
        "neurips", "iclr", "icml", "cvpr", "iccv", "eccv",
        "robotics: science", "robot learning",
    ]
    for ck in top_conf_keywords:
        if ck in v.lower():
            return f"顶会: {v}", 15

    # 其他已发表
    return f"已发表: {v}", 10


def query_semantic_scholar(arxiv_id: str) -> dict:
    """
    查询 Semantic Scholar 获取论文的发表场所信息。
    返回 {'venue': str, 'year': int, 'citation_count': int}
    """
    url = f"https://api.semanticscholar.org/graph/v1/paper/arXiv:{arxiv_id}"
    params = {"fields": "venue,year,publicationVenue,citationCount,externalIds"}

    try:
        resp = requests.get(url, params=params, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            venue = data.get("venue", "") or ""
            pub_venue = data.get("publicationVenue") or {}
            # publicationVenue 优先级更高
            if pub_venue.get("name"):
                venue = pub_venue["name"]
            return {
                "venue": venue,
                "year": data.get("year"),
                "citation_count": data.get("citationCount", 0),
            }
        elif resp.status_code == 429:
            logger.warning("Semantic Scholar 请求频率限制，等待后重试...")
            time.sleep(3)
            return {}
    except Exception as e:
        logger.debug(f"Semantic Scholar 查询失败 {arxiv_id}: {e}")

    return {}


# ─── 创新点提取 ─────────────────────────────────────────

def extract_innovations(abstract: str, title: str) -> list[str]:
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
    """
    搜索近N天 arXiv 论文，返回 {base_id: paper_dict}
    """
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
                }
                logger.info(f"  [score={score}] {title[:70]}")

        except Exception as e:
            logger.warning(f"arXiv search error for '{kw}': {e}")

        time.sleep(0.5)  # 防止请求过快

    logger.info(f"arXiv 相关性过滤后: {len(results)} 篇")
    return results


# ─── 来源信息补充（Semantic Scholar） ─────────────────────

def enrich_with_semantic_scholar(papers: dict[str, dict]) -> dict[str, dict]:
    """
    批量查询 Semantic Scholar，补充发表场所信息。
    每篇论文间隔 1s，避免触发频率限制。
    """
    logger.info(f"[S2] 查询 {len(papers)} 篇论文的发表信息...")
    enriched = 0

    for base_id, paper in papers.items():
        time.sleep(1)  # Semantic Scholar 公开 API 限速 ~1 req/s
        s2_data = query_semantic_scholar(base_id)

        if s2_data:
            venue = s2_data.get("venue", "")
            if venue:
                label, bonus = get_venue_label(venue)
                paper["venue"] = venue
                paper["venue_label"] = label
                paper["quality_bonus"] = bonus
                paper["citation_count"] = s2_data.get("citation_count", 0)
                enriched += 1
                logger.info(f"  [S2] {paper['title'][:50]} -> {label}")
            else:
                logger.debug(f"  [S2] 暂无发表信息: {paper['title'][:50]}")

    logger.info(f"[S2] 成功获取 {enriched}/{len(papers)} 篇论文发表信息")
    return papers


# ─── 消息格式化 ─────────────────────────────────────────

VENUE_EMOJI = {
    "顶会": "🏆",
    "期刊": "📖",
    "已发表": "✅",
    "arXiv": "📄",
}

def venue_emoji(label: str) -> str:
    for key, emoji in VENUE_EMOJI.items():
        if label.startswith(key):
            return emoji
    return "📄"


def format_message(papers: list[dict], date_str: str) -> str:
    if not papers:
        return (
            f"# 📚 触觉×VLA 最新论文\n"
            f"**追踪日期**: {date_str}\n\n"
            f"本周暂无新发表论文，继续关注中..."
        )

    # 统计来源分布
    top_conf_count = sum(1 for p in papers if p["venue_label"].startswith("顶会"))
    journal_count = sum(1 for p in papers if p["venue_label"].startswith("期刊"))
    arxiv_count = sum(1 for p in papers if p["venue_label"].startswith("arXiv"))

    lines = [
        f"# 📚 触觉×VLA 最新论文",
        f"**追踪周期**: 近7天 | **收录**: {len(papers)} 篇",
        f"**来源**: 🏆 顶会 {top_conf_count} 篇 | 📖 期刊 {journal_count} 篇 | 📄 预印本 {arxiv_count} 篇",
        "",
        "---",
    ]

    for i, p in enumerate(papers, 1):
        innovations = extract_innovations(p["abstract"], p["title"])
        emoji = venue_emoji(p["venue_label"])

        lines.append(f"## {i}. {p['title']}")
        lines.append("")
        lines.append(f"**👥 作者**: {', '.join(p['authors'])}")
        lines.append(f"**📅 发表**: {p['published']} | **相关度**: {p['score']}/100")
        lines.append(f"**{emoji} 来源**: {p['venue_label']}")

        # 引用量（仅当有引用时显示）
        if p.get("citation_count", 0) > 0:
            lines.append(f"**🔢 引用量**: {p['citation_count']}")

        lines.append("")

        abstract = p["abstract"]
        if len(abstract) > 300:
            abstract = abstract[:300].strip().rstrip(".") + "..."
        lines.append(f"**📝 摘要**: {abstract}")
        lines.append("")

        lines.append("**💡 核心创新**:")
        for ip in innovations:
            lines.append(f"- {ip}")
        lines.append("")

        lines.append(f"**🔗 链接**: [摘要]({p['abs_url']}) | [PDF]({p['pdf_url']})")
        lines.append("")
        lines.append("---")

    lines.extend([
        "",
        "> 🤖 TactileVLA Tracker v3 | arXiv + Semantic Scholar 双源追踪",
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
    logger.info("触觉×VLA 论文追踪器 v3 启动（双源追踪）")
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

    # 2. Semantic Scholar 来源信息补充
    if papers_dict:
        papers_dict = enrich_with_semantic_scholar(papers_dict)

    # 3. 排序：顶会/期刊优先（score + quality_bonus），同等情况按时间
    all_papers = sorted(
        papers_dict.values(),
        key=lambda x: (x["score"] + x["quality_bonus"], x["published"]),
        reverse=True,
    )[:max_papers * 2]  # 先多取，去重后再截

    # 4. 历史去重
    history = load_json(HISTORY_FILE)
    sent_ids = {p["id"] for p in history.get("papers", [])}
    new_papers = [p for p in all_papers if p["base_id"] not in sent_ids][:max_papers]
    logger.info(f"去重后新增论文: {len(new_papers)} 篇（上限 {max_papers} 篇）")

    if not new_papers:
        logger.info("本周无新论文，跳过推送")
        return 0

    # 5. 格式化消息
    date_str = datetime.now().strftime("%Y-%m-%d")
    message = format_message(new_papers, date_str)

    preview = message[:1500]
    if len(message) > 1500:
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
