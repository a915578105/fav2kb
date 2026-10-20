"""采集阶段：整个项目中唯一使用抖音登录态的地方。

用本地浏览器 profile 打开收藏页，让页面自己发请求（抖音接口带一次性签名，
无法重放），程序只监听两个接口的响应：

- collects/list        收藏夹列表
- collects/video/list  收藏夹内作品，按 collects_id 区分

注意：收藏夹列表页会为每个收藏夹预取第一页作封面，所以必须按 collects_id
过滤，且要点进目标收藏夹再滚动才会继续翻页。

产出 raw/<collects_id>.json，里面带齐全部媒体直链；后续处理只读这个文件。
"""

from __future__ import annotations

import json
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

from .config import PROFILE, RAW_DIR, CaptureError, write_json

HOME_URL = "https://www.douyin.com/"
FAVORITE_URL = (
    "https://www.douyin.com/user/self"
    "?showTab=favorite_collection&showSubTab=favorite_folder"
)
FOLDER_LIST_API = "/aweme/v1/web/collects/list/"
VIDEO_LIST_API = "/aweme/v1/web/collects/video/list/"
FOLDER_CARDS = 'ul[data-e2e="scroll-list"] > li'
WORK_CARDS = 'a[href*="/video/"], a[href*="/note/"], a[href*="/article/"]'
LOGIN_COOKIES = {"sessionid", "sessionid_ss"}

LOGIN_TIMEOUT_S = 300
STALE_ROUNDS = 8  # 连续多少轮滚动没有新响应就停

# 把最后一张卡片滚进视口，触发页面的懒加载
SCROLL_LAST_JS = """sel => {
  const els = document.querySelectorAll(sel);
  if (els.length) els[els.length - 1].scrollIntoView({block: 'end'});
}"""


class Recorder:
    """监听页面响应，按 collects_id 归档。"""

    def __init__(self, page) -> None:
        self.folders: dict[str, dict] = {}  # 保持接口顺序，与页面卡片顺序一致
        self.folders_more = True
        self.items: dict[str, dict[str, dict]] = defaultdict(dict)
        self.has_more: dict[str, bool] = {}
        self.responses: dict[str, int] = defaultdict(int)
        page.on("response", self._on_response)

    def _on_response(self, response) -> None:
        parsed = urlparse(response.url)
        if parsed.path not in (FOLDER_LIST_API, VIDEO_LIST_API):
            return
        try:
            data = response.json()
        except Exception:
            return

        if parsed.path == FOLDER_LIST_API:
            for folder in data.get("collects_list") or []:
                cid = folder["collects_id_str"]
                self.folders[cid] = {
                    "id": cid,
                    "name": folder.get("collects_name") or "",
                    "total": folder.get("total_number") or 0,
                }
            self.folders_more = bool(data.get("has_more"))
            return

        cid = (parse_qs(parsed.query).get("collects_id") or [""])[0]
        if not cid:
            return
        for aweme in data.get("aweme_list") or []:
            if aweme.get("aweme_id"):
                self.items[cid].setdefault(str(aweme["aweme_id"]), aweme)
        self.has_more[cid] = bool(data.get("has_more"))
        self.responses[cid] += 1


def _launch(pw, headed: bool):
    try:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            headless=not headed,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            args=["--disable-blink-features=AutomationControlled"],
        )
    except Exception as exc:
        if "Executable doesn't exist" in str(exc):
            raise CaptureError("还没装浏览器内核，先运行：fav2kb setup") from exc
        # 同一 profile 不能被两个浏览器同时打开
        raise CaptureError(
            f"浏览器启动失败（{type(exc).__name__}）。若有另一个 fav2kb 正在运行，等它结束后重试。"
        ) from exc
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    return context


def _logged_in(context) -> bool:
    return bool({c["name"] for c in context.cookies()} & LOGIN_COOKIES)


def _page(context):
    return context.pages[0] if context.pages else context.new_page()


