"""
Benchmark Compare — So sánh kết quả giữa SDO RL Agent và K8s Native.
Xuất bảng + radar chart cho luận văn.
"""

import json
import os
import sys

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def load_sdo_results():
    """Load từ evaluation_report.md (parse bảng markdown) hoặc dùng hardcoded từ lần chạy gần nhất."""
    # Thử load từ file json nếu có
    if os.path.exists("sdo_benchmark_results.json"):
        return pd.read_json("sdo_benchmark_results.json")

    # Fallback: parse evaluation_report.md
    if os.path.exists("evaluation_report.md"):
        with open("evaluation_report.md", "r", encoding="utf-8") as f:
            content = f.read()

        # Tìm bảng markdown
        lines = content.split("\n")
        table_lines = []
        in_table = False
        for line in lines:
            if line.strip().startswith("|") and "Ep" in line:
                in_table = True
            if in_table and line.strip().startswith("|"):
                table_lines.append(line.strip())
            elif in_table and not line.strip().startswith("|"):
                break

        if len(table_lines) >= 3:
            # Parse header
            headers = [h.strip() for h in table_lines[0].split("|")[1:-1]]
            rows = []
            for line in table_lines[2:]:  # Bỏ header và separator
                vals = [v.strip() for v in line.split("|")[1:-1]]
                if len(vals) == len(headers):
                    rows.append(vals)

            df = pd.DataFrame(rows, columns=headers)
            for col in ["P1", "P2", "P3"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df["Ep"] = pd.to_numeric(df["Ep"], errors="coerce")
            return df

    return None


def load_k8s_results():
    # if os.path.exists("k8s_benchmark_results.json"):
    #     return pd.read_json("k8s_benchmark_results.json")
    # return None
    if os.path.exists("k8s_evaluation_report.md"):
        with open("k8s_evaluation_report.md", "r", encoding="utf-8") as f:
            content = f.read()

        lines = content.split("\n")
        table_lines = []
        in_table = False

        for line in lines:
            if line.strip().startswith("|") and "Ep" in line:
                in_table = True

            if in_table and line.strip().startswith("|"):
                table_lines.append(line.strip())
            elif in_table and not line.strip().startswith("|"):
                break

        if len(table_lines) >= 3:
            headers = [h.strip() for h in table_lines[0].split("|")[1:-1]]
            rows = []

            for line in table_lines[2:]:
                vals = [v.strip() for v in line.split("|")[1:-1]]
                if len(vals) == len(headers):
                    rows.append(vals)

            df = pd.DataFrame(rows, columns=headers)

            for col in ["P1", "P2", "P3"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")

            df["Ep"] = pd.to_numeric(df["Ep"], errors="coerce")

            return df

    # Fallback: nếu không có markdown thì dùng JSON raw
    if os.path.exists("k8s_benchmark_results.json"):
        return pd.read_json("k8s_benchmark_results.json")

    return None


def create_comparison_report(sdo_df, k8s_df):
    """Tạo báo cáo so sánh chi tiết."""

    # Merge
    merged = pd.merge(
        sdo_df[["Ep", "Type", "Target", "Flags", "P1", "P2", "P3"]],
        k8s_df[["Ep", "P1", "P2", "P3"]],
        on="Ep",
        suffixes=("_SDO", "_K8s"),
    )

    # Tính composite score cho mỗi bên
    sdo_avg = {
        "P1": sdo_df["P1"].mean(),
        "P2": sdo_df["P2"].mean(),
        "P3": sdo_df["P3"].mean(),
    }
    k8s_avg = {
        "P1": k8s_df["P1"].mean(),
        "P2": k8s_df["P2"].mean(),
        "P3": k8s_df["P3"].mean(),
    }

    sdo_base = (
        sdo_avg["P1"] * 0.35 + sdo_avg["P2"] * 0.35 + sdo_avg["P3"] * 0.20
    ) * 100
    k8s_base = (
        k8s_avg["P1"] * 0.35 + k8s_avg["P2"] * 0.35 + k8s_avg["P3"] * 0.20
    ) * 100

    # ============================================================
    # BÁIN CÁO MARKDOWN
    # ============================================================
    report = []
    report.append("# So sánh Benchmark: SDO RL Agent vs. Kubernetes Native\n")
    report.append(f"Ngày chạy: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}\n")

    report.append("\n## Tổng quan Điểm\n")
    report.append(
        "| Phương pháp | P1 (Perception) | P2 (Action) | P3 (Timing) | Base Score |"
    )
    report.append(
        "|-------------|----------------|-------------|-------------|------------|"
    )
    report.append(
        f"| **SDO RL Agent** | {sdo_avg['P1']:.2f} | {sdo_avg['P2']:.2f} | {sdo_avg['P3']:.2f} | **{sdo_base:.1f}** |"
    )
    report.append(
        f"| **K8s Native** | {k8s_avg['P1']:.2f} | {k8s_avg['P2']:.2f} | {k8s_avg['P3']:.2f} | **{k8s_base:.1f}** |"
    )

    delta = sdo_base - k8s_base
    if delta > 0:
        report.append(
            f"\n> **SDO RL Agent vượt trội hơn K8s Native {delta:.1f} điểm base score.**\n"
        )
    else:
        report.append(
            f"\n> K8s Native vượt trội hơn SDO RL Agent {abs(delta):.1f} điểm base score.\n"
        )

    report.append("\n## Chi tiết từng Episode\n")
    report.append(
        "| Ep | Type | Target | Flags | P1_SDO | P1_K8s | P2_SDO | P2_K8s | P3_SDO | P3_K8s |"
    )
    report.append(
        "|---|------|--------|-------|--------|--------|--------|--------|--------|--------|"
    )
    for _, row in merged.iterrows():
        ep = int(row["Ep"])
        report.append(
            f"| {ep} | {row['Type']} | {row['Target']} | {row['Flags']} | "
            f"{row['P1_SDO']:.2f} | {row['P1_K8s']:.2f} | "
            f"{row['P2_SDO']:.2f} | {row['P2_K8s']:.2f} | "
            f"{row['P3_SDO']:.2f} | {row['P3_K8s']:.2f} |"
        )

    # Phân tích theo fault type
    report.append("\n## Phân tích theo loại lỗi\n")
    fault_types = [
        "cpu_stress",
        "memory_leak",
        "network_chaos",
        "make_unhealthy",
        "crash_container",
        "fp",
    ]
    report.append("| Fault Type | SDO P2 avg | K8s P2 avg | SDO tốt hơn? |")
    report.append("|------------|-----------|-----------|--------------|")
    for ft in fault_types:
        mask_sdo = sdo_df["Type"] == ft
        mask_k8s = k8s_df["Type"] == ft
        if mask_sdo.sum() > 0:
            s = sdo_df.loc[mask_sdo, "P2"].mean()
            k = k8s_df.loc[mask_k8s, "P2"].mean()
            better = "✅" if s >= k else "❌"
            report.append(f"| {ft} | {s:.2f} | {k:.2f} | {better} |")

    report.append("\n## Kết luận\n")
    report.append("### Ưu điểm SDO RL Agent so với K8s Native:\n")
    report.append(
        "1. **Network Chaos**: K8s hoàn toàn mù trước network delay/loss. SDO RL Agent có thể phát hiện và phản ứng (scale_up, kill_and_replace)."
    )
    report.append(
        "2. **Hành động đa dạng**: SDO có 5 loại hành động (scale_up, scale_down, restart, kill_replace, increase_limit). K8s chỉ có restart + HPA scale."
    )
    report.append(
        "3. **Cascade Awareness**: SDO RL Agent học được mối quan hệ cause-effect giữa các service. K8s xử lý độc lập."
    )
    report.append(
        '4. **False Positive**: Cả hai đều có khả năng "im lặng" khi không có lỗi.\n'
    )
    report.append("### Ưu điểm K8s Native:")
    report.append("1. **Tốc độ restart**: Kubelet restart pod gần như tức thì (< 5s).")
    report.append(
        "2. **Không cần training**: Hoạt động ngay không cần dữ liệu huấn luyện."
    )
    report.append("3. **Ổn định**: Không có rủi ro model RL đưa ra quyết định sai.\n")

    report_text = "\n".join(report)

    with open("benchmark_comparison.md", "w", encoding="utf-8") as f:
        f.write(report_text)

    print(report_text)

    # ============================================================
    # RADAR CHART
    # ============================================================
    create_radar_chart(sdo_avg, k8s_avg)
    create_bar_chart(merged)

    print("\n[+] Đã lưu:")
    print("    - benchmark_comparison.md")
    print("    - benchmark_radar.png")
    print("    - benchmark_bars.png")


def create_radar_chart(sdo_avg, k8s_avg):
    """Radar chart so sánh 3 trục P1/P2/P3."""
    categories = ["P1\n(Perception)", "P2\n(Action)", "P3\n(Timing)"]

    sdo_vals = [sdo_avg["P1"], sdo_avg["P2"], sdo_avg["P3"]]
    k8s_vals = [k8s_avg["P1"], k8s_avg["P2"], k8s_avg["P3"]]

    # Close the polygon
    sdo_vals += sdo_vals[:1]
    k8s_vals += k8s_vals[:1]

    angles = np.linspace(0, 2 * np.pi, len(categories), endpoint=False).tolist()
    angles += angles[:1]

    fig, ax = plt.subplots(figsize=(8, 8), subplot_kw=dict(polar=True))

    ax.fill(angles, sdo_vals, alpha=0.25, color="#2196F3", label="SDO RL Agent")
    ax.plot(angles, sdo_vals, linewidth=2, color="#2196F3", marker="o")

    ax.fill(angles, k8s_vals, alpha=0.25, color="#FF5722", label="K8s Native")
    ax.plot(angles, k8s_vals, linewidth=2, color="#FF5722", marker="s")

    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(categories, fontsize=13, fontweight="bold")
    ax.set_ylim(0, 1.1)
    ax.set_title(
        "SDO RL Agent vs K8s Native\nBenchmark Comparison",
        fontsize=16,
        fontweight="bold",
        pad=30,
    )
    ax.legend(loc="upper right", bbox_to_anchor=(1.3, 1.1), fontsize=12)

    plt.tight_layout()
    plt.savefig("benchmark_radar.png", dpi=150, bbox_inches="tight")
    plt.close()


def create_bar_chart(merged):
    """Bar chart so sánh P2 (Action Quality) theo từng episode."""
    fig, ax = plt.subplots(figsize=(16, 6))

    x = np.arange(len(merged))
    width = 0.35

    bars1 = ax.bar(
        x - width / 2,
        merged["P2_SDO"],
        width,
        label="SDO RL Agent",
        color="#2196F3",
        alpha=0.8,
    )
    bars2 = ax.bar(
        x + width / 2,
        merged["P2_K8s"],
        width,
        label="K8s Native",
        color="#FF5722",
        alpha=0.8,
    )

    ax.set_xlabel("Episode", fontsize=12)
    ax.set_ylabel("P2 (Action Quality)", fontsize=12)
    ax.set_title(
        "Action Quality (P2) — SDO RL Agent vs K8s Native",
        fontsize=14,
        fontweight="bold",
    )
    ax.set_xticks(x)
    ax.set_xticklabels([f"Ep{int(e)}" for e in merged["Ep"]], rotation=45)
    ax.legend(fontsize=12)
    ax.set_ylim(0, 1.15)
    ax.grid(axis="y", alpha=0.3)

    plt.tight_layout()
    plt.savefig("benchmark_bars.png", dpi=150, bbox_inches="tight")
    plt.close()


if __name__ == "__main__":
    sdo = load_sdo_results()
    k8s = load_k8s_results()

    if sdo is None:
        print("ERROR: Không tìm thấy kết quả SDO. Chạy benchmark.py trước.")
        sys.exit(1)
    if k8s is None:
        print("ERROR: Không tìm thấy kết quả K8s. Chạy benchmark_k8s.py trước.")
        sys.exit(1)

    create_comparison_report(sdo, k8s)
