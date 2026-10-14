"""SigLIP2 powered tagging and semantic search (text -> image, image -> image).

The model runs locally from a checkout on disk (no network). Embeddings are L2
normalised 768-d float32 vectors stored as SQLite BLOBs; similarity is a plain dot
product. Tagging uses SigLIP's native sigmoid probabilities over a prompt vocabulary,
then rescales them within each photo so the strongest tag reads as 100%.
"""
from __future__ import annotations

import io
import json
import os
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

DEFAULT_MODEL_DIR = r"D:\siglip2-base-patch16-224"
MODEL_FOLDER_NAME = "siglip2-base-patch16-224"
VECTOR_DIM = 768
EMBED_MODEL_TAG = MODEL_FOLDER_NAME
MODEL_LABEL = "SigLIP2-base-patch16-224"


class SiglipUnavailable(RuntimeError):
    """Raised when torch/transformers or the local model weights are missing."""


# ---------------------------------------------------------------------------
# Tag vocabulary: (Chinese label, English prompt)
# ---------------------------------------------------------------------------
# SigLIP's native training format is a single class description, so prompts stay short.
VOCAB: List[Tuple[str, str]] = [
    # 主题
    ("食物", "a photo of food"),
    ("饮品", "a photo of a drink"),
    ("人物", "a photo of a person"),
    ("人像特写", "a close-up portrait of a face"),
    ("合影", "a group photo of people"),
    ("自拍", "a selfie"),
    ("动物", "a photo of an animal"),
    ("猫", "a photo of a cat"),
    ("狗", "a photo of a dog"),
    ("鸟", "a photo of a bird"),
    ("昆虫", "a photo of an insect"),
    ("植物", "a photo of a plant or flower"),
    ("风景", "a landscape photo of scenery"),
    ("天空", "a photo of the sky or clouds"),
    ("海与水域", "a photo of the sea, lake or water"),
    ("山", "a photo of mountains"),
    ("雪景", "a photo of snow"),
    ("日落夜景", "a photo of a sunset or night scene"),
    ("城市街景", "a photo of a city street"),
    ("建筑", "a photo of a building"),
    ("室内空间", "a photo of an indoor room"),
    ("交通工具", "a photo of a vehicle"),
    ("汽车", "a photo of a car"),
    ("飞机", "a photo of an airplane"),
    ("火车", "a photo of a train"),
    ("家具家居", "a photo of furniture or home interior"),
    ("电子产品", "a photo of an electronic device"),
    ("电脑界面截图", "a screenshot of a computer interface"),
    ("手机截图", "a screenshot of a phone app"),
    ("网页截图", "a screenshot of a website"),
    ("文字文档", "a page of text or a document"),
    ("图表数据", "a chart or data visualization"),
    ("图标", "an icon or logo graphic"),
    ("标志与品牌", "a brand logo"),
    ("海报广告", "a poster or advertisement"),
    ("专辑封面", "an album cover artwork"),
    ("动漫插画", "an anime or manga illustration"),
    ("卡通形象", "a cartoon character"),
    ("绘画手绘", "a drawing or painting"),
    ("数字艺术", "digital artwork"),
    ("像素画", "pixel art"),
    ("3D渲染", "a 3D rendered image"),
    ("抽象图案", "an abstract pattern"),
    ("纹理贴图", "a texture or material swatch"),
    ("表情包", "a meme or reaction image"),
    ("游戏画面", "a video game screenshot"),
    ("玩具模型", "a toy or figurine"),
    ("服装时尚", "a photo of clothing or fashion"),
    ("体育运动", "a photo of sports or exercise"),
    ("乐器音乐", "a photo of a musical instrument"),
    ("书籍", "a photo of a book"),
    ("室内装饰", "an interior design or decoration photo"),
    ("城市俯瞰", "an aerial view of a city"),
    ("无人空景", "an empty room or liminal space"),
    ("文字标题特写", "a photo dominated by large text"),
    ("漫画分镜", "a comic strip panel"),
    ("科幻风格", "a science fiction themed image"),
    ("复古风格", "a retro or vintage style image"),
    ("怀旧滤镜", "a nostalgic photo with a vintage filter"),
    ("黑白照片", "a black and white photo"),
    ("暖色调", "a warm-toned image"),
    ("冷色调", "a cool-toned image"),
    ("小清新", "a light and airy aesthetic photo"),
    ("诡异氛围", "an eerie or unsettling image"),
    ("可爱风格", "a cute and kawaii style image"),
    ("简约风格", "a minimal and clean design"),
    ("Logo文字设计", "a typographic logo design"),
    # 画面构成（对“这是什么类型的图”很有用）
    ("照片写实", "a realistic photograph"),
    ("插画绘制", "an illustration or drawing"),
    ("设计排版", "a graphic design layout"),
    ("纯色背景", "an image with a plain solid background"),
    ("特写细节", "a close-up of a small detail"),
    ("远景全景", "a wide panoramic view"),
    ("正面构图", "a centered composition"),
    ("低角度拍摄", "a photo taken from a low angle"),
    ("俯视角度", "a photo taken from above"),
    ("强对比明暗", "an image with strong light and shadow contrast"),
    ("高饱和度", "a highly saturated colorful image"),
    ("模糊虚焦", "a blurry or out-of-focus image"),
    ("老旧画质", "a low quality or heavily compressed image"),
    ("拼贴多图", "a collage of several images"),
    ("带水印", "an image with a watermark"),
    ("包含中文文字", "an image containing Chinese text"),
    ("包含英文文字", "an image containing English text"),
    ("夜晚灯光", "a photo of night lighting"),
    ("雨雪天气", "a photo taken in rain or snow"),
    ("手写笔记", "a handwritten note"),
    ("桌面摆拍", "a flat lay photo on a desk"),
    ("墙面纹理", "a photo of a wall or surface texture"),
    ("天空背景", "a photo against the sky"),
    ("人群密集", "a crowded scene with many people"),
    ("街头摄影", "a street photography shot"),
    ("产品展示", "a product photo on display"),
    ("LOGO合集", "a collection of many logos"),
]

