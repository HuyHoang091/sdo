"""
SDO Behavioral Cloning Model
Supervised learning: obs → action (classification)
Thay thế PPO cho offline training vì data đã có expert labels.
"""

import os
import json
import logging
import numpy as np
import torch
import torch.nn as nn
from collections import Counter
from orchestrator.config import MULTI_STATE_DIM, NUM_ACTIONS

log = logging.getLogger("sdo.bc")


class BCNetwork(nn.Module):
    """MLP classifier: 28D obs → 7 actions."""

    def __init__(self, obs_dim=MULTI_STATE_DIM, n_actions=NUM_ACTIONS,
                 hidden_sizes=(128, 128)):
        super().__init__()
        layers = []
        prev = obs_dim
        for h in hidden_sizes:
            layers.extend([nn.Linear(prev, h), nn.ReLU(), nn.Dropout(0.1)])
            prev = h
        layers.append(nn.Linear(prev, n_actions))
        self.net = nn.Sequential(*layers)

    def forward(self, x):
        return self.net(x)


class BCModel:
    """
    Wrapper tương thích với SB3 predict() interface.
    Agent code gọi model.predict(obs, deterministic=True) → (action, None)
    """

    def __init__(self, obs_dim=MULTI_STATE_DIM, n_actions=NUM_ACTIONS):
        self.obs_dim = obs_dim
        self.n_actions = n_actions
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.net = BCNetwork(obs_dim, n_actions).to(self.device)

    def predict(self, obs, deterministic=True):
        """Tương thích SB3: trả (action, None)."""
        self.net.eval()
        with torch.no_grad():
            if isinstance(obs, np.ndarray):
                t = torch.FloatTensor(obs).to(self.device)
            else:
                t = obs.to(self.device)
            if t.dim() == 1:
                t = t.unsqueeze(0)

            logits = self.net(t)

            if deterministic:
                action = logits.argmax(dim=-1).cpu().numpy()
            else:
                probs = torch.softmax(logits, dim=-1)
                action = torch.multinomial(probs, 1).squeeze(-1).cpu().numpy()

        # Trả scalar nếu input là 1 sample
        if action.shape == (1,):
            action = int(action[0])
        return action, None

    def train_from_data(self, data: list, epochs=200, lr=1e-3,
                        val_split=0.15):
        """
        Train BC model từ collected chaos data (list of episodes).

        Tự động tính class_weight từ frequency để xử lý imbalance.
        """
        # === 1. Extract (obs, action) pairs ===
        obs_list, action_list = [], []
        for ep in data:
            for t in ep.get("transitions", []):
                if t.get("multi_obs"):
                    obs_list.append(t["multi_obs"])
                elif t.get("state"):
                    s = t["state"]
                    if len(s) < self.obs_dim:
                        s = s + [0] * (self.obs_dim - len(s))
                    obs_list.append(s[:self.obs_dim])
                else:
                    continue
                action_list.append(t.get("action", 0))

        if not obs_list:
            log.error("No training samples found!")
            return

        X = torch.FloatTensor(np.array(obs_list)).to(self.device)
        y = torch.LongTensor(action_list).to(self.device)

        log.info("Training BC model: %d samples, %d features, %d actions",
                 len(X), self.obs_dim, self.n_actions)

        # === 2. Class weights (inverse frequency) ===
        counts = Counter(action_list)
        total = len(action_list)
        # weight = total / (n_classes * count_per_class)
        weight = torch.zeros(self.n_actions, device=self.device)
        for cls, cnt in counts.items():
            if cls < self.n_actions:
                weight[cls] = total / (len(counts) * cnt)

        log.info("Class weights: %s",
                 {i: f"{w:.2f}" for i, w in enumerate(weight.tolist()) if w > 0})
        log.info("Class distribution: %s", dict(counts.most_common()))

        # === 3. Train/Val split ===
        n_val = int(len(X) * val_split)
        indices = torch.randperm(len(X))
        val_idx, train_idx = indices[:n_val], indices[n_val:]

        X_train, y_train = X[train_idx], y[train_idx]
        X_val, y_val = X[val_idx], y[val_idx]

        # === 4. Training loop ===
        criterion = nn.CrossEntropyLoss(weight=weight)
        optimizer = torch.optim.Adam(self.net.parameters(), lr=lr,
                                     weight_decay=1e-4)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
            optimizer, T_max=epochs)

        self.net.train()
        best_val_acc = 0
        best_state = None
        patience = 30
        no_improve = 0

        for epoch in range(epochs):
            # Mini-batch training
            perm = torch.randperm(len(X_train))
            batch_size = 128
            total_loss = 0
            n_batches = 0

            for i in range(0, len(X_train), batch_size):
                idx = perm[i:i + batch_size]
                logits = self.net(X_train[idx])
                loss = criterion(logits, y_train[idx])

                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                total_loss += loss.item()
                n_batches += 1

            scheduler.step()

            # Validation
            self.net.eval()
            with torch.no_grad():
                val_logits = self.net(X_val)
                val_preds = val_logits.argmax(dim=-1)
                val_acc = (val_preds == y_val).float().mean().item()

                # Per-class accuracy
                if (epoch + 1) % 50 == 0 or epoch == 0:
                    avg_loss = total_loss / max(n_batches, 1)
                    log.info("Epoch %3d | loss=%.4f | val_acc=%.1f%%",
                             epoch + 1, avg_loss, val_acc * 100)

                    for cls in sorted(counts.keys()):
                        mask = y_val == cls
                        if mask.sum() > 0:
                            cls_acc = (val_preds[mask] == cls).float().mean()
                            log.info("  Action %d: val_acc=%.1f%% (n=%d)",
                                     cls, cls_acc.item() * 100, mask.sum().item())

            if val_acc > best_val_acc:
                best_val_acc = val_acc
                best_state = {k: v.clone() for k, v in
                              self.net.state_dict().items()}
                no_improve = 0
            else:
                no_improve += 1

            self.net.train()

            if no_improve >= patience:
                log.info("Early stopping at epoch %d, best val_acc=%.1f%%",
                         epoch + 1, best_val_acc * 100)
                break

        # Restore best weights
        if best_state:
            self.net.load_state_dict(best_state)
        log.info("Training complete. Best val_acc=%.1f%%", best_val_acc * 100)

    def save(self, path: str):
        """Save model (thêm .pt extension)."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        save_path = path if path.endswith(".pt") else path + ".pt"
        torch.save({
            "state_dict": self.net.state_dict(),
            "obs_dim": self.obs_dim,
            "n_actions": self.n_actions,
        }, save_path)
        log.info("BC model saved to %s", save_path)

    @classmethod
    def load(cls, path: str) -> "BCModel":
        """Load model."""
        load_path = path if path.endswith(".pt") else path + ".pt"
        checkpoint = torch.load(load_path, map_location="cpu",
                                weights_only=True)
        model = cls(
            obs_dim=checkpoint.get("obs_dim", MULTI_STATE_DIM),
            n_actions=checkpoint.get("n_actions", NUM_ACTIONS),
        )
        model.net.load_state_dict(checkpoint["state_dict"])
        model.net.eval()
        log.info("BC model loaded from %s", load_path)
        return model
