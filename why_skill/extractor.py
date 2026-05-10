import re  # <--- 必须加这一行！
def extract_clean_narrative(raw_log):
    """
    独立创建：人性噪音过滤器。
    功能：在交给 AI 前，完成物理层面的“语义降噪”。
    """
    # 1. 剥离 QQ/微信 导出记录的系统噪音
    clean = re.sub(r'\[图片: .*?\]|\[表情\d+\]|资源: \d+ 个文件', '', raw_log)
    clean = re.sub(r'时间: .*?\n', '', clean)
    
    # 2. 提取情绪爆发点 (通过标点符号频率检测 [A-02] 能量爆裂点)
    spikes = len(re.findall(r'[!！?？]{2,}', clean))
    
    # 3. 输出纯净文本 + 情绪热图数据
    return {
        "payload": clean.strip(),
        "emotional_intensity": spikes,
        "is_high_risk": spikes > 20 # 判定该记录是否带有高强度主观偏见
    }