def _goto(page, url: str) -> None:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass  # 抖音常在加载中途重定向，打断 goto 属正常


def _wait(page, condition, timeout_s: float) -> bool:
    for _ in range(int(timeout_s * 2)):
        if condition():
            return True
        page.wait_for_timeout(500)
    return bool(condition())


@contextmanager
def browser(headed: bool = False):
    """打开已登录的浏览器；未登录时弹出有头窗口等待扫码。"""
    PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        context = _launch(pw, headed)
        try:
            if not _logged_in(context):
                if not headed:
                    context.close()
                    context = _launch(pw, headed=True)
                page = _page(context)
                _goto(page, HOME_URL)
                print(f"请在弹出的浏览器里扫码登录抖音（{LOGIN_TIMEOUT_S} 秒内）…", flush=True)
                if not _wait(page, lambda: _logged_in(context), LOGIN_TIMEOUT_S):
                    raise CaptureError("等待登录超时")
                page.wait_for_timeout(3000)  # 让登录后的 Cookie 写全
            page = _page(context)
            yield page, Recorder(page)
        finally:
            context.close()


def open_favorites(page, rec: Recorder) -> list[dict]:
    """打开收藏夹列表页并加载出全部收藏夹。"""
    rec.folders_more = True
    _goto(page, FAVORITE_URL)
    if not _wait(page, lambda: rec.folders, 20):
        raise CaptureError(
            "没拿到收藏夹列表。可能弹出了安全验证或登录失效，"
            "请加 --headed 在浏览器里手动处理后重试。"
        )
    stale = 0
    while rec.folders_more and stale < STALE_ROUNDS:
        before = len(rec.folders)
        page.evaluate(SCROLL_LAST_JS, FOLDER_CARDS)
        page.wait_for_timeout(1200)
        stale = stale + 1 if len(rec.folders) == before else 0
    return list(rec.folders.values())


def collect_folder(page, rec: Recorder, folder: dict) -> dict:
    """点进收藏夹并滚动到底，返回该收藏夹的采集结果。"""
    open_favorites(page, rec)
    cid = folder["id"]
    index = list(rec.folders).index(cid)
    cards = page.locator(FOLDER_CARDS)
    if not _wait(page, lambda: cards.count() > index, 10):
        raise CaptureError(f"页面上找不到收藏夹「{folder['name']}」的卡片")
    card = cards.nth(index)
    if not card.inner_text().strip().startswith(folder["name"]):
        raise CaptureError("页面卡片顺序与接口不一致，无法安全定位收藏夹")

    # 列表页的预取已拿到第一页；点进去后滚动，页面才会请求后续分页
    card.click()
    page.wait_for_timeout(2000)
    page.mouse.move(720, 600)
    stale, seen = 0, rec.responses[cid]
    while rec.has_more.get(cid, True) and stale < STALE_ROUNDS:
        if stale:  # 卡住多半是懒加载哨兵已在视口内不再触发：先往回滚，再滚到底
            page.mouse.wheel(0, -1200)
            page.wait_for_timeout(400)
        page.evaluate(SCROLL_LAST_JS, WORK_CARDS)
        page.mouse.wheel(0, 1500)
        page.wait_for_timeout(1500)
        if rec.responses[cid] == seen:
            stale += 1
        else:
            stale, seen = 0, rec.responses[cid]

    return {
        "folder": folder,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "reached_end": rec.has_more.get(cid) is False,
        "items": [normalize(aweme) for aweme in rec.items[cid].values()],
    }


def resolve(folders: list[dict], wanted: list[str]) -> list[dict]:
    """按 collects_id 或名称挑出收藏夹；重名时要求改用 id。"""
    picked = []
    for key in wanted:
        hits = [f for f in folders if key in (f["id"], f["name"])]
        if not hits:
            raise CaptureError(f"没有名为「{key}」的收藏夹")
        if len(hits) > 1:
            options = "，".join(f"{f['id']}（{f['total']} 个）" for f in hits)
            raise CaptureError(f"有多个「{key}」收藏夹，请改用 id：{options}")
        picked.append(hits[0])
    return picked


