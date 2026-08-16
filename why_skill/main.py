# why_skill/main.py
import os
import sys
import time
import html
import argparse
import traceback
from datetime import datetime
import httpx
from openai import OpenAI, APIConnectionError, APITimeoutError

try:
    import winreg
except ImportError:
    winreg = None  # 非 Windows 平台（Linux/macOS）不加载

# --- 配置区：手动代理（可选，留空则自动探测）---
MANUAL_PROXY = None  # 例如 "http://127.0.0.1:7897"
PROVIDER = "deepseek"  # "deepseek" 或 "claude"
# API Key：优先读环境变量 WHY_SKILL_API_KEY；兜底用旧明文（网页版上线后删除）
API_KEY = os.environ.get("WHY_SKILL_API_KEY", "") or "sk-221b3407d1a34a02ac7562b927cc086c"
# 默认 flash（快、省）；可切 deepseek-v4-pro / deepseek-reasoner（深度推理，出 reasoning_content）
MODEL_ID = os.environ.get("WHY_SKILL_MODEL", "deepseek-v4-flash")
# OpenAI 兼容接口地址；换服务商时改这里（如 OpenAI: https://api.openai.com/v1）
BASE_URL = os.environ.get("WHY_SKILL_BASE_URL", "https://api.deepseek.com")
MAX_TOKENS = int(os.environ.get("WHY_SKILL_MAX_TOKENS", "32768"))  # 输出上限拉到 API 最高（DS 便宜，防止思考吞掉正文）
# v1 先关闭质检（避免"1 个模糊词就 FAIL"误报干扰验收），规则定稿后按版块标题重写
CHECK_QUALITY = False
# ---------------------------------------------

if PROVIDER == "claude":
    try:
        from anthropic import Anthropic
    except ImportError:
        pass

try:
    from .extractor import extract_clean_narrative
    from .checker import cross_check_evidence  # 保留导入；接入流程待定
    from .sanitizer import check_why_quality
except (ImportError, ValueError):
    try:
        import extractor as extractor_mod
        import sanitizer as sanitizer_mod
        extract_clean_narrative = extractor_mod.extract_clean_narrative
        check_why_quality = sanitizer_mod.check_why_quality
    except ImportError:
        def extract_clean_narrative(x): return {"payload": x}
        def check_why_quality(x): return [(True, "规则缺失，跳过质检")]

# 极短 system：角色 + 铁律。规则全文放在 user message 开头（长 system 会稀释约束）
SYSTEM_SHORT = (
    "你是事件归因分析引擎。严格遵守两阶段流程："
    "阶段一只输出缺失信息探针清单，禁止任何结论；"
    "阶段二才输出终极审计报告。不编造信息，不确定处标注。"
    "思考过程保持简洁，直接输出结论性内容。"
)

# ================= 代理自动探测 =================
def get_active_proxy():
    if winreg is None:
        return None
    try:
        reg = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r'Software\Microsoft\Windows\CurrentVersion\Internet Settings')
        is_enabled, _ = winreg.QueryValueEx(reg, 'ProxyEnable')
        if is_enabled:
            server, _ = winreg.QueryValueEx(reg, 'ProxyServer')
            return f"http://{server}"
    except Exception:
        pass
    return None

def get_proxy_url():
    if MANUAL_PROXY:
        return MANUAL_PROXY
    auto_proxy = get_active_proxy()
    if auto_proxy:
        return auto_proxy
    return None

PROXY_URL = get_proxy_url()
if PROXY_URL:
    print(f"🔌 使用代理: {PROXY_URL}")
else:
    print("🔌 不使用代理，将直连网络")
# ==============================================

def parse_atfile(line: str):
    """解析 '@file 路径' 或 '@file路径'（有无空格均可）。返回路径；不是 @file 命令返回 None"""
    stripped = line.strip()
    if not stripped.lower().startswith("@file"):
        return None
    path = stripped[5:].strip().strip('"').strip()
    return path or None


