"""
触觉 x VLA 论文追踪器 v2
每周自动搜索 arXiv 最新论文并通过 Server酱 推送到微信
增强版：相关性评分 + 数量上限
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


# ─── 辅助函数 ────────────────────────────────────────────

def load_json(path: Path) -> dict:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


# 触觉/力感知核心词
CORE_TACTILE = [
    "tactile", "haptic", "force", "touch",
]
# 机器人相关核心词（必须与触觉结合，否则不算相关）
CORE_ROBOT = [
    "robot", "manipulation", "arm", "gripper",
    "grasping", "dexterous", "dexterity",
]
# VLA/多模态词
VLA_TERMS = [
    "vision language", "vla", "vision-language", "vlm",
    "large language model", "llm", "multimodal",
    "action", "policy", "control",
]
# 加分词
BONUS_TERMS = [
    "foundation model", "pretrain", "pre-train",
    "zero-shot", "zero shot", "generaliz",
    "real-world", "real world", "real robot",
    "grasp", "closed-loop", "contact-rich",
    "sim-to-real", "sim2real",
    "diffusion", "transformer", "language instruction",
]
# 必须排除的领域关键词（这些论文不属于触觉×VLA机器人领域）
EXCLUDE_TERMS = [
    "fundus", "retinal", "retina", "eye", "ophthalm",
    "skin disease", "dermatol", "biomedical", "medical",
    "covid", "ct scan", "mri", "x-ray", "xray",
    "electrocardi", "eeg", "fmri",
    "acoustic sensing", "audio", "speech", "voice",
]


def score_relevance(title: str, abstract: str) -> tuple[int, str]:
    """
    计算论文相关性评分 (0-100)，越高越相关。
    同时返回拒绝原因（空字符串=通过）。
    """
    text = (title + " " + abstract).lower()

    # Step 1: 排除明显不相关领域
    for excl in EXCLUDE_TERMS:
        if excl in text:
            return 0, f"排除领域: {excl}"

    # Step 2: 必须有触觉/力感知词
    tactile_hits = sum(1 for t in CORE_TACTILE if t in text)
    if tactile_hits == 0:
        return 0, "无触觉/力感知词"

    # Step 3: 必须有机器人相关词（与触觉结合的才是我们的领域）
    robot_hits = sum(1 for t in CORE_ROBOT if t in text)
    if robot_hits == 0:
        return 0, "无机器人相关词"

    # Step 4: 计算综合评分
    score = 0
    # 触觉词：最多 35 分
    score += min(tactile_hits * 12, 35)
    # 机器人词：最多 25 分
    score += min(robot_hits * 10, 25)
    # VLA词：最多 25 分
    vla_hits = sum(1 for t in VLA_TERMS if t in text)
    score += min(vla_hits * 8, 25)
    # 加分词：最多 15 分
    bonus_hits = sum(1 for t in BONUS_TERMS if t in text)
    score += min(bonus_hits * 3, 15)

    return score, ""


# ─── 创新点提取 ─────────────────────────────────────────

def extract_innovations(abstract: str, title: str) -> list[str]:
    """从摘要中提取/推断核心创新点"""
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
        # 兜底
        points.append(abstract[:120].strip().rstrip(".") + "...")
    else:
        points[:] = points[:4]  # 最多4个

    return points


# ─── 论文搜索 ───────────────────────────────────────────

def search_recent_papers(
    keywords: list[str],
    days: int = 7,
    max_papers: int = 20,
    min_score: int = 25,
) -> list[dict]:
    """
    搜索近N天发表的论文，返回相关性评分最高的论文。

    Args:
        keywords: 搜索关键词列表
        days: 搜索天数
        max_papers: 最多返回论文数
        min_score: 最低相关性评分阈值
    """
    cutoff = datetime.now() - timedelta(days=days)
    scored_results = {}  # base_id -> (paper, score)

    for kw in keywords:
        logger.info(f"Searching: {kw}")
        try:
            client = arxiv.Client()
            search = arxiv.Search(
                query=kw,
                max_results=30,  # 扩大搜索范围再过滤
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

                # 避免跨关键词重复
                if base_id in scored_results:
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

                paper = {
                    "base_id": base_id,
                    "versioned_id": raw_id,
                    "title": title,
                    "authors": authors,
                    "published": pub_date.isoformat(),
                    "abstract": abstract,
                    "abs_url": p.entry_id,
                    "pdf_url": p.entry_id.replace("/abs/", "/pdf/"),
                    "score": score,
                }
                scored_results[base_id] = (paper, score)
                logger.info(f"  [score={score}] {title[:70]}")

        except Exception as e:
            logger.warning(f"Search error for '{kw}': {e}")
            continue

    # 按相关性评分排序，取最高的 max_papers 篇
    sorted_papers = sorted(
        [p for p, s in scored_results.values()],
        key=lambda x: (x["score"], x["published"]),
        reverse=True,
    )[:max_papers]

    logger.info(f"相关性过滤后: {len(sorted_papers)} 篇 (上限 {max_papers})")
    return sorted_papers


# ─── 消息格式化 ─────────────────────────────────────────

def format_message(papers: list[dict], date_str: str) -> str:
    """构造 Server酱 Markdown 推送内容"""
    if not papers:
        return (
            f"# 📚 触觉×VLA 最新论文\n"
            f"**追踪日期**: {date_str}\n\n"
            f"本周暂无新发表论文，继续关注中..."
        )

    lines = [
        f"# 📚 触觉×VLA 最新论文",
        f"**追踪周期**: 近7天",
        f"**收录论文**: {len(papers)} 篇（相关性评分排序）",
        "",
        "---",
    ]

    for i, p in enumerate(papers, 1):
        innovations = extract_innovations(p["abstract"], p["title"])

        lines.append(f"## {i}. {p['title']}")
        lines.append("")
        lines.append(f"**👥 作者**: {', '.join(p['authors'])}")
        lines.append(f"**📅 发表**: {p['published']} | **相关度**: {p['score']}/100")
        lines.append("")

        # 摘要（限制长度）
        abstract = p["abstract"]
        if len(abstract) > 300:
            abstract = abstract[:300].strip().rstrip(".") + "..."
        lines.append(f"**📝 摘要**: {abstract}")
        lines.append("")

        # 创新点
        lines.append("**💡 核心创新**:")
        for ip in innovations:
            lines.append(f"- {ip}")
        lines.append("")

        # 链接
        lines.append(
            f"**🔗 arXiv**: [摘要]({p['abs_url']}) | [PDF]({p['pdf_url']})"
        )
        lines.append("")
        lines.append("---")

    lines.extend([
        "",
        "> 🤖 由 TactileVLA Tracker 自动推送 | 相关性评分过滤，仅收录高相关论文",
    ])

    return "\n".join(lines)


# ─── Server酱 推送 ──────────────────────────────────────

def send_serverchan(sendkey: str, content: str) -> bool:
    """调用 Server酱 API 发送消息，支持重试"""
    url = f"https://sctapi.ftqq.com/{sendkey}.send"

    # 截取标题（Server酱标题最长支持到一定长度）
    title = f"📚 触觉×VLA 新论文 | {datetime.now().strftime('%Y-%m-%d')}"

    payload = {
        "title": title,
        "desp": content,
        "content": content,
    }

    for attempt in range(3):
        try:
            resp = requests.post(url, data=payload, timeout=15)
            result = resp.json()
            logger.info(f"Server酱响应: {result}")

            if result.get("code") == 0 or result.get("data", {}).get("error") == "success":
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
    logger.info("触觉×VLA 论文追踪器 v2 启动")
    logger.info("=" * 50)

    # 1. 加载配置
    config = load_json(CONFIG_FILE)
    if not config.get("server_sendkey"):
        logger.error("错误：config.json 中未配置 server_sendkey！")
        print("请先在 config.json 中填入你的 Server酱 SendKey")
        sys.exit(1)

    keywords = config.get("keywords", [])
    days = config.get("days_back", 7)
    max_papers = config.get("max_papers", 20)
    min_score = config.get("min_score", 25)
    sendkey = config["server_sendkey"]

    logger.info(f"搜索策略: 近 {days} 天，最多 {max_papers} 篇，最低相关度 {min_score}/100")

    # 2. 搜索论文
    all_papers = search_recent_papers(
        keywords, days=days, max_papers=max_papers * 2, min_score=min_score
    )

    # 3. 加载历史，去重
    history = load_json(HISTORY_FILE)
    sent_ids = {p["id"] for p in history.get("papers", [])}
    new_papers = [p for p in all_papers if p["base_id"] not in sent_ids]
    # 截取上限
    new_papers = new_papers[:max_papers]
    logger.info(f"去重后新增论文: {len(new_papers)} 篇（上限 {max_papers} 篇）")

    if not new_papers:
        logger.info("本周无新论文，跳过推送")
        return 0

    # 4. 格式化消息
    date_str = datetime.now().strftime("%Y-%m-%d")
    message = format_message(new_papers, date_str)

    preview = message[:1500]
    if len(message) > 1500:
        preview += f"\n... (共 {len(message)} 字符，已截断预览)"
    logger.info(
        "\n" + "=" * 50 + "\n推送内容预览:\n" + "=" * 50
        + "\n" + preview + "\n" + "=" * 50 + "\n"
    )

    # 5. 发送推送
    success = send_serverchan(sendkey, message)

    # 6. 更新历史记录
    if success:
        today = datetime.now().strftime("%Y-%m-%d")
        for p in new_papers:
            history["papers"].insert(0, {
                "id": p["base_id"],
                "title": p["title"],
                "sent_date": today,
            })
        history["papers"] = history["papers"][:200]
        save_json(HISTORY_FILE, history)
        logger.info("历史记录已更新")

    logger.info("运行完成！")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