def list_folders(headed: bool = False) -> list[dict]:
    with browser(headed) as (page, rec):
        return open_favorites(page, rec)


def capture(wanted: list[str] | None, headed: bool = False) -> list[Path]:
    """采集收藏夹（wanted 为 None 表示全部），返回写出的 raw 文件。"""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    outputs = []
    with browser(headed) as (page, rec):
        folders = open_favorites(page, rec)
        for folder in folders if wanted is None else resolve(folders, wanted):
            print(f"采集「{folder['name']}」({folder['id']})…", flush=True)
            result = collect_folder(page, rec, folder)
            warn = "" if result["reached_end"] else "  ⚠ 未确认到达末尾，可能不完整"
            print(f"  {len(result['items'])} 条有效作品（收藏夹显示 {folder['total']}）{warn}")
            path = RAW_DIR / f"{folder['id']}.json"
            write_json(path, result)
            outputs.append(path)
    return outputs


# --------------------------------------------------------------------------- #
# 把接口原始条目压缩成处理阶段需要的字段（含全部媒体直链）
# --------------------------------------------------------------------------- #

def _image_urls(image: dict) -> list[str]:
    urls = image.get("watermark_free_download_url_list") or image.get("url_list") or []
    return sorted(urls, key=lambda u: ".jpeg" not in u)  # jpeg 优先，webp 兜底


def _article_markdown(aweme: dict) -> str:
    content = (aweme.get("article_info") or {}).get("article_content")
    if not content:
        return ""
    try:
        return json.loads(content).get("markdown") or ""
    except (TypeError, ValueError, AttributeError):
        return ""


def normalize(aweme: dict) -> dict:
    aweme_id = str(aweme["aweme_id"])
    video = aweme.get("video") or {}
    article = _article_markdown(aweme)
    images = [u for i in aweme.get("images") or [] if isinstance(i, dict) and (u := _image_urls(i))]
    kind = "article" if article else "note" if images else "video"

    # 只要音轨：有独立音频流（视频自身的口播，不是 music 里的 BGM）就只下它；
    # 否则用最低码率档（1080p 常有几十 MB，低档只有几 MB），默认档兜底。
    # URL 含 /dash/ 的视频档位是纯视频流，没有音轨，跳过。
    video_sources: list[list[str]] = []
    if kind == "video":
        for stream in video.get("bit_rate_audio") or []:
            urls = (stream.get("audio_meta") or {}).get("url_list") or {}
            if audio := [u for u in (urls.get("main_url"), urls.get("backup_url")) if u]:
                video_sources.append(audio)
                break
        gears = sorted(video.get("bit_rate") or [], key=lambda g: g.get("bit_rate") or 0)
        addrs = [g.get("play_addr") or {} for g in gears] + [video.get("play_addr") or {}]
        with_audio = [a["url_list"] for a in addrs
                      if a.get("url_list") and not any("/dash/" in u for u in a["url_list"])]
        video_sources += with_audio[:1] + with_audio[1:][-1:]  # 最低档 + 默认档

    return {
        "aweme_id": aweme_id,
        "kind": kind,  # video 视频 / note 图文 / article 文章
        "title": aweme.get("item_title") or "",
        "desc": aweme.get("desc") or "",
        "author": (aweme.get("author") or {}).get("nickname") or "",
        "create_time": aweme.get("create_time"),
        "duration_s": round((aweme.get("duration") or video.get("duration") or 0) / 1000),
        "url": f"https://www.douyin.com/{'note' if kind == 'note' else 'video'}/{aweme_id}",
        "video_sources": video_sources,  # 每个元素是同一档位的多条 CDN 直链
        "images": images,
        "article_md": article,
    }
