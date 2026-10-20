# fav2kb：把收藏夹变成你的 AI 知识库

**刷到有用的抖音，就放进对应的收藏夹（比如「健身」「菜谱」），你的 agent（Claude Code / Codex 等）会把它整理进你的知识库。**

> **目前支持：抖音收藏夹**（视频、图文、文章）。名字里的 fav2kb 意思是 favorites → knowledge base。
>
> Turn your Douyin (Chinese TikTok) favorites folders into a personal knowledge base for AI agents: local speech-to-text, images and articles saved as Markdown, distilled by your own agent via an Agent Skill.

收藏夹就是主题：你选哪些收藏夹进知识库，agent 只整理这些。只点了收藏、没放进任何收藏夹的作品不会同步。

- 视频 → 本地转写为带时间戳的文字（音轨也保留）
- 图文 → 文案 + 全部图片
- 文章 → Markdown 正文 + 内嵌图片

**知识库用什么都行**：一个普通的 Markdown 文件夹（任何编辑器都能打开，也可以放进你的项目里给 agent 读），或者一个 Obsidian 库（会用上双链 `[[ ]]`、标签和 frontmatter），也可以沿用你已有的笔记格式。

放在哪、存成什么格式、要不要提炼、怎么提炼（不提炼 / 一行摘要 / 每条一篇笔记 / 按主题合并，每个收藏夹可以不同；同步后立即提炼或用到时再提炼），都由 agent 第一次使用时问你，之后按你的偏好执行。待提炼的内容很多时，agent 会先估算要消耗多少 token，由你决定提炼多少、怎么提炼。

## 安装

### 方式一：让 agent 自己装（推荐）

把下面这段话发给你的 agent（Claude Code、Codex 等）：

> 帮我安装 fav2kb：https://github.com/a915578105/fav2kb ，按仓库 README 里「给 agent 的安装步骤」操作，装好后带我完成首次配置。

agent 会自己完成这些事：下载 skill、安装命令行工具、问你要不要视频转写、问清知识库的存放和整理偏好，最后弹出浏览器让你扫码登录。你只需要回答问题和扫码。

### 方式二：手动安装

1. 把 [`skills/fav2kb/`](skills/fav2kb/SKILL.md) 整个目录复制到你的 agent 的 skills 目录：

   | Agent | 目录 |
   |---|---|
   | Claude Code | `~/.claude/skills/fav2kb/` |
   | Codex | `~/.codex/skills/fav2kb/` |
   | 其他支持 Agent Skills 的 agent | 见各自文档 |

