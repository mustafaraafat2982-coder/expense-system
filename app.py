"""
نظامي المالي الشخصي - الإصدار 3.1
تشغيل: py -m streamlit run app.py
"""
import calendar
import html
import io
import sqlite3
import threading
from datetime import date, datetime

import altair as alt
import pandas as pd
import streamlit as st

# ==========================================================
# 0. الإعدادات والثوابت
# ==========================================================
st.set_page_config(page_title="نظامي المالي الشخصي", page_icon="💰",
                   layout="wide", initial_sidebar_state="collapsed")

DB_PATH = "personal_finance.db"
CUR = "ج.م"
MONTHS_AR = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
             "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]

# التصنيف: (أيقونة، نوع) — need = احتياج، want = رغبة
CAT_META = {
    "طعام وشراب": ("🍽️", "need"), "مواصلات": ("🚌", "need"), "فواتير": ("🧾", "need"),
    "صحة": ("🩺", "need"), "تعليم": ("🎓", "need"),
    "ملابس": ("👕", "want"), "ترفيه": ("🎬", "want"), "أخرى": ("📦", "want"),
}
DOC_KINDS = ["فاتورة", "رسالة بنك", "معاملة إلكترونية", "إيصال", "أخرى"]
FILE_TYPES = ["png", "jpg", "jpeg", "webp", "pdf"]
FIN_KINDS = ["مدخرات", "حساب بنكي", "كاش", "ذهب", "استثمارات", "دين لي", "دين عليّ", "أخرى"]
LIABILITY = "دين عليّ"
INCOME_SOURCES = ["راتب", "دخل إضافي", "مكافأة", "حوالة", "أخرى"]
WISH_KINDS = ["احتياج ضروري", "رغبة ترفيهية", "استثمار/تطوير"]
NUM = "%g"  # عرض الأرقام بدون أصفار زائدة

PAGES = {
    "dashboard": "📊 لوحة التحكم", "expenses": "💸 المصاريف", "monthly": "📅 الدخل الشهري",
    "budgets": "🎯 الميزانيات والأهداف", "wishes": "✨ الأمنيات", "settings": "⚙️ الإعدادات",
}

# هوية الألوان: خلفية بيج، نص بني غامق، أخضر للدخل، مرجاني للمصروف
C_PRIMARY, C_DEEP, C_INCOME = "#5a3e2b", "#3b2a1f", "#2f7d5b"
C_EXPENSE, C_ACCENT, C_INK, C_MUTED = "#c8465a", "#b9791f", "#3b2a1f", "#7a6552"
C_BG, C_CARD, C_LINE = "#f5ede0", "#fffaf2", "#e3d6c1"

# ==========================================================
# 1. قاعدة البيانات
# ==========================================================
_lock = threading.Lock()

