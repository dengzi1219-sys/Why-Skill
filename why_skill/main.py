# why_skill/main.py
import os
import sys
import time
import argparse
import traceback
import httpx
from openai import OpenAI, APIConnectionError, APITimeoutError

# --- 配置区：在这里一键切换 ---
PROVIDER = "deepseek"  # 可选: "claude" 或 "deepseek"
API_KEY = "输入你的key"
PROXY_URL = "输入你的端口"  # 你的物理代理端口
# ---------------------------

# 自动根据选择导入对应的库
if PROVIDER == "claude":
    try:
        from anthropic import Anthropic
    except ImportError:
        pass

# 尝试导入核心模块 (带死机兜底机制)
try:
    from .extractor import extract_clean_narrative
    from .checker import cross_check_evidence
    from .sanitizer import check_why_quality
except (ImportError, ValueError):
    try:
        import extractor as extractor_mod
        import sanitizer as sanitizer_mod
        extract_clean_narrative = extractor_mod.extract_clean_narrative
        check_why_quality = sanitizer_mod.check_why_quality
    except ImportError:
        # 兜底：如果模块实在找不到，定义简单函数防止崩溃
        def extract_clean_narrative(x): return {"payload": x}
        def check_why_quality(x): return [(True, "规则缺失，跳过质检")]

class WhyAuditor:
    def __init__(self):
        self.activated = False
        # 兼容当前目录和上级目录的 rules
        self.rules_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "rules"))
        
        # 初始化对应的客户端
        if PROVIDER == "claude":
            self.client = Anthropic(api_key=API_KEY)
            self.model_id = "claude-3-5-sonnet-20240620"
        else:
            # 【核心修复1】强行注入代理，且使用单数 proxy 适配最新版 httpx，增加 180 秒超长思考耐力
            self.http_client = httpx.Client(
                proxy=PROXY_URL, 
                timeout=httpx.Timeout(180.0, connect=10.0)
            )
            self.client = OpenAI(
                api_key=API_KEY, 
                base_url="https://api.deepseek.com",
                http_client=self.http_client
            )
            # 官方标准思考模型 ID
            self.model_id = "deepseek-reasoner"

    def load_rules(self):
        """装载 4 个 MD 文件作为系统大脑"""
        rule_content = ""
        if not os.path.exists(self.rules_path):
            return "⚠️ 警告：未找到 rules 文件夹，系统将以裸机模式运行。"
        
        try:
            for file in os.listdir(self.rules_path):
                if file.endswith(".md"):
                    with open(os.path.join(self.rules_path, file), "r", encoding="utf-8") as f:
                        rule_content += f"\n--- {file} ---\n{f.read()}\n"
        except Exception as e:
            print(f"⚠️ 读取规则库异常: {e}")
            
        return rule_content

    def call_model(self, system_prompt, user_content):
        """【核心修复2】流式打字机输出逻辑，实时捕获 DeepSeek 脑电波"""
        print(f"\n[📡 物理链路] 正在穿透代理 {PROXY_URL} 连接 {self.model_id}...")
        start_time = time.time()

        try:
            if PROVIDER == "claude":
                # Claude 暂保留非流式（依你之前配置）
                response = self.client.messages.create(
                    model=self.model_id,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_content}],
                    max_tokens=4000
                )
                return response.content[0].text
            else:
                # DeepSeek-R1 流式调用
                response = self.client.chat.completions.create(
                    model=self.model_id,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content}
                    ],
                    stream=True # 开启流式瀑布
                )
                
                print("\n" + "🧠"*5 + " [DEEPSEEK 深度推演中] " + "🧠"*5)
                
                full_content = ""
                is_thinking = False
                has_printed_divider = False

                for chunk in response:
                    delta = chunk.choices[0].delta
                    
                    # 1. 实时打印思考过程 (Reasoning Content)
                    if hasattr(delta, 'reasoning_content') and delta.reasoning_content:
                        if not is_thinking: is_thinking = True
                        sys.stdout.write(delta.reasoning_content)
                        sys.stdout.flush()

                    # 2. 实时打印最终答案 (Content)
                    if hasattr(delta, 'content') and delta.content:
                        if is_thinking and not has_printed_divider:
                            print("\n\n" + "🤖"*5 + " [正式审计报告] " + "🤖"*5)
                            has_printed_divider = True
                            is_thinking = False
                        
                        sys.stdout.write(delta.content)
                        sys.stdout.flush()
                        full_content += delta.content
                
                duration = time.time() - start_time
                print(f"\n\n[✅ 响应成功] 物理耗时: {duration:.2f}s")
                print("="*50)
                return full_content

        except APITimeoutError:
            print("\n❌ [致命错误: 响应超时] 代理没扛住，或者 DeepSeek 想得太久了。")
        except (APIConnectionError, httpx.ConnectError) as e:
            print(f"\n❌ [致命错误: 物理连接断开] 请求死在了本地。请确认 {PROXY_URL} 代理软件已开启并允许连接。")
        except Exception:
            print("\n❌ [未知异常发生]")
            traceback.print_exc()
        
        return None

    def run(self):
        print(f"🛡️ Why-Skill V3.0.5 调度枢纽已就位 [当前动力源: {PROVIDER.upper()} / 流式激活]")
        while True:
            cmd = input("\n📥 [首席审计官] > ")
            
            if not cmd.strip(): continue
            if cmd.lower() in ["exit", "quit", "退出"]: break

            if "准备分析事件" in cmd or "why start" in cmd:
                self.activated = True
                print(f"✨ 核心审计协议已载入，{PROVIDER.upper()} 引擎已预热。")
                continue

            if self.activated:
                # 1. 预处理
                clean_data = extract_clean_narrative(cmd)
                print(f"⚙️ 噪音剥离完成。")

                # 2. 调用模型 (这里会自动走流式打印，不用再单独 print report 了)
                report = self.call_model(self.load_rules(), clean_data['payload'])

                if report:
                    # 3. 后置质检
                    quality_results = check_why_quality(report)
                    print("\n✅ 报告内部质检完成:")
                    for passed, msg in quality_results:
                        print(f"   - {'[PASS]' if passed else '[FAIL]'} {msg}")
                else:
                    print("⚠️ 审计报告生成失败，请检查上方链路报错。")
            else:
                print("⚠️ 系统待命。请输入 '准备分析事件'。")

def main():
    # 使用 argparse 处理 Claude Code 联动
    parser = argparse.ArgumentParser(description="Why-Skill V3.0.5 首席审计官终端")
    parser.add_argument("--audit", type=str, help="直接审计一段文本并返回预处理结果")
    args = parser.parse_args()

    auditor = WhyAuditor()
    
    if args.audit:
        # Claude Code 联动模式：只做数据清洗，减少 API 压力
        clean_data = extract_clean_narrative(args.audit)
        print(f"--- PRE-PROCESSED DATA ---\n{clean_data['payload']}\n--- END ---")
    else:
        # 标准交互模式
        auditor.run()

if __name__ == "__main__":
    main()