2. 安装命令行工具（需要 [uv](https://docs.astral.sh/uv/)）：

   ```bash
   uv tool install "fav2kb[asr] @ git+https://github.com/a915578105/fav2kb"
   ```

   ```bash
   fav2kb setup --asr
   ```

   不需要视频转写时，去掉 `[asr]` 和 `--asr`。

3. 对 agent 说：**"把我的抖音收藏整理进知识库"**。

### 给 agent 的安装步骤

如果你是被用户要求安装 fav2kb 的 agent，按顺序执行；每一步失败都要如实告诉用户，不要跳过：

1. 确认本机有 `uv`（`uv --version`）。没有的话，告诉用户需要安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，征得同意后再装。
2. 问用户要不要视频转写：转写在本地完成，首次会下载约 230MB 模型。要的话后面带 `[asr]` / `--asr`。
3. 安装命令行工具：`uv tool install "fav2kb[asr] @ git+https://github.com/a915578105/fav2kb"`，然后运行 `fav2kb setup --asr`。
4. 把本仓库的 `skills/fav2kb/` 目录复制到你自己的 skills 目录（Claude Code 是 `~/.claude/skills/fav2kb/`，Codex 是 `~/.codex/skills/fav2kb/`；其他 agent 按自己的约定放）。可以浅克隆仓库后复制，复制完删掉临时克隆。
5. 运行 `fav2kb --help` 确认命令可用，然后读刚装好的 `SKILL.md`，从「首次配置」开始带用户完成设置。

## 工作方式

```
抖音收藏 ──(登录态，仅此一步)──▶ 列表与直链 ──(无登录态)──▶ 原始素材 ──(你的 agent)──▶ 你的笔记
           fav2kb sync                        下载/转写        _fav2kb/          按 preferences.md 提炼
```

- **登录态只在采集列表时使用**：用本地浏览器 profile（`~/.fav2kb/browser/`）打开收藏页，让页面自己发请求，程序只监听响应。抖音接口带一次性签名，直接调用会被拦截，所以不走逆向签名。
- **下载不带 Cookie**：浏览器关闭后，只用 CDN 直链下载，请求头只有 `User-Agent` 和 `Referer`。
- **转写在本地完成**：[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) + [SenseVoiceSmall](https://github.com/FunAudioLLM/SenseVoice)（int8）。纯 CPU 运行，不依赖 torch，自带标点；约 10 秒处理一条 1～3 分钟的视频。
- **提炼由你自己的 agent 完成**：不需要另配 LLM API key，按你写下的偏好执行。转写会初筛三类情况：内容很少、混有背景音乐歌词、信息主要在画面里。agent 按你的偏好处理（排除歌词，或保留做台词库；依赖画面的视频标注"需看画面"并汇总成待回看清单），不会硬编笔记。

## 命令

| 命令 | 作用 |
|---|---|
| `fav2kb setup [--asr]` | 安装浏览器内核；`--asr` 时下载转写模型（约 230MB） |
| `fav2kb folders [--json]` | 列出收藏夹（首次会弹窗扫码） |
| `fav2kb init --kb DIR (--folders ID... \| --all) [--asr [--no-audio]]` | 创建或修改知识库配置，并设为默认；`--no-audio` 表示转写后删除音频 |
| `fav2kb sync [--limit N]` | 采集 → 下载素材 →（转写）；可重复运行，只处理新增和失败的条目。`--limit` 用于首次大量导入时分批 |
| `fav2kb pending [--json] [--limit N]` | 待 agent 提炼的条目 |
| `fav2kb stats [--json]` | 估算提炼全部待提炼条目要读入多少 token、多少张图，以及素材占用的磁盘 |
| `fav2kb done ID... [--skip 原因]` | 标记为已提炼；`--skip` 表示没写笔记，并记录原因 |
| `fav2kb transcribe` | 单独补转写 |

遇到安全验证时，给 `folders` / `sync` 加 `--headed`，在浏览器里手动完成验证。

## 目录

```
~/.fav2kb/            程序数据（可用环境变量 FAV2KB_HOME 修改）
  browser/               登录态，内含 Cookie，勿外传
  raw/  models/  config.json
<你的知识库>/
  _fav2kb/               fav2kb 维护的原始素材，请勿手改
    config.json          同步哪些收藏夹、是否转写
    preferences.md       你的整理偏好（agent 写入和读取）
    manifest.json        每条作品的状态（是否完整、失败原因、转写质量、是否已提炼或跳过）
    INDEX.md
    items/<aweme_id>/    README.md、audio.m4a、images/
  ……                     agent 按你的偏好写的笔记（普通 Markdown 或 Obsidian 格式）
```

## 已知限制

- 只同步收藏夹里的作品；没放进收藏夹的「收藏」不会同步
- 收藏夹显示的数量包含已失效作品，实际同步的条数可能更少
- **暂不支持「纯音乐 + 画面字幕」的视频**（以及只有动作演示、没有口播的视频）：抖音网页端不提供字幕文件，字幕都烧录在画面里，提取要逐帧识图，成本很高。这类视频会被标为「转写内容很少」，只保留标题和文案；有口播的视频由语音转写覆盖
- 依赖抖音网页的当前结构，网页改版后可能需要更新
- 转写会有少量同音字错误；中英混说的专有名词、背景音乐很重的视频识别效果较差

## 同类项目

[douyin-favorites-to-knowledge](https://github.com/tars1230/douyin-favorites-to-knowledge) 定位是后台静默同步：默认云端转写，按日生成日报，按全部收藏同步。本项目的区别在于：由 agent 询问用户并负责提炼，可以按收藏夹选择主题，转写在本地完成，下载不带登录态，图文保留全部图片。

## 声明

仅用于整理你本人账号的收藏，供个人学习使用。视频、图片、文字的版权归原作者所有。
