# Douyin Profile Video Collect

抖音主页公开作品采集与媒体下载 Skill。

给它一个抖音主页链接、账号分享文案、单条视频/图文链接或 `aweme_id`，它可以采集公开作品数据，整理账号互动基础盘，并在用户确认后下载视频或图文素材。

## 可以做什么

- 采集抖音主页公开作品数据：点赞、评论、收藏、分享、发布时间、标题等。
- 整理账号公开互动基础盘，避免只看单条爆款。
- 区分普通作品和公开字段可识别的挂车/商品锚点作品。
- 下载前先让用户选择下载范围：
  - 下载全部采集作品
  - 只下载高低位抽样作品
  - 暂不下载
- 下载媒体素材：
  - 视频作品保存为 `.mp4`
  - 图片/图文作品保存为图片组和 JSON 清单
- 为后续内容拆解、达人初筛、素材库搭建准备数据和本地媒体文件。

## 适合场景

- 看一个达人主页的公开互动基础盘。
- 批量整理某个账号最近公开作品。
- 采集主页数据后，再决定要不要下载素材。
- 下载真实视频/图文素材，交给后续内容分析流程。
- 给团队建立达人内容素材库。

## 不适合做什么

- 不抓私密数据，只使用公开页面和公开接口能拿到的信息。
- 不直接判断 ROI、转化率、合作价值或产品适配度。
- 不替代完整内容拆解；它主要负责采集、基础整理和媒体下载。

## 安装

在 Codex 中可以直接说：

```text
安装这个 skill：https://github.com/mayichao1993/douyin-profile-video-collect
```

如果用命令安装：

```bash
python3 ~/.codex/skills/.system/skill-installer/scripts/install-skill-from-github.py \
  --repo mayichao1993/douyin-profile-video-collect \
  --path . \
  --name douyin-profile-video-collect
```

安装后重启 Codex。

## 基本用法

让 Codex 使用本 skill：

```text
用 douyin-profile-video-collect 采集这个抖音主页：
https://v.douyin.com/xxxxxx/
```

也可以给单条作品链接：

```text
用 douyin-profile-video-collect 下载这条抖音视频：
https://v.douyin.com/xxxxxx/
```

## 命令行用法

进入 skill 目录后运行：

```bash
python3 scripts/collect_profile_and_download.py '抖音主页链接或分享文案'
```

默认会先采集，再停下来给下载选项，不会直接下载全部。

指定采集普通作品数量：

```bash
python3 scripts/collect_profile_and_download.py 'https://www.douyin.com/user/xxx' --count 20
```

只采集数据，不下载媒体：

```bash
python3 scripts/collect_profile_and_download.py 'https://www.douyin.com/user/xxx' --download-mode none
```

用户已经确认后，下载全部采集作品：

```bash
python3 scripts/collect_profile_and_download.py 'https://www.douyin.com/user/xxx' --download-mode all-collected
```

只下载高低位抽样作品：

```bash
python3 scripts/collect_profile_and_download.py 'https://www.douyin.com/user/xxx' --download-mode samples --top-k 2
```

从已有结果目录继续下载，不重新采集：

```bash
python3 scripts/collect_profile_and_download.py \
  --resume-run-dir '/path/to/outputs/douyin_profile_video_collect/<timestamp>' \
  --download-mode all-collected
```

## 主要输出

每次运行会在 `outputs/douyin_profile_video_collect/<timestamp>/` 下生成结果。

常见文件：

- `raw.json`：原始采集数据和路由信息。
- `01_主页普通作品明细.csv`：主页普通作品明细。
- `01_公开互动基础盘数据.csv`：互动基础盘数据。
- `01_公开互动基础盘.md`：互动基础盘阅读版报告。
- `01_挂车作品明细.csv`：公开字段识别到的挂车/商品锚点作品。
- `02B_媒体细看抽样清单数据.csv`：下载候选清单。
- `02B_候选媒体下载结果.csv`：媒体下载结果。
- `2b_<作品ID>.mp4`：下载的视频文件。
- `2b_<作品ID>_images/`：图文作品图片组。
- `2b_<作品ID>_images.json`：图文作品图片清单和失败记录。

## 注意事项

- 抖音短链和媒体 URL 可能会过期，建议需要素材时及时下载。
- 图片/图文作品没有 `.mp4` 是正常的，会输出图片组。
- 单张图片下载失败不代表整条图文失败；只要至少一张图下载成功，就会保留图片证据，并在 JSON 清单里记录失败图片。
- 如果某条作品下载很慢，可以用 `--item-timeout <秒数>` 控制单条作品最大处理时间。
