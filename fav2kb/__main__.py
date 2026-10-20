"""命令行入口。供 agent 按 SKILL.md 调用，人也可以直接用。

    fav2kb setup [--asr]                        安装浏览器内核（--asr 顺带下载转写模型）
    fav2kb folders [--json]                     列出收藏夹（需登录，首次会弹窗扫码）
    fav2kb init --kb DIR (--folders A B | --all) [--asr [--no-audio]]
                                                   创建/修改知识库配置，并设为默认
    fav2kb sync [--kb DIR] [--limit N]          采集 → 下载素材 → （转写）；--limit 分批
    fav2kb pending [--kb DIR] [--json] [--limit N]  列出待 agent 提炼的条目
    fav2kb stats [--kb DIR] [--json]            估算提炼全部待提炼条目的代价
    fav2kb done ID... [--skip 原因] [--kb DIR]  标记已提炼（--skip：没写笔记及原因）
    fav2kb transcribe [--kb DIR]                单独补转写

只有 folders / sync 的采集段会用到登录态；采集代码按需导入，其余命令不加载 playwright。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .config import CaptureError, init_kb, load_kb_config, material_dir, resolve_kb

ASR_HINT = "转写需要可选依赖 asr：按 README 用 fav2kb[asr] 重新安装"


def _transcribe(kb: Path) -> None:
    try:
        from .transcribe import transcribe
    except ImportError:
        raise SystemExit(ASR_HINT)
    transcribe(material_dir(kb), load_kb_config(kb).get("keep_audio", True))


def cmd_setup(args) -> None:
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    if args.asr:
        try:
            from .transcribe import ensure_models
        except ImportError:
            raise SystemExit(ASR_HINT)
        ensure_models()
    print("准备完成")


def cmd_init(args) -> None:
    if not args.folders and not args.all:
        raise SystemExit("请用 --folders 指定收藏夹（名称或 id），或用 --all")
    if args.no_audio and not args.asr:
        raise SystemExit("--no-audio 需要同时开启 --asr（不转写时音频是唯一内容）")
    kb = init_kb(args.kb, None if args.all else args.folders, args.asr, not args.no_audio)
    out = material_dir(kb)
    print(f"知识库：{kb}")
    print(f"素材目录：{out}")
    print(f"偏好文件（由 agent 写）：{out / 'preferences.md'}")


def cmd_folders(args) -> None:
    from .capture import list_folders

    folders = list_folders(headed=args.headed)
    if args.json:
        print(json.dumps(folders, ensure_ascii=False))
        return
    for f in folders:
        print(f"{f['id']}  {f['name']}（{f['total']} 个）")


def cmd_sync(args) -> None:
    from .capture import capture
    from .export import export, load_manifest, pending

    kb = resolve_kb(args.kb)
    config, out = load_kb_config(kb), material_dir(kb)
    raw_files = capture(config.get("folders"), headed=args.headed)  # 返回前浏览器已关闭
    export(raw_files, out, args.limit)
    if config.get("asr"):
        _transcribe(kb)

    failed = [e for e in load_manifest(out).values() if e.get("error")]
    print(f"\n待提炼 {len(pending(out, config.get('asr', False)))} 条；媒体不完整 {len(failed)} 条")
    for e in failed[:10]:
        print(f"  ⚠ {e['title']}：{e['error']}")


def cmd_pending(args) -> None:
    from .export import pending

    kb = resolve_kb(args.kb)
    items = pending(material_dir(kb), load_kb_config(kb).get("asr", False))
    total, items = len(items), items[: args.limit] if args.limit else items
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=1))
        return
    for e in items:
        print(f"{e['aweme_id']}  {e['type']}  {e['title']}  →  {e['readme']}")
    print(f"共 {total} 条待提炼" + (f"，以上为前 {len(items)} 条" if len(items) < total else ""))


def cmd_done(args) -> None:
    from .export import mark_done

    missing = mark_done(material_dir(resolve_kb(args.kb)), args.ids, args.skip or "")
    if missing:
        raise SystemExit(f"找不到这些 id：{' '.join(missing)}")
    print(f"已标记 {len(args.ids)} 条" + (f"为跳过：{args.skip}" if args.skip else "为已提炼"))


def cmd_stats(args) -> None:
    from .export import stats

    kb = resolve_kb(args.kb)
    s = stats(material_dir(kb), load_kb_config(kb).get("asr", False))
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=1))
        return
    types = " / ".join(f"{k} {v}" for k, v in s["by_type"].items())
    folders = "，".join(f"{k} {v}" for k, v in s["by_folder"].items())
    print(f"待提炼 {s['pending']} 条（{types}）")
    print(f"按收藏夹：{folders}")
    print(f"提炼需读入：文本约 {s['est_text_tokens']:,} token，图片 {s['images']} 张"
          f"（约 {s['est_image_tokens']:,} token）")
    print(f"其中转写内容很少的视频 {s['low_quality_transcripts']} 条（提炼价值低），"
          f"含疑似歌词或杂音的视频 {s['lyric_suspects']} 条")
    print(f"依赖画面：信息主要在画面 {s['visual_high']} 条，部分需看画面 {s['visual_medium']} 条"
          f"（只看文字会丢信息）")
    print(f"素材占用磁盘：{s['material_mb']} MB")


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    parser = argparse.ArgumentParser(prog="fav2kb", description="把收藏夹沉淀为 agent 知识库（目前支持抖音收藏夹）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("setup", help="安装浏览器内核；--asr 顺带下载转写模型")
    p.add_argument("--asr", action="store_true")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("init", help="创建或修改知识库配置，并设为默认知识库")
    p.add_argument("--kb", type=Path, required=True, help="知识库根目录")
    p.add_argument("--folders", nargs="+", help="要同步的收藏夹名称或 collects_id")
    p.add_argument("--all", action="store_true", help="同步全部收藏夹")
    p.add_argument("--asr", action="store_true", help="视频转写为文字")
    p.add_argument("--no-audio", action="store_true", help="转写成功后删除音频，省磁盘")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("folders", help="列出收藏夹（需登录）")
    p.add_argument("--json", action="store_true")
    p.add_argument("--headed", action="store_true", help="显示浏览器（处理安全验证时用）")
    p.set_defaults(func=cmd_folders)

    p = sub.add_parser("sync", help="同步收藏到素材目录（直链有时效，采集与下载一次完成）")
    p.add_argument("--kb", type=Path)
    p.add_argument("--limit", type=int, help="本次最多处理 N 条新条目（首次大量导入时分批）")
    p.add_argument("--headed", action="store_true", help="显示浏览器（处理安全验证时用）")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("pending", help="列出待提炼的条目")
    p.add_argument("--kb", type=Path)
    p.add_argument("--json", action="store_true")
    p.add_argument("--limit", type=int, help="只列前 N 条（分批提炼用）")
    p.set_defaults(func=cmd_pending)

    p = sub.add_parser("stats", help="估算提炼全部待提炼条目的代价")
    p.add_argument("--kb", type=Path)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("done", help="标记条目已提炼")
    p.add_argument("ids", nargs="+")
    p.add_argument("--skip", metavar="原因", help="没写笔记时注明原因，会显示在素材索引里")
    p.add_argument("--kb", type=Path)
    p.set_defaults(func=cmd_done)

    p = sub.add_parser("transcribe", help="单独补转写视频")
    p.add_argument("--kb", type=Path)
    p.set_defaults(func=lambda a: _transcribe(resolve_kb(a.kb)))

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except CaptureError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