class WhyAuditor:
    def __init__(self):
        self.rules_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "rules"))
        self.conversation_history = []

        if PROVIDER == "claude":
            self.client = Anthropic(api_key=API_KEY)
            self.model_id = "claude-3-5-sonnet-20240620"
        else:
            http_client = httpx.Client(
                proxy=PROXY_URL if PROXY_URL else None,
                timeout=httpx.Timeout(300.0, connect=10.0)
            )
            self.client = OpenAI(
                api_key=API_KEY,
                base_url=BASE_URL,
                http_client=http_client
            )
            self.model_id = MODEL_ID

    def load_rules(self, phase="phase1"):
        """阶段一 = 元引擎 + 逻辑库；阶段二 = 逻辑库 + 解剖刀 + 审计模板。
        阶段二不再加载元引擎，避免"阶段一禁止结论"与"输出报告"互相打架。"""
        rule_content = ""
        if phase == "phase2":
            files_to_load = ["why-logic-library.md", "scalpel-template.md", "audit-report-standard.md"]
        else:
            files_to_load = ["why-meta-engine.md", "why-logic-library.md"]
        if not os.path.exists(self.rules_path):
            return "⚠️ 警告：未找到 rules 文件夹，系统将以裸机模式运行。"
        try:
            for file in files_to_load:
                filepath = os.path.join(self.rules_path, file)
                if os.path.exists(filepath):
                    with open(filepath, "r", encoding="utf-8") as f:
                        rule_content += f"\n--- {file} ---\n{f.read()}\n"
                else:
                    print(f"⚠️ 未找到规则文件: {file}")
        except Exception as e:
            print(f"⚠️ 读取规则库异常: {e}")
        return rule_content

    def call_model_stream(self, messages, phase="phase1"):
        rules_text = self.load_rules(phase)
        # 规则全文前置到第一条 user 消息（system 保持极短）
        if messages and messages[0].get("role") == "user":
            messages = [{"role": "user", "content": f"{rules_text}\n\n{messages[0]['content']}"}] + messages[1:]
        system_prompt = SYSTEM_SHORT
        print(f"\n[📡 物理链路] 正在连接 {self.model_id} (阶段: {phase})...")
        start_time = time.time()

        for attempt in range(1, 4):
            try:
                if PROVIDER == "claude":
                    response = self.client.messages.create(
                        model=self.model_id,
                        system=system_prompt,
                        messages=messages,
                        max_tokens=MAX_TOKENS
                    )
                    full_content = response.content[0].text
                    if not full_content.strip():
                        raise RuntimeError("模型未返回正式内容")
                    print(full_content)
                    return full_content
                else:
                    response = self.client.chat.completions.create(
                        model=self.model_id,
                        messages=[{"role": "system", "content": system_prompt}] + messages,
                        stream=True
                        # 不设 max_tokens：与老版本/女娲一致，避免"思考挤掉正文"的截断问题
                    )
                    print("\n" + "🧠"*5 + f" [{self.model_id} 推演中] " + "🧠"*5)
                    full_content = ""
                    is_thinking = False
                    has_printed_divider = False
                    for chunk in response:
                        if not chunk.choices:  # 流式结束包（仅 usage，无 choices）直接跳过
                            continue
                        delta = chunk.choices[0].delta
                        if hasattr(delta, 'reasoning_content') and delta.reasoning_content:
                            if not is_thinking:
                                is_thinking = True
                            sys.stdout.write(delta.reasoning_content)
                            sys.stdout.flush()
                        if hasattr(delta, 'content') and delta.content:
                            if is_thinking and not has_printed_divider:
                                print("\n\n" + "🤖"*5 + " [正式输出] " + "🤖"*5)
                                has_printed_divider = True
                                is_thinking = False
                            sys.stdout.write(delta.content)
                            sys.stdout.flush()
                            full_content += delta.content
                    duration = time.time() - start_time
                    print(f"\n\n[✅ 响应成功] 物理耗时: {duration:.2f}s")
                    print("="*50)
                    if not full_content.strip():
                        # 思考过长占满输出上限 → 视为失败，触发重试
                        raise RuntimeError(
                            f"模型未返回正式内容（思考过长超出 {MAX_TOKENS} tokens 上限），已自动重试"
                        )
                    return full_content
            except Exception as e:
                print(f"\n❌ 第 {attempt} 次调用失败: {e}")
                traceback.print_exc()
                if attempt < 3:
                    time.sleep(2 * attempt)

        print("调用模型失败（已重试 3 次），请检查网络/Key 后重试。")
        return None

    def save_report(self, text: str):
        """报告落盘：.md（源文件）+ .html（单文件，双击即开、可打印）"""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
        os.makedirs(out_dir, exist_ok=True)
        md_path = os.path.join(out_dir, f"why-report_{ts}.md")
        html_path = os.path.join(out_dir, f"why-report_{ts}.html")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(text)
        page = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Why-Skill 审计报告</title>
