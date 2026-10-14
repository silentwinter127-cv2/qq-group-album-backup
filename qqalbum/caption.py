"""BLIP image captioning: batched CPU inference with optional int8 quantization.

Captions are English (BLIP's training language). To let Chinese queries reach them,
:func:`expand_query` maps common Chinese words to the English terms BLIP actually
emits, and an optional local zh->en model can translate a whole query.
"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

DEFAULT_CAPTION_DIR = r"D:\blip-image-captioning-base"
DEFAULT_TRANSLATE_DIR = r"D:\opus-mt-zh-en"
CAPTION_MODEL_TAG = "blip-image-captioning-base"
CAPTION_LABEL = "BLIP-base"

CAPTION_FOLDER_NAMES = ("blip-image-captioning-base", "blip-image-captioning-large",
                        "blip2", "Florence-2-base", "florence-2-base")


class CaptionUnavailable(RuntimeError):
    """Raised when the caption model or its dependencies are missing."""


def _looks_like_model(path: Optional[str]) -> bool:
    if not path or not os.path.isdir(path):
        return False
    return (os.path.isfile(os.path.join(path, "config.json"))
            and (os.path.isfile(os.path.join(path, "pytorch_model.bin"))
                 or os.path.isfile(os.path.join(path, "model.safetensors"))))


def find_caption_dir(explicit: Optional[str] = None) -> str:
    """Locate the local caption model checkout.

    An explicitly requested directory must really contain a model: silently falling
    back to another location would hide a typo in `--caption-dir`.
    """
    env = os.environ.get("QQALBUM_CAPTION_DIR")
    if explicit:
        if _looks_like_model(explicit):
            return explicit
        raise CaptionUnavailable(
            f"指定的描述模型目录不可用：{explicit}（需要 config.json 与权重文件）")
    if env:
        if _looks_like_model(env):
            return env
        raise CaptionUnavailable(
            f"环境变量 QQALBUM_CAPTION_DIR 指向的目录不可用：{env}")

    candidates: List[str] = [DEFAULT_CAPTION_DIR]
    for drive in ("D:\\", "E:\\", "C:\\"):
        for name in CAPTION_FOLDER_NAMES:
            candidates.append(os.path.join(drive, name))
            candidates.append(os.path.join(drive, "models", name))
    for path in candidates:
        if _looks_like_model(path):
            return path
    raise CaptionUnavailable(
        "找不到本地 BLIP 描述模型（需要 config.json + 权重）。已尝试："
        + ", ".join(candidates)
        + "。可用 --caption-dir 或 QQALBUM_CAPTION_DIR 指定。")


# ---------------------------------------------------------------------------
# Chinese -> English query expansion (BLIP captions are English)
# ---------------------------------------------------------------------------
# Only common visual words; matched as substrings of the user's query.
ZH_EN_TERMS: Dict[str, str] = {
    "女孩": "girl", "女生": "girl", "女人": "woman", "女士": "woman", "女性": "woman",
    "男人": "man", "男生": "man", "男孩": "boy", "男性": "man", "人": "person",
    "老人": "old man", "小孩": "child", "孩子": "child", "婴儿": "baby",
    "人群": "crowd", "人们": "people", "脸": "face", "头发": "hair", "眼睛": "eyes",
    "猫": "cat", "小猫": "cat", "狗": "dog", "小狗": "dog", "鸟": "bird", "动物": "animal",
    "鱼": "fish", "马": "horse", "熊": "bear", "兔子": "rabbit", "老鼠": "mouse",
    "花": "flower", "树": "tree", "草": "grass", "植物": "plant", "森林": "forest",
    "山": "mountain", "海": "sea", "海滩": "beach", "湖": "lake", "河": "river",
    "水": "water", "天空": "sky", "云": "cloud", "太阳": "sun", "月亮": "moon",
    "雪": "snow", "雨": "rain", "日落": "sunset", "夜晚": "night", "夜景": "night",
    "白天": "day", "街道": "street", "城市": "city", "建筑": "building", "房子": "house",
    "房间": "room", "室内": "room", "窗户": "window", "门": "door", "墙": "wall",
    "车": "car", "汽车": "car", "火车": "train", "飞机": "airplane", "船": "boat",
    "自行车": "bicycle", "摩托": "motorcycle", "公交": "bus", "地铁": "subway",
    "食物": "food", "吃": "eating", "饭": "rice", "面": "noodles", "蛋糕": "cake",
    "水果": "fruit", "蔬菜": "vegetables", "肉": "meat", "饮料": "drink", "咖啡": "coffee",
    "茶": "tea", "酒": "wine", "瓶": "bottle", "杯": "cup", "盘子": "plate",
    "电脑": "computer", "屏幕": "screen", "手机": "phone", "相机": "camera", "键盘": "keyboard",
    "书": "book", "纸": "paper", "文字": "text", "标志": "sign", "海报": "poster",
    "画": "painting", "插画": "illustration", "动漫": "anime", "漫画": "comic",
    "卡通": "cartoon", "游戏": "game", "图标": "logo", "图案": "pattern", "地图": "map",
    "桌子": "table", "椅子": "chair", "床": "bed", "沙发": "sofa", "灯": "light",
    "衣服": "clothes", "裙子": "dress", "帽子": "hat", "鞋": "shoes", "包": "bag",
    "眼镜": "glasses", "玩具": "toy", "玩偶": "doll", "吉他": "guitar", "乐器": "instrument",
    "球": "ball", "运动": "sport", "跑": "running", "站": "standing", "坐": "sitting",
    "走": "walking", "拿着": "holding", "戴着": "wearing", "微笑": "smiling",
    "红色": "red", "蓝色": "blue", "绿色": "green", "黄色": "yellow", "黑色": "black",
    "白色": "white", "粉色": "pink", "紫色": "purple", "橙色": "orange", "灰色": "gray",
    "背景": "background", "前景": "foreground", "特写": "closeup", "全景": "panorama",
    "招牌": "sign", "商店": "store", "便利店": "store", "超市": "supermarket",
    "餐厅": "restaurant", "厨房": "kitchen", "卧室": "bedroom", "办公室": "office",
    "教室": "classroom", "医院": "hospital", "车站": "station", "商场": "mall",
    "公园": "park", "桥": "bridge", "路": "road", "台阶": "stairs", "走廊": "hallway",
    "电视": "tv", "窗户外面": "outside", "里面": "inside", "外面": "outside",
    "看起来": "", "照片": "photo", "图片": "picture", "图形": "graphic",
}

_CJK = re.compile(r"[\u4e00-\u9fff]")


def expand_query(text: str, limit: int = 12) -> str:
    """Append English terms for any Chinese words found in `text`.

    Used so a Chinese query can match BLIP's English captions without a translation
    model: the original text is kept (for vector search) and the expansion is appended
    (for caption full-text search).
    """
    if not text or not _CJK.search(text):
        return text
    terms: List[str] = []
    for zh, en in ZH_EN_TERMS.items():
        if zh and en and zh in text and en not in terms:
            terms.append(en)
        if len(terms) >= limit:
            break
    if not terms:
        return text
    return f"{text} {' '.join(terms)}"


def english_only(text: str) -> str:
    """Strip CJK characters, leaving the English part of a query."""
    return re.sub(r"\s+", " ", re.sub(r"[\u4e00-\u9fff]+", " ", text or "")).strip()


def has_cjk(text: str) -> bool:
    return bool(_CJK.search(text or ""))


@dataclass
class CaptionResult:
    photo_id: str
    caption: str
    model: str = CAPTION_MODEL_TAG


class CaptionEngine:
    """Lazy singleton wrapper around BLIP (batched, optional int8)."""

    _lock = threading.Lock()
    _instance: Optional["CaptionEngine"] = None

    def __init__(self, model_dir: str, log=None, batch_size: int = 12,
                 max_new_tokens: int = 24, quantize: bool = True):
        self.model_dir = model_dir
        self.log = log or (lambda msg: None)
        self.batch_size = max(1, int(batch_size))
        self.max_new_tokens = max(4, int(max_new_tokens))
        self.quantize = quantize
        self._model = None
        self._proc = None
        self._torch = None
        self._translator = None
        self._translator_tried = False

    @classmethod
    def instance(cls, model_dir: Optional[str] = None, log=None) -> "CaptionEngine":
        with cls._lock:
            if cls._instance is None:
                cls._instance = CaptionEngine(find_caption_dir(model_dir), log=log)
            return cls._instance

    # ------------------------------------------------------------------ load
    def load(self) -> None:
        if self._model is not None:
            return
        try:
            import torch  # noqa: PLC0415
            from transformers import BlipForConditionalGeneration, BlipProcessor  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise CaptionUnavailable(f"缺少依赖：{exc}") from None
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        t0 = time.time()
        self.log(f"加载描述模型 {self.model_dir} …")
        self._proc = BlipProcessor.from_pretrained(self.model_dir, local_files_only=True)
        model = BlipForConditionalGeneration.from_pretrained(self.model_dir, local_files_only=True)
        model.eval()
        if self.quantize:
            try:
                model = torch.quantization.quantize_dynamic(model, {torch.nn.Linear},
                                                            dtype=torch.qint8)
                model.eval()
                self.log("已启用 int8 动态量化")
            except Exception as exc:
                self.log(f"量化失败，使用 fp32：{type(exc).__name__}")
        self._model = model
        self._torch = torch
        # default to a conservative thread count so the WebUI and the SigLIP
        # indexing job keep most of the CPU
        default_threads = max(1, min(8, (os.cpu_count() or 8) // 2))
        torch.set_num_threads(max(1, int(os.environ.get("QQALBUM_CAPTION_THREADS")
                                         or os.environ.get("QQALBUM_TORCH_THREADS")
                                         or default_threads)))
        self.log(f"描述模型就绪（{time.time() - t0:.1f}s，batch={self.batch_size}，"
                 f"CPU 线程 {torch.get_num_threads()}）")

    @property
    def model(self):
        self.load()
        return self._model

    def available(self) -> Tuple[bool, str]:
        try:
            find_caption_dir(self.model_dir)
            import torch  # noqa: F401,PLC0415
            import transformers  # noqa: F401,PLC0415
        except Exception as exc:
            return False, str(exc)
        return True, ""

    # ------------------------------------------------------------- inference
    def caption_images(self, images: Sequence[Any]) -> List[str]:
        """Caption a batch of PIL images (one generate() call)."""
        if not images:
            return []
        torch = self._torch or __import__("torch")
        out: List[str] = []
        with torch.no_grad():
            for start in range(0, len(images), self.batch_size):
                chunk = list(images[start:start + self.batch_size])
                inputs = self._proc(images=chunk, return_tensors="pt")
                generated = self._model.generate(**inputs, max_new_tokens=self.max_new_tokens,
                                                 num_beams=1, do_sample=False)
                out.extend(self._proc.decode(g, skip_special_tokens=True).strip()
                           for g in generated)
        return out

    def caption_image(self, image) -> str:
        caps = self.caption_images([image])
        return caps[0] if caps else ""

    # ------------------------------------------------------------ translation
    def translate_to_english(self, text: str) -> str:
        """Best-effort zh->en translation using a local model; falls back to ''."""
        if not has_cjk(text):
            return text
        if self._translator is None and not self._translator_tried:
            self._translator_tried = True
            try:
                import transformers  # noqa: PLC0415
                model_cls = getattr(transformers, "MarianMTModel", None)
                tokenizer_cls = getattr(transformers, "MarianTokenizer", None)
                if model_cls is None or tokenizer_cls is None:
                    # transformers 5.x dropped these from AutoTokenizer, but the
                    # concrete classes exist for backward compatibility
                    raise ImportError("transformers 未提供 MarianMTModel/MarianTokenizer")
                path = os.environ.get("QQALBUM_TRANSLATE_DIR") or DEFAULT_TRANSLATE_DIR
                if os.path.isdir(path):
                    self.log(f"加载翻译模型 {path} …")
                    tokenizer = tokenizer_cls.from_pretrained(path, local_files_only=True)
                    model = model_cls.from_pretrained(path, local_files_only=True).eval()
                    self._translator = (tokenizer, model)
                    self.log("翻译模型就绪")
                else:
                    self.log(f"未找到翻译模型目录 {path}，改用关键词映射")
            except Exception as exc:
                self.log(f"翻译模型不可用（改用关键词映射）：{type(exc).__name__}: {str(exc)[:90]}")
                self._translator = None
        if not self._translator:
            return ""
        tokenizer, model = self._translator
        torch = self._torch or __import__("torch")
        try:
            with torch.no_grad():
                batch = tokenizer([text], return_tensors="pt", padding=True, truncation=True)
                generated = model.generate(**batch, max_new_tokens=64, num_beams=1)
                return tokenizer.decode(generated[0], skip_special_tokens=True).strip()
        except Exception:
            return ""

    @staticmethod
    def load_image(path: str):
        from PIL import Image  # noqa: PLC0415
        with Image.open(path) as img:
            return img.convert("RGB")
