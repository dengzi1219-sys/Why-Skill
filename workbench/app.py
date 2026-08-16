"""
Why-Skill 案件工作台（Streamlit）
=================================
把一次分析变成一件可持久化的"案子"：
  案件列表 → 新建案件（事件材料）→ 阶段一探针（待办清单，可勾选查证）
  → 补充查证 → 再出一轮探针 / 直接生成报告 → 报告归档

核心设计：
- 案件 = JSON 文件（saves/cases/<id>.json），刷新不丢、隔天可继续
- 阶段一只输出探针 + 事实卡（结构化压缩）；阶段二只发"事实卡 + 探针状态 + 补充"
- 探针是可勾选待办：待查证 / 已查证 / 证伪，查证结果进入报告的证据强度
- 思考轮 + 成文轮兜底，思考收纳进案件
- 自带 key + 任意 OpenAI 兼容服务商（Base URL + 自定义模型）

运行：python app.py（自动拉起 Streamlit）
"""

import os
import sys
import re
import json
import uuid
from datetime import datetime

import streamlit as st
import httpx
from openai import OpenAI

# 自动启动 Streamlit
if not st.runtime.exists():
    import streamlit.web.cli as stcli
    sys.argv = ["streamlit", "run", sys.argv[0]]
    sys.exit(stcli.main())

# ==========================================
# 常量
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RULES_DIR = os.path.join(BASE_DIR, "rules")
SAVES_DIR = os.path.join(BASE_DIR, "saves", "cases")
GUIDE_PATH = os.path.join(BASE_DIR, "guide.md")
MAX_ROUNDS = 4  # 多轮闭环硬上限，防无限烧钱

PHASE_FILES = {
    "phase1": ["why-meta-engine.md", "why-logic-library.md"],
    "phase2": ["why-logic-library.md", "scalpel-template.md", "audit-report-standard.md"],
}

SYSTEM_SHORT = (
    "你是事件归因分析引擎。严格遵守两阶段流程："
    "阶段一只输出缺失信息探针清单，禁止任何结论；"
    "阶段二才输出终极审计报告。不编造信息，不确定处标注。"
    "思考过程保持简洁，直接输出结论性内容。"
)

# 网页版附加要求（web 层增强，不改 rules/*.md 语义）
WEB_PHASE1_EXTRA = """
## 网页版附加要求（工作台）

1. 在探针清单开头依次输出：【事实卡·三要素】、【事实卡·关键声明】（各方核心说法的原话要点）、【事实卡·物理锚点】（时间/地点/金额/记录，保留具体数值）。
2. 五节探针照常以人类可读 Markdown 输出。
3. 最后必须附加一个 JSON 代码块（`json`），给出探针结构化清单：

```json
{"probes": [{"id": "p1", "text": "探针内容", "section": "物理断层"}]}
```

section 取值只能是：物理断层 / 博弈流程 / 底噪概率 / 查证关键词 / 构造痕迹。
"""


def load_guide() -> str:
    if os.path.exists(GUIDE_PATH):
        with open(GUIDE_PATH, encoding="utf-8") as f:
            return f.read()
    return ""


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


# ==========================================
# API 客户端 + 流式（思考轮 / 成文轮兜底）
# ==========================================
def make_client(api_key: str, base_url: str = "", proxy: str = ""):
    kwargs = {
        "api_key": api_key,
        "base_url": base_url.strip() or "https://api.deepseek.com",
    }
    if proxy and proxy.strip():
        kwargs["http_client"] = httpx.Client(
            proxy=proxy.strip(), timeout=httpx.Timeout(600.0, connect=10.0)
        )
    return OpenAI(**kwargs)


def stream_once(client, model, sys_msg, msgs, thinking_ph, content_ph):
    thinking, content, finish = "", "", None
    last_t, last_c = 0, 0
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
            if len(thinking) - last_t >= 300:
                thinking_ph.markdown(f"🧠 **思考中...**\n\n```text\n{thinking[-4000:]}```")
                last_t = len(thinking)
        c = getattr(d, "content", None)
        if c:
            content += c
            if len(content) - last_c >= 200:
                content_ph.markdown(content + "▌")
                last_c = len(content)
    if content:
        content_ph.markdown(content)
    return thinking, content, finish


