# why_skill/sanitizer.py
import re

def check_why_quality(ai_output):
    """
    语义除垢协议：自动审计 Claude 的报告质量。
    """
    checks = []
    
    # 1. 模糊形容词检测 (逻辑硬度检查)
    adjectives = ["可能", "好像", "比较", "非常", "应该是", "大概"]
    found_adjs = [a for a in adjectives if a in ai_output]
    checks.append((len(found_adjs) == 0, f"语义降噪检查: 发现 {len(found_adjs)} 个模糊词"))

    # 2. 外部能量源分析检测
    has_energy_audit = any(word in ai_output for word in ["能量", "动机", "代偿", "熵"])
    checks.append((has_energy_audit, "闭环能量审计: 是否排查了外部暗能量源"))

    # 3. 诚实边界描述检测
    has_boundary = any(word in ai_output for word in ["诚实边界", "无法证明", "黑盒", "局限"])
    checks.append((has_boundary, "诚实边界检查: 是否标注了认知黑盒"))

    return checks