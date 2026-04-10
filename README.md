# 触觉 x VLA 论文追踪器 v2

自动追踪 arXiv 上触觉感知与 VLA（Vision-Language-Action）结合的最新论文，每周推送到微信。

---

## 功能特点

- 🔍 精准关键词搜索，覆盖触觉+VLA 核心方向
- 🎯 **相关性评分过滤**：只推送真正相关的论文，排除医学影像、纯音频等干扰论文
- 📊 每周推送上限 20 篇（可在 config.json 调整）
- 🚫 智能去重，不重复推送已推送过的论文
- 📱 通过 Server酱 推送至微信
- ⏰ Windows 任务计划程序，每周一定时自动运行

---

## 相关性评分机制

系统内置相关性评分（0-100），每篇论文必须满足以下**硬性条件**才可能通过：

| 条件 | 关键词示例 |
|------|-----------|
| 有触觉/力感知词 | tactile, haptic, force, touch |
| 有机器人相关词 | robot, manipulation, grasp, dexterity |
| ❌ 无排除领域词 | fundus/retinal（眼底影像）、audio/speech（音频）、medical（医学）等 |

满足硬性条件后，分数由触觉词命中数 × 权重 + 机器人词命中数 × 权重 + VLA词命中数 × 权重综合计算。

---

## 目录结构

```
tactile_vla_tracker/
├── paper_tracker.py   # 主程序（搜索→评分过滤→去重→推送）
├── config.json        # 配置文件（SendKey + 关键词 + 策略参数）
├── sent_papers.json   # 已推送论文历史记录（自动生成）
├── tracker.log        # 运行日志（自动生成）
├── requirements.txt   # Python 依赖
└── README.md          # 本文件
```

---

## 快速上手

### 第一步：安装依赖

克隆仓库后，进入项目目录并安装依赖：

```powershell
cd tactile-vla-tracker
pip install -r requirements.txt
```

### 第二步：配置

将 `config.example.json` 复制为 `config.json`，填入你的 Server酱 SendKey：

```powershell
copy config.example.json config.json
# 然后用文本编辑器打开 config.json，填入 SendKey
```

### 第三步：测试运行

```powershell
python paper_tracker.py
```

运行后：
1. 搜索近 7 天论文（相关性评分过滤）
2. 预览推送内容
3. 发送到你的微信

---

## 定时任务（每周一自动推送）

已通过 schtasks 创建定时任务，每周一早上 9:00 自动运行。

```powershell
# 创建定时任务（将路径替换为你本地克隆的目录）
schtasks /create `
    /tn "TactileVLA_PaperTracker" `
    /tr "python X:\你的路径\tactile-vla-tracker\paper_tracker.py" `
    /sc weekly `
    /d MON `
    /st 09:00 `
    /f

# 查看已创建的任务
schtasks /query /tn "TactileVLA_PaperTracker"

# 手动立即运行
schtasks /run /tn "TactileVLA_PaperTracker"

# 删除定时任务
schtasks /delete /tn "TactileVLA_PaperTracker" /f
```

---

## 自定义配置

编辑 `config.json`：

```json
{
    "server_sendkey": "你的SendKey",
    "keywords": [
        "tactile VLA",
        "tactile vision language action",
        ...
    ],
    "days_back": 7,
    "max_papers": 20,
    "min_score": 25
}
```

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `days_back` | 搜索近几天内的论文 | 7 |
| `max_papers` | 每周最多推送多少篇 | 20 |
| `min_score` | 最低相关性评分阈值（0-100） | 25 |

---

## 常见问题

**Q: 推送没收到？**
1. 检查 `config.json` 中 SendKey 是否正确
2. 确认已关注「Server酱」公众号
3. 查看 `tracker.log` 日志排查错误

**Q: 想手动触发一次？**
```powershell
python paper_tracker.py
```

**Q: 想重新推送所有论文？**
删除 `sent_papers.json`，脚本将重新推送所有搜索到的论文。
