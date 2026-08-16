"""
Why-Skill 网页版（Streamlit）
=============================
两阶段缺失信息归因审计：
  setup（输入事件+可选问题）→ probe（探针清单，思考折叠展示）
  → collect（用户补充查证信息）→ report（终极审计报告）→ result

关键设计：
- 思考轮 + 成文轮两次调用：第一轮保留完整思考给用户看；
  若思考过长导致正文缺失/截断，自动用思考结果二次成文，保证报告完整。
- 规则单一来源：直接读取 3.0/rules/*.md，与 CLI 共用同一套产品逻辑。
- 自带 key：用户在前端填入 DeepSeek API Key，仅存于本页面会话，不落盘。

运行：cd D:\\code\\why-skill-project\\3.0 && streamlit run web/app.py
"""

import os
import sys

import streamlit as st
import httpx
from openai import OpenAI

# 自动启动：直接运行 python app.py 也会自动拉起 Streamlit（无需手动输入 streamlit run）
if not st.runtime.exists():
    import streamlit.web.cli as stcli
    sys.argv = ["streamlit", "run", sys.argv[0]]
    sys.exit(stcli.main())

# ==========================================
# 规则加载（与 CLI 共用 3.0/rules/*.md）
# ==========================================
RULES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rules")
PHASE_FILES = {
    "phase1": ["why-meta-engine.md", "why-logic-library.md"],
    "phase2": ["why-logic-library.md", "scalpel-template.md", "audit-report-standard.md"],
}


def load_rules(phase: str) -> str:
    parts = []
    for name in PHASE_FILES.get(phase, PHASE_FILES["phase1"]):
        p = os.path.join(RULES_DIR, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                parts.append(f"--- {name} ---\n{f.read()}")
    if not parts:
        return "⚠️ 未找到 rules 文件夹，系统将以裸机模式运行。"
    return "\n\n".join(parts)


# 与 CLI 的 SYSTEM_SHORT 保持一致（角色+铁律极短，规则全文放 user 消息）
SYSTEM_SHORT = (
    "你是事件归因分析引擎。严格遵守两阶段流程："
    "阶段一只输出缺失信息探针清单，禁止任何结论；"
    "阶段二才输出终极审计报告。不编造信息，不确定处标注。"
    "思考过程保持简洁，直接输出结论性内容。"
)


# ==========================================
# API 客户端
# ==========================================
def make_client(api_key: str, proxy: str = ""):
    kwargs = {"api_key": api_key, "base_url": "https://api.deepseek.com"}
    if proxy and proxy.strip():
        kwargs["http_client"] = httpx.Client(
            proxy=proxy.strip(), timeout=httpx.Timeout(600.0, connect=10.0)
        )
    return OpenAI(**kwargs)


# ==========================================
# 流式调用（思考轮 + 成文轮兜底）
# ==========================================
def stream_once(client, model, sys_msg, msgs, thinking_ph, content_ph):
    """第一轮：流式输出，分别捕获思考(reasoning)与正文(content)"""
    thinking, content, finish = "", "", None
    last_think_update, last_content_update = 0, 0
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": sys_msg}] + msgs,
        stream=True,
    )
    for chunk in resp:
        if not chunk.choices:
            continue
        d = chunk.choices[0].delta
        if chunk.choices[0].finish_reason:
            finish = chunk.choices[0].finish_reason
        rc = getattr(d, "reasoning_content", None)
        if rc:
            thinking += rc
            if len(thinking) - last_think_update >= 300:
                thinking_ph.markdown(f"🧠 **思考中...**\n\n```text\n{thinking[-4000:]}```")
                last_think_update = len(thinking)
        c = getattr(d, "content", None)
        if c:
            content += c
            if len(content) - last_content_update >= 200:
                content_ph.markdown(content + "▌")
                last_content_update = len(content)
    if content:
        content_ph.markdown(content)
    return thinking, content, finish


