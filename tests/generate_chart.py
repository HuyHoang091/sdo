import pandas as pd
import matplotlib.pyplot as plt

results = [
    {"Scenario": "CPU Stress (api)", "Recovery Time (s)": 7.46, "Recovered": "Yes"},
    {"Scenario": "Memory Leak (web-app)", "Recovery Time (s)": 5.45, "Recovered": "Yes"},
    {"Scenario": "Network Delay (db)", "Recovery Time (s)": 13.66, "Recovered": "Yes"},
    {"Scenario": "Container Crash (api)", "Recovery Time (s)": 5.33, "Recovered": "Yes"}
]

df = pd.DataFrame(results)

# Xuất kết quả
with open("benchmark_results.md", "w", encoding="utf-8") as f:
    f.write("# Kết quả Benchmark sự cố SDO\n\n")
    f.write(df.to_markdown(index=False))

# Generate Chart
plt.figure(figsize=(10, 6))
bars = plt.bar(df["Scenario"], df["Recovery Time (s)"], color=['#4caf50' if r == 'Yes' else '#f44336' for r in df["Recovered"]])
plt.axhline(y=300, color='r', linestyle='--', label='Timeout')
plt.xlabel('Kịch bản (Incident)')
plt.ylabel('Thời gian phục hồi (giây)')
plt.title('Benchmark: Thời gian phục hồi của SDO RL Agent')
plt.xticks(rotation=15)
plt.legend()

for bar in bars:
    yval = bar.get_height()
    plt.text(bar.get_x() + bar.get_width()/2, yval + 0.5, f"{yval:.2f}s", ha='center', va='bottom')

plt.tight_layout()
plt.savefig("benchmark_chart.png", dpi=300)
print("Đã tạo bảng kết quả và biểu đồ.")