# extra free-form hints used to boost n-gram quality in captions
TAG_COLORS = {
    "食物": "#ff9f43", "饮品": "#ff9f43", "动物": "#2ecc71", "猫": "#2ecc71", "狗": "#2ecc71",
    "植物": "#2ecc71", "风景": "#4a9eff", "天空": "#4a9eff", "海与水域": "#4a9eff",
    "城市街景": "#4a9eff", "建筑": "#4a9eff", "人物": "#e056a0", "人像特写": "#e056a0",
    "动漫插画": "#b06bff", "卡通形象": "#b06bff", "绘画手绘": "#b06bff", "数字艺术": "#b06bff",
    "图标": "#8892a6", "标志与品牌": "#8892a6", "海报广告": "#8892a6",
    "电脑界面截图": "#f0b429", "手机截图": "#f0b429", "网页截图": "#f0b429", "文字文档": "#f0b429",
    "游戏画面": "#f0b429", "表情包": "#f0b429",
}


def tag_color(name: str) -> str:
    return TAG_COLORS.get(name, "#7d5cff")


@dataclass
class TagHit:
    tag_id: int
    name: str
    name_en: str
    score: float
    confidence: float
    rank: int


@dataclass
class TagResult:
    photo_id: str
    tags: List[TagHit] = field(default_factory=list)


def find_model_dir(explicit: Optional[str] = None) -> str:
    """Locate the local model checkout."""
    candidates = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("QQALBUM_SIGLIP_DIR")
    if env:
        candidates.append(env)
    candidates.append(DEFAULT_MODEL_DIR)
    for drive in ("D:\\", "E:\\", "C:\\"):
        candidates.append(os.path.join(drive, MODEL_FOLDER_NAME))
        candidates.append(os.path.join(drive, "models", MODEL_FOLDER_NAME))
    for path in candidates:
        if path and os.path.isfile(os.path.join(path, "model.safetensors")):
            return path
    raise SiglipUnavailable(
        "找不到本地 SigLIP2 模型（需要 model.safetensors、config.json、tokenizer.json）。"
        f"已尝试：{', '.join(p for p in candidates if p)}。"
        "可用 --siglip-dir 参数或 QQALBUM_SIGLIP_DIR 环境变量指定目录。")
    return ""  # pragma: no cover