def compose_once(client, model, sys_msg, msgs, thinking, content_ph):
    """第二轮成文：把第一轮思考作为上下文，用全新额度直接输出正文"""
    extra = [
        {
            "role": "user",
            "content": (
                "以下是你的思考过程（仅作参考，不要重复思考）：\n"
                f"{thinking[-12000:]}\n\n"
                "请直接输出最终内容，不要重复思考过程。"
            ),
        }
    ]
    content = ""
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": sys_msg}] + msgs + extra,
        stream=True,
        max_tokens=32768,
    )
    for chunk in resp:
        if not chunk.choices:
            continue
        d = chunk.choices[0].delta
        c = getattr(d, "content", None)
        if c:
            content += c
            content_ph.markdown(content + "▌")
    if content:
        content_ph.markdown(content)
    return content


def run_phase(client, model, phase, messages, thinking_ph, content_ph):
    """完整执行一个阶段：思考轮 + 需要时成文轮兜底"""
    rules = load_rules(phase)
    msgs = [{"role": "user", "content": rules + "\n\n" + messages[0]["content"]}] + messages[1:]
    sys_msg = SYSTEM_SHORT

    thinking, content, finish = stream_once(client, model, sys_msg, msgs, thinking_ph, content_ph)

    # 正文为空或截断（思考过长占满预算）→ 用思考结果二次成文
    if content.strip() and finish != "length":
        return thinking, content, False

    thinking_ph.markdown("⏳ 第一轮思考过长，正在用思考结果重新成文...")
    content2 = compose_once(client, model, sys_msg, msgs, thinking, content_ph)
    if content2 and content2.strip():
        return thinking, content2, True
    return thinking, content, False


# ==========================================
# 输入处理
# ==========================================
def decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def build_corpus(files, pasted: str) -> str:
    parts = []
    for f in files or []:
        try:
            text = decode_bytes(f.read())
            parts.append(f"### 文件：{f.name}\n\n{text}")
        except Exception:
            pass
    if pasted and pasted.strip():
        parts.append(f"### 手动粘贴内容\n\n{pasted.strip()}")
    return "\n\n---\n\n".join(parts)


def build_supplement(files, pasted: str) -> str:
    parts = []
    for f in files or []:
        try:
            text = decode_bytes(f.read())
            parts.append(f"### 补充文件：{f.name}\n\n{text}")
        except Exception:
            pass
    if pasted and pasted.strip():
        parts.append(pasted.strip())
    return "\n\n---\n\n".join(parts)


# ==========================================
# 页面
# ==========================================
st.set_page_config(page_title="Why-Skill · 归因审计", page_icon="🕵️", layout="wide")

with st.sidebar:
    st.header("⚙️ 配置")
    api_key = st.text_input(
        "DeepSeek API Key",
        type="password",
        help="platform.deepseek.com 注册获取",
    )
    model = st.selectbox(
        "模型",
        ["deepseek-v4-flash", "deepseek-v4-pro"],
        help="flash 快省；pro 推理更深（思考更长，需靠成文轮兜底）",
    )
    proxy = st.text_input(
        "代理地址（可选）",
        placeholder="http://127.0.0.1:7890",
        help="留空直连。Clash 默认 7890。",
    )
    st.caption("🔒 Key 只存于本页面会话内存，不落盘、不上传。")
    st.divider()
    if st.button("🧹 重置全部", use_container_width=True):
        for k in list(st.session_state.keys()):
            del st.session_state[k]
        st.rerun()

DEFAULTS = {
    "stage": "setup",
    "event_name": "",
    "question": "",
    "corpus": "",
    "probes": "",
    "thinking1": "",
    "supplement": "",
    "report": "",
    "thinking2": "",
    "fallback_used": False,
}
for k, v in DEFAULTS.items():
    st.session_state.setdefault(k, v)

