"""
SDO ONNX Inference - Lightweight model inference without PyTorch/SB3.

Dùng ONNX Runtime (~50MB) thay vì PyTorch+SB3 (~2GB) cho production.
Cho phép ship pre-trained model mà user không cần cài deep learning stack.

Usage:
    predictor = OnnxPredictor("models/sdo_agent.onnx")
    action = predictor.predict(obs_vector)
"""

import logging
import numpy as np
from typing import Optional

log = logging.getLogger("sdo.inference")


class OnnxPredictor:
    """
    Inference PPO model từ file ONNX.
    Thay thế stable_baselines3.PPO.predict() mà không cần PyTorch.
    """

    def __init__(self, model_path: str):
        try:
            import onnxruntime as ort
        except ImportError:
            raise ImportError(
                "onnxruntime is required for ONNX inference. "
                "Install: pip install onnxruntime"
            )

        self.session = ort.InferenceSession(
            model_path,
            providers=["CPUExecutionProvider"],
        )
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name

        log.info("ONNX model loaded: %s", model_path)
        log.info("  Input:  %s %s", self.input_name,
                 self.session.get_inputs()[0].shape)
        log.info("  Output: %s %s", self.output_name,
                 self.session.get_outputs()[0].shape)

    def predict(self, obs: np.ndarray, deterministic: bool = True) -> int:
        """
        Predict action from observation vector.

        Args:
            obs: numpy array shape (STATE_DIM,) hoặc (1, STATE_DIM)
            deterministic: True = argmax, False = sample from logits

        Returns:
            action: int (0-6)
        """
        # Ensure correct shape: (1, STATE_DIM)
        if obs.ndim == 1:
            obs = obs.reshape(1, -1)
        obs = obs.astype(np.float32)

        # Run inference
        outputs = self.session.run(None, {self.input_name: obs})
        logits = outputs[0]  # Shape: (1, NUM_ACTIONS)

        if deterministic:
            action = int(np.argmax(logits[0]))
        else:
            # Softmax → sample
            probs = _softmax(logits[0])
            action = int(np.random.choice(len(probs), p=probs))

        return action

    def predict_with_probs(self, obs: np.ndarray) -> tuple:
        """
        Predict action + trả về probability distribution.
        Hữu ích cho debugging và logging.

        Returns:
            (action, probs_dict)
        """
        if obs.ndim == 1:
            obs = obs.reshape(1, -1)
        obs = obs.astype(np.float32)

        outputs = self.session.run(None, {self.input_name: obs})
        logits = outputs[0][0]
        probs = _softmax(logits)
        action = int(np.argmax(probs))

        from orchestrator.config import ACTIONS
        probs_dict = {ACTIONS.get(i, f"a{i}"): round(float(p), 4)
                      for i, p in enumerate(probs)}

        return action, probs_dict


def _softmax(x: np.ndarray) -> np.ndarray:
    """Numerically stable softmax"""
    e_x = np.exp(x - np.max(x))
    return e_x / e_x.sum()


def load_predictor(model_dir: str = "models") -> Optional[OnnxPredictor]:
    """
    Tìm và load model theo thứ tự ưu tiên:
    1. ONNX model (lightweight, preferred)
    2. Không tìm thấy → return None (agent sẽ dùng rule-based)

    Chú ý: SB3 .zip model được xử lý riêng trong agent.py
    vì cần import torch/sb3.
    """
    import os
    from orchestrator.config import ONNX_MODEL_PATH

    # Priority 1: ONNX
    onnx_path = ONNX_MODEL_PATH
    if os.path.exists(onnx_path):
        return OnnxPredictor(onnx_path)

    # Tìm trong model_dir
    if os.path.isdir(model_dir):
        for f in os.listdir(model_dir):
            if f.endswith(".onnx"):
                return OnnxPredictor(os.path.join(model_dir, f))

    log.info("No ONNX model found in %s, will use rule-based or SB3", model_dir)
    return None