class SiglipEngine:
    """Lazy singleton wrapper around the transformers model."""

    _lock = threading.Lock()
    _instance: Optional["SiglipEngine"] = None

    def __init__(self, model_dir: str, log=None, threads: int = 0):
        self.model_dir = model_dir
        self.log = log or (lambda msg: None)
        self.threads = threads or max(4, min(12, (os.cpu_count() or 8)))
        self._model = None
        self._proc = None
        self._torch = None
        self._tag_ids: Dict[str, int] = {}
        self._vocab_cache: Optional[Tuple[List[str], Any]] = None
        self.torch_threads = max(1, int(os.environ.get("QQALBUM_TORCH_THREADS") or self.threads))

    # ------------------------------------------------------------ lifecycle
    @classmethod
    def instance(cls, model_dir: Optional[str] = None, log=None) -> "SiglipEngine":
        with cls._lock:
            if cls._instance is None:
                cls._instance = SiglipEngine(find_model_dir(model_dir), log=log)
            return cls._instance

    def load(self) -> None:
        if self._model is not None:
            return
        try:
            import torch  # noqa: PLC0415
            from transformers import AutoModel, AutoProcessor  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise SiglipUnavailable(
                f"缺少依赖：{exc}. 需要 torch 与 transformers："
                "pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple torch transformers"
            ) from None
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        t0 = time.time()
        self.log(f"加载模型 {self.model_dir} …")
        self._model = AutoModel.from_pretrained(self.model_dir, local_files_only=True).eval()
        self._proc = AutoProcessor.from_pretrained(self.model_dir, local_files_only=True)
        self._torch = torch
        torch.set_num_threads(self.torch_threads)
        self.log(f"模型就绪（{time.time() - t0:.1f}s，CPU 线程 {torch.get_num_threads()}）")

    @property
    def model(self):
        self.load()
        return self._model

    @property
    def processor(self):
        self.load()
        return self._proc

    @property
    def torch(self):
        self.load()
        return self._torch

    def available(self) -> Tuple[bool, str]:
        try:
            find_model_dir(self.model_dir)
            import torch  # noqa: F401,PLC0415
            import transformers  # noqa: F401,PLC0415
        except Exception as exc:
            return False, str(exc)
        return True, ""

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _pooled(out: Any):
        """transformers 5.x returns an *Output object, older versions a tensor."""
        if hasattr(out, "pooler_output") and out.pooler_output is not None:
            return out.pooler_output
        if hasattr(out, "last_hidden_state") and out.last_hidden_state is not None:
            return out.last_hidden_state[:, 0]
        return out

    def _normalise(self, tensor):
        torch = self.torch
        return torch.nn.functional.normalize(tensor, dim=-1)

    def sigmoid_thresholds(self) -> Tuple[float, float]:
        model = self.model
        torch = self.torch
        try:
            scale = float(model.logit_scale.exp().detach())
        except AttributeError:
            scale = 100.0
        bias = float(getattr(model, "logit_bias", torch.tensor(0.0)).detach())
        return scale, bias

    # ------------------------------------------------------------ encoding
    def encode_images(self, images: Sequence[Any], batch_size: int = 16):
        torch = self.torch
        feats = []
        with torch.no_grad():
            for i in range(0, len(images), batch_size):
                chunk = list(images[i:i + batch_size])
                inputs = self.processor(images=chunk, return_tensors="pt")
                out = self.model.get_image_features(**inputs)
                feats.append(self._normalise(self._pooled(out)))
        return torch.cat(feats) if feats else torch.zeros((0, VECTOR_DIM))

    def encode_image(self, image):
        return self.encode_images([image])[0]

    def encode_texts(self, texts: Sequence[str], batch_size: int = 32):
        torch = self.torch
        feats = []
        with torch.no_grad():
            for i in range(0, len(texts), batch_size):
                chunk = list(texts[i:i + batch_size])
                try:
                    inputs = self.processor(text=chunk, padding="max_length", return_tensors="pt")
                    out = self.model.get_text_features(**inputs)
                except Exception:
                    # some tokenizer configs refuse max_length padding: pad manually
                    tok = self.processor.tokenizer
                    raw = [tok(t)["input_ids"] for t in chunk]
                    width = max(len(ids) for ids in raw)
                    pad = tok.pad_token_id if tok.pad_token_id is not None else 0
                    ids = torch.tensor([x + [pad] * (width - len(x)) for x in raw])
                    mask = torch.tensor([[1] * len(x) + [0] * (width - len(x)) for x in raw])
                    with torch.no_grad():
                        out = self.model.get_text_features(input_ids=ids, attention_mask=mask)
                feats.append(self._normalise(self._pooled(out)))
        return torch.cat(feats) if feats else torch.zeros((0, VECTOR_DIM))

    def encode_text(self, text: str):
        return self.encode_texts([text])[0]

    # ---------------------------------------------------- vocabulary caching
    def vocab_matrix(self):
        if self._vocab_cache is None:
            names = [name for name, _ in VOCAB]
            prompts = [prompt for _, prompt in VOCAB]
            self._vocab_cache = (names, self.encode_texts(prompts))
        return self._vocab_cache

    def tag_probabilities(self, image_features) -> Tuple[List[str], Any]:
        """Sigmoid probabilities of each vocabulary prompt for the given image vectors."""
        names, text_features = self.vocab_matrix()
        torch = self.torch
        scale, bias = self.sigmoid_thresholds()
        logits = image_features @ text_features.T * scale + bias
        return names, torch.sigmoid(logits)

    # ----------------------------------------------------------------- files
    @staticmethod
    def load_image(path: str, max_side: int = 512):
        from PIL import Image  # noqa: PLC0415
        with Image.open(path) as img:
            img = img.convert("RGB")
            if max_side and max(img.size) > max_side * 2:
                img.thumbnail((max_side * 2, max_side * 2))
            return img

    @staticmethod
    def load_image_bytes(data: bytes):
        from PIL import Image  # noqa: PLC0415
        with Image.open(io.BytesIO(data)) as img:
            return img.convert("RGB")

    # ------------------------------------------------------------ tag a photo
    def tag_image_features(self, image_features, top_k: int = 0,
                           min_probability: float = 0.0,
                           min_tags: int = 3) -> List[Dict[str, Any]]:
        """Tags for one image, sorted by score.

        SigLIP2 is trained with a learned bias (around -16) so absolute sigmoid values
        are small and not calibrated; `score` keeps the raw probability for sorting and
        `confidence` is the score relative to that photo's best tag (so the top tag is
        1.0). By default every vocabulary entry is returned so the UI can re-rank per
        photo without re-running the model; `top_k`/`min_probability` trim it if wanted.
        """
        names, probs = self.tag_probabilities(image_features.reshape(1, -1))
        row = probs[0]
        torch = self.torch
        k = len(names) if not top_k else min(int(top_k), len(names))
        values, indices = torch.topk(row, k)
        best = float(values[0]) if k else 0.0
        out: List[Dict[str, Any]] = []
        for rank, (value, index) in enumerate(zip(values.tolist(), indices.tolist()), start=1):
            probability = float(value)
            if len(out) >= min_tags and min_probability and probability < min_probability:
                continue
            out.append({
                "name": names[index],
                "name_en": VOCAB[index][1],
                "score": probability,
                "confidence": (probability / best) if best > 0 else 0.0,
                "rank": rank,
            })
        return out


# ---------------------------------------------------------------------------
# Vector (de)serialisation helpers
# ---------------------------------------------------------------------------
def vector_to_blob(vector) -> bytes:
    """Pack a 1-D float tensor/array into little-endian float32 bytes."""
    try:
        import numpy as np  # noqa: PLC0415
        array = np.asarray(vector, dtype="<f4")
        return array.tobytes()
    except ImportError:  # pragma: no cover
        values = [float(x) for x in vector]
        return struct.pack(f"<{len(values)}f", *values)


def blob_to_vector(blob: bytes):
    import numpy as np  # noqa: PLC0415
    return np.frombuffer(blob, dtype="<f4")


def build_matrix(blobs: Sequence[bytes]):
    """Stack blobs into an (n, dim) float32 matrix."""
    import numpy as np  # noqa: PLC0415
    if not blobs:
        return np.zeros((0, VECTOR_DIM), dtype="<f4")
    return np.vstack([blob_to_vector(b) for b in blobs]).astype("<f4", copy=False)