st.title("🕵️ Why-Skill · 缺失信息归因引擎")
st.caption("两阶段审计：探针清单 → 现实查证补充 → 终极审计报告。真相永远在表层叙述之外。")

stage = st.session_state.stage

# ---------- 阶段 1：输入 ----------
if stage == "setup":
    c1, c2 = st.columns([2, 1])
    with c1:
        st.text_input("🎯 事件名称（可选）", key="event_name", placeholder="例如：麓湖转场 / 展会取消 / 宿舍冲突")
        st.text_input(
            "❓ 你想搞清楚的问题（可选）",
            key="question",
            placeholder="例如：她为什么死守麓湖？展会为什么突然取消？",
        )
        uploaded = st.file_uploader(
            "📂 上传事件材料 (.txt / .md，可多选)",
            type=["txt", "md"],
            accept_multiple_files=True,
        )
        pasted = st.text_area(
            "或直接粘贴事件描述 / 聊天记录：",
            height=220,
            placeholder="把事件材料粘贴在这里...（可同时上传文件和粘贴文本，会合并）",
        )
        corpus = build_corpus(uploaded, pasted)
        if corpus.strip():
            st.caption(f"📊 语料总计：{len(corpus)} 字符")
        if st.button("🔍 开始探针分析", type="primary", use_container_width=True):
            if not api_key:
                st.error("请先在左侧填入 DeepSeek API Key")
            elif not corpus.strip():
                st.error("请上传文件或粘贴事件材料")
            else:
                st.session_state.corpus = corpus
                st.session_state.stage = "probe"
                st.rerun()
    with c2:
        st.info(
            "**使用流程**\n\n"
            "1. 输入事件材料（文件或粘贴）\n"
            "2. 阶段一输出**缺失信息探针**（不结论）\n"
            "3. 你带着探针去现实查证，回来补充\n"
            "4. 阶段二输出**终极审计报告**（最可能→次可能→备选+置信度）\n\n"
            "思考过程会完整保留，可折叠查看。"
        )

# ---------- 阶段 2：探针 ----------
elif stage == "probe":
    if not st.session_state.probes:
        st.subheader("阶段一：缺失信息探针")
        thinking_ph = st.empty()
        content_ph = st.empty()
        try:
            client = make_client(api_key, proxy)
            question = st.session_state.question
            user_msg = f"事件描述：\n{st.session_state.corpus}\n"
            if question and question.strip():
                user_msg += f"\n[用户关注点]：{question.strip()}\n"
            user_msg += "\n请执行阶段一：抽取并回显三要素，然后输出缺失信息探针清单。禁止输出任何结论。"
            thinking, content, fallback = run_phase(
                client,
                model,
                "phase1",
                [{"role": "user", "content": user_msg}],
                thinking_ph,
                content_ph,
            )
            if not content.strip():
                st.error("阶段一未返回内容。请检查 API Key / 网络，或重试。")
                if st.button("🔄 重试"):
                    st.rerun()
                st.stop()
            st.session_state.thinking1 = thinking
            st.session_state.probes = content
            st.session_state.fallback_used = fallback
        except Exception as e:
            st.error(f"阶段一调用失败：{e}")
            if st.button("🔄 重试"):
                st.rerun()
            st.stop()
        st.rerun()

    st.subheader("🔍 缺失信息探针清单")
    st.markdown(st.session_state.probes)
    with st.expander("🧠 阶段一思考过程", expanded=False):
        st.text(st.session_state.thinking1 or "（模型未输出思考过程）")
    if st.session_state.fallback_used:
        st.caption("⏳ 本阶段思考过长，已用成文轮补齐正文。")
    if st.button("➡️ 去补充查证信息", type="primary", use_container_width=True):
        st.session_state.stage = "collect"
        st.rerun()

