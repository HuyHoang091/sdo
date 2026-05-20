"""
SDO ONNX Export — Chuyển đổi SB3 PPO model sang ONNX format.

Script này cần PyTorch + SB3 (chỉ chạy trên máy train, KHÔNG cần trên production).

Usage:
    python -m orchestrator.export_onnx                    # Default paths
    python -m orchestrator.export_onnx models/custom.zip  # Custom model
"""

import os
import sys
import logging
import numpy as np

log = logging.getLogger("sdo.export")


def export_to_onnx(
    sb3_model_path: str = None,
    onnx_output_path: str = None,
    validate: bool = True,
):
    """
    Export SB3 PPO model → ONNX.

    Args:
        sb3_model_path: Path to .zip model (without extension)
        onnx_output_path: Output .onnx file path
        validate: Validate ONNX output matches SB3 output
    """
    try:
        import torch
        from stable_baselines3 import PPO
    except ImportError:
        print("ERROR: PyTorch and stable-baselines3 are required for export.")
        print("Install: pip install torch stable-baselines3")
        sys.exit(1)

    from orchestrator.config import MODEL_PATH, ONNX_MODEL_PATH, STATE_DIM

    sb3_model_path = sb3_model_path or MODEL_PATH
    onnx_output_path = onnx_output_path or ONNX_MODEL_PATH

    # 1. Load SB3 model
    if not os.path.exists(f"{sb3_model_path}.zip"):
        print(f"ERROR: Model not found: {sb3_model_path}.zip")
        print("Train a model first: python -m orchestrator.main train-offline <data>")
        sys.exit(1)

    print(f"Loading SB3 model: {sb3_model_path}.zip")
    model = PPO.load(sb3_model_path)

    # 2. Extract PyTorch policy network
    policy = model.policy
    policy.eval()

    # 3. Create wrapper that outputs action logits
    class PolicyWrapper(torch.nn.Module):
        """Wrap SB3 policy to output raw action logits"""
        def __init__(self, sb3_policy):
            super().__init__()
            self.mlp_extractor = sb3_policy.mlp_extractor
            self.action_net = sb3_policy.action_net

        def forward(self, obs):
            features = self.mlp_extractor.forward_actor(
                self.mlp_extractor.shared_net(obs)
                if hasattr(self.mlp_extractor, 'shared_net')
                and self.mlp_extractor.shared_net is not None
                else obs
            )
            return self.action_net(features)

    # SB3 policy uses features_extractor → mlp_extractor → action_net
    # We need to trace through the full forward pass
    class FullPolicyWrapper(torch.nn.Module):
        """Full SB3 policy forward pass → action logits"""
        def __init__(self, sb3_policy):
            super().__init__()
            self.policy = sb3_policy

        def forward(self, obs):
            features = self.policy.extract_features(obs, self.policy.pi_features_extractor)
            latent_pi = self.policy.mlp_extractor.forward_actor(features)
            return self.policy.action_net(latent_pi)

    wrapper = FullPolicyWrapper(policy)
    wrapper.eval()

    # 4. Export to ONNX
    dummy_input = torch.randn(1, STATE_DIM)

    os.makedirs(os.path.dirname(onnx_output_path) or ".", exist_ok=True)

    print(f"Exporting to ONNX: {onnx_output_path}")
    torch.onnx.export(
        wrapper,
        dummy_input,
        onnx_output_path,
        input_names=["obs"],
        output_names=["action_logits"],
        dynamic_axes={
            "obs": {0: "batch_size"},
            "action_logits": {0: "batch_size"},
        },
        opset_version=14,
    )
    print(f"  ✅ ONNX exported: {onnx_output_path}")

    # 5. Validate
    if validate:
        print("Validating ONNX output...")
        try:
            import onnxruntime as ort
        except ImportError:
            print("  ⚠️  onnxruntime not installed, skipping validation")
            return

        session = ort.InferenceSession(onnx_output_path,
                                        providers=["CPUExecutionProvider"])
        test_obs = np.random.randn(1, STATE_DIM).astype(np.float32)

        # SB3 prediction
        with torch.no_grad():
            sb3_logits = wrapper(torch.from_numpy(test_obs)).numpy()

        # ONNX prediction
        onnx_logits = session.run(None, {"obs": test_obs})[0]

        # Compare
        max_diff = np.max(np.abs(sb3_logits - onnx_logits))
        sb3_action = int(np.argmax(sb3_logits[0]))
        onnx_action = int(np.argmax(onnx_logits[0]))

        print(f"  SB3 action:  {sb3_action}  logits: {sb3_logits[0][:4]}...")
        print(f"  ONNX action: {onnx_action}  logits: {onnx_logits[0][:4]}...")
        print(f"  Max diff:    {max_diff:.8f}")

        if sb3_action == onnx_action and max_diff < 1e-4:
            print("  ✅ Validation PASSED — outputs match!")
        elif sb3_action == onnx_action:
            print(f"  ⚠️  Actions match but logits differ by {max_diff:.6f}")
        else:
            print("  ❌ Validation FAILED — actions differ!")
            sys.exit(1)

    # 6. Size comparison
    sb3_size = os.path.getsize(f"{sb3_model_path}.zip") / 1024
    onnx_size = os.path.getsize(onnx_output_path) / 1024
    print(f"\nSize comparison:")
    print(f"  SB3 (.zip):  {sb3_size:.1f} KB")
    print(f"  ONNX (.onnx): {onnx_size:.1f} KB")
    print(f"  Ratio:       {onnx_size/sb3_size:.1%}")


if __name__ == "__main__":
    sb3_path = sys.argv[1] if len(sys.argv) > 1 else None
    onnx_path = sys.argv[2] if len(sys.argv) > 2 else None
    export_to_onnx(sb3_path, onnx_path)