@st.cache_resource
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS monthly_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT, year INTEGER NOT NULL, month INTEGER NOT NULL,
            basic_salary REAL DEFAULT 0, cash_income REAL DEFAULT 0,
            opening_balance REAL DEFAULT 0, carry_note TEXT, UNIQUE(year, month));
        CREATE TABLE IF NOT EXISTS daily_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT, year INTEGER NOT NULL, month INTEGER NOT NULL,
            day INTEGER NOT NULL, expense_date DATE NOT NULL, amount REAL NOT NULL,
            details TEXT, category TEXT);
        CREATE TABLE IF NOT EXISTS budgets (category TEXT PRIMARY KEY, monthly_limit REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            target REAL NOT NULL, saved REAL NOT NULL DEFAULT 0, created_at TEXT);
        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            expense_id INTEGER REFERENCES daily_expenses(id) ON DELETE CASCADE,
            kind TEXT, name TEXT, mime TEXT, data BLOB, note TEXT, doc_date TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS incomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, amount REAL NOT NULL, for_year INTEGER NOT NULL,
            for_month INTEGER NOT NULL, received_date TEXT NOT NULL, source TEXT, note TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS fin_details (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, name TEXT,
            amount REAL NOT NULL DEFAULT 0, note TEXT);
        CREATE TABLE IF NOT EXISTS wishes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT, price REAL NOT NULL,
            saved REAL NOT NULL DEFAULT 0, target_date TEXT, note TEXT,
            status TEXT NOT NULL DEFAULT 'active', created_at TEXT, bought_at TEXT);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE INDEX IF NOT EXISTS idx_inc_ym ON incomes(for_year, for_month);
        CREATE INDEX IF NOT EXISTS idx_exp_ym ON daily_expenses(year, month);
        CREATE INDEX IF NOT EXISTS idx_exp_cat ON daily_expenses(category);
        CREATE INDEX IF NOT EXISTS idx_att_exp ON attachments(expense_id);
    """)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(monthly_data)")]
    if "carry_note" not in cols:
        conn.execute("ALTER TABLE monthly_data ADD COLUMN carry_note TEXT")
    conn.commit()
    return conn

def query_df(sql: str, params: tuple = ()) -> pd.DataFrame:
    with _lock:
        return pd.read_sql_query(sql, get_conn(), params=params)

def run(sql: str, params: tuple = ()) -> int:
    with _lock:
        conn = get_conn()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.lastrowid
        except Exception:
            conn.rollback()
            raise

def run_many(statements: list) -> None:
    with _lock:
        conn = get_conn()
        try:
            for sql, params in statements:
                conn.execute(sql, params)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

def get_setting(key: str, default: str = "") -> str:
    df = query_df("SELECT value FROM settings WHERE key=?", (key,))
    return df.value[0] if not df.empty and df.value[0] is not None else default

def set_setting(key: str, value: str) -> None:
    run("INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value))

# ==========================================================
# 2. الحسابات
# ==========================================================
def prev_ym(year: int, month: int) -> tuple:
    return (year, month - 1) if month > 1 else (year - 1, 12)

def month_summary(year: int, month: int) -> dict:
    m = query_df("SELECT basic_salary, cash_income, opening_balance, carry_note FROM monthly_data "
                 "WHERE year=? AND month=?", (year, month))
    e = query_df("SELECT COALESCE(SUM(amount),0) AS s, COUNT(*) AS n FROM daily_expenses "
                 "WHERE year=? AND month=?", (year, month))
    r = query_df("SELECT COALESCE(SUM(amount),0) AS s, COUNT(*) AS n FROM incomes "
                 "WHERE for_year=? AND for_month=?", (year, month))
    ex = not m.empty
    salary = float(m.basic_salary[0] or 0) if ex else 0.0
    cash = float(m.cash_income[0] or 0) if ex else 0.0
    opening = float(m.opening_balance[0] or 0) if ex else 0.0
    note = (m.carry_note[0] or "") if ex else ""
    received, rn, exp = float(r.s[0]), int(r.n[0]), float(e.s[0])
    income = salary + cash + received
    saving = income - exp
    return {"exists": ex, "salary": salary, "cash": cash, "received": received, "received_n": rn,
            "income": income, "opening": opening, "carry_note": note, "expenses": exp,
            "count": int(e.n[0]), "saving": saving,
            "rate": (saving / income * 100) if income > 0 else 0.0,
            "closing": opening + saving, "active": ex or income > 0 or exp > 0}

def year_summary(year: int) -> pd.DataFrame:
    inc = query_df("SELECT month, COALESCE(basic_salary,0) + COALESCE(cash_income,0) AS income, "
                   "COALESCE(opening_balance,0) AS opening FROM monthly_data WHERE year=?", (year,))
    exp = query_df("SELECT month, SUM(amount) AS expenses FROM daily_expenses WHERE year=? GROUP BY month", (year,))
    rec = query_df("SELECT for_month AS month, SUM(amount) AS received FROM incomes "
                   "WHERE for_year=? GROUP BY for_month", (year,))
    df = (pd.DataFrame({"month": range(1, 13)}).merge(inc, on="month", how="left")
          .merge(exp, on="month", how="left").merge(rec, on="month", how="left"))
    df = df.fillna({"income": 0.0, "opening": 0.0, "expenses": 0.0, "received": 0.0})
    df["income"] += df["received"]
    df["name"] = df["month"].map(lambda i: MONTHS_AR[i - 1])
    df["saving"] = df["income"] - df["expenses"]
    df["has_data"] = (df["income"] > 0) | (df["expenses"] > 0) | (df["opening"] > 0)
    return df

def category_totals(year: int, month=None) -> pd.DataFrame:
    base = ("SELECT COALESCE(NULLIF(category,''),'أخرى') AS category, SUM(amount) AS amount "
            "FROM daily_expenses WHERE year=? ")
    if month:
        return query_df(base + "AND month=? GROUP BY 1 ORDER BY 2 DESC", (year, month))
    return query_df(base + "GROUP BY 1 ORDER BY 2 DESC", (year,))

def get_budgets() -> dict:
    df = query_df("SELECT category, monthly_limit FROM budgets")
    return dict(zip(df.category, df.monthly_limit))

def all_categories() -> list:
    used = query_df("SELECT DISTINCT category FROM daily_expenses "
                    "WHERE category IS NOT NULL AND category<>''").category.tolist()
    return list(CAT_META) + [c for c in used if c not in CAT_META]

def cat_label(c: str) -> str:
    return f"{CAT_META.get(c, ('🏷️', ''))[0]} {c}"

def fin_totals():
    df = query_df("SELECT kind, name, amount, note FROM fin_details ORDER BY id")
    if df.empty:
        return df, 0.0, 0.0
    liab = float(df[df.kind == LIABILITY].amount.sum())
    return df, float(df.amount.sum()) - liab, liab

def month_insights(year: int, month: int, s: dict, prev: dict) -> dict:
    """مؤشرات ذكية: متوسط يومي، توقع نهاية الشهر، أعلى تصنيف، التغير عن الشهر السابق."""
    today = date.today()
    days_in = calendar.monthrange(year, month)[1]
    is_current = (year, month) == (today.year, today.month)
    elapsed = today.day if is_current else days_in
    avg = s["expenses"] / elapsed if elapsed else 0.0
    cats = category_totals(year, month)
    top = (cats.category[0], float(cats.amount[0])) if not cats.empty else None
    change = ((s["expenses"] - prev["expenses"]) / prev["expenses"] * 100) if prev["expenses"] > 0 else None
    needs = sum(float(a) for c, a in zip(cats.category, cats.amount) if CAT_META.get(c, ("", "want"))[1] == "need")
    return {"avg": avg, "projected": avg * days_in if is_current else s["expenses"], "is_current": is_current,
            "top": top, "change": change,
            "needs_pct": (needs / s["expenses"] * 100) if s["expenses"] > 0 else 0.0}

# ==========================================================
# 3. المرفقات
# ==========================================================
def prepare_file(f) -> tuple:
    raw, mime, name = f.getvalue(), (f.type or "application/octet-stream"), f.name
    if mime.startswith("image/"):
        try:
            from PIL import Image, ImageOps
            im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
            im.thumbnail((1800, 1800))
            if im.mode != "RGB":
                im = im.convert("RGB")
            out = io.BytesIO()
            im.save(out, "JPEG", quality=85)
            raw, mime = out.getvalue(), "image/jpeg"
        except Exception:
            pass
    return name, mime, raw

def save_attachments(files, kind: str, expense_id, doc_date: str, note: str = "") -> int:
    stmts = []
    for f in files or []:
        name, mime, raw = prepare_file(f)
        stmts.append(("INSERT INTO attachments(expense_id, kind, name, mime, data, note, doc_date, created_at) "
                      "VALUES(?,?,?,?,?,?,?,?)",
                      (expense_id, kind, name, mime, raw, note, doc_date,
                       datetime.now().isoformat(timespec="seconds"))))
    if stmts:
        run_many(stmts)
    return len(stmts)

def show_attachment(att_id: int, width: int = 240) -> None:
    df = query_df("SELECT name, mime, data FROM attachments WHERE id=?", (att_id,))
    if df.empty:
        return
    name, mime, data = df.name[0], df.mime[0], df.data[0]
    if (mime or "").startswith("image/"):
        st.image(data, width=width)
    else:
        st.download_button(f"📄 {name}", data=data, file_name=name, mime=mime, key=f"dl_{att_id}")

# ==========================================================
# 4. التصميم والمكونات المشتركة
# ==========================================================
def fmt(x: float) -> str:
    return f"{x:,.0f} {CUR}"

def esc(x) -> str:
    return html.escape(str(x))

def num(label: str, value=None, step: float = 100.0, **kw):
    """حقل رقم فارغ افتراضيًا (بدون 0.00). يرجع 0.0 لو ترك فارغًا."""
    v = st.number_input(label, min_value=0.0, value=value, step=step, format=NUM, **kw)
    return float(v or 0.0)

def inject_css() -> None:
    st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;800&display=swap');
:root {{ --primary:{C_PRIMARY}; --deep:{C_DEEP}; --ink:{C_INK}; --muted:{C_MUTED}; --bg:{C_BG};
        --card:{C_CARD}; --line:{C_LINE}; --accent:{C_ACCENT}; --bad:{C_EXPENSE}; --good:{C_INCOME}; }}
.stApp, .stApp p, .stApp label, .stApp input, .stApp textarea, .stApp button,
.stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp [data-baseweb] {{ font-family:'Cairo',sans-serif !important; }}
.stApp {{ direction:rtl; background:var(--bg); color:var(--ink); font-size:17px; color-scheme:light; }}
.stApp p, .stApp label, .stApp span, .stApp h1, .stApp h2, .stApp h3, .stApp h4,
.stApp li, .stApp [data-testid="stCaptionContainer"] {{ color:var(--ink); }}
.block-container {{ max-width:1320px; padding-top:1.2rem; padding-bottom:4rem; }}
header[data-testid="stHeader"], section[data-testid="stSidebar"], footer {{ display:none !important; }}
h1,h2,h3,h4,p,label,.stMarkdown {{ text-align:right; }}

/* الحقول: خلفية فاتحة ونص بني، وبدون أزرار +/- */
div[data-baseweb="input"], div[data-baseweb="base-input"], div[data-baseweb="select"] > div,
div[data-baseweb="textarea"], div[data-testid="stDateInput"] div[data-baseweb="input"] {{
    background:var(--card) !important; border-color:var(--line) !important; border-radius:12px !important; }}
.stApp input, .stApp textarea {{ background:var(--card) !important; color:var(--ink) !important;
    -webkit-text-fill-color:var(--ink) !important; font-size:17px !important; }}
button[data-testid="stNumberInputStepUp"], button[data-testid="stNumberInputStepDown"] {{ display:none !important; }}
div[data-testid="stNumberInput"] > div {{ background:var(--card) !important; border-radius:12px; }}

/* شريط التنقل: أقراص أفقية */
div[data-testid="stRadio"] > div[role="radiogroup"] {{
    display:flex; flex-wrap:wrap; gap:8px; justify-content:center;
    background:var(--card); padding:8px; border-radius:16px; border:1px solid var(--line);
    box-shadow:0 2px 10px rgba(59,42,31,.07); }}
div[data-testid="stRadio"] label[data-baseweb="radio"] {{
    padding:9px 18px; border-radius:12px; cursor:pointer; margin:0; transition:background .15s; }}
div[data-testid="stRadio"] label[data-baseweb="radio"] > div:first-of-type {{ display:none !important; }}
div[data-testid="stRadio"] label[data-baseweb="radio"] p {{ color:var(--ink) !important; opacity:1 !important;
    font-weight:700; font-size:16px; margin:0; }}
div[data-testid="stRadio"] label[data-baseweb="radio"]:hover {{ background:#efe3cf; }}
div[data-testid="stRadio"] label[data-baseweb="radio"]:has(input:checked) {{ background:var(--deep); }}
div[data-testid="stRadio"] label[data-baseweb="radio"]:has(input:checked) p {{ color:#fff !important; }}

/* ترويسة الصفحة */
.app-header {{ background:linear-gradient(120deg,var(--deep),#6b4a33); color:#fff;
    padding:26px 32px; border-radius:20px; margin:18px 0 22px; }}
.app-header h1 {{ margin:0; font-size:2rem; font-weight:800; color:#fff; }}
.app-header p {{ margin:6px 0 0; opacity:.88; font-size:1.05rem; color:#fff; }}

/* بطاقات المؤشرات */
.kpi {{ background:var(--card); border:1px solid var(--line); border-radius:16px; padding:16px 20px;
    margin-bottom:12px; height:calc(100% - 12px); position:relative; overflow:hidden; }}
.kpi::before {{ content:""; position:absolute; top:0; right:0; width:5px; height:100%; background:var(--primary); }}
.kpi.good::before {{ background:var(--good); }} .kpi.bad::before {{ background:var(--bad); }}
.kpi.warn::before {{ background:var(--accent); }}
.kpi-label {{ font-size:.95rem; color:var(--muted); font-weight:600; }}
.kpi-value {{ font-size:1.7rem; font-weight:800; line-height:1.5; color:var(--ink); }}
.kpi-sub {{ font-size:.88rem; color:var(--muted); min-height:1.2em; }}
.kpi.main {{ background:linear-gradient(135deg,var(--deep),#6b4a33); border:none; }}
.kpi.main::before {{ display:none; }}
.kpi.main .kpi-label, .kpi.main .kpi-value, .kpi.main .kpi-sub {{ color:#fff; }}
.kpi.main .kpi-value {{ font-size:2.1rem; }}

.section-title {{ font-weight:800; font-size:1.3rem; margin:26px 0 12px; padding-right:12px;
    border-right:4px solid var(--accent); color:var(--ink); }}

/* شريط التقدم */
.bar-row {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:12px 16px; margin-bottom:10px; }}
.bar-head {{ display:flex; justify-content:space-between; font-weight:700; margin-bottom:8px; color:var(--ink); }}
.bar-head span:last-child {{ color:var(--muted); font-weight:600; font-size:.92rem; }}
.bar-track {{ background:#eadfcb; border-radius:99px; height:10px; overflow:hidden; }}
.bar-fill {{ height:100%; border-radius:99px; }}

.tip {{ background:#fbf0d9; border:1px solid #ecd49b; border-right:5px solid var(--accent);
    border-radius:14px; padding:12px 16px; margin:8px 0; color:var(--ink); }}

.stButton > button, .stDownloadButton > button {{ border-radius:12px; font-weight:700; padding:8px 22px; }}
div[data-testid="stForm"], div[data-testid="stExpander"], div[data-testid="stVerticalBlockBorderWrapper"] {{
    background:var(--card); border-radius:16px; border-color:var(--line); }}
@media (max-width:760px) {{ .kpi-value {{ font-size:1.35rem; }} .app-header {{ padding:18px; }} }}
</style>""", unsafe_allow_html=True)

