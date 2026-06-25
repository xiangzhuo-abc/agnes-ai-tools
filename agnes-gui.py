#!/usr/bin/env python3
"""
Agnes AI 生成工具 — GUI 桌面版 v3
纯自然语言描述 → 自动识别意图 → 支持参考图 → 调用 Agnes API → 保存到指定文件夹

支持模式：
  🖼️ 文生图 (text2img)      — 纯文字描述 → 生成图片
  🖼️ 图生图 (img2img)       — 参考图 + 文字 → 编辑/风格转换
  🎬 文生视频 (text2video)    — 纯文字描述 → 生成视频
  🎬 图生视频 (img2video)    — 单张参考图 + 文字 → 动态视频
  🔀 关键帧动画 (keyframes)   — 多张参考图 → 平滑过渡动画

用法：python agnes-gui.py
"""

import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox
import threading
import json
import re
import os
import time
import base64
import mimetypes
from io import BytesIO
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

# ============================================================
# 常量
# ============================================================
CONFIG_DIR = Path.home() / ".agnes"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_API_KEY = ""
DEFAULT_OUTPUT_DIR = str(Path.home() / "Desktop" / "Agnes生成")
API_BASE = "https://apihub.agnes-ai.com"
MAX_LOCAL_FILE_MB = 10  # 本地文件最大大小


# ============================================================
# 配置管理
# ============================================================
class ConfigManager:
    """本地配置读写，持久化 API Key 和输出目录"""

    @staticmethod
    def load() -> dict:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        cfg = {
            "api_key": DEFAULT_API_KEY,
            "output_dir": DEFAULT_OUTPUT_DIR,
            "image_size": "1024x1024",
            "video_duration": 5,
            "video_fps": 24,
        }
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                cfg.update(saved)
                # 迁移旧配置：video_frames → video_duration
                if "video_frames" in cfg and "video_duration" not in cfg:
                    old_frames = cfg.pop("video_frames", 121)
                    old_fps = cfg.get("video_fps", 24)
                    cfg["video_duration"] = max(1, round(old_frames / old_fps))
            except (json.JSONDecodeError, IOError):
                pass
        return cfg

    @staticmethod
    def save(config: dict):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)


# ============================================================
# 意图识别器
# ============================================================
class IntentDetector:
    """从自然语言描述中自动识别用户想生成图片还是视频"""

    VIDEO_SIGNALS = [
        r"视频", r"影片", r"录像", r"动画", r"短片", r"短视频",
        r"镜头", r"拍摄", r"录制", r"播放", r"慢动作", r"延时",
        r"运镜", r"转场", r"剪辑", r"帧", r"片段",
        r"行走", r"奔跑", r"飞舞", r"飘动", r"流动", r"旋转",
        r"爆炸", r"燃烧", r"闪烁", r"渐变", r"变形",
        r"\bvideo\b", r"\banimation\b", r"\bclip\b", r"\bfootage\b",
        r"\bcinematic\b", r"\bmotion\b",
        r"\bmp4\b", r"\bmov\b", r"\bavi\b", r"\bwebm\b",
    ]

    IMAGE_SIGNALS = [
        r"图片", r"照片", r"图像", r"海报", r"插画", r"画作",
        r"壁纸", r"头像", r"截图", r"封面", r"绘画", r"素描",
        r"水彩", r"油画", r"漫画", r"像素画", r"logo", r"图标",
        r"一张", r"一幅", r"这只",
        r"\bimage\b", r"\bphoto\b", r"\bpicture\b", r"\bposter\b",
        r"\billustration\b", r"\bpainting\b", r"\bwallpaper\b",
        r"\bpng\b", r"\bjpg\b", r"\bjpeg\b", r"\bwebp\b",
    ]

    @classmethod
    def detect(cls, prompt: str) -> tuple[str, float]:
        """返回 (类型, 置信度, 匹配详情): ('image'|'video', 0.0~1.0, matched_words)"""
        lowered = prompt.lower()

        video_matched = [pat for pat in cls.VIDEO_SIGNALS if re.search(pat, lowered)]
        image_matched = [pat for pat in cls.IMAGE_SIGNALS if re.search(pat, lowered)]

        video_hits = len(video_matched)
        image_hits = len(image_matched)

        if video_hits == 0 and image_hits == 0:
            return ("image", 0.0, [])

        total = video_hits + image_hits
        if video_hits > image_hits:
            conf = video_hits / total
            highlights = [cls._readable(p) for p in video_matched[:5]]
            return ("video", conf, highlights)
        else:
            conf = image_hits / total
            highlights = [cls._readable(p) for p in image_matched[:5]]
            return ("image", conf, highlights)

    @classmethod
    def _readable(cls, pattern: str) -> str:
        """将 regex 模式转为可读的触发词"""
        for ch in r"\\b()[]{}^$|?+*.":
            pattern = pattern.replace(ch, "")
        return pattern.strip()

    @classmethod
    def explain(cls, prompt: str) -> str:
        intent, conf, matched = cls.detect(prompt)
        type_cn = "🎬 视频" if intent == "video" else "🖼️ 图片"
        if conf == 0.0:
            return f"{type_cn}（无明确信号，可尝试添加「视频」「图片」等关键词）"
        detail = ""
        if matched:
            detail = f" | 触发词: {', '.join(matched)}"
        return f"{type_cn}（置信度 {conf:.0%}{detail}）"


# ============================================================
# 时长解析器
# ============================================================
class DurationParser:
    """从自然语言描述中提取视频时长（秒），未找到返回 None"""

    # 中文数字映射
    _CN_NUM = {
        "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
        "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15,
        "十六": 16, "十七": 17, "十八": 18, "十九": 19, "二十": 20,
        "三十": 30, "四十": 40, "五十": 50, "六十": 60,
        "十": 10,  # bare "十" = 10
    }

    # 时长匹配模式（按优先级排列）
    _PATTERNS = [
        # "二十多秒" → 20秒（必须在"十多秒"之前，避免被错误匹配）
        (re.compile(r"(?:二)?十[多来几]秒"), lambda m: 20 if m.group(0).startswith("二") else 10),
        # 中文数字 + 秒：三秒钟 / 15秒 / 十秒
        (re.compile(r"(\d+|[一二三四五六七八九十两]+)\s*秒(?:钟|长)?"),
         lambda m: DurationParser._to_int(m.group(1))),
        # "10s" / "10 s" (纯数字+s结尾)
        (re.compile(r"(\d+)\s*s\b", re.IGNORECASE),
         lambda m: int(m.group(1))),
        # 英文：10 seconds / 10 sec
        (re.compile(r"(\d+)\s*se?c(?:ond)?s?\b", re.IGNORECASE),
         lambda m: int(m.group(1))),
    ]

    @classmethod
    def _to_int(cls, raw: str) -> int | None:
        try:
            return int(raw)
        except ValueError:
            return cls._CN_NUM.get(raw)

    @classmethod
    def parse(cls, text: str) -> int | None:
        """返回时长秒数，未匹配到返回 None"""
        for pattern, resolver in cls._PATTERNS:
            m = pattern.search(text)
            if m:
                result = resolver(m)
                if result is not None:
                    return result
        return None