<style>
body{{max-width:860px;margin:32px auto;padding:0 20px 60px;font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;line-height:1.8;color:#222;}}
pre{{white-space:pre-wrap;word-wrap:break-word;font-family:inherit;}}
</style>
</head>
<body><pre>{html.escape(text)}</pre></body>
</html>"""
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(page)
        return md_path, html_path

    # ========== 核心审计流程（统一入口） ==========
    def process_event(self, raw_input: str, source: str = "manual"):
        """处理事件描述，执行两阶段审计"""
        self.conversation_history = []
        clean_data = extract_clean_narrative(raw_input)
        print(f"⚙️ 噪音剥离完成。情绪强度: {clean_data.get('emotional_intensity', 0)}")

        # 阶段一
        print("\n--- 阶段一：生成缺失信息探针 ---")
        emotion_hint = ""
        if clean_data.get("is_high_risk"):
            emotion_hint = (
                f"\n[输入情绪强度高（{clean_data.get('emotional_intensity', '?')}），"
                "存在较强主观滤镜：探针必须包含非情绪化的物理查证路径。]"
            )
        user_msg = (
            f"事件描述：\n{clean_data['payload']}\n{emotion_hint}\n\n"
            "请执行阶段一：抽取并回显三要素，然后输出缺失信息探针清单。禁止输出任何结论。"
        )
        self.conversation_history = [{"role": "user", "content": user_msg}]
        phase1_report = self.call_model_stream(self.conversation_history, phase="phase1")
        if not phase1_report:
            print("阶段一报告生成失败，请重试。")
            return

        self.conversation_history.append({"role": "assistant", "content": phase1_report})
        print("\n⏸️ 引擎已暂停。请根据以上探针在现实世界中搜集/回忆证据。")
        print("请输入补充信息（多行以 EOF 结束）：")
        supp_lines = []
        while True:
            line = input()
            if line.strip() == "EOF":
                break
            supp_lines.append(line)
        supplement = "\n".join(supp_lines).strip()
        if not supplement:
            ans = input("未收到补充信息。直接进入阶段二（按现有信息出报告）？(y/n): ").strip().lower()
            if ans != 'y':
                print("已结束审计。")
                return
            supplement = "（用户未提供补充信息）"

        # 阶段二
        print("\n--- 阶段二：生成终极审计报告 ---")
        phase2_user_msg = (
            "以下是阶段一输出的探针清单：\n"
            f"{phase1_report}\n\n"
            "用户补充的信息：\n"
            f"{supplement}\n\n"
            "请执行阶段二：先逐项标记每条探针为 [已核实]/[未核实]/[证伪]，"
            "再按审计模板输出终极审计报告。"
        )
        self.conversation_history.append({"role": "user", "content": phase2_user_msg})
        phase2_report = self.call_model_stream(self.conversation_history, phase="phase2")
        if phase2_report:
            md_path, html_path = self.save_report(phase2_report)
            print(f"\n📄 报告已保存：\n  {md_path}\n  {html_path}")
            if CHECK_QUALITY:
                quality_results = check_why_quality(phase2_report)
                print("\n✅ 报告内部质检完成:")
                for passed, msg in quality_results:
                    print(f"   - {'[PASS]' if passed else '[FAIL]'} {msg}")
        else:
            print("⚠️ 最终报告生成失败。")

    # ========== 交互主循环 ==========
    def run(self):
        print(f"🛡️ Why-Skill V3.2 自适应元引擎已就位 [动力源: {PROVIDER.upper()} / 模型: {self.model_id}]")
        print("命令说明：")
        print("  - 直接粘贴事件描述（多行以 EOF 结束）")
        print("  - 或输入 @file <文件路径> 读取文本文件")
        print("  - 输入 exit/quit 退出\n")

        while True:
            # 统一输入入口
            print("\n📥 请输入事件（手动粘贴多行以 EOF 结束，或输入 @file 路径）：")
            first_line = input().strip()
            if not first_line:
                continue
            if first_line.lower() in ["exit", "quit", "退出"]:
                break

            # 处理 @file 命令（兼容有无空格）
            filepath = parse_atfile(first_line)
            if filepath is not None:
                if not os.path.exists(filepath):
                    print(f"❌ 文件不存在: {filepath}")
                    print("   提示：路径不要带引号，反斜杠/正斜杠均可，例如 @file D:\\CHAT\\xxx.txt")
                    continue
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        raw_input = f.read()
                    print(f"📄 已读取文件: {filepath} (字符数: {len(raw_input)})")
                    if len(raw_input) > 50000:
                        print("⚠️ 文件超过 5 万字符，可能会导致 API 超时或费用增加。建议先手动摘取核心部分。")
                        confirm = input("仍要继续审计吗？(y/n): ")
                        if confirm.lower() != 'y':
                            continue
                    self.process_event(raw_input, source="file")
                except Exception as e:
                    print(f"❌ 读取文件失败: {e}")
                continue

            # 否则当作手动多行输入（以 EOF 结束）
            # 第一行已经读入，可能不是 "EOF"，需要继续读后续行
            if first_line.strip() == "EOF":
                print("⚠️ 请先输入事件内容，然后再输入 EOF 结束。")
                continue
            lines = [first_line]
            while True:
                line = input()
                if line.strip() == "EOF":
                    break
                lines.append(line)
            raw_input = "\n".join(lines)
            if not raw_input.strip():
                print("⚠️ 未检测到有效输入，请重新输入。")
                continue
            self.process_event(raw_input, source="manual")

def main():
    parser = argparse.ArgumentParser(description="Why-Skill V3.2 首席审计官终端")
    parser.add_argument("--audit", type=str, help="直接审计一段文本并返回预处理结果")
    args = parser.parse_args()
    auditor = WhyAuditor()
    if args.audit:
        clean_data = extract_clean_narrative(args.audit)
        print(f"--- PRE-PROCESSED DATA ---\n{clean_data['payload']}\n--- END ---")
    else:
        auditor.run()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已退出 Why-Skill。")