def render_top_nav() -> None:
    if st.session_state.get("current_page") not in PAGES:
        st.session_state["current_page"] = "dashboard"
    st.radio("التنقل", list(PAGES), format_func=PAGES.get, key="current_page",
             horizontal=True, label_visibility="collapsed")

def page_header(title: str, subtitle: str = "") -> None:
    sub = f"<p>{esc(subtitle)}</p>" if subtitle else ""
    st.markdown(f'<div class="app-header"><h1>{esc(title)}</h1>{sub}</div>', unsafe_allow_html=True)

def kpi(label: str, value: str, sub: str = "", tone: str = "") -> None:
    st.markdown(f'<div class="kpi {tone}"><div class="kpi-label">{esc(label)}</div>'
                f'<div class="kpi-value">{esc(value)}</div><div class="kpi-sub">{esc(sub)}</div></div>',
                unsafe_allow_html=True)

def section(title: str) -> None:
    st.markdown(f'<div class="section-title">{esc(title)}</div>', unsafe_allow_html=True)

def progress_bar(title: str, used: float, limit: float, right_note: str = "") -> None:
    pct = used / limit * 100 if limit > 0 else 0
    color = C_INCOME if pct < 80 else C_ACCENT if pct < 100 else C_EXPENSE
    note = right_note or f"{fmt(used)} من {fmt(limit)} ({pct:.0f}%)"
    st.markdown(f'<div class="bar-row"><div class="bar-head"><span>{esc(title)}</span><span>{esc(note)}</span></div>'
                f'<div class="bar-track"><div class="bar-fill" style="width:{min(pct, 100):.1f}%;background:{color}"></div>'
                f'</div></div>', unsafe_allow_html=True)