# ============================================================
# 模式检测器
# ============================================================
class ModeDetector:
    """
    根据意图 + 参考图数量，确定最终模式：
      text2img   — 图片意图 + 无参考图
      img2img    — 图片意图 + 有参考图
      text2video — 视频意图 + 无参考图
      img2video  — 视频意图 + 1张参考图
      keyframes  — 视频意图 + 2+张参考图
    """

    MODE_META = {
        "text2img":   {"icon": "🖼️", "label": "文生图",     "ext": "png", "prefix": "img"},
        "img2img":    {"icon": "🖼️", "label": "图生图",     "ext": "png", "prefix": "img2img"},
        "text2video": {"icon": "🎬", "label": "文生视频",   "ext": "mp4", "prefix": "vid"},
        "img2video":  {"icon": "🎬", "label": "图生视频",   "ext": "mp4", "prefix": "img2vid"},
        "keyframes":  {"icon": "🔀", "label": "关键帧动画", "ext": "mp4", "prefix": "keyframes"},
    }

    @classmethod
    def detect(cls, intent: str, ref_count: int) -> str:
        if intent == "image":
            return "img2img" if ref_count > 0 else "text2img"
        # video
        if ref_count == 0:
            return "text2video"
        elif ref_count == 1:
            return "img2video"
        else:
            return "keyframes"

    @classmethod
    def describe(cls, intent: str, ref_count: int) -> str:
        mode = cls.detect(intent, ref_count)
        meta = cls.MODE_META[mode]
        ref_note = ""
        if ref_count > 0:
            ref_note = f" ({ref_count}张参考图)"
        return f"{meta['icon']} {meta['label']}{ref_note}"


# ============================================================
# 参考图工具
# ============================================================

# 尝试加载 PIL，用于图片缩放压缩
try:
    from PIL import Image as PILImage
    _HAS_PIL = True
except ImportError:
    PILImage = None
    _HAS_PIL = False

# 图片缩放上限（长边最大像素），超过此值自动缩小
MAX_IMAGE_DIMENSION = 1920
# 压缩后 JPEG 质量
JPEG_QUALITY = 85
# 视频时长选项（秒）
DURATION_MIN = 1
DURATION_MAX = 18


class ImageRef:
    """单张参考图的抽象，自动处理缩放和 base64 编码"""

    def __init__(self, source: str, source_type: str = "url"):
        self.source = source
        self.source_type = source_type

    @property
    def display_name(self) -> str:
        if self.source_type == "file":
            return os.path.basename(self.source)
        name = self.source.rstrip("/").split("/")[-1]
        return name[:40] if name else self.source[:40]

    def to_api_value(self, max_dimension: int = MAX_IMAGE_DIMENSION) -> str:
        """转为 API 可用的值：URL 原样返回，本地文件经缩放压缩后编码为 base64 data URI"""
        if self.source_type == "url":
            return self.source

        path = Path(self.source)
        if not path.exists():
            raise FileNotFoundError(f"参考图不存在: {self.source}")

        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_LOCAL_FILE_MB:
            raise ValueError(
                f"参考图过大 ({size_mb:.1f} MB)，上限 {MAX_LOCAL_FILE_MB} MB。请压缩后重试。"
            )

        if _HAS_PIL:
            return self._to_api_value_pil(path, max_dimension)
        else:
            return self._to_api_value_raw(path)

    def _to_api_value_pil(self, path: Path, max_dimension: int) -> str:
        """使用 PIL 加载 → 缩放 → 压缩为 JPEG → base64"""
        img = PILImage.open(path)

        # 等比缩放到目标尺寸以内
        longest = max(img.size)
        if longest > max_dimension:
            scale = max_dimension / longest
            new_w = int(img.size[0] * scale)
            new_h = int(img.size[1] * scale)
            img = img.resize((new_w, new_h), PILImage.LANCZOS)

        # 统一转 RGB
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")

        # JPEG 压缩（视频模式用更低质量）
        quality = JPEG_QUALITY if max_dimension > 1280 else 75
        buffer = BytesIO()
        img.save(buffer, format="JPEG", quality=quality)
        jpeg_data = buffer.getvalue()
        b64 = base64.b64encode(jpeg_data).decode("ascii")

        return f"data:image/jpeg;base64,{b64}"

    def _to_api_value_raw(self, path: Path) -> str:
        """无 PIL 时的兜底：直接 base64 编码原文件"""
        mime, _ = mimetypes.guess_type(str(path))
        if not mime or not mime.startswith("image/"):
            mime = "image/png"

        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")

        return f"data:{mime};base64,{b64}"


