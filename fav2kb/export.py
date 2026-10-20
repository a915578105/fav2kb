"""处理阶段：把采集结果写成原始素材。不使用任何抖音登录态。

只用采集阶段交来的 CDN 直链下载，请求头只有 User-Agent 和 Referer，
不带 Cookie，也不读取浏览器 profile。本模块不得 import capture / playwright。

素材结构（<out> 即 <知识库>/_fav2kb）：
    <out>/INDEX.md                      素材索引，按收藏夹分组
    <out>/manifest.json                 每条状态：媒体是否完整、失败原因、是否转写、是否已提炼
    <out>/items/<aweme_id>/README.md    元信息 + 文案 / 文章正文（+ 转写）
    <out>/items/<aweme_id>/audio.m4a    视频的音轨
    <out>/items/<aweme_id>/images/      图文、文章的图片

素材只增不改；按用户偏好提炼成笔记是 agent 的事，提炼完用 done 标记。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from imageio_ffmpeg import get_ffmpeg_exe

from .config import read_json, write_atomic, write_json

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Referer": "https://www.douyin.com/",
}
KIND_LABEL = {"video": "视频", "note": "图文", "article": "文章"}
MD_IMAGE = re.compile(r"!\[[^\]]*\]\((\S+?)(?:\s[^)]*)?\)")


def download(urls: list[str], dest: Path) -> tuple[str | None, str]:
    """依次尝试直链，返回 (成功所用的 URL, 最后一次失败原因)。"""
    part = dest.with_name(dest.name + ".part")
    error = "没有可用直链"
    for url in urls:
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=60) as resp, part.open("wb") as f:
                shutil.copyfileobj(resp, f)
            part.replace(dest)
            return url, ""
        except urllib.error.HTTPError as exc:
            error = f"下载被拒 HTTP {exc.code}（直链可能已过期）"
        except Exception as exc:
            error = f"下载失败 {type(exc).__name__}"
        part.unlink(missing_ok=True)
    return None, error


def extract_audio(src: Path, dest: Path) -> bool:
    """去掉视频轨；能直接拷贝音轨就不重新编码。"""
    for codec in (["-c:a", "copy"], ["-c:a", "aac", "-b:a", "96k"]):
        cmd = [get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(src), "-vn", *codec, str(dest)]
        if subprocess.run(cmd, capture_output=True).returncode == 0:
            return True
    dest.unlink(missing_ok=True)
    return False


def _image_name(index: int, url: str) -> str:
    ext = ".webp" if ".webp" in url.split("?")[0] else ".jpg"
    return f"images/{index:02d}{ext}"


def _download_images(candidates: list[list[str]], folder: Path) -> tuple[list[str | None], str]:
    """每张图有多个候选直链；返回 (相对路径，失败的为 None；失败原因)。"""
    (folder / "images").mkdir(exist_ok=True)
    paths: list[str | None] = []
    errors = []
    for index, urls in enumerate(candidates, 1):
        rel = _image_name(index, urls[0])
        used, error = download(urls, folder / rel)
        if used and _image_name(index, used) != rel:  # 用了兜底格式，改成正确扩展名
            (folder / rel).replace(folder / _image_name(index, used))
            rel = _image_name(index, used)
        paths.append(rel if used else None)
        if error:
            errors.append(f"图{index}：{error}")
    return paths, "；".join(errors)


def export_media(item: dict, folder: Path) -> tuple[str, str]:
    """下载媒体，返回 (README 中的媒体段落, 失败原因；空串表示全部成功)。"""
    kind = item["kind"]

    if kind == "video":
        src, error = folder / "_source.mp4", "没有带音轨的播放地址"
        for urls in item["video_sources"]:  # 某档下载或抽音失败就换下一档
            used, error = download(urls, src)
            if used and not extract_audio(src, folder / "audio.m4a"):
                error = "抽取音轨失败"
            src.unlink(missing_ok=True)
            if not error:
                break
        return f"## 音频\n\n[audio.m4a](audio.m4a)（时长 {item['duration_s']} 秒）", error

    if kind == "note":
        paths, error = _download_images(item["images"], folder)
        lines = [f"![图{i}]({p})" for i, p in enumerate(paths, 1) if p]
        return "## 图片\n\n" + "\n\n".join(lines), error

    # article：下载正文里的图片并改成本地路径
    markdown = item["article_md"]
    urls = [m.group(1) for m in MD_IMAGE.finditer(markdown)]
    paths, error = _download_images([[u] for u in urls], folder)
    local = dict(zip(urls, paths))
    markdown = MD_IMAGE.sub(
        lambda m: f"![]({local[m.group(1)]})" if local.get(m.group(1)) else m.group(0),
        markdown,
    )
    return "## 正文\n\n" + markdown, error


def title_of(item: dict) -> str:
    """标题取首个非空行，去掉话题标签和 @。"""
    for line in (item["title"] + "\n" + item["desc"]).splitlines():
        line = re.sub(r"[#@]\S+", "", line).strip()
        if line:
            return line[:50]
    return item["aweme_id"]


def render(item: dict, media_md: str, error: str) -> str:
    created = item["create_time"] and datetime.fromtimestamp(item["create_time"]).strftime("%Y-%m-%d")
    meta = {
        "aweme_id": item["aweme_id"],
        "type": KIND_LABEL[item["kind"]],
        "author": item["author"],
        "created": created or "",
        "url": item["url"],
    }
    front = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in meta.items())
    parts = [
        f"---\n{front}\n---",
        f"# {title_of(item)}",
        f"作者：{item['author']}　发布：{created or '未知'}　[原链接]({item['url']})",
    ]
    if item["desc"]:
        parts.append("## 文案\n\n" + item["desc"])
    parts.append(media_md)
    if item["kind"] == "video":
        parts.append("> 「文案」是作者发布时写的描述，不是视频逐字稿；逐字稿见「转写」（若已转写）。")
    if error:
        parts.append(f"> ⚠ 媒体不完整：{error}。下次 sync 会用新直链自动重试。")
    return "\n\n".join(parts) + "\n"


def write_index(out: Path, manifest: dict) -> None:
    groups: dict[str, list[dict]] = {}
    for entry in manifest.values():
        for folder in entry["folders"]:
            groups.setdefault(folder, []).append(entry)
    # 同名收藏夹在 manifest 里按 id 区分；索引里合并显示，条目各自带收藏夹 id

    lines = [
        "# 抖音收藏原始素材",
        "",
        "由 fav2kb 生成，请勿手改。每条作品一个目录：`items/<aweme_id>/README.md` 是元信息、"
        "文案/正文与转写，视频音轨在同目录 `audio.m4a`，图文/文章的图片在 `images/`。",
        "",
    ]
    for folder in sorted(groups):
        entries = sorted(groups[folder], key=lambda e: e["created"], reverse=True)
        lines += [f"## {folder}（{len(entries)}）", ""]
        for e in entries:
            warn = " ⚠媒体不全" if not e["complete"] else ""
            if e.get("transcript_quality") in ("low", "none"):
                warn += " · 转写内容很少"
            if e.get("skip_reason"):
                warn += f" · 未提炼：{e['skip_reason']}"
            lines.append(
                f"- [{e['title']}](items/{e['aweme_id']}/README.md)"
                f" · {e['type']} · {e['author']} · {e['created']}{warn}"
            )
        lines.append("")
    write_atomic(out / "INDEX.md", "\n".join(lines))


def load_manifest(out: Path) -> dict:
    return read_json(out / "manifest.json", {})


def save_manifest(out: Path, manifest: dict) -> None:
    write_json(out / "manifest.json", manifest)


def export(raw_files: list[Path], out: Path, limit: int | None = None) -> None:
    """limit：本次最多处理多少条新条目，其余留给下次 sync（届时会重新拉列表拿新直链）。"""
    out.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(out)
    processed = deferred = 0
    print("收藏列表已采集完，浏览器已关闭；开始下载媒体（不再使用登录态）", flush=True)

    for raw_file in raw_files:
        data = json.loads(Path(raw_file).read_text(encoding="utf-8"))
        folder_name, folder_id = data["folder"]["name"], data["folder"]["id"]
        items = data["items"]
        print(f"导出「{folder_name}」{len(items)} 条 → {out}", flush=True)

        for n, item in enumerate(items, 1):
            entry = manifest.get(item["aweme_id"])
            folders = entry["folders"] if entry else []
            folder_ids = entry.setdefault("folder_ids", []) if entry else []
            if folder_id not in folder_ids:  # 同名收藏夹靠 id 区分
                folder_ids.append(folder_id)
                if folder_name not in folders:
                    folders.append(folder_name)
            if entry and entry["complete"]:
                continue
            if limit is not None and processed >= limit:
                deferred += 1
                continue
            processed += 1

            item_dir = out / "items" / item["aweme_id"]
            item_dir.mkdir(parents=True, exist_ok=True)
            media_md, error = export_media(item, item_dir)
            write_atomic(item_dir / "README.md", render(item, media_md, error))

            created = item["create_time"]
            manifest[item["aweme_id"]] = {
                "aweme_id": item["aweme_id"],
                "title": title_of(item),
                "type": KIND_LABEL[item["kind"]],
                "author": item["author"],
                "created": datetime.fromtimestamp(created).strftime("%Y-%m-%d") if created else "",
                "folders": folders,
                "folder_ids": folder_ids,
                "complete": not error,
                "error": error,
                "distilled": False,
            }
            save_manifest(out, manifest)
            print(f"  [{n}/{len(items)}] {KIND_LABEL[item['kind']]} {title_of(item)}"
                  f" {f'⚠ {error}' if error else '✓'}", flush=True)

    save_manifest(out, manifest)
    write_index(out, manifest)
    print(f"索引：{out / 'INDEX.md'}")
    if deferred:
        print(f"本次处理 {processed} 条，还有 {deferred} 条留到下次 sync（会重新拉列表，拿新直链）")


def pending(out: Path, asr: bool) -> list[dict]:
    """素材已齐（要转写的已转写）但 agent 还没提炼的条目。"""
    return [
        {**e, "readme": str(out / "items" / e["aweme_id"] / "README.md")}
        for e in load_manifest(out).values()
        if e["complete"] and not e.get("distilled")
        and not (asr and e["type"] == "视频" and not e.get("transcribed"))
    ]


def mark_done(out: Path, ids: list[str], skip_reason: str = "") -> list[str]:
    """标记为已处理，返回找不到的 id。skip_reason 非空表示没写笔记及原因，会显示在索引里。"""
    manifest = load_manifest(out)
    for aweme_id in ids:
        if aweme_id in manifest:
            manifest[aweme_id]["distilled"] = True
            manifest[aweme_id]["skip_reason"] = skip_reason
    save_manifest(out, manifest)
    write_index(out, manifest)
    return [i for i in ids if i not in manifest]


IMAGE_TOKENS = 1200  # 多模态模型读一张图的输入 token，按常见尺寸粗估


def stats(out: Path, asr: bool) -> dict:
    """估算提炼全部待提炼条目要读入多少内容，供 agent 向用户说明代价。"""
    items = pending(out, asr)
    by_type: dict[str, int] = {}
    by_folder: dict[str, int] = {}
    text_chars = images = low = lyrics = 0
    visual: dict[str, int] = {}
    for e in items:
        item_dir = out / "items" / e["aweme_id"]
        text_chars += len((item_dir / "README.md").read_text(encoding="utf-8"))
        if (item_dir / "images").exists():
            images += sum(1 for _ in (item_dir / "images").iterdir())
        by_type[e["type"]] = by_type.get(e["type"], 0) + 1
        low += e.get("transcript_quality") in ("low", "none")
        lyrics += bool(e.get("lyric_lines"))
        level = e.get("visual_dependency", "low")
        visual[level] = visual.get(level, 0) + 1
        for folder in e["folders"]:
            by_folder[folder] = by_folder.get(folder, 0) + 1
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    return {
        "pending": len(items),
        "by_type": by_type,
        "by_folder": by_folder,
        "text_chars": text_chars,
        "est_text_tokens": text_chars,  # 中文约 1 字 ≈ 1 token，偏保守
        "images": images,
        "est_image_tokens": images * IMAGE_TOKENS,
        "low_quality_transcripts": low,
        "lyric_suspects": lyrics,
        "visual_high": visual.get("high", 0),  # 信息主要在画面，纯文字笔记会丢信息
        "visual_medium": visual.get("medium", 0),
        "material_mb": round(size / 2**20, 1),
    }