def tip(text: str) -> None:
    st.markdown(f'<div class="tip">💡 {esc(text)}</div>', unsafe_allow_html=True)

def rate_tone(rate: float) -> str:
    return "good" if rate >= 20 else "warn" if rate >= 10 else "bad"

def _stretch(fn, *args, **kwargs):
    try:
        return fn(*args, width="stretch", **kwargs)
    except TypeError:
        return fn(*args, use_container_width=True, **kwargs)

def show_df(df: pd.DataFrame, **kwargs):
    return _stretch(st.dataframe, df, hide_index=True, **kwargs)

def show_chart(chart) -> None:
    _stretch(st.altair_chart, chart)

def flash(msg: str) -> None:
    st.session_state["flash"] = msg

def year_picker(key: str, col=None) -> int:
    this_year = date.today().year
    years = list(range(2024, this_year + 2))
    return (col or st).selectbox("السنة", years, index=years.index(this_year), key=key)

def month_picker(key: str, col=None, allow_all: bool = False) -> int:
    opts = ([0] if allow_all else []) + list(range(1, 13))
    default = 0 if allow_all else date.today().month - 1
    return (col or st).selectbox("الشهر", opts, index=default, key=key,
                                 format_func=lambda i: "كل الأشهر" if i == 0 else MONTHS_AR[i - 1])

def category_input(label: str, key: str, current: str = ""):
    if current and key not in st.session_state:
        st.session_state[key] = current
    opts = all_categories()
    val = st.session_state.get(key)
    if val and val not in opts:
        opts.append(val)
    return st.selectbox(label, opts, index=None, key=key, accept_new_options=True)

def donut(df: pd.DataFrame):
    return (alt.Chart(df).mark_arc(innerRadius=62, outerRadius=122, cornerRadius=4)
            .encode(theta="amount:Q",
                    color=alt.Color("category:N", legend=alt.Legend(title=None, orient="bottom"),
                                    scale=alt.Scale(scheme="tableau10")),
                    tooltip=[alt.Tooltip("category:N", title="التصنيف"),
                             alt.Tooltip("amount:Q", title="المبلغ", format=",.0f")])
            .properties(height=330))

def ensure_profile() -> None:
    if get_setting("user_name"):
        return
    page_header("👋 أهلاً بك في نظامك المالي", "اكتب اسمك وابدأ تسجيل أول مصروف في أقل من دقيقة")
    with st.form("welcome"):
        name = st.text_input("اسم المستخدم")
        saved0 = num("المبلغ المدخر بالفعل (اختياري)", step=1000.0)
        ok = st.form_submit_button("ابدأ الاستخدام", type="primary")
    if ok:
        if name.strip():
            set_setting("user_name", name.strip())
            if saved0 > 0:
                run("INSERT INTO fin_details(kind, name, amount, note) VALUES('مدخرات','مدخرات قبل البدء',?,'')",
                    (saved0,))
            st.rerun()
        else:
            st.error("اكتب اسمك للمتابعة.")
    st.stop()

# ==========================================================
# 5. نماذج المصاريف
# ==========================================================
def render_expense_form(scope: str) -> None:
    ss = st.session_state
    nonce = ss.setdefault(f"nonce_{scope}", 0)
    def k(n): return f"{scope}_{n}_{nonce}"

    files = st.file_uploader("📎 ارفع فاتورة أو سكرين رسالة بنك (اختياري)", type=FILE_TYPES,
                             accept_multiple_files=True, key=k("files"))
    if files:
        st.selectbox("نوع المستند", DOC_KINDS, key=k("kind"))
    a, b = st.columns(2)
    d = a.date_input("التاريخ", value="today", key=k("date"))
    with b:
        cat = category_input("التصنيف", k("cat"))
    c, e = st.columns(2)
    with c:
        amount = num("المبلغ (ج.م)", step=10.0, key=k("amount"))
    details = e.text_input("التفاصيل", key=k("details"))

    if st.button("➕ إضافة المصروف", type="primary", key=k("save")):
        if amount <= 0:
            st.error("أدخل مبلغًا أكبر من صفر.")
            return
        cat_final = (cat or "").strip() or "أخرى"
        eid = run("INSERT INTO daily_expenses (year, month, day, expense_date, amount, details, category) "
                  "VALUES (?,?,?,?,?,?,?)",
                  (d.year, d.month, d.day, d.isoformat(), amount, details.strip() or cat_final, cat_final))
        n = save_attachments(files, ss.get(k("kind"), DOC_KINDS[0]), eid, d.isoformat())
        ss[f"nonce_{scope}"] = nonce + 1
        flash(f"✅ تمت إضافة {fmt(amount)} ({cat_final})" + (f" مع {n} مرفق" if n else ""))
        st.rerun()

