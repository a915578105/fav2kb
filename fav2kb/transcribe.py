"""语音转写（可选）：把知识库里视频的 audio.m4a 转成文字，写进对应 README。

纯本地计算，不涉及登录态。基于开源的 sherpa-onnx（k2-fsa）+ SenseVoice：
中文识别优于同体量 Whisper、自带标点，纯 CPU 运行，不依赖 torch。
先用 silero VAD 按语音切段，再逐段识别，输出带时间戳的文本。

模型首次使用时从 sherpa-onnx 官方 release 下载到 ~/.fav2kb/models/（解压后约 230MB）。
"""

from __future__ import annotations

import os
import re
import subprocess
import tarfile
import urllib.request
from pathlib import Path

import numpy as np
import sherpa_onnx
from imageio_ffmpeg import get_ffmpeg_exe

from .config import MODEL_DIR, write_atomic
from .export import load_manifest, save_manifest

RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
SENSE_VOICE = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"  # 官方 SenseVoiceSmall；2025-09-09 版是粤语微调
VAD_MODEL = "silero_vad.onnx"
SAMPLE_RATE = 16000
SECTION = "## 转写"
MIN_CHARS = 60  # 有效字数（中文字 + 外文词）低于此值，多为背景音乐/画面演示为主的视频，口播信息很少
LOW_NOTE = "> ⚠ 转写内容很少：视频可能以背景音乐或画面演示为主，口播信息有限。"
LYRICS_TAG = "〔疑似歌词/杂音〕"
LYRICS_NOTE = f"> 标 {LYRICS_TAG} 的行可能是背景音乐歌词或被误识别的杂音（如喘气声），是否排除按用户偏好决定。"

MIN_DENSITY = 100  # 每分钟有效字数低于此值：口播稀疏，信息主要在画面（实测有口播讲解的 160~300）
VISUAL_REFS = re.compile(
    r"像这样|看这里|看这边|如图|看一下这|你看这|大家看|这个位置|这个角度|这个姿势|这个动作|"
    r"跟着我做|跟我一起做|点开|点击|选中|拖到|勾选|按住"
)
VISUAL_NOTE = {
    "high": "> 👁 本条信息主要在画面里（口播很少），只看文字会丢信息，建议回看原视频。",
    "medium": "> 👁 讲解多次指向画面（如“这个位置”“点击”），部分信息只能看视频。",
}

Line = tuple[float, str, str]  # (开始秒数, 文本, 语种)


def _units(text: str) -> int:
    """有效字数：中文按字、外文按词计（外文歌词按字母数会虚高）。"""
    return len(re.findall(r"[\u4e00-\u9fff]", text)) + len(re.findall(r"[A-Za-z]+", text))


def quality(lines: list[Line]) -> str:
    """none：没识别到人声；low：有效字数太少（实测多为歌词或零碎杂音）；ok：正常。"""
    units = _units("".join(t for _, t, _ in lines))
    return "none" if not lines else "low" if units < MIN_CHARS else "ok"


def visual_dependency(lines: list[Line], duration_s: float) -> str:
    """high：信息主要在画面；medium：部分信息要看画面；low：文字基本够用。agent 读转写时再确认。"""
    text = "".join(t for _, t, _ in lines)
    if quality(lines) != "ok" or _units(text) / max(duration_s, 1) * 60 < MIN_DENSITY:
        return "high"
    return "medium" if len(VISUAL_REFS.findall(text)) >= 3 else "low"


def lyric_flags(lines: list[Line]) -> list[bool]:
    """粗筛疑似歌词的行：日韩语段、中文视频里的整段英文、重复出现的句子（副歌）。

    中文歌词靠规则分不出来，由 agent 读转写时判断。
    """
    langs = [lang for _, _, lang in lines]
    main = max(set(langs), key=langs.count) if langs else ""
    keys = [re.sub(r"\W", "", text) for _, text, _ in lines]
    return [
        lang in ("ko", "ja")
        or (lang == "en" and main in ("zh", "yue"))
        or (len(key) >= 4 and keys.count(key) >= 2)
        for (_, _, lang), key in zip(lines, keys)
    ]


def _fetch(url: str, dest: Path) -> None:
    part = dest.with_name(dest.name + ".part")
    urllib.request.urlretrieve(url, part)
    part.replace(dest)


def ensure_models() -> tuple[Path, Path]:
    """返回 (SenseVoice 模型目录, VAD 模型文件)，缺失则下载。"""
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    sense_voice = MODEL_DIR / SENSE_VOICE
    if not (sense_voice / "tokens.txt").exists():
        archive = MODEL_DIR / f"{SENSE_VOICE}.tar.bz2"
        print(f"首次使用，下载 SenseVoice 模型（约 160MB）到 {MODEL_DIR} …", flush=True)
        _fetch(RELEASE + archive.name, archive)
        with tarfile.open(archive) as tar:
            tar.extractall(MODEL_DIR, filter="data")
        archive.unlink()
    vad = MODEL_DIR / VAD_MODEL
    if not vad.exists():
        _fetch(RELEASE + VAD_MODEL, vad)
    return sense_voice, vad


def decode(audio: Path) -> np.ndarray:
    """用 ffmpeg 解成 16kHz 单声道 float32。"""
    cmd = [get_ffmpeg_exe(), "-loglevel", "error", "-i", str(audio),
           "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"]
    return np.frombuffer(subprocess.run(cmd, capture_output=True, check=True).stdout, dtype=np.float32)