# ============================================================
# Agnes API 封装
# ============================================================
class AgnesAPI:
    """Agnes AI 全模态 API 封装：文生图 / 图生图 / 文生视频 / 图生视频 / 关键帧"""

    def __init__(self, api_key: str, log_callback=None):
        self.api_key = api_key
        self.log = log_callback or (lambda msg: None)

    # ---- 底层 HTTP ----
    def _req(self, method: str, path: str, body: dict = None,
             timeout: int = 120, retries: int = 1) -> dict:
        url = f"{API_BASE}{path}"
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            size_mb = len(data) / (1024 * 1024)
            if size_mb > 0.5:
                self.log(f"[HTTP] 请求体大小: {size_mb:.1f} MB，超时={timeout}s")

        last_error = None
        last_err_body = ""  # 保存错误响应体（避免重复读取已消费的流）
        for attempt in range(retries + 1):
            if attempt > 0:
                self.log(f"[HTTP] 第 {attempt + 1}/{retries + 1} 次重试...")
                time.sleep(2)

            req = urllib.request.Request(
                url, data=data,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method=method,
            )

            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                last_error = e
                try:
                    last_err_body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    last_err_body = str(e)
                if e.code and 500 <= e.code < 600:
                    self.log(f"[HTTP] 服务端 {e.code} 错误，{'将重试' if attempt < retries else '重试已用完'}")
                    continue
                else:
                    break
            except urllib.error.URLError as e:
                last_error = e
                last_err_body = str(e.reason)
                self.log(f"[HTTP] 网络错误: {e.reason}，{'将重试' if attempt < retries else '重试已用完'}")
                continue

        # 所有重试耗尽
        if isinstance(last_error, urllib.error.HTTPError):
            err_detail = ""
            if "Internal generation failed" in last_err_body:
                err_detail = (
                    "\n\n💡 建议：\n"
                    "  1. 检查参考图分辨率是否过大（已自动缩放到目标尺寸，如仍失败请手动压缩）\n"
                    "  2. 尝试简化 prompt 描述\n"
                    "  3. 可能是 Agnes 服务端瞬时负载过高，稍后重试"
                )
            raise Exception(
                f"HTTP {last_error.code}: {last_err_body}{err_detail}"
            ) from last_error
        elif isinstance(last_error, urllib.error.URLError):
            raise Exception(
                f"网络错误（已重试 {retries} 次）: {last_err_body}\n"
                f"💡 请检查网络连接或 Agnes API 服务状态"
            ) from last_error
        else:
            raise Exception(f"请求失败（已重试 {retries} 次）")

    # ---- 图片请求体构建 ----
    @staticmethod
    def _build_image_body(prompt: str, size: str, extra: dict = None) -> dict:
        """
        解析 size 字符串，支持两种格式：
          "1024x1024"  → 传统固定像素
          "4K 16:9"    → 品质等级 + 比例
        """
        body = {
            "model": "agnes-image-2.1-flash",
            "prompt": prompt,
            "n": 1,
            "extra_body": {"response_format": "url"},
        }

        parts = size.split()
        if len(parts) == 2 and parts[0].endswith("K") and ":" in parts[1]:
            # 新格式："4K 16:9"
            body["size"] = parts[0]
            body["extra_body"]["ratio"] = parts[1]
        else:
            # 传统格式："1024x1024"
            body["size"] = parts[0]

        if extra:
            body["extra_body"].update(extra)
        return body

    # ---- 文生图 ----
    def generate_image_text2img(self, prompt: str, size: str = "1024x1024") -> str:
        self.log(f"[文生图] 正在生成: {prompt[:60]}... ({size})")
        body = self._build_image_body(prompt, size)
        result = self._req("POST", "/v1/images/generations", body, timeout=180)
        url = result["data"][0]["url"]
        self.log(f"[文生图] 生成完成")
        return url

    # ---- 图生图 ----
    def generate_image_img2img(
        self, prompt: str, ref_images: list[ImageRef], size: str = "1024x1024"
    ) -> str:
        self.log(f"[图生图] 处理 {len(ref_images)} 张参考图... ({size})")
        ref_urls = []
        for r in ref_images:
            val = r.to_api_value()
            if val.startswith("data:"):
                b64_part = val.split(",", 1)[1] if "," in val else val
                kb = len(b64_part) * 3 / 4 / 1024
                self.log(f"  📎 {r.display_name} → base64 ({kb:.0f} KB)")
            ref_urls.append(val)
        self.log(f"[图生图] prompt: {prompt[:50]}...")

        body = self._build_image_body(prompt, size, extra={
            "image": ref_urls,
        })
        body["model"] = "agnes-image-2.0-flash"
        body["tags"] = ["img2img"]
        result = self._req("POST", "/v1/images/generations", body, timeout=300)
        url = result["data"][0]["url"]
        self.log(f"[图生图] 生成完成")
        return url

    # ---- 文生视频 ----
    def generate_video_text2video(
        self, prompt: str, num_frames: int = 121, fps: int = 24,
        width: int = 1152, height: int = 768,
    ) -> str:
        num_frames = self._fix_frames(num_frames)
        self.log(f"[文生视频] 提交: {prompt[:50]}... ({num_frames}帧, {fps}fps)")
        result = self._req("POST", "/v1/videos", {
            "model": "agnes-video-v2.0",
            "prompt": prompt,
            "num_frames": num_frames,
            "frame_rate": fps,
            "width": width,
            "height": height,
        }, timeout=300)
        return self._poll_video(result)

    # ---- 图生视频（单张参考图）----
    def generate_video_img2video(
        self, prompt: str, ref_image: ImageRef,
        num_frames: int = 121, fps: int = 24,
        width: int = 1152, height: int = 768,
    ) -> str:
        num_frames = self._fix_frames(num_frames)
        ref_url = ref_image.to_api_value(max_dimension=max(width, height))
        if ref_url.startswith("data:"):
            b64_part = ref_url.split(",", 1)[1] if "," in ref_url else ref_url
            kb = len(b64_part) * 3 / 4 / 1024
            self.log(f"[图生视频] 📎 {ref_image.display_name} → base64 ({kb:.0f} KB)")
        self.log(f"[图生视频] prompt: {prompt[:40]}...")
        result = self._req("POST", "/v1/videos", {
            "model": "agnes-video-v2.0",
            "prompt": prompt,
            "image": ref_url,
            "num_frames": num_frames,
            "frame_rate": fps,
            "width": width,
            "height": height,
        }, timeout=300)
        return self._poll_video(result)

    # ---- 关键帧动画（多张参考图）----
    def generate_video_keyframes(
        self, prompt: str, ref_images: list[ImageRef],
        num_frames: int = 161, fps: int = 24,
        width: int = 1152, height: int = 768,
    ) -> str:
        num_frames = self._fix_frames(num_frames)
        self.log(f"[关键帧] 处理 {len(ref_images)} 张参考图...")
        ref_urls = []
        for r in ref_images:
            val = r.to_api_value(max_dimension=max(width, height))
            if val.startswith("data:"):
                b64_part = val.split(",", 1)[1] if "," in val else val
                kb = len(b64_part) * 3 / 4 / 1024
                self.log(f"  📎 {r.display_name} → base64 ({kb:.0f} KB)")
            ref_urls.append(val)
        self.log(f"[关键帧] prompt: {prompt[:40]}...")
        result = self._req("POST", "/v1/videos", {
            "model": "agnes-video-v2.0",
            "prompt": prompt,
            "num_frames": num_frames,
            "frame_rate": fps,
            "width": width,
            "height": height,
            "extra_body": {
                "image": ref_urls,
                "mode": "keyframes",
            },
        }, timeout=300)
        return self._poll_video(result)

    # ---- 视频轮询 ----
    def _poll_video(self, submit_result: dict) -> str:
        video_id = submit_result.get("video_id") or submit_result.get("id") or submit_result.get("task_id")
        if not video_id:
            raise Exception(f"未获取到视频任务 ID，响应: {submit_result}")

        self.log(f"[视频] 已提交 | ID: {video_id} | 预计: {submit_result.get('seconds', '?')}秒")

        poll_path = f"/agnesapi?video_id={video_id}&model_name=agnes-video-v2.0"
        max_wait = 600
        waited = 0
        while waited < max_wait:
            time.sleep(5)
            waited += 5
            poll_result = self._req("GET", poll_path, timeout=60)
            status = poll_result.get("status", "unknown")
            progress = poll_result.get("progress", 0)
            self.log(f"[视频] 等待... {waited}s | {status} ({progress}%)")

            if status == "completed":
                url = poll_result.get("video_url") or poll_result.get("url") or poll_result.get("remixed_from_video_id")
                if not url:
                    raise Exception(f"视频完成但未获取到 URL: {poll_result}")
                self.log(f"[视频] 生成完成！")
                return url
            elif status == "failed":
                raise Exception(f"视频生成失败: {poll_result.get('error', '未知错误')}")

        raise TimeoutError(f"视频生成超时（等待了 {max_wait} 秒）")

    @staticmethod
    def _fix_frames(n: int) -> int:
        if (n - 1) % 8 != 0:
            return max(81, ((n - 1) // 8) * 8 + 1)
        return n

    # ---- 下载 ----
    def download(self, url: str, output_path: str) -> str:
        self.log(f"[下载] {url[:80]}...")
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=300) as resp:
            content = resp.read()

        with open(output_path, "wb") as f:
            f.write(content)

        size_kb = len(content) / 1024
        self.log(f"[下载] 完成 → {output_path} ({size_kb:.1f} KB)")
        return output_path


# ============================================================
# 历史记录管理
# ============================================================
HISTORY_FILE = CONFIG_DIR / "history.json"
MAX_HISTORY = 100


class HistoryManager:
    """生成历史记录读写"""

    @staticmethod
    def load() -> list[dict]:
        if HISTORY_FILE.exists():
            try:
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return []

    @staticmethod
    def save(history: list[dict]):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history[-MAX_HISTORY:], f, ensure_ascii=False, indent=2)

    @staticmethod
    def add(entry: dict):
        history = HistoryManager.load()
        history.append(entry)
        if len(history) > MAX_HISTORY:
            history = history[-MAX_HISTORY:]
        HistoryManager.save(history)