def render_edit_panel(eid: int) -> None:
    df = query_df("SELECT * FROM daily_expenses WHERE id=?", (eid,))
    if df.empty:
        return
    r, k = df.iloc[0], f"edit_{eid}"
    with st.container(border=True):
        section("✏️ تعديل المصروف")
        a, b = st.columns(2)
        d = a.date_input("التاريخ", value=date.fromisoformat(str(r.expense_date)[:10]), key=f"{k}_date")
        with b:
            cat = category_input("التصنيف", f"{k}_cat", current=r.category or "")
        c, e = st.columns(2)
        with c:
            amount = num("المبلغ (ج.م)", value=float(r.amount), step=10.0, key=f"{k}_amount")
        details = e.text_input("التفاصيل", value=r.details or "", key=f"{k}_details")

        atts = query_df("SELECT id, kind, name FROM attachments WHERE expense_id=?", (eid,))
        to_delete = []
        if not atts.empty:
            st.write("**المرفقات الحالية:**")
            cols = st.columns(3)
            for i, (_, at) in enumerate(atts.iterrows()):
                with cols[i % 3]:
                    show_attachment(int(at.id), width=200)
                    if st.checkbox(f"حذف ({at.kind})", key=f"{k}_delatt_{at.id}"):
                        to_delete.append(int(at.id))
        new_files = st.file_uploader("📎 إضافة مرفقات جديدة", type=FILE_TYPES,
                                     accept_multiple_files=True, key=f"{k}_newfiles")
        kind = st.selectbox("نوع المستند", DOC_KINDS, key=f"{k}_kind") if new_files else DOC_KINDS[0]

        s1, s2 = st.columns(2)
        if s1.button("💾 حفظ التعديلات", type="primary", key=f"{k}_save"):
            if amount <= 0:
                st.error("المبلغ يجب أن يكون أكبر من صفر.")
            else:
                cat_final = (cat or "").strip() or "أخرى"
                stmts = [("UPDATE daily_expenses SET year=?, month=?, day=?, expense_date=?, amount=?, "
                          "details=?, category=? WHERE id=?",
                          (d.year, d.month, d.day, d.isoformat(), amount,
                           details.strip() or cat_final, cat_final, eid))]
                stmts += [("DELETE FROM attachments WHERE id=?", (i,)) for i in to_delete]
                run_many(stmts)
                save_attachments(new_files, kind, eid, d.isoformat())
                flash("✅ تم حفظ التعديلات.")
                st.rerun()
        with s2:
            if st.checkbox("أؤكد حذف هذا المصروف ومرفقاته", key=f"{k}_confirm"):
                if st.button("🗑️ حذف المصروف", key=f"{k}_del"):
                    run_many([("DELETE FROM attachments WHERE expense_id=?", (eid,)),
                              ("DELETE FROM daily_expenses WHERE id=?", (eid,))])
                    flash("🗑️ تم حذف المصروف.")
                    st.rerun()

# ==========================================================
# 6. الصفحات
# ==========================================================
def carry_forward(year: int, month: int, amount: float, note: str) -> None:
    run("INSERT INTO monthly_data (year, month, basic_salary, cash_income, opening_balance, carry_note) "
        "VALUES (?,?,0,0,?,?) ON CONFLICT(year, month) DO UPDATE SET "
        "opening_balance=excluded.opening_balance, carry_note=excluded.carry_note", (year, month, amount, note))
    flash(f"✅ تم ترحيل {fmt(amount)} إلى هذا الشهر.")

def render_insights(year: int, month: int, s: dict, prev: dict) -> None:
    if s["expenses"] <= 0:
        return
    ins = month_insights(year, month, s, prev)
    section("🧠 قراءة ذكية لشهرك")
    c = st.columns(4)
    with c[0]: kpi("متوسط الصرف اليومي", fmt(ins["avg"]), "منذ بداية الشهر")
    with c[1]:
        if ins["is_current"]:
            over = ins["projected"] > s["income"] > 0
            kpi("توقع إجمالي الشهر", fmt(ins["projected"]),
                "فوق دخلك!" if over else "بنفس الوتيرة الحالية", "bad" if over else "")
        else:
            kpi("إجمالي الشهر", fmt(s["expenses"]), "شهر مكتمل")
    with c[2]:
        if ins["top"]:
            kpi("أكثر تصنيف صرفًا", cat_label(ins["top"][0]), fmt(ins["top"][1]), "warn")
    with c[3]:
        if ins["change"] is None:
            kpi("مقارنة بالشهر السابق", "—", "لا توجد بيانات سابقة")
        else:
            kpi("مقارنة بالشهر السابق", f"{ins['change']:+.0f}%",
                "المصروف ارتفع" if ins["change"] > 0 else "المصروف انخفض",
                "bad" if ins["change"] > 0 else "good")
    if s["income"] > 0 and s["rate"] < 10:
        tip(f"نسبة ادخارك {s['rate']:.1f}% فقط. جرّب هدف 20% ببدء الشهر بتحويل مبلغ ثابت للمدخرات.")
    if ins["needs_pct"] > 0:
        st.caption(f"توزيع الإنفاق: {ins['needs_pct']:.0f}% احتياجات و{100 - ins['needs_pct']:.0f}% رغبات.")

def render_budget_status(year: int, month: int) -> None:
    budgets = {c: v for c, v in get_budgets().items() if v and v > 0}
    if not budgets:
        return
    section("🎯 الميزانيات هذا الشهر")
    cats = category_totals(year, month)
    spent = dict(zip(cats.category, cats.amount))
    over = [c for c, lim in budgets.items() if float(spent.get(c, 0)) > lim]
    if over:
        st.error("تجاوزت الميزانية في: " + "، ".join(over))
    for c, lim in sorted(budgets.items(), key=lambda x: -float(spent.get(x[0], 0)) / x[1]):
        progress_bar(cat_label(c), float(spent.get(c, 0)), lim)