class Transcriber:
    def __init__(self) -> None:
        sense_voice, vad = ensure_models()
        model = next(sense_voice.glob("model*.onnx"))
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model),
            tokens=str(sense_voice / "tokens.txt"),
            num_threads=min(8, os.cpu_count() or 4),
            use_itn=True,  # 数字、标点规范化
        )
        self.vad_config = sherpa_onnx.VadModelConfig()
        self.vad_config.silero_vad.model = str(vad)
        self.vad_config.silero_vad.max_speech_duration = 20  # SenseVoice 适合 30 秒以内的段
        self.vad_config.sample_rate = SAMPLE_RATE

    def _segments(self, samples: np.ndarray) -> list[tuple[float, np.ndarray]]:
        """VAD 切出语音段，返回 [(开始秒数, 采样), …]。"""
        vad = sherpa_onnx.VoiceActivityDetector(
            self.vad_config, buffer_size_in_seconds=len(samples) / SAMPLE_RATE + 10
        )
        segments = []

        def drain() -> None:
            # front 引用 VAD 内部缓冲，pop 之后失效，必须先拷贝
            while not vad.empty():
                front = vad.front
                segments.append((front.start / SAMPLE_RATE, np.array(front.samples, dtype=np.float32)))
                vad.pop()

        window = self.vad_config.silero_vad.window_size
        for start in range(0, len(samples), window):
            vad.accept_waveform(samples[start:start + window])
            drain()
        vad.flush()
        drain()
        return segments

    def __call__(self, audio: Path) -> tuple[list[Line], float]:
        """返回 ([(开始秒数, 文本, 语种), …], 音频时长秒)，语种如 zh / en / ja。"""
        samples = decode(audio)
        duration = len(samples) / SAMPLE_RATE
        segments = self._segments(samples)
        if not segments:  # 纯音乐等无人声；空列表传给 decode_streams 会崩溃
            return [], duration
        streams = []
        for _, samples in segments:
            stream = self.recognizer.create_stream()
            stream.accept_waveform(SAMPLE_RATE, samples)
            streams.append(stream)
        self.recognizer.decode_streams(streams)
        lines = [
            (start, s.result.text.strip(), s.result.lang.strip("<|>"))
            for (start, _), s in zip(segments, streams)
        ]
        return [line for line in lines if line[1]], duration


def transcribe(out: Path, keep_audio: bool = True) -> None:
    """转写知识库目录里所有尚未转写的视频；keep_audio=False 时转写成功后删除音频省磁盘。"""
    manifest = load_manifest(out)
    pending = [
        e for e in manifest.values()
        if e["type"] == "视频" and e["complete"] and not e.get("transcribed")
    ]
    if not pending:
        print("没有待转写的视频")
        return

    print(f"开始转写 {len(pending)} 条视频（本地运行）", flush=True)
    transcriber = Transcriber()  # 模型加载失败直接报错，不把每条都记成失败
    for n, entry in enumerate(pending, 1):
        item_dir = out / "items" / entry["aweme_id"]
        try:
            lines, duration = transcriber(item_dir / "audio.m4a")
        except subprocess.CalledProcessError:
            entry["error"] = "音频解码失败，无法转写"
            save_manifest(out, manifest)
            print(f"  [{n}/{len(pending)}] {entry['title']}  ⚠ {entry['error']}", flush=True)
            continue
        flags = lyric_flags(lines)
        body = "\n\n".join(
            f"[{int(t) // 60:02d}:{int(t) % 60:02d}] {LYRICS_TAG if lyric else ''}{text}"
            for (t, text, _), lyric in zip(lines, flags)
        )

        readme = item_dir / "README.md"
        content = readme.read_text(encoding="utf-8").split(f"\n{SECTION}\n")[0].rstrip()
        if not keep_audio:
            content = content.replace("[audio.m4a](audio.m4a)", "音频已在转写后删除")
        level, visual = quality(lines), visual_dependency(lines, duration)
        notes = [LOW_NOTE] * (level == "low") + [LYRICS_NOTE] * any(flags)
        notes += [VISUAL_NOTE[visual]] if visual in VISUAL_NOTE else []
        if level == "none":
            body = "（未识别到人声，可能是纯音乐）"
        write_atomic(readme, f"{content}\n\n{SECTION}\n\n" + "\n\n".join([*notes, body]) + "\n")

        entry["transcribed"] = True
        entry["transcript_quality"] = level
        entry["lyric_lines"] = sum(flags)
        entry["visual_dependency"] = visual
        entry["error"] = ""
        save_manifest(out, manifest)
        if not keep_audio:  # 转写和状态都落盘后再删，中途崩溃不会丢音频
            (item_dir / "audio.m4a").unlink(missing_ok=True)
        mark = {"ok": "", "low": "  ⚠ 内容很少", "none": "  （无人声）"}[level]
        mark += f"  疑似歌词/杂音 {sum(flags)} 行" if any(flags) else ""
        mark += {"high": "  👁 信息主要在画面", "medium": "  👁 部分需看画面"}.get(visual, "")
        print(f"  [{n}/{len(pending)}] {entry['title']}  {len(lines)} 段{mark}", flush=True)