# ============================================================
# 后台生成线程
# ============================================================
class GenerationWorker(threading.Thread):
    """在后台线程中执行生成任务，不阻塞 GUI"""

    @staticmethod
    def _duration_to_frames(duration_sec: int, fps: int) -> int:
        """秒数 → 帧数（确保 8n+1 且 ≤441）"""
        target = duration_sec * fps
        # round 比 floor 更接近用户期望的时长
        n = max(10, round((target - 1) / 8))
        return int(min(n * 8 + 1, 441))

    def __init__(self, api: AgnesAPI, prompt: str, output_dir: str, config: dict,
                 ref_images: list[ImageRef], manual_intent: str | None,
                 manual_duration: int | None,
                 on_log, on_done, on_error):
        super().__init__(daemon=True)
        self.api = api
        self.prompt = prompt
        self.output_dir = output_dir
        self.config = config
        self.ref_images = ref_images
        self.manual_intent = manual_intent
        self.manual_duration = manual_duration  # None=自动, int=指定秒数
        self.on_log = on_log
        self.on_done = on_done
        self.on_error = on_error

    def run(self):
        try:
            # 1. 识别意图（手动优先）
            if self.manual_intent:
                intent = self.manual_intent
                tag = "🎬" if intent == "video" else "🖼️"
                self.on_log(f"🧠 意图: {tag} {'视频' if intent == 'video' else '图片'}（手动指定）")
            else:
                intent, conf, _ = IntentDetector.detect(self.prompt)
                self.on_log(f"🧠 意图: {IntentDetector.explain(self.prompt)}（自动识别）")

            # 2. 判定模式
            mode = ModeDetector.detect(intent, len(self.ref_images))
            mode_meta = ModeDetector.MODE_META[mode]
            source = "手动" if self.manual_intent else "自动"
            self.on_log(f"🔀 模式: {mode_meta['icon']} {mode_meta['label']}（{source}）")

            # 2.5 解析时长（视频模式）
            video_fps = int(self.config.get("video_fps", 24))

            if self.manual_duration:
                num_frames = self._duration_to_frames(self.manual_duration, video_fps)
                self.on_log(
                    f"⏱️ 手动指定时长: {self.manual_duration}秒 → {num_frames}帧 "
                    f"({num_frames / video_fps:.1f}秒 @ {video_fps}fps)"
                )
            elif intent == "video":
                detected = DurationParser.parse(self.prompt)
                if detected:
                    num_frames = self._duration_to_frames(detected, video_fps)
                    self.on_log(
                        f"⏱️ 从提示词提取时长: {detected}秒 → {num_frames}帧 "
                        f"({num_frames / video_fps:.1f}秒 @ {video_fps}fps)"
                    )
                else:
                    default_dur = int(self.config.get("video_duration", 5))
                    num_frames = self._duration_to_frames(default_dur, video_fps)
                    self.on_log(
                        f"⏱️ 使用设置默认时长: {default_dur}秒 → {num_frames}帧 "
                        f"({num_frames / video_fps:.1f}秒 @ {video_fps}fps)"
                    )
            else:
                num_frames = None  # 图片模式不需要

            # 3. 按模式路由
            if mode == "text2img":
                url = self.api.generate_image_text2img(
                    self.prompt,
                    size=self.config.get("image_size", "1024x1024"),
                )
            elif mode == "img2img":
                url = self.api.generate_image_img2img(
                    self.prompt,
                    self.ref_images,
                    size=self.config.get("image_size", "1024x1024"),
                )
            elif mode == "text2video":
                url = self.api.generate_video_text2video(
                    self.prompt,
                    num_frames=num_frames,
                    fps=video_fps,
                )
            elif mode == "img2video":
                url = self.api.generate_video_img2video(
                    self.prompt,
                    self.ref_images[0],
                    num_frames=num_frames,
                    fps=video_fps,
                )
            elif mode == "keyframes":
                url = self.api.generate_video_keyframes(
                    self.prompt,
                    self.ref_images,
                    num_frames=num_frames,
                    fps=video_fps,
                )
            else:
                raise Exception(f"未知模式: {mode}")

            # 4. 下载
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            filename = f"{mode_meta['prefix']}_{ts}.{mode_meta['ext']}"
            out_path = os.path.join(self.output_dir, filename)
            saved = self.api.download(url, out_path)

            # 5. 完成
            self.on_done(saved, mode)

        except Exception as e:
            self.on_error(str(e))


# ============================================================
# 设置对话框
# ============================================================
class SettingsDialog(tk.Toplevel):
    """API Key 和生成参数设置"""

    def __init__(self, parent, config: dict, on_save):
        super().__init__(parent)
        self.config = config
        self.on_save = on_save
        self.title("⚙️ 设置")
        self.geometry("520x420")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self._build()

    def _build(self):
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)

        row = 0

        # API Key
        ttk.Label(frame, text="🔑 API Key", font=("", 10, "bold")).grid(
            row=row, column=0, sticky=tk.W, pady=(0, 2))
        row += 1
        self.key_var = tk.StringVar(value=self.config.get("api_key", ""))
        ttk.Entry(frame, textvariable=self.key_var, show="•", width=58).grid(
            row=row, column=0, columnspan=2, sticky=tk.EW, pady=(0, 4))
        row += 1
        ttk.Button(frame, text="显示/隐藏", command=self._toggle_key).grid(
            row=row, column=0, sticky=tk.W, pady=(0, 12))
        row += 1

        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(
            row=row, column=0, columnspan=2, sticky=tk.EW, pady=8)
        row += 1

        # 图片尺寸
        ttk.Label(frame, text="🖼️ 图片默认尺寸").grid(row=row, column=0, sticky=tk.W)
        row += 1
        size_frame = ttk.Frame(frame)
        size_frame.grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=(0, 8))

        # Agnes 支持的全部尺寸（传统像素 + 品质等级×比例）
        IMAGE_SIZES = [
            # === 品质等级 × 比例 ===
            "4K 1:1",   "4K 3:4",   "4K 4:3",   "4K 16:9",
            "4K 9:16",  "4K 3:2",   "4K 2:3",   "4K 21:9",
            "3K 1:1",   "3K 3:4",   "3K 4:3",   "3K 16:9",
            "3K 9:16",  "3K 3:2",   "3K 2:3",   "3K 21:9",
            "2K 1:1",   "2K 3:4",   "2K 4:3",   "2K 16:9",
            "2K 9:16",  "2K 3:2",   "2K 2:3",   "2K 21:9",
            "1K 1:1",   "1K 3:4",   "1K 4:3",   "1K 16:9",
            "1K 9:16",  "1K 3:2",   "1K 2:3",   "1K 21:9",
            # === 传统固定像素 ===
            "1792x1024", "1024x1792", "1024x1024", "1024x768",
            "768x1024",  "512x512",
        ]
        self.size_var = tk.StringVar(value=self.config.get("image_size", "1024x1024"))
        self.size_combo = ttk.Combobox(size_frame, values=IMAGE_SIZES,
                                       width=16, state="readonly")
        self.size_combo.pack(side=tk.LEFT)
        self.size_combo.set(self.size_var.get())
        self.size_combo.bind("<<ComboboxSelected>>",
                             lambda e: self.size_var.set(self.size_combo.get()))
        ttk.Label(size_frame, text="（品质等级 1K-4K + 比例，或固定像素）",
                  foreground="gray").pack(side=tk.LEFT, padx=(8, 0))
        row += 1

        # 视频时长
        ttk.Label(frame, text="🎬 视频默认时长").grid(row=row, column=0, sticky=tk.W)
        row += 1
        dur_frame = ttk.Frame(frame)
        dur_frame.grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=(0, 8))
        self.duration_var = tk.StringVar(value=str(self.config.get("video_duration", 5)))
        self.duration_spin = ttk.Spinbox(
            dur_frame, from_=DURATION_MIN, to=DURATION_MAX,
            textvariable=self.duration_var, width=5)
        self.duration_spin.pack(side=tk.LEFT)
        ttk.Label(dur_frame, text=f"秒（{DURATION_MIN}-{DURATION_MAX}，帧数自动计算为 8n+1）").pack(
            side=tk.LEFT, padx=(6, 0))
        row += 1

        # 视频帧率
        ttk.Label(frame, text="🎬 视频帧率 (fps)").grid(row=row, column=0, sticky=tk.W)
        row += 1
        self.fps_var = tk.StringVar(value=str(self.config.get("video_fps", 24)))
        fps_frame = ttk.Frame(frame)
        fps_frame.grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=(0, 12))
        fps_values = ["16", "20", "24", "30"]
        self.fps_combo = ttk.Combobox(fps_frame, values=fps_values,
                                      width=5, state="readonly")
        self.fps_combo.pack(side=tk.LEFT)
        self.fps_combo.set(self.fps_var.get())
        self.fps_combo.bind("<<ComboboxSelected>>",
                            lambda e: self.fps_var.set(self.fps_combo.get()))
        ttk.Label(fps_frame, text="fps").pack(side=tk.LEFT, padx=(6, 0))
        row += 1

        # 按钮
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=row, column=0, columnspan=2, sticky=tk.E, pady=(8, 0))
        ttk.Button(btn_frame, text="取消", command=self.destroy).pack(
            side=tk.RIGHT, padx=(8, 0))
        ttk.Button(btn_frame, text="💾 保存", command=self._save).pack(side=tk.RIGHT)

    def _toggle_key(self):
        for child in self.winfo_children():
            for c in child.winfo_children():
                if isinstance(c, ttk.Entry):
                    c.config(show="" if c.cget("show") == "•" else "•")

    def _save(self):
        dur = int(self.duration_var.get())
        fps = int(self.fps_var.get())
        self.config["api_key"] = self.key_var.get()
        self.config["image_size"] = self.size_var.get()
        self.config["video_duration"] = dur
        self.config["video_fps"] = fps
        self.config.pop("video_frames", None)
        ConfigManager.save(self.config)
        print(f"[Settings] 已保存: duration={dur}s, fps={fps} → {GenerationWorker._duration_to_frames(dur, fps)}帧")
        self.on_save()
        self.destroy()