def compose_once(client, model, sys_msg, msgs, thinking, content_ph):
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
    rules = load_rules(phase)
    msgs = [{"role": "user", "content": rules + "\n\n" + messages[0]["content"]}] + messages[1:]
    thinking, content, finish = stream_once(client, model, SYSTEM_SHORT, msgs, thinking_ph, content_ph)
    if content.strip() and finish != "length":
        return thinking, content, False
    thinking_ph.markdown("⏳ 第一轮思考过长，正在用思考结果重新成文...")
    content2 = compose_once(client, model, SYSTEM_SHORT, msgs, thinking, content_ph)
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


def build_materials(files, pasted: str) -> str:
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
# 案件持久化
# ==========================================
def new_case_id() -> str:
    return uuid.uuid4().hex[:12]


def new_case(title: str, question: str, materials: str) -> dict:
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "case_id": new_case_id(),
        "title": title or "未命名案件",
        "question": question,
        "materials": materials,
        "created_at": now,
        "updated_at": now,
        "rounds": [],
        "supplements": [],
        "report": {"thinking": "", "content": "", "generated_at": ""},
        "suggest_report": False,
        "archived": False,
        "provider": {"base_url": "", "model": ""},
    }


def case_path(case_id: str) -> str:
    return os.path.join(SAVES_DIR, f"{case_id}.json")


def save_case(case: dict):
    os.makedirs(SAVES_DIR, exist_ok=True)
    case["updated_at"] = datetime.now().isoformat(timespec="seconds")
    with open(case_path(case["case_id"]), "w", encoding="utf-8") as f:
        json.dump(case, f, ensure_ascii=False, indent=2)


def load_case(case_id: str):
    p = case_path(case_id)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def list_cases():
    cases = []
    if os.path.isdir(SAVES_DIR):
        for name in os.listdir(SAVES_DIR):
            if name.endswith(".json"):
                c = load_case(name[:-5])
                if c:
                    rounds = c.get("rounds", [])
                    probes = rounds[-1].get("probes", []) if rounds else []
                    done = sum(1 for p in probes if p.get("status") in ("已查证", "证伪"))
                    cases.append(
                        {
                            "case_id": c["case_id"],
                            "title": c.get("title", "未命名"),
                            "question": c.get("question", ""),
                            "updated_at": c.get("updated_at", ""),
                            "round": len(rounds),
                            "probe_done": done,
                            "probe_total": len(probes),
                            "has_report": bool(c.get("report", {}).get("content")),
                            "archived": c.get("archived", False),
                        }
                    )
    cases.sort(key=lambda x: x["updated_at"], reverse=True)
    return cases


# ==========================================
# 探针解析（JSON 优先，Markdown 启发式兜底）
# ==========================================
SECTION_ALIAS = {
    "物理": "物理断层",
    "博弈": "博弈流程",
    "流程": "博弈流程",
    "底噪": "底噪概率",
    "事故": "底噪概率",
    "关键词": "查证关键词",
    "构造": "构造痕迹",
}


def norm_section(s: str) -> str:
    for k, v in SECTION_ALIAS.items():
        if k in s:
            return v
    return "其他"