# ---------- 阶段 3：补充 ----------
elif stage == "collect":
    st.subheader("✏️ 补充查证信息")
    st.info(
        "带着上面的探针去现实世界查证：问当事人、查记录、做实验。"
        "把结果粘贴回来或上传文件，然后生成报告。"
    )
    with st.expander("📋 回顾探针清单", expanded=False):
        st.markdown(st.session_state.probes)
    supp_files = st.file_uploader(
        "📂 上传补充材料 (.txt / .md，可多选)",
        type=["txt", "md"],
        accept_multiple_files=True,
        key="supp_files",
    )
    supp_paste = st.text_area(
        "粘贴查证结果：",
        height=200,
        key="supp_paste",
        placeholder="例如：我查到/问到/验证了……",
    )
    if st.button("🚀 生成终极审计报告", type="primary", use_container_width=True):
        supp = build_supplement(supp_files, supp_paste)
        if not supp.strip():
            supp = "（用户未提供补充信息）"
        st.session_state.supplement = supp
        st.session_state.stage = "report"
        st.rerun()

# ---------- 阶段 4：报告 ----------
elif stage == "report":
    if not st.session_state.report:
        st.subheader("阶段二：终极审计报告")
        thinking_ph = st.empty()
        content_ph = st.empty()
        try:
            client = make_client(api_key, proxy)
            question = st.session_state.question
            event_msg = f"事件描述：\n{st.session_state.corpus}\n"
            if question and question.strip():
                event_msg += f"\n[用户关注点]：{question.strip()}\n"
            messages = [
                {"role": "user", "content": event_msg},
                {"role": "assistant", "content": st.session_state.probes},
                {
                    "role": "user",
                    "content": (
                        f"以下是阶段一的探针清单：\n{st.session_state.probes}\n\n"
                        f"用户补充的信息：\n{st.session_state.supplement}\n\n"
                        "请执行阶段二：先逐项标记每条探针为 [已核实]/[未核实]/[证伪]，"
                        "再按审计模板输出终极审计报告。"
                    ),
                },
            ]
            thinking, content, fallback = run_phase(
                client, model, "phase2", messages, thinking_ph, content_ph
            )
            if not content.strip():
                st.error("阶段二未返回内容。请检查 API Key / 网络，或重试。")
                if st.button("🔄 重试"):
                    st.rerun()
                st.stop()
            st.session_state.thinking2 = thinking
            st.session_state.report = content
            st.session_state.fallback_used = fallback
        except Exception as e:
            st.error(f"阶段二调用失败：{e}")
            if st.button("🔄 重试"):
                st.rerun()
            st.stop()
        st.rerun()

    st.session_state.stage = "result"
    st.rerun()

# ---------- 阶段 5：结果 ----------
elif stage == "result":
    st.success("✅ 审计完成")
    if st.session_state.fallback_used:
        st.caption("⏳ 报告阶段思考过长，已用成文轮补齐正文。")
    tab1, tab2, tab3 = st.tabs(["📄 终极审计报告", "🧠 思考过程", "🔍 探针清单"])
    with tab1:
        st.markdown(st.session_state.report)
        st.download_button(
            "⬇️ 下载报告 (.md)",
            data=st.session_state.report,
            file_name="why-report.md",
            mime="text/markdown",
        )
    with tab2:
        st.markdown("**阶段一思考**")
        st.text(st.session_state.thinking1 or "（无）")
        st.markdown("**阶段二思考**")
        st.text(st.session_state.thinking2 or "（无）")
    with tab3:
        st.markdown(st.session_state.probes)

    st.divider()
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🔄 同一事件再来一次", use_container_width=True):
            st.session_state.probes = ""
            st.session_state.thinking1 = ""
            st.session_state.supplement = ""
            st.session_state.report = ""
            st.session_state.thinking2 = ""
            st.session_state.fallback_used = False
            st.session_state.stage = "probe"
            st.rerun()
    with c2:
        if st.button("🆕 新事件", use_container_width=True):
            for k in DEFAULTS:
                st.session_state[k] = DEFAULTS[k]
            st.rerun()