# ============================================================
# 主窗口
# ============================================================
class AgnesApp:
    """Agnes AI 生成工具 — 主 GUI 程序 v3"""

    def __init__(self):
        self.config = ConfigManager.load()
        self.worker = None
        self.ref_images: list[ImageRef] = []

        # 根窗口
        self.root = tk.Tk()
        self.root.title("Agnes AI 生成工具 v3")
        self.root.geometry("760x720")
        self.root.minsize(640, 560)

        self.style = ttk.Style()
        self.style.theme_use("clam")

        self._build_ui()

        # 首次启动：如果没有有效 API Key，弹设置
        api_key = self.config.get("api_key", "")
        if not api_key or api_key.startswith("sk-你的") or api_key.startswith("sk-占位"):
            self.root.after(500, self._first_run_setup)

    # ================================================================
    # UI 构建
    # ================================================================
    def _build_ui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill=tk.BOTH, expand=True)

        # ---- 标题 ----
        ttk.Label(main, text="✨ Agnes AI 生成工具 v3",
                  font=("Microsoft YaHei UI", 16, "bold")).pack(pady=(0, 2))
        ttk.Label(main, text="自然语言描述 + 可选参考图 → 智能识别 → 文生图/图生图/文生视频/图生视频/关键帧",
                  foreground="gray").pack(pady=(0, 10))

        # ---- 输入区域 ----
        prompt_label_frame = ttk.Frame(main)
        prompt_label_frame.pack(fill=tk.X, pady=(0, 0))
        ttk.Label(prompt_label_frame, text="📝 描述你想要生成的内容：", font=("", 10)).pack(side=tk.LEFT)
        ttk.Button(prompt_label_frame, text="清空", command=self._clear_prompt,
                   width=4).pack(side=tk.RIGHT)

        input_frame = ttk.Frame(main)
        input_frame.pack(fill=tk.X, pady=(2, 6))

        self.prompt_text = tk.Text(input_frame, height=4,
                                   font=("Microsoft YaHei UI", 11),
                                   wrap=tk.WORD, relief=tk.SOLID, borderwidth=1)
        self.prompt_text.pack(fill=tk.X)
        self.prompt_text.focus_set()

        self._placeholder = (
            "例如：一只橘猫坐在赛博朋克城市的霓虹灯下，画面有雨天倒影\n"
            "例如：一段海浪拍打礁石的慢动作视频，夕阳逆光\n"
            "💡 添加参考图可实现：图生图（风格转换）、图生视频（让静态图动起来）、关键帧动画（多图过渡）"
        )
        self.prompt_text.insert("1.0", self._placeholder)
        self.prompt_text.config(foreground="gray")
        self.prompt_text.bind("<FocusIn>", self._on_focus_in)
        self.prompt_text.bind("<FocusOut>", self._on_focus_out)
        self.prompt_text.bind("<KeyRelease>", self._on_input_change)
        # 显式绑定粘贴快捷键（Windows 上 Text 组件有时不响应默认 Ctrl+V）
        self.prompt_text.bind("<Control-v>", self._on_paste)
        self.prompt_text.bind("<Control-V>", self._on_paste)
        self.prompt_text.bind("<Shift-Insert>", self._on_paste)
        # 右键粘贴菜单
        self.prompt_text.bind("<Button-3>", self._on_right_click)

        # ---- 意图切换 ----
        intent_toggle_frame = ttk.Frame(main)
        intent_toggle_frame.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(intent_toggle_frame, text="🎯 生成类型：").pack(side=tk.LEFT)

        self.intent_var = tk.StringVar(value="auto")
        ttk.Radiobutton(intent_toggle_frame, text="🤖 自动识别",
                        variable=self.intent_var, value="auto",
                        command=self._update_mode_label).pack(side=tk.LEFT, padx=(4, 12))
        ttk.Radiobutton(intent_toggle_frame, text="🖼️ 图片",
                        variable=self.intent_var, value="image",
                        command=self._update_mode_label).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Radiobutton(intent_toggle_frame, text="🎬 视频",
                        variable=self.intent_var, value="video",
                        command=self._update_mode_label).pack(side=tk.LEFT)

        # ---- 时长选择（视频模式）----
        duration_frame = ttk.Frame(main)
        duration_frame.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(duration_frame, text="⏱️ 视频时长：").pack(side=tk.LEFT)

        self.duration_auto_var = tk.BooleanVar(value=True)
        self.duration_auto_cb = ttk.Checkbutton(
            duration_frame, text="自动检测（从提示词提取）",
            variable=self.duration_auto_var,
            command=self._on_duration_auto_toggle)
        self.duration_auto_cb.pack(side=tk.LEFT, padx=(4, 8))

        ttk.Label(duration_frame, text="手动指定：").pack(side=tk.LEFT)
        self.duration_manual_var = tk.StringVar(value="5")
        self.duration_spin = ttk.Spinbox(
            duration_frame, from_=DURATION_MIN, to=DURATION_MAX,
            textvariable=self.duration_manual_var, width=4, state=tk.DISABLED)
        self.duration_spin.pack(side=tk.LEFT, padx=(2, 2))
        self.duration_spin.bind("<FocusOut>", lambda e: self._update_mode_label())
        self.duration_spin.bind("<Return>", lambda e: self._update_mode_label())
        self.duration_spin.bind("<<Increment>>", lambda e: self.root.after(10, self._update_mode_label))
        ttk.Label(duration_frame, text="秒").pack(side=tk.LEFT)

        ttk.Label(duration_frame, text="（取消勾选可手动指定 1-18 秒任意值）",
                  foreground="gray").pack(side=tk.LEFT, padx=(8, 0))

        # ---- 参考图区域 ----
        ttk.Label(main, text="📎 参考图（可选·支持图生图/图生视频/关键帧）：",
                  font=("", 10)).pack(anchor=tk.W)

        ref_input_frame = ttk.Frame(main)
        ref_input_frame.pack(fill=tk.X, pady=(4, 2))

        self.ref_entry = ttk.Entry(ref_input_frame, width=45)
        self.ref_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        self.ref_entry.insert(0, "输入图片 URL 或选择本地文件...")
        self.ref_entry.config(foreground="gray")
        self.ref_entry.bind("<FocusIn>", self._on_ref_focus_in)
        self.ref_entry.bind("<FocusOut>", self._on_ref_focus_out)
        self.ref_entry.bind("<Return>", lambda e: self._add_ref())
        self.ref_entry.bind("<Control-v>", self._on_ref_paste)
        self.ref_entry.bind("<Control-V>", self._on_ref_paste)
        self.ref_entry.bind("<Button-3>",
            lambda e: self._entry_right_click(e, self.ref_entry))

        ttk.Button(ref_input_frame, text="选择文件...",
                   command=self._pick_ref_file).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(ref_input_frame, text="➕ 添加",
                   command=self._add_ref).pack(side=tk.LEFT)
        ttk.Button(ref_input_frame, text="清空全部",
                   command=self._clear_refs).pack(side=tk.LEFT, padx=(4, 0))

        # 参考图列表
        ref_list_frame = ttk.Frame(main)
        ref_list_frame.pack(fill=tk.X, pady=(0, 8))

        # 用 Listbox + 滚动条
        list_container = ttk.Frame(ref_list_frame)
        list_container.pack(fill=tk.X)

        self.ref_listbox = tk.Listbox(list_container, height=3,
                                      font=("Consolas", 9),
                                      relief=tk.SOLID, borderwidth=1)
        self.ref_listbox.pack(side=tk.LEFT, fill=tk.X, expand=True)

        ref_scrollbar = ttk.Scrollbar(list_container, orient=tk.VERTICAL,
                                      command=self.ref_listbox.yview)
        ref_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.ref_listbox.config(yscrollcommand=ref_scrollbar.set)

        # 删除选中
        del_btn = ttk.Button(ref_list_frame, text="🗑 删除选中",
                             command=self._remove_selected_ref)
        del_btn.pack(anchor=tk.E, pady=(2, 0))

        # ---- 输出目录 ----
        path_frame = ttk.Frame(main)
        path_frame.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(path_frame, text="📁 输出目录：").pack(side=tk.LEFT)
        self.output_var = tk.StringVar(value=self.config.get("output_dir", DEFAULT_OUTPUT_DIR))
        ttk.Entry(path_frame, textvariable=self.output_var, width=45).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4))
        ttk.Button(path_frame, text="浏览...", command=self._browse_output).pack(side=tk.LEFT)
        ttk.Button(path_frame, text="打开文件夹", command=self._open_output).pack(
            side=tk.LEFT, padx=(4, 0))

        # ---- 按钮栏 ----
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill=tk.X, pady=(0, 6))

        self.generate_btn = ttk.Button(btn_frame, text="🚀 生成",
                                       command=self._on_generate, width=12)
        self.generate_btn.pack(side=tk.LEFT)

        self.stop_btn = ttk.Button(btn_frame, text="⏹ 停止", command=self._on_stop,
                                   state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=(8, 0))

        # 模式和意图预览
        self.mode_label = ttk.Label(btn_frame, text="", foreground="#007acc",
                                    font=("", 10, "bold"))
        self.mode_label.pack(side=tk.LEFT, padx=(20, 0))

        ttk.Button(btn_frame, text="⚙️ 设置", command=self._open_settings).pack(
            side=tk.RIGHT, padx=(4, 0))
        ttk.Button(btn_frame, text="🗑 清空日志", command=self._clear_log).pack(side=tk.RIGHT)

        # ---- 进度条 ----
        self.progress = ttk.Progressbar(main, mode="indeterminate")
        self.progress.pack(fill=tk.X, pady=(0, 4))

        # ---- 日志 ----
        # 使用 PanedWindow 分割日志区和历史记录
        log_paned = ttk.PanedWindow(main, orient=tk.VERTICAL)
        log_paned.pack(fill=tk.BOTH, expand=True)

        # 日志上半部分
        log_top = ttk.Frame(log_paned)
        ttk.Label(log_top, text="📋 运行日志：", font=("", 10)).pack(anchor=tk.W)
        self.log_area = scrolledtext.ScrolledText(
            log_top, font=("Consolas", 9),
            wrap=tk.WORD, relief=tk.SOLID, borderwidth=1, state=tk.DISABLED)
        self.log_area.pack(fill=tk.BOTH, expand=True)
        log_paned.add(log_top, weight=2)

        # 历史记录下半部分
        hist_bottom = ttk.Frame(log_paned)
        hist_header = ttk.Frame(hist_bottom)
        hist_header.pack(fill=tk.X)
        ttk.Label(hist_header, text="📜 生成历史：", font=("", 10)).pack(side=tk.LEFT)
        ttk.Button(hist_header, text="清空", command=self._clear_history,
                   width=4).pack(side=tk.RIGHT, padx=(4, 0))

        self.history_listbox = tk.Listbox(hist_bottom, height=4,
                                          font=("Consolas", 9),
                                          relief=tk.SOLID, borderwidth=1)
        self.history_listbox.pack(fill=tk.BOTH, expand=True, pady=(2, 0))
        self.history_listbox.bind("<Double-Button-1>", self._on_history_double_click)
        hist_bottom.pack_propagate(False)
        log_paned.add(hist_bottom, weight=1)

        # 加载已有历史
        self._update_history_panel()

        # ---- 状态栏 ----
        self.status_var = tk.StringVar(value="✅ 就绪 — 输入描述后点击生成")
        ttk.Label(main, textvariable=self.status_var, relief=tk.SUNKEN,
                  anchor=tk.W, padding=(6, 2)).pack(fill=tk.X, pady=(6, 0))

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ================================================================
    # 占位文字
    # ================================================================
    def _on_focus_in(self, event):
        if self.prompt_text.get("1.0", "end-1c") == self._placeholder:
            self.prompt_text.delete("1.0", tk.END)
            self.prompt_text.config(foreground="black")

    def _on_focus_out(self, event):
        if not self.prompt_text.get("1.0", "end-1c").strip():
            self.prompt_text.insert("1.0", self._placeholder)
            self.prompt_text.config(foreground="gray")

    def _get_prompt(self) -> str:
        text = self.prompt_text.get("1.0", "end-1c").strip()
        return "" if text == self._placeholder else text

    def _on_ref_focus_in(self, event):
        if self.ref_entry.get() == "输入图片 URL 或选择本地文件...":
            self.ref_entry.delete(0, tk.END)
            self.ref_entry.config(foreground="black")

    def _on_ref_focus_out(self, event):
        if not self.ref_entry.get().strip():
            self.ref_entry.insert(0, "输入图片 URL 或选择本地文件...")
            self.ref_entry.config(foreground="gray")

    def _get_ref_text(self) -> str:
        text = self.ref_entry.get().strip()
        return "" if text == "输入图片 URL 或选择本地文件..." else text

    def _on_ref_paste(self, event=None):
        """参考图输入框 Ctrl+V 粘贴"""
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return "break"
        if self.ref_entry.get() == "输入图片 URL 或选择本地文件...":
            self.ref_entry.delete(0, tk.END)
            self.ref_entry.config(foreground="black")
        self.ref_entry.insert(tk.INSERT, text)
        return "break"

    @staticmethod
    def _entry_right_click(event, entry: ttk.Entry):
        """Entry 右键菜单"""
        menu = tk.Menu(entry, tearoff=0)
        def _paste():
            try:
                text = entry.clipboard_get()
            except tk.TclError:
                return
            # 清空占位文字
            if entry.get().startswith("输入图片"):
                entry.delete(0, tk.END)
                entry.config(foreground="black")
            entry.insert(tk.INSERT, text)
        menu.add_command(label="粘贴", command=_paste)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ================================================================
    # 参考图管理
    # ================================================================
    def _pick_ref_file(self):
        files = filedialog.askopenfilenames(
            title="选择参考图（可多选）",
            filetypes=[
                ("图片文件", "*.png *.jpg *.jpeg *.webp *.bmp *.gif"),
                ("所有文件", "*.*"),
            ],
        )
        for f in files:
            source_type = "url" if f.startswith(("http://", "https://")) else "file"
            self.ref_images.append(ImageRef(f, source_type))
        self._refresh_ref_list()
        self._log(f"📎 已添加 {len(files)} 张参考图")

    def _add_ref(self):
        text = self._get_ref_text()
        if not text:
            return

        # 支持分号或逗号分隔的多条
        parts = re.split(r"[;,；，]", text)
        added = 0
        for part in parts:
            part = part.strip()
            if not part:
                continue
            source_type = "url" if part.startswith(("http://", "https://")) else "file"
            # 如果是本地文件，验证存在
            if source_type == "file" and not os.path.isfile(part):
                self._log(f"⚠️ 文件不存在，跳过: {part}")
                continue
            self.ref_images.append(ImageRef(part, source_type))
            added += 1

        if added > 0:
            self._refresh_ref_list()
            self.ref_entry.delete(0, tk.END)
            self._update_mode_label()
            self._log(f"📎 已添加 {added} 张参考图")

    def _remove_selected_ref(self):
        sel = self.ref_listbox.curselection()
        if not sel:
            return
        # 从后往前删，避免索引偏移
        for i in sorted(sel, reverse=True):
            del self.ref_images[i]
        self._refresh_ref_list()
        self._update_mode_label()
        self._log(f"📎 已删除选中参考图，剩余 {len(self.ref_images)} 张")

    def _clear_refs(self):
        if not self.ref_images:
            return
        self.ref_images.clear()
        self._refresh_ref_list()
        self._update_mode_label()
        self._log("📎 已清空全部参考图")

    def _refresh_ref_list(self):
        self.ref_listbox.delete(0, tk.END)
        for r in self.ref_images:
            tag = "🌐" if r.source_type == "url" else "📁"
            self.ref_listbox.insert(tk.END, f"{tag} {r.display_name}")

    # ================================================================
    # 时长选择
    # ================================================================
    def _on_duration_auto_toggle(self):
        """切换自动/手动检测模式"""
        if self.duration_auto_var.get():
            self.duration_spin.config(state=tk.DISABLED)
        else:
            # 切到手动模式时，用当前配置的默认值初始化 Spinbox
            default_dur = str(self.config.get("video_duration", 5))
            self.duration_manual_var.set(default_dur)
            self.duration_spin.config(state=tk.NORMAL)
        self._update_mode_label()

    def _get_manual_duration(self) -> int | None:
        """
        返回手动指定的秒数。
        自动模式 → None（由 Worker 决定）
        手动模式 → int（1-18）
        """
        if self.duration_auto_var.get():
            return None
        try:
            val = int(self.duration_manual_var.get())
            return max(DURATION_MIN, min(DURATION_MAX, val))
        except ValueError:
            return None

    # ================================================================
    # 模式预览
    # ================================================================
    def _on_input_change(self, event=None):
        self._update_mode_label()

    # ---- 粘贴支持 ----
    def _on_paste(self, event=None):
        """显式处理粘贴：先清空占位文字，再粘贴剪贴板内容"""
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return "break"
        # 如果还在占位状态，先清空
        if self.prompt_text.get("1.0", "end-1c") == self._placeholder:
            self.prompt_text.delete("1.0", tk.END)
            self.prompt_text.config(foreground="black")
        # 插入剪贴板内容
        self.prompt_text.insert(tk.INSERT, text)
        self._update_mode_label()
        return "break"

    def _on_right_click(self, event):
        """右键弹出粘贴菜单"""
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="粘贴", command=self._on_paste)
        menu.add_command(label="清空", command=self._clear_prompt)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _clear_prompt(self):
        """清空输入框"""
        self.prompt_text.delete("1.0", tk.END)
        self.prompt_text.config(foreground="black")
        self._update_mode_label()

    def _update_mode_label(self):
        manual = self.intent_var.get()
        prompt = self._get_prompt()
        manual_dur = self._get_manual_duration()

        if manual != "auto":
            intent = manual
            desc = ModeDetector.describe(intent, len(self.ref_images))
            label = f"🎯 {desc}（手动）"
        elif prompt and len(prompt) > 3:
            intent, _, _ = IntentDetector.detect(prompt)
            desc = ModeDetector.describe(intent, len(self.ref_images))
            label = f"🔍 {desc}（自动）"
        elif self.ref_images:
            desc = ModeDetector.describe("image", len(self.ref_images))
            label = f"🔍 {desc}（自动）"
        else:
            self.mode_label.config(text="")
            return

        # 视频模式下显示时长
        if intent == "video":
            fps = int(self.config.get("video_fps", 24))
            if manual_dur:
                frames = GenerationWorker._duration_to_frames(manual_dur, fps)
                label += f" | ⏱ {manual_dur}秒→{frames}帧({frames/fps:.1f}s) 手动"
            elif prompt:
                detected = DurationParser.parse(prompt)
                if detected:
                    frames = GenerationWorker._duration_to_frames(detected, fps)
                    label += f" | ⏱ {detected}秒→{frames}帧({frames/fps:.1f}s) 自动提取"
                else:
                    default_dur = self.config.get("video_duration", 5)
                    frames = GenerationWorker._duration_to_frames(default_dur, fps)
                    label += f" | ⏱ 默认{default_dur}秒→{frames}帧"

        self.mode_label.config(text=label)

    # ================================================================
    # 输出目录
    # ================================================================
    def _browse_output(self):
        folder = filedialog.askdirectory(
            initialdir=self.output_var.get(), title="选择输出文件夹")
        if folder:
            self.output_var.set(folder)
            self.config["output_dir"] = folder
            ConfigManager.save(self.config)

    def _open_output(self):
        out_dir = self.output_var.get()
        if out_dir and os.path.isdir(out_dir):
            os.startfile(out_dir)
        else:
            os.makedirs(out_dir, exist_ok=True)
            os.startfile(out_dir)

    # ================================================================
    # 日志
    # ================================================================
    def _log(self, msg: str):
        self.log_area.config(state=tk.NORMAL)
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_area.insert(tk.END, f"[{ts}] {msg}\n")
        self.log_area.see(tk.END)
        self.log_area.config(state=tk.DISABLED)

    def _clear_log(self):
        self.log_area.config(state=tk.NORMAL)
        self.log_area.delete("1.0", tk.END)
        self.log_area.config(state=tk.DISABLED)

    # ================================================================
    # 历史记录
    # ================================================================
    def _update_history_panel(self):
        """刷新历史列表"""
        self.history_listbox.delete(0, tk.END)
        history = HistoryManager.load()
        for entry in reversed(history[-30:]):  # 最新在前，最多显示 30 条
            icon = ModeDetector.MODE_META.get(entry["mode"], {}).get("icon", "📄")
            prompt_short = entry["prompt"][:50]
            self.history_listbox.insert(tk.END,
                f"{icon} [{entry['time']}] {prompt_short} → {os.path.basename(entry['output'])}")

    def _on_history_double_click(self, event):
        """双击历史记录打开文件"""
        sel = self.history_listbox.curselection()
        if not sel:
            return
        history = HistoryManager.load()
        # 列表是反转的
        idx = len(history) - 1 - sel[0]
        if 0 <= idx < len(history):
            path = history[idx]["output"]
            if os.path.exists(path):
                os.startfile(path)
                self._log(f"📜 打开历史文件: {path}")
            else:
                self._log(f"⚠️ 历史文件已不存在: {path}")

    def _clear_history(self):
        """清空历史记录"""
        if messagebox.askyesno("确认清空", "确定要清空所有生成历史记录吗？\n（文件不会被删除）"):
            HistoryManager.save([])
            self._update_history_panel()
            self._log("📜 历史记录已清空")

    # ================================================================
    # 生成
    # ================================================================
    def _on_generate(self):
        prompt = self._get_prompt()
        if not prompt:
            messagebox.showwarning("提示", "请先输入描述内容")
            return

        output_dir = self.output_var.get()
        if not output_dir:
            messagebox.showwarning("提示", "请先设置输出目录")
            return

        # 更新配置
        self.config["output_dir"] = output_dir
        ConfigManager.save(self.config)

        # 创建 API 实例
        api_key = self.config.get("api_key", DEFAULT_API_KEY)
        if not api_key:
            messagebox.showwarning("未配置 API Key", "请先点击「⚙️ 设置」填入你的 Agnes AI API Key。")
            self._open_settings()
            return
        if api_key.startswith("sk-你的") or api_key.startswith("sk-占位"):
            messagebox.showwarning("请替换 API Key", "请先点击「⚙️ 设置」将 API Key 替换为你自己的 Key。\n\n获取方式：https://platform.agnes-ai.com")
            self._open_settings()
            return
        api = AgnesAPI(api_key, log_callback=self._log)

        # 确认模式（尊重手动选择）
        manual_val = self.intent_var.get()
        manual_intent = None if manual_val == "auto" else manual_val
        manual_duration = self._get_manual_duration()
        if manual_intent:
            intent = manual_intent
        else:
            intent, _, _ = IntentDetector.detect(prompt)
        mode = ModeDetector.detect(intent, len(self.ref_images))
        mode_meta = ModeDetector.MODE_META[mode]
        tag = "手动" if manual_intent else "自动"
        self._log(f"🚀 启动: {mode_meta['icon']} {mode_meta['label']}（{tag}）")
        if intent == "video":
            dur = self.config.get("video_duration", 5)
            fps = self.config.get("video_fps", 24)
            self._log(f"⚙️ 配置: 默认时长={dur}s, fps={fps}")

        # UI 切换
        self._set_running(True)

        # 后台线程
        self.worker = GenerationWorker(
            api=api, prompt=prompt, output_dir=output_dir,
            config=self.config, ref_images=list(self.ref_images),
            manual_intent=manual_intent,
            manual_duration=manual_duration,
            on_log=self._log, on_done=self._on_done, on_error=self._on_error,
        )
        self.worker.start()

    def _on_stop(self):
        self._log("⚠️ 用户停止了等待（后台任务可能仍在运行）")
        self._set_running(False)
        self.status_var.set("⏹ 已停止")

    def _on_done(self, output_path: str, mode: str):
        self.root.after(0, lambda: self._handle_done(output_path, mode))

    def _handle_done(self, output_path: str, mode: str):
        self._set_running(False)
        meta = ModeDetector.MODE_META.get(mode, {})
        icon = meta.get("icon", "✅")
        label = meta.get("label", mode)
        self._log(f"{icon} ✅ {label}完成！文件已保存到：")
        self._log(f"   📁 {output_path}")
        self.status_var.set(f"✅ 完成 → {output_path}")

        # 保存历史记录
        prompt = self._get_prompt()
        entry = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode": mode, "label": label,
            "prompt": prompt[:100], "output": output_path,
        }
        HistoryManager.add(entry)
        self._update_history_panel()

        if messagebox.askyesno("生成完成",
                               f"[{label}] 文件已保存到：\n{output_path}\n\n是否打开文件？"):
            os.startfile(output_path)

        out_dir = os.path.dirname(output_path)
        os.startfile(out_dir)

    def _on_error(self, error_msg: str):
        self.root.after(0, lambda: self._handle_error(error_msg))

    def _handle_error(self, error_msg: str):
        self._set_running(False)
        self._log(f"❌ 错误: {error_msg}")
        self.status_var.set(f"❌ 失败: {error_msg[:80]}")
        messagebox.showerror("生成失败", error_msg)

    def _set_running(self, running: bool):
        if running:
            self.generate_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.NORMAL)
            self.progress.start(10)
            self.status_var.set("⏳ 生成中...")
        else:
            self.generate_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            self.progress.stop()
            self.worker = None

    # ================================================================
    # 设置 & 关闭
    # ================================================================
    def _open_settings(self):
        SettingsDialog(self.root, self.config, on_save=self._on_settings_saved)

    def _on_settings_saved(self):
        self._log("⚙️ 设置已更新")

    def _first_run_setup(self):
        """首次启动：如果没有配置有效 API Key，弹设置窗口"""
        self._log("🔑 未检测到 API Key，请先在设置中填入你的 Key")
        self._open_settings()
        messagebox.showinfo(
            "欢迎使用 Agnes AI 生成工具",
            "首次使用，请在设置中填入你的 Agnes AI API Key。\n\n"
            "获取方式：访问 https://platform.agnes-ai.com 注册获取。\n"
            "API Key 仅保存在你的电脑上（%USERPROFILE%\\.agnes\\config.json）。"
        )

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askokcancel("退出", "任务正在运行中，确定要退出吗？"):
                return
        ConfigManager.save(self.config)
        self.root.destroy()

    def run(self):
        self._log("🚀 Agnes AI 生成工具 v3 已启动")
        self._log(f"📁 输出目录: {self.config.get('output_dir', DEFAULT_OUTPUT_DIR)}")
        self._log(f"🔑 API Key: {'已配置' if self.config.get('api_key') else '未配置'}")
        self._log(f"🖼️ 图片处理: {'PIL 已加载（自动缩放压缩）' if _HAS_PIL else '⚠️ PIL 未安装，建议 pip install Pillow 以自动压缩大图'}")
        self._log("📎 支持模式: 文生图 | 图生图 | 文生视频 | 图生视频 | 关键帧动画")
        self.root.mainloop()


# ============================================================
# 入口
# ============================================================
def main():
    app = AgnesApp()
    app.run()


if __name__ == "__main__":
    main()