def parse_probes(phase1_md: str) -> list:
    probes = []
    m = re.search(r"```json\s*(\{.*?\})\s*```", phase1_md, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            for item in data.get("probes", []):
                if isinstance(item, dict) and str(item.get("text", "")).strip():
                    probes.append(
                        {
                            "id": str(item.get("id") or f"p{len(probes) + 1}"),
                            "text": str(item["text"]).strip(),
                            "section": norm_section(str(item.get("section", "其他"))),
                            "status": "待查证",
                            "finding": "",
                        }
                    )
        except Exception:
            probes = []
    if not probes:
        current = "其他"
        for line in phase1_md.splitlines():
            sm = re.match(r"^#{1,6}\s*(?:\d+[.、]\s*)?([^\n#]+)", line)
            if sm:
                t = sm.group(1)
                if any(k in t for k in ("物理", "博弈", "底噪", "事故", "关键词", "构造", "分析")):
                    current = norm_section(t)
            s = line.strip()
            if s.startswith(("-", "*", "•")) and len(s) > 4:
                probes.append(
                    {
                        "id": f"p{len(probes) + 1}",
                        "text": s.lstrip("-*• ")[:200],
                        "section": current,
                        "status": "待查证",
                        "finding": "",
                    }
                )
    return probes


# ==========================================
# 提示词构建
# ==========================================
def build_phase1_user(case: dict, round_idx: int) -> str:
    base = f"事件描述：\n{case['materials']}\n"
    if case.get("question", "").strip():
        base += f"\n[用户关注点]：{case['question'].strip()}\n"
    if round_idx == 0:
        base += (
            "\n请执行阶段一：抽取三要素，输出事实卡与缺失信息探针清单。禁止输出任何结论。"
            + WEB_PHASE1_EXTRA
        )
    else:
        prev = case["rounds"][round_idx - 1]
        prev_probes = "\n".join(
            f"- {p['id']} [{p.get('status', '待查证')}] {p['text']}"
            + (f" → {p['finding']}" if p.get("finding") else "")
            for p in prev.get("probes", [])
        )
        supplements = "\n\n".join(case.get("supplements", []))
        base += (
            f"\n\n上一轮探针与查证状态：\n{prev_probes}\n"
            f"\n用户补充的信息：\n{supplements}\n"
            "\n请针对最新补充，检查是否仍有重大缺失信息。若有，输出补充事实卡与新探针"
            "（含 JSON 清单）；若没有新缺口，请明确写'未发现新的重大缺口'，JSON 中返回空 probes 数组。"
            + WEB_PHASE1_EXTRA
        )
    return base


def build_phase2_user(case: dict, include_full: bool) -> str:
    latest = case["rounds"][-1]
    status_lines = []
    for p in latest.get("probes", []):
        line = f"- {p['id']} [{p.get('status', '待查证')}] {p['text']}"
        if p.get("finding", "").strip():
            line += f" → 查证结果：{p['finding'].strip()}"
        status_lines.append(line)
    msg = (
        f"### 事件事实卡与探针（第 {len(case['rounds'])} 轮）\n\n"
        f"{latest['phase1_output']}\n\n"
        "### 探针查证状态（用户核实后）\n\n"
        + ("\n".join(status_lines) or "（无探针）")
        + "\n\n### 用户补充信息\n\n"
        + ("\n\n---\n\n".join(case.get("supplements", [])) or "（用户未提供补充）")
    )
    if include_full:
        msg += f"\n\n### 原始事件材料（完整）\n\n{case['materials']}"
    msg += (
        "\n\n请执行阶段二：先核对探针查证状态，再按审计模板输出终极审计报告。"
        "证据强度必须基于查证状态：已查证/证伪的探针结果可作为证据，待查证的一律标为未知或旁证。"
    )
    return msg


# ==========================================
# 页面
# ==========================================
st.set_page_config(page_title="Why-Skill · 案件工作台", page_icon="🗂️", layout="wide")

with st.sidebar:
    st.header("⚙️ 配置")
    api_key = st.text_input(
        "API Key",
        type="password",
        help="DeepSeek 在 platform.deepseek.com 获取；其他 OpenAI 兼容服务商用各自的 Key",
    )
    base_url = st.text_input(
        "API 地址（Base URL）",
        value="https://api.deepseek.com",
        help="OpenAI 兼容接口地址。换服务商改这里。",
    )
    model_choice = st.selectbox(
        "模型",
        ["deepseek-v4-flash", "deepseek-v4-pro", "自定义"],
        help="DeepSeek 预设；选「自定义」可填任意 OpenAI 兼容模型名",
    )
    if model_choice == "自定义":
        model = st.text_input("自定义模型名", placeholder="如 gpt-4o / kimi-k2 / qwen-max")
    else:
        model = model_choice
    proxy = st.text_input(
        "代理地址（可选）",
        placeholder="http://127.0.0.1:7890",
        help="留空直连。Clash 默认 7890。",
    )
    include_full = st.checkbox(
        "完整模式（报告时发送全部原文）",
        value=False,
        help="默认只发事实卡+探针+补充（省 token）；勾选后把全部事件原文也发给模型。",
    )
    st.caption("🔒 Key 只存于本页面会话内存，不落盘、不上传。")
    st.info(
        "🌐 **在线版**：无本地存档，刷新/休眠后案件不保留——请用「导出/导入 JSON」续跑，报告记得下载。\n\n"
        "💻 **本地版**（python app.py）：案件存自己电脑（saves/cases/），可长期跟进。"
    )
    st.divider()
    if st.button("🏠 回案件列表", use_container_width=True):
        st.session_state.page = "list"
        st.session_state.case_id = None
        st.rerun()
    if st.button("⚡ 快速分析（不建档）", use_container_width=True):
        st.session_state.page = "quick"
        st.session_state.case_id = None
        st.rerun()
    if st.button("🧹 重置页面状态", use_container_width=True):
        for k in list(st.session_state.keys()):
            del st.session_state[k]
        st.rerun()
    st.divider()
    with st.expander("📖 使用攻略", expanded=False):
        guide = load_guide()
        if guide.strip():
            st.markdown(guide)
        else:
            st.caption("还没有攻略。展开下方「编辑使用攻略」粘贴内容（支持 Markdown）。")
    with st.expander("✏️ 编辑使用攻略", expanded=False):
        gv = st.text_area("攻略内容（Markdown）", value=load_guide(), height=220, key="guide_edit")
        if st.button("💾 保存攻略到 guide.md"):
            with open(GUIDE_PATH, "w", encoding="utf-8") as f:
                f.write(gv)
            st.success("已保存到 workbench/guide.md")
            st.rerun()

st.title("🗂️ Why-Skill · 案件工作台")
st.caption("真相永远在表层叙述之外。每个案件 = 探针待办 + 现实查证 + 审计报告，刷新不丢。")

st.session_state.setdefault("page", "list")
st.session_state.setdefault("case_id", None)
st.session_state.setdefault("quick_materials", "")
st.session_state.setdefault("quick_question", "")
st.session_state.setdefault("quick_probes", "")
st.session_state.setdefault("quick_thinking1", "")
st.session_state.setdefault("quick_supplement", "")
st.session_state.setdefault("quick_report", "")
st.session_state.setdefault("quick_thinking2", "")

page = st.session_state.page
case = load_case(st.session_state.case_id) if st.session_state.case_id else None


# ---------- 案件列表 ----------
if page == "list" or (page == "case" and case is None):
    st.session_state.page = "list"
    st.subheader("📁 我的案件")
    c1, c2 = st.columns([3, 1])
    with c1:
        st.caption("案件按最近更新时间排序。关闭浏览器后回来，案件和探针状态都还在。")
    with c2:
        if st.button("➕ 新建案件", type="primary", use_container_width=True):
            st.session_state.page = "new"
            st.rerun()

    with st.expander("📥 导入案件（断点续跑）"):
        imp = st.file_uploader(
            "上传之前导出的案件 JSON（含事件材料/探针/查证状态/报告）",
            type=["json"],
            key="import_file",
        )
        if st.button("📥 导入并继续"):
            if imp is None:
                st.warning("请先选择 JSON 文件")
            else:
                try:
                    data = json.loads(imp.read().decode("utf-8"))
                    cid = data.get("case_id")
                    if not cid or "rounds" not in data:
                        st.error("不是有效的案件 JSON（缺少 case_id / rounds）")
                    else:
                        save_case(data)
                        st.session_state.case_id = cid
                        st.session_state.page = "case"
                        st.rerun()
                except Exception as e:
                    st.error(f"导入失败：{e}")

    cases = list_cases()
    active = [c for c in cases if not c["archived"]]
    archived = [c for c in cases if c["archived"]]
    if not active:
        st.info("还没有案件。点击「新建案件」开始第一件。")
    for c in active:
        progress = f"{c['probe_done']}/{c['probe_total']} 已查证" if c["probe_total"] else "待生成探针"
        with st.container(border=True):
            cols = st.columns([3, 1, 1, 1, 1])
            cols[0].markdown(f"**{c['title']}**\n\n{c['question'] or '_无问题_'}")
            cols[1].markdown(f"第 {c['round']} 轮")
            cols[2].markdown(progress)
            cols[3].markdown("✅ 有报告" if c["has_report"] else "⏳ 未完成")
            if cols[4].button("打开", key=f"open_{c['case_id']}"):
                st.session_state.case_id = c["case_id"]
                st.session_state.page = "case"
                st.rerun()
    if archived:
        with st.expander(f"🗄️ 已归档（{len(archived)}）"):
            for c in archived:
                if st.button(f"{c['title']}（第{c['round']}轮）", key=f"arch_{c['case_id']}"):
                    st.session_state.case_id = c["case_id"]
                    st.session_state.page = "case"
                    st.rerun()


# ---------- 新建案件 ----------
elif page == "new":
    st.subheader("➕ 新建案件")
    title = st.text_input("案件标题", placeholder="如：麓湖转场 / 展会取消 / 宿舍冲突")
    question = st.text_input("你想搞清楚的问题（可选）", placeholder="如：她为什么死守麓湖？")
    uploaded = st.file_uploader(
        "📂 上传事件材料 (.txt / .md，可多选)",
        type=["txt", "md"],
        accept_multiple_files=True,
    )
    pasted = st.text_area(
        "或粘贴事件描述 / 聊天记录：",
        height=220,
        placeholder="把事件材料粘贴在这里...（可同时上传文件和粘贴文本，会合并）",
    )
    materials = build_materials(uploaded, pasted)
    if materials.strip():
        st.caption(f"📊 材料总计：{len(materials)} 字符")
    if st.button("📁 创建案件并开始", type="primary", use_container_width=True):
        if not api_key:
            st.error("请先在左侧填入 API Key")
        elif not materials.strip():
            st.error("请上传文件或粘贴事件材料")
        else:
            c = new_case(title, question, materials)
            save_case(c)
            st.session_state.case_id = c["case_id"]
            st.session_state.page = "case"
            st.rerun()


# ---------- 快速分析（不建档）----------
elif page == "quick":
    st.subheader("⚡ 快速分析（不建档）")
    st.caption("一次性使用：结果不保存，报告记得下载。需要长期跟进请回到案件工作台。")

    if not st.session_state.quick_probes:
        q_files = st.file_uploader(
            "📂 上传事件材料 (.txt / .md，可多选)",
            type=["txt", "md"],
            accept_multiple_files=True,
            key="q_files",
        )
        q_paste = st.text_area("或粘贴事件描述 / 聊天记录：", height=200, key="q_paste")
        q_q = st.text_input("你想搞清楚的问题（可选）", key="q_q")
        if st.button("🔍 开始探针分析", type="primary", use_container_width=True):
            materials = build_materials(q_files, q_paste)
            if not api_key:
                st.error("请先在左侧填入 API Key")
            elif not materials.strip():
                st.error("请上传文件或粘贴事件材料")
            else:
                t_ph = st.empty()
                c_ph = st.empty()
                try:
                    client = make_client(api_key, base_url, proxy)
                    case_tmp = {"materials": materials, "question": q_q, "rounds": [], "supplements": []}
                    thinking, content, _fb = run_phase(
                        client, model, "phase1",
                        [{"role": "user", "content": build_phase1_user(case_tmp, 0)}],
                        t_ph, c_ph,
                    )
                    if not content.strip():
                        st.error("阶段一未返回内容，请检查 Key/网络后重试。")
                        st.stop()
                    st.session_state.quick_materials = materials
                    st.session_state.quick_question = q_q
                    st.session_state.quick_thinking1 = thinking
                    st.session_state.quick_probes = content
                    st.rerun()
                except Exception as e:
                    st.error(f"调用失败：{e}")
        st.stop()

    st.markdown("### 🔍 缺失信息探针清单")
    st.markdown(st.session_state.quick_probes)
    with st.expander("🧠 思考过程", expanded=False):
        st.text(st.session_state.quick_thinking1 or "（无思考输出）")

    st.divider()
    if not st.session_state.quick_report:
        st.markdown("### ✏️ 补充查证信息")
        s_files = st.file_uploader(
            "📂 上传补充材料 (.txt / .md)",
            type=["txt", "md"],
            accept_multiple_files=True,
            key="qs_files",
        )
        s_paste = st.text_area("粘贴查证结果：", height=140, key="qs_paste")
        if st.button("🚀 生成终极审计报告", type="primary", use_container_width=True):
            supp = build_supplement(s_files, s_paste)
            if not supp.strip():
                supp = "（用户未提供补充信息）"
            t_ph = st.empty()
            c_ph = st.empty()
            try:
                client = make_client(api_key, base_url, proxy)
                case_tmp = {
                    "materials": st.session_state.quick_materials,
                    "question": st.session_state.quick_question,
                    "rounds": [
                        {
                            "phase1_output": st.session_state.quick_probes,
                            "probes": parse_probes(st.session_state.quick_probes),
                        }
                    ],
                    "supplements": [supp],
                }
                thinking, content, fb = run_phase(
                    client, model, "phase2",
                    [{"role": "user", "content": build_phase2_user(case_tmp, include_full)}],
                    t_ph, c_ph,
                )
                if not content.strip():
                    st.error("报告未返回内容，请重试。")
                    st.stop()
                st.session_state.quick_supplement = supp
                st.session_state.quick_thinking2 = thinking
                st.session_state.quick_report = content
                st.rerun()
            except Exception as e:
                st.error(f"报告生成失败：{e}")
        st.stop()

    st.markdown("### 📄 终极审计报告")
    st.markdown(st.session_state.quick_report)
    st.download_button(
        "⬇️ 下载报告 (.md)",
        data=st.session_state.quick_report,
        file_name="why-report.md",
        mime="text/markdown",
    )
    with st.expander("🧠 报告阶段思考过程", expanded=False):
        st.text(st.session_state.quick_thinking2 or "（无思考输出）")
    if st.button("🔄 再来一次", use_container_width=True):
        for k in ("quick_materials", "quick_question", "quick_probes", "quick_thinking1",
                  "quick_supplement", "quick_report", "quick_thinking2"):
            st.session_state[k] = ""
        st.rerun()


# ---------- 案件详情 ----------
elif page == "case" and case is not None:
    rounds = case["rounds"]
    latest = rounds[-1] if rounds else None

    st.subheader(f"📂 {case['title']}")
    overview = st.columns(5)
    overview[0].metric("轮次", len(rounds))
    if latest:
        done = sum(1 for p in latest.get("probes", []) if p.get("status") in ("已查证", "证伪"))
        overview[1].metric("探针查证", f"{done}/{len(latest.get('probes', []))}")
    else:
        overview[1].metric("探针查证", "0/0")
    overview[2].metric("材料字符", len(case.get("materials", "")))
    overview[3].metric("补充轮次", len(case.get("supplements", [])))
    overview[4].metric("报告", "✅" if case.get("report", {}).get("content") else "—")

    # ---- 第一阶段（首轮）----
    if not rounds:
        st.info("案件已创建。点击下面按钮生成第一轮探针。")
        if st.button("🔍 生成第一轮探针", type="primary"):
            if not api_key:
                st.error("请先在左侧填入 API Key")
            else:
                t_ph = st.empty()
                c_ph = st.empty()
                try:
                    client = make_client(api_key, base_url, proxy)
                    thinking, content, fallback = run_phase(
                        client, model, "phase1",
                        [{"role": "user", "content": build_phase1_user(case, 0)}],
                        t_ph, c_ph,
                    )
                    if not content.strip():
                        st.error("阶段一未返回内容，请检查 Key/网络后重试。")
                        st.stop()
                    round_data = {
                        "round": 1,
                        "phase1_thinking": thinking,
                        "phase1_output": content,
                        "probes": parse_probes(content),
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                    }
                    case["rounds"].append(round_data)
                    case["provider"] = {"base_url": base_url, "model": model}
                    save_case(case)
                    st.rerun()
                except Exception as e:
                    st.error(f"阶段一调用失败：{e}")
        st.stop()

    # ---- 探针工作区（最新一轮）----
    st.markdown("### 🔍 探针待办清单")
    sections = ["物理断层", "博弈流程", "底噪概率", "查证关键词", "构造痕迹"]
    groups = {s: [p for p in latest.get("probes", []) if p.get("section") == s] for s in sections}
    groups["其他"] = [p for p in latest.get("probes", []) if p.get("section") not in sections]

    probe_count = len(latest.get("probes", []))
    if probe_count == 0:
        st.info("本轮未解析出可勾选探针（可能模型未输出 JSON 清单）。可在下方展开查看原始探针文本。")

    for section in sections + ["其他"]:
        items = groups.get(section, [])
        if not items:
            continue
        with st.expander(f"📌 {section}（{len(items)}）", expanded=True):
            for p in items:
                pid = p["id"]
                cols = st.columns([3, 1, 4])
                cols[0].markdown(p["text"])
                status_key = f"st_{case['case_id']}_{pid}"
                finding_key = f"fd_{case['case_id']}_{pid}"
                idx = ["待查证", "已查证", "证伪"].index(p.get("status", "待查证"))
                cols[1].selectbox(
                    "状态", ["待查证", "已查证", "证伪"], index=idx, key=status_key, label_visibility="collapsed"
                )
                cols[2].text_input("查证结果", value=p.get("finding", ""), key=finding_key, label_visibility="collapsed")

    c_save, c_export = st.columns([1, 1])
    with c_save:
        if st.button("💾 保存查证状态", type="primary", use_container_width=True):
            for p in latest.get("probes", []):
                pid = p["id"]
                p["status"] = st.session_state.get(f"st_{case['case_id']}_{pid}", "待查证")
                p["finding"] = st.session_state.get(f"fd_{case['case_id']}_{pid}", "").strip()
            save_case(case)
            st.success("已保存")
            st.rerun()
    with c_export:
        probes_json = json.dumps(latest.get("probes", []), ensure_ascii=False, indent=2)
        st.download_button(
            "⬇️ 导出探针清单 (json)",
            data=probes_json,
            file_name=f"{case['title']}_探针.json",
            mime="application/json",
            use_container_width=True,
        )

    # 原始探针文本 + 思考抽屉
    with st.expander("🧠 本轮思考过程", expanded=False):
        st.text(latest.get("phase1_thinking") or "（无思考输出）")
    with st.expander("📄 本轮原始探针输出（含事实卡）", expanded=False):
        st.markdown(latest.get("phase1_output", ""))

    # ---- 补充与多轮 ----
    st.divider()
    st.markdown("### ✏️ 补充查证信息")
    st.info("带着探针去现实世界查证（问人/查记录/做实验），把结果粘贴回来或上传文件。")
    supp_files = st.file_uploader(
        "📂 上传补充材料 (.txt / .md，可多选)",
        type=["txt", "md"],
        accept_multiple_files=True,
        key="supp_files",
    )
    supp_paste = st.text_area("粘贴查证结果：", height=160, key="supp_paste")

    can_reprobe = len(rounds) < MAX_ROUNDS and not case.get("suggest_report", False)
    b1, b2 = st.columns(2)
    with b1:
        reprobe_btn = st.button(
            "🔄 再出一轮探针",
            disabled=not can_reprobe,
            use_container_width=True,
        )
    with b2:
        report_btn = st.button("🚀 直接生成报告", type="primary", use_container_width=True)

    if not can_reprobe:
        reason = "已达最大轮数" if len(rounds) >= MAX_ROUNDS else "上一轮未发现新缺口，建议直接出报告"
        st.caption(f"⏸️ {reason}（可直接生成报告）")

    # ---- 执行：再出一轮 ----
    if reprobe_btn:
        supp = build_supplement(supp_files, supp_paste)
        if not supp.strip():
            st.warning("请先输入补充信息，或直接点「生成报告」。")
            st.stop()
        t_ph = st.empty()
        c_ph = st.empty()
        try:
            client = make_client(api_key, base_url, proxy)
            thinking, content, fallback = run_phase(
                client, model, "phase1",
                [{"role": "user", "content": build_phase1_user(case, len(rounds))}],
                t_ph, c_ph,
            )
            if not content.strip():
                st.error("本轮未返回内容，请重试。")
                st.stop()
            new_probes = parse_probes(content)
            prev_texts = {p["text"] for r in rounds for p in r.get("probes", [])}
            new_count = sum(1 for p in new_probes if p["text"] not in prev_texts)
            round_data = {
                "round": len(rounds) + 1,
                "phase1_thinking": thinking,
                "phase1_output": content,
                "probes": new_probes,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "new_probe_count": new_count,
            }
            case["rounds"].append(round_data)
            case["supplements"].append(supp)
            case["suggest_report"] = new_count == 0
            save_case(case)
            st.rerun()
        except Exception as e:
            st.error(f"本轮调用失败：{e}")

    # ---- 执行：生成报告 ----
    if report_btn:
        supp = build_supplement(supp_files, supp_paste)
        if supp.strip():
            case["supplements"].append(supp)
        t_ph = st.empty()
        c_ph = st.empty()
        try:
            client = make_client(api_key, base_url, proxy)
            thinking, content, fallback = run_phase(
                client, model, "phase2",
                [{"role": "user", "content": build_phase2_user(case, include_full)}],
                t_ph, c_ph,
            )
            if not content.strip():
                st.error("报告未返回内容，请重试。")
                st.stop()
            case["report"] = {
                "thinking": thinking,
                "content": content,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "fallback_used": fallback,
            }
            save_case(case)
            st.rerun()
        except Exception as e:
            st.error(f"报告生成失败：{e}")

    # ---- 报告区 ----
    report = case.get("report", {})
    if report.get("content"):
        st.divider()
        st.markdown("### 📄 终极审计报告")
        if report.get("fallback_used"):
            st.caption("⏳ 思考过长，已用成文轮补齐正文。")
        st.markdown(report["content"])
        st.download_button(
            "⬇️ 下载报告 (.md)",
            data=report["content"],
            file_name=f"{case['title']}_审计报告.md",
            mime="text/markdown",
        )
        with st.expander("🧠 报告阶段思考过程", expanded=False):
            st.text(report.get("thinking") or "（无思考输出）")

    # ---- 危险区 ----
    st.divider()
    with st.expander("🗑️ 危险操作"):
        cc = st.columns(2)
        with cc[0]:
            if st.button("🗄️ 归档案件", use_container_width=True):
                case["archived"] = True
                save_case(case)
                st.rerun()
        with cc[1]:
            confirm = st.checkbox("确认删除此案件（不可恢复）")
            if st.button("🗑️ 删除案件", disabled=not confirm, use_container_width=True):
                p = case_path(case["case_id"])
                if os.path.exists(p):
                    os.remove(p)
                st.session_state.case_id = None
                st.session_state.page = "list"
                st.rerun()