def page_dashboard() -> None:
    page_header(f"أهلاً {get_setting('user_name')} 👋", "هذه صورة وضعك المالي الآن")
    c1, c2, _ = st.columns([1, 1, 2])
    year, month = year_picker("dash_year", c1), month_picker("dash_month", c2)
    s = month_summary(year, month)
    py, pm = prev_ym(year, month)
    prev = month_summary(py, pm)

    if not s["exists"] and prev["active"] and prev["closing"] > 0:
        note = f"متبقي من شهر {MONTHS_AR[pm - 1]}"
        a, b = st.columns([3, 1])
        a.info(f"عندك {fmt(prev['closing'])} {note}. تحب ترحّلهم لشهر {MONTHS_AR[month - 1]}؟")
        b.button("🔁 رحّل الرصيد", on_click=carry_forward, args=(year, month, prev["closing"], note))

    sub_in = f"راتب {s['salary']:,.0f} + إضافي {s['cash']:,.0f}"
    if s["received_n"]:
        sub_in += f" + مقبوضات {s['received']:,.0f}"
    c = st.columns([1.3, 1, 1, 1, 1])
    with c[0]: kpi("متبقي معك", fmt(s["closing"]), "المرحل + المستلم − المصروف", "main")
    with c[1]: kpi("استلمت", fmt(s["income"]), sub_in, "good")
    with c[2]: kpi("صرفت", fmt(s["expenses"]), f"{s['count']} عملية", "bad")
    with c[3]: kpi("ادخرت", fmt(s["saving"]), f"{s['rate']:.1f}% من الدخل", rate_tone(s["rate"]))
    with c[4]: kpi("الرصيد المرحل", fmt(s["opening"]), s["carry_note"] or "لا يوجد ترحيل")

    with st.expander("⚡ إضافة مصروف سريع"):
        render_expense_form("dash")

    render_insights(year, month, s, prev)
    render_budget_status(year, month)

    fdf, assets, liab = fin_totals()
    if not fdf.empty:
        section("💼 ملخص الوضع المالي")
        f = st.columns(3)
        with f[0]: kpi("مدخراتك وأصولك", fmt(assets), f"{len(fdf)} بند", "good")
        with f[1]: kpi("التزامات عليك", fmt(liab), "", "bad" if liab else "")
        with f[2]: kpi("إجمالي ما تملكه تقريبًا", fmt(assets - liab + s["closing"]), "التفاصيل + متبقي معك")

    df = year_summary(year)
    data = df[df.has_data]
    if data.empty:
        st.info("لا توجد بيانات لهذه السنة بعد. ابدأ بإضافة أول مصروف من الأعلى.")
        return

    left, right = st.columns([3, 2])
    with left:
        section(f"استلمت وصرفت شهريًا — {year}")
        long = data[["name", "income", "expenses"]].melt("name", var_name="type", value_name="amount")
        long["type"] = long["type"].map({"income": "استلمت", "expenses": "صرفت"})
        ch = (alt.Chart(long).mark_bar(cornerRadiusTopLeft=5, cornerRadiusTopRight=5)
              .encode(x=alt.X("name:N", sort=MONTHS_AR, title=None), xOffset="type:N",
                      y=alt.Y("amount:Q", title=None),
                      color=alt.Color("type:N", legend=alt.Legend(title=None, orient="top"),
                                      scale=alt.Scale(domain=["استلمت", "صرفت"], range=[C_INCOME, C_EXPENSE])),
                      tooltip=[alt.Tooltip("name:N", title="الشهر"), alt.Tooltip("type:N", title="النوع"),
                               alt.Tooltip("amount:Q", title="المبلغ", format=",.0f")])
              .properties(height=330))
        show_chart(ch)
    with right:
        section(f"مصاريف {MONTHS_AR[month - 1]} حسب التصنيف")
        cats = category_totals(year, month)
        if cats.empty:
            st.info("لا توجد مصاريف لهذا الشهر بعد.")
        else:
            show_chart(donut(cats))

    daily = query_df("SELECT day, SUM(amount) AS amount FROM daily_expenses WHERE year=? AND month=? "
                     "GROUP BY day ORDER BY day", (year, month))
    if not daily.empty:
        section("📈 تراكم المصروف خلال الشهر")
        daily["cum"] = daily["amount"].cumsum()
        base = alt.Chart(daily).encode(x=alt.X("day:Q", title="اليوم"), y=alt.Y("cum:Q", title=None),
                                       tooltip=[alt.Tooltip("day:Q", title="اليوم"),
                                                alt.Tooltip("cum:Q", title="التراكم", format=",.0f")])
        layers = base.mark_area(opacity=.18, color=C_EXPENSE) + base.mark_line(color=C_EXPENSE, point=True)
        if s["income"] > 0:
            layers += alt.Chart(pd.DataFrame({"y": [s["income"]]})).mark_rule(
                color=C_INCOME, strokeDash=[6, 4]).encode(y="y:Q")
        show_chart(layers.properties(height=280))
        if s["income"] > 0:
            st.caption("الخط المتقطع الأخضر = دخلك هذا الشهر. عندما يلمسه الخط الأحمر فقد صرفت كل دخلك.")

def page_expenses() -> None:
    page_header("💸 سجل المصاريف", "إضافة وتعديل وبحث في مصاريفك اليومية")
    tab1, tab2 = st.tabs(["➕ إضافة مصروف جديد", "📋 قائمة المصاريف"])
    with tab1:
        render_expense_form("page_exp")
    with tab2:
        c1, c2, c3 = st.columns([1, 1, 2])
        y, m = year_picker("exp_y", c1), month_picker("exp_m", c2, allow_all=True)
        query = c3.text_input("🔎 بحث بالتفاصيل أو التصنيف", key="exp_q")

        sql, params = "SELECT id, expense_date, amount, details, category FROM daily_expenses WHERE year=? ", [y]
        if m > 0:
            sql += "AND month=? "
            params.append(m)
        if query.strip():
            sql += "AND (details LIKE ? OR category LIKE ?) "
            params += [f"%{query.strip()}%"] * 2
        df = query_df(sql + "ORDER BY expense_date DESC, id DESC", tuple(params))
        if df.empty:
            st.info("لا توجد مصاريف مطابقة. جرّب شهرًا آخر أو امسح البحث.")
            return

        x1, x2 = st.columns([3, 1])
        x1.write(f"**الإجمالي:** {fmt(df.amount.sum())} — {len(df)} عملية")
        x2.download_button("⬇️ تصدير CSV", df.to_csv(index=False).encode("utf-8-sig"),
                           "expenses.csv", "text/csv")
        shown = df.copy()
        shown["amount"] = shown["amount"].map(fmt)
        shown.columns = ["م", "التاريخ", "المبلغ", "التفاصيل", "التصنيف"]
        show_df(shown)

        sel = st.selectbox("اختر العملية للتعديل أو الحذف", df["id"].tolist(),
                           format_func=lambda i: f"#{i} — {df.loc[df.id == i, 'details'].iloc[0]} "
                                                 f"({fmt(df.loc[df.id == i, 'amount'].iloc[0])})")
        render_edit_panel(sel)

