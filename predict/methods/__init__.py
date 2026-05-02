"""PII Detection Methods"""

from .base import PIIDetector, PIIDetection, BBox, DetectionResult, compute_iou, compute_containment

__all__ = [
    "PIIDetector",
    "PIIDetection",
    "BBox",
    "DetectionResult",
    "compute_iou",
    "compute_containment",
    "PresidioDetector",
    "LLMDetector",
    "PaddleDetector",
    "PaddleLLMDetector",
    "TesseractDetector",
]


def __getattr__(name: str):
    """Lazy-load optional detector implementations.

    OCR/LLM baselines depend on heavier optional packages. Keeping these imports
    lazy lets CLI help and non-OCR utilities run after only the base install.
    """
    if name == "PresidioDetector":
        from .presidio_detector import PresidioDetector
        return PresidioDetector
    if name == "LLMDetector":
        from .llm_detector import LLMDetector
        return LLMDetector
    if name == "PaddleDetector":
        from .paddle_detector import PaddleDetector
        return PaddleDetector
    if name == "PaddleLLMDetector":
        from .paddle_detector import PaddleLLMDetector
        return PaddleLLMDetector
    if name == "TesseractDetector":
        from .tesseract_detector import TesseractDetector
        return TesseractDetector
    raise AttributeError(name)
