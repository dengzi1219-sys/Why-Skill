import re
from collections import Counter

def cross_check_evidence(side_a_text, side_b_text):
    """
    Why-Skill V3.0.5 广义物理断层对撞机
    功能：自动发现两份文本在物理数据和核心实体上的“非对称性”。
    """
    findings = []
    
    # --- 1. 广义物理量对撞 (数字+单位/标识) ---
    # 匹配金额、百分比、时间点、摄氏度、重量等一切物理量
    # 例如: 100°C, 28.88元, 15min, 1000点
    pattern_physics = r'\d+\.?\d*[a-zA-Z%元°度]'
    anchors_a = set(re.findall(pattern_physics, side_a_text))
    anchors_b = set(re.findall(pattern_physics, side_b_text))
    
    # 找出双方提到的物理量差异 (A-02 葡萄干法则)
    physics_mismatch = anchors_a.symmetric_difference(anchors_b)
    if physics_mismatch:
        findings.append(f"【物理量断层】: 监测到不匹配的锚点数据: {list(physics_mismatch)}")

    # --- 2. 核心名词(实体)对撞 ---
    # 提取两边文本中的所有核心词汇（此处可以用简单的分词逻辑或高频词过滤）
    # 为保证普适性，我们提取 2 个字以上的词语，并排除常用连接词
    def get_entities(text):
        # 简单正则匹配，提取连续的汉字作为潜在实体
        words = re.findall(r'[\u4e00-\u9fa5]{2,}', text)
        stop_words = {'发现', '觉得', '因为', '所以', '可能', '好像'} # 排除主观降噪词
        return {w for w in words if w not in stop_words}

    entities_a = get_entities(side_a_text)
    entities_b = get_entities(side_b_text)

    # 寻找“叙事孤岛”：只有一方提到的核心物质/动作
    islands_a = entities_a - entities_b # A提了但B没提
    islands_b = entities_b - entities_a # B提了但A没提

    if islands_a:
        findings.append(f"【叙事孤岛(A侧)】: 甲方特有的物理/背景变量: {list(islands_a)[:5]}")
    if islands_b:
        findings.append(f"【叙事孤岛(B侧)】: 乙方特有的物理/背景变量: {list(islands_b)[:5]}")

    # --- 3. 情绪/能量剧烈震荡点 ---
    # 检测重复标点符号，定位 [A-01] 能量溢出区
    spikes_a = len(re.findall(r'[!！?？]{2,}', side_a_text))
    spikes_b = len(re.findall(r'[!！?？]{2,}', side_b_text))
    
    if abs(spikes_a - spikes_b) > 5:
        findings.append(f"【能量非对称】: 双方情绪压强差异巨大，疑似存在单向信息加工。")

    return findings