def page_monthly_details() -> None:
    page_header("📅 الدخل الشهري", "الراتب والدخل الإضافي والرصيد المرحل")
    c1, c2 = st.columns(2)
    y, m = year_picker("m_det_y", c1), month_picker("m_det_m", c2)
    s = month_summary(y, m)

    section(f"بيانات شهر {MONTHS_AR[m - 1]} {y}")
    with st.form(f"monthly_form_{y}_{m}"):
        col1, col2 = st.columns(2)
        with col1:
            sal = num("الراتب الأساسي", value=s["salary"] or None, step=500.0)
        with col2:
            cash = num("دخل كاش / إضافي", value=s["cash"] or None, step=500.0)
        col3, col4 = st.columns(2)
        with col3:
            opening = num("رصيد مرحل من الشهر السابق", value=s["opening"] or None, step=500.0)
        note = col4.text_input("ملاحظة الترحيل", value=s["carry_note"])
        if st.form_submit_button("💾 حفظ البيانات الشهرية", type="primary"):
            run("INSERT INTO monthly_data (year, month, basic_salary, cash_income, opening_balance, carry_note) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(year, month) DO UPDATE SET "
                "basic_salary=excluded.basic_salary, cash_income=excluded.cash_income, "
                "opening_balance=excluded.opening_balance, carry_note=excluded.carry_note",
                (y, m, sal, cash, opening, note))
            flash("✅ تم حفظ البيانات الشهرية.")
            st.rerun()

    section("💵 مبالغ مستلمة أخرى (مكافآت، حوالات...)")
    with st.form(f"income_form_{y}_{m}", clear_on_submit=True):
        a, b, c = st.columns(3)
        with a:
            amt = num("المبلغ")
        src = b.selectbox("المصدر", INCOME_SOURCES)
        rd = c.date_input("تاريخ الاستلام", value="today")
        inote = st.text_input("ملاحظة")
        if st.form_submit_button("➕ إضافة المبلغ"):
            if amt > 0:
                run("INSERT INTO incomes(amount, for_year, for_month, received_date, source, note, created_at) "
                    "VALUES(?,?,?,?,?,?,?)", (amt, y, m, rd.isoformat(), src, inote.strip(),
                                              datetime.now().isoformat(timespec="seconds")))
                flash(f"✅ تمت إضافة {fmt(amt)}.")
                st.rerun()
            else:
                st.error("أدخل مبلغًا أكبر من صفر.")
    inc = query_df("SELECT id, received_date, amount, source, note FROM incomes WHERE for_year=? AND for_month=? "
                   "ORDER BY received_date DESC", (y, m))
    for _, r in inc.iterrows():
        a, b = st.columns([6, 1])
        a.write(f"**{fmt(r.amount)}** — {r.source} — {r.received_date} {('· ' + r.note) if r.note else ''}")
        if b.button("🗑️", key=f"delinc_{r.id}"):
            run("DELETE FROM incomes WHERE id=?", (int(r.id),))
            st.rerun()

def page_budgets() -> None:
    page_header("🎯 الميزانيات والأهداف", "حدود الإنفاق وأهداف الادخار")
    tab1, tab2 = st.tabs(["📉 الميزانيات الشهرية", "🚀 الأهداف المالية"])
    with tab1:
        section("حد أقصى شهري لكل تصنيف (اتركه فارغًا لعدم التحديد)")
        budgets = get_budgets()
        with st.form("budget_form"):
            new_b, cols = {}, st.columns(2)
            for i, c in enumerate(all_categories()):
                with cols[i % 2]:
                    new_b[c] = num(cat_label(c), value=float(budgets.get(c, 0.0)) or None, key=f"bud_{c}")
            if st.form_submit_button("💾 حفظ الميزانيات", type="primary"):
                run_many([("INSERT INTO budgets (category, monthly_limit) VALUES (?,?) "
                           "ON CONFLICT(category) DO UPDATE SET monthly_limit=excluded.monthly_limit", (c, v))
                          for c, v in new_b.items()])
                flash("✅ تم حفظ الميزانيات. ستظهر حالتها في لوحة التحكم.")
                st.rerun()
    with tab2:
        goals = query_df("SELECT * FROM goals ORDER BY id DESC")
        if goals.empty:
            st.info("لا أهداف بعد. أضف هدفًا مثل «سفر» أو «صندوق طوارئ».")
        for _, g in goals.iterrows():
            progress_bar(f"{g['name']}", g.saved, g.target,
                         f"{fmt(g.saved)} / {fmt(g.target)} — باقي {fmt(max(g.target - g.saved, 0))}")
            a, b, c = st.columns([2, 1, 1])
            with a:
                add = num("إضافة مبلغ", key=f"gadd_{g.id}", label_visibility="collapsed")
            if b.button("➕ ادّخر", key=f"gsave_{g.id}") and add > 0:
                run("UPDATE goals SET saved=saved+? WHERE id=?", (add, int(g.id)))
                flash(f"✅ أُضيف {fmt(add)} إلى «{g['name']}».")
                st.rerun()
            if c.button("🗑️ حذف", key=f"gdel_{g.id}"):
                run("DELETE FROM goals WHERE id=?", (int(g.id),))
                st.rerun()
        with st.expander("➕ إضافة هدف جديد"):
            with st.form("new_goal", clear_on_submit=True):
                name = st.text_input("اسم الهدف")
                target = num("المبلغ المستهدف", step=1000.0)
                saved = num("المدخر حاليًا", step=500.0)
                if st.form_submit_button("إضافة الهدف", type="primary"):
                    if name.strip() and target > 0:
                        run("INSERT INTO goals (name, target, saved, created_at) VALUES (?,?,?,?)",
                            (name.strip(), target, saved, datetime.now().isoformat()))
                        flash("✅ تمت إضافة الهدف.")
                        st.rerun()
                    else:
                        st.error("اكتب اسم الهدف ومبلغًا مستهدفًا أكبر من صفر.")

def page_wishes() -> None:
    page_header("✨ قائمة الأمنيات", "خطّط لمشترياتك قبل ما تصرف")
    wishes = query_df("SELECT * FROM wishes WHERE status='active' ORDER BY id DESC")
    if wishes.empty:
        st.info("القائمة فاضية. أضف أول أمنية من النموذج بالأسفل.")
    for _, w in wishes.iterrows():
        with st.container(border=True):
            st.markdown(f"### {esc(w['name'])}")
            st.caption(f"{w.kind or 'غير محدد'}" + (f" — {w.note}" if w.note else ""))
            progress_bar("التقدم", w.saved, w.price,
                         f"{fmt(w.saved)} / {fmt(w.price)} — باقي {fmt(max(w.price - w.saved, 0))}")
            a, b, c, d = st.columns([2, 1, 1, 1])
            with a:
                add = num("مبلغ", key=f"wadd_{w.id}", label_visibility="collapsed")
            if b.button("➕ خصّص", key=f"wsave_{w.id}") and add > 0:
                run("UPDATE wishes SET saved=saved+? WHERE id=?", (add, int(w.id)))
                st.rerun()
            if c.button("✅ تم الشراء", key=f"buy_{w.id}"):
                run("UPDATE wishes SET status='bought', bought_at=? WHERE id=?",
                    (datetime.now().isoformat(), int(w.id)))
                flash("🎉 مبروك! نُقلت إلى المشتريات.")
                st.rerun()
            if d.button("🗑️ حذف", key=f"wdel_{w.id}"):
                run("DELETE FROM wishes WHERE id=?", (int(w.id),))
                st.rerun()

    with st.expander("➕ إضافة أمنية جديدة", expanded=wishes.empty):
        with st.form("new_wish", clear_on_submit=True):
            w1, w2 = st.columns(2)
            name, kind = w1.text_input("اسم السلعة / الخدمة"), w2.selectbox("النوع", WISH_KINDS)
            w3, w4 = st.columns(2)
            with w3:
                price = num("السعر المتوقع", step=500.0)
            with w4:
                saved = num("المخصص لها حاليًا")
            note = st.text_input("رابط المنتج أو ملاحظات")
            if st.form_submit_button("إضافة للقائمة", type="primary"):
                if name.strip() and price > 0:
                    run("INSERT INTO wishes (name, kind, price, saved, note, created_at) VALUES (?,?,?,?,?,?)",
                        (name.strip(), kind, price, saved, note.strip(), datetime.now().isoformat()))
                    flash("✅ تمت الإضافة.")
                    st.rerun()
                else:
                    st.error("اكتب الاسم وسعرًا أكبر من صفر.")

    bought = query_df("SELECT name, price, bought_at FROM wishes WHERE status='bought' ORDER BY bought_at DESC")
    if not bought.empty:
        with st.expander(f"🛍️ تم شراؤها ({len(bought)})"):
            bought["bought_at"] = bought["bought_at"].str[:10]
            bought["price"] = bought["price"].map(fmt)
            bought.columns = ["الاسم", "السعر", "تاريخ الشراء"]
            show_df(bought)

def page_settings() -> None:
    page_header("⚙️ الإعدادات", "الاسم، النسخ الاحتياطي، وإدارة البيانات")
    section("بيانات المستخدم")
    new_name = st.text_input("اسم المستخدم", value=get_setting("user_name"))
    if st.button("حفظ الاسم") and new_name.strip():
        set_setting("user_name", new_name.strip())
        flash("✅ تم تحديث الاسم.")
        st.rerun()

    section("💾 النسخ الاحتياطي")
    st.caption("نزّل نسخة من بياناتك بانتظام، خصوصًا قبل أي تحديث للبرنامج.")
    a, b = st.columns(2)
    try:
        with _lock:
            get_conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with open(DB_PATH, "rb") as f:
            a.download_button("⬇️ نسخة كاملة (قاعدة البيانات)", f.read(),
                              f"finance_backup_{date.today().isoformat()}.db", "application/octet-stream")
    except OSError:
        a.warning("تعذر قراءة ملف قاعدة البيانات.")
    alle = query_df("SELECT expense_date, amount, details, category FROM daily_expenses ORDER BY expense_date")
    b.download_button("⬇️ كل المصاريف (CSV)", alle.to_csv(index=False).encode("utf-8-sig"),
                      "all_expenses.csv", "text/csv")

    section("منطقة الخطر")
    st.warning("⚠️ إعادة الضبط تحذف كل البيانات ولا يمكن التراجع عنها.")
    if not st.session_state.get("confirm_reset"):
        if st.button("🔴 بدء إعادة ضبط المصنع"):
            st.session_state["confirm_reset"] = True
            st.rerun()
    else:
        st.error("تأكيد نهائي: هل أنت متأكد من مسح كل شيء؟")
        c1, c2 = st.columns(2)
        if c1.button("نعم، امسح كل البيانات", type="primary"):
            run_many([(f"DELETE FROM {t}", ()) for t in
                      ("attachments", "daily_expenses", "monthly_data", "budgets", "goals",
                       "incomes", "fin_details", "wishes", "settings")])
            st.session_state.clear()
            flash("تم مسح جميع البيانات.")
            st.rerun()
        if c2.button("إلغاء"):
            st.session_state["confirm_reset"] = False
            st.rerun()

# ==========================================================
# 7. التوجيه الرئيسي
# ==========================================================
ROUTES = {"dashboard": page_dashboard, "expenses": page_expenses, "monthly": page_monthly_details,
          "budgets": page_budgets, "wishes": page_wishes, "settings": page_settings}

def main() -> None:
    inject_css()
    get_conn()
    ensure_profile()
    if st.session_state.get("flash"):
        st.toast(st.session_state.pop("flash"))
    render_top_nav()
    ROUTES.get(st.session_state["current_page"], page_dashboard)()

if __name__ == "__main__":
    main()
