"""
نظامي المالي الشخصي - الإصدار 2
تشغيل:  streamlit run app.py
"""
import base64
import io
import json
import math
import os
import sqlite3
import threading
from datetime import date, datetime

import altair as alt
import pandas as pd
import streamlit as st

# ==========================================================
# 0. الإعدادات العامة
# ==========================================================
st.set_page_config(
    page_title="نظامي المالي الشخصي",
    page_icon="💰",
    layout="wide",
    initial_sidebar_state="expanded",
)

DB_PATH = "personal_finance.db"
CUR = "ج.م"
MONTHS_AR = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
             "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]

# تصنيفات مقترحة فقط (تقدر تكتب أي تصنيف تحبه): الاسم -> (أيقونة، احتياج/رغبة)
CAT_META = {
    "طعام وشراب": ("🍽️", "need"), "مواصلات": ("🚌", "need"),
    "فواتير": ("🧾", "need"), "صحة": ("🩺", "need"), "تعليم": ("🎓", "need"),
    "ملابس": ("👕", "want"), "ترفيه": ("🎬", "want"), "أخرى": ("📦", "want"),
}
DOC_KINDS = ["فاتورة", "رسالة بنك", "معاملة إلكترونية", "إيصال", "أخرى"]
FILE_TYPES = ["png", "jpg", "jpeg", "webp", "pdf"]
MODEL_DEFAULT = "claude-sonnet-5-5"

C_PRIMARY, C_INCOME, C_EXPENSE, C_ACCENT, C_INK = "#0b5d4b", "#0b5d4b", "#d1495b", "#c8861a", "#1b2a2f"

# ==========================================================
# 1. قاعدة البيانات
# ==========================================================
_lock = threading.Lock()


@st.cache_resource
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS monthly_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            year INTEGER NOT NULL, month INTEGER NOT NULL,
            basic_salary REAL DEFAULT 0, cash_income REAL DEFAULT 0,
            opening_balance REAL DEFAULT 0, carry_note TEXT,
            UNIQUE(year, month)
        );
        CREATE TABLE IF NOT EXISTS daily_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            year INTEGER NOT NULL, month INTEGER NOT NULL, day INTEGER NOT NULL,
            expense_date DATE NOT NULL, amount REAL NOT NULL,
            details TEXT, category TEXT
        );
        CREATE TABLE IF NOT EXISTS budgets (
            category TEXT PRIMARY KEY, monthly_limit REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            target REAL NOT NULL, saved REAL NOT NULL DEFAULT 0, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            expense_id INTEGER REFERENCES daily_expenses(id) ON DELETE CASCADE,
            kind TEXT, name TEXT, mime TEXT, data BLOB,
            note TEXT, doc_date TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS incomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount REAL NOT NULL, for_year INTEGER NOT NULL, for_month INTEGER NOT NULL,
            received_date TEXT NOT NULL, source TEXT, note TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS fin_details (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT, name TEXT, amount REAL NOT NULL DEFAULT 0, note TEXT
        );
        CREATE TABLE IF NOT EXISTS wishes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, kind TEXT, price REAL NOT NULL, saved REAL NOT NULL DEFAULT 0,
            target_date TEXT, note TEXT, status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT, bought_at TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE INDEX IF NOT EXISTS idx_inc_ym ON incomes(for_year, for_month);
        CREATE INDEX IF NOT EXISTS idx_exp_ym ON daily_expenses(year, month);
        CREATE INDEX IF NOT EXISTS idx_exp_cat ON daily_expenses(category);
        CREATE INDEX IF NOT EXISTS idx_att_exp ON attachments(expense_id);
        """
    )
    # ترقية قاعدة البيانات القديمة: إضافة عمود ملاحظة الترحيل
    cols = [r[1] for r in conn.execute("PRAGMA table_info(monthly_data)")]
    if "carry_note" not in cols:
        conn.execute("ALTER TABLE monthly_data ADD COLUMN carry_note TEXT")
    conn.commit()
    return conn


def query_df(sql: str, params: tuple = ()) -> pd.DataFrame:
    with _lock:
        return pd.read_sql_query(sql, get_conn(), params=params)


def run(sql: str, params: tuple = ()) -> int:
    """تنفيذ أمر واحد وإرجاع رقم آخر سجل مضاف."""
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
    """مجموعة أوامر كمعاملة واحدة (إما تنجح كلها أو تتراجع)."""
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
    run("INSERT INTO settings(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# ==========================================================
# 2. الحسابات
# ==========================================================
def prev_ym(year: int, month: int) -> tuple:
    return (year, month - 1) if month > 1 else (year - 1, 12)


def month_summary(year: int, month: int) -> dict:
    """استلمت = راتب + دخل إضافي + مقبوضات مسجلة | صرفت = مجموع المصاريف
    ادخرت = استلمت - صرفت | متبقي معك = الرصيد المرحل + استلمت - صرفت"""
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
    received, rn = float(r.s[0]), int(r.n[0])
    exp = float(e.s[0])
    income = salary + cash + received
    saving = income - exp
    return {"exists": ex, "salary": salary, "cash": cash, "received": received, "received_n": rn,
            "income": income, "opening": opening, "carry_note": note, "expenses": exp,
            "count": int(e.n[0]), "saving": saving,
            "rate": (saving / income * 100) if income > 0 else 0.0,
            "closing": opening + saving, "active": ex or income > 0 or exp > 0}


def year_summary(year: int) -> pd.DataFrame:
    inc = query_df("SELECT month, basic_salary + cash_income AS income, opening_balance AS opening, "
                   "carry_note FROM monthly_data WHERE year=?", (year,))
    exp = query_df("SELECT month, SUM(amount) AS expenses FROM daily_expenses "
                   "WHERE year=? GROUP BY month", (year,))
    rec = query_df("SELECT for_month AS month, SUM(amount) AS received FROM incomes "
                   "WHERE for_year=? GROUP BY for_month", (year,))
    df = (pd.DataFrame({"month": range(1, 13)})
          .merge(inc, on="month", how="left").merge(exp, on="month", how="left")
          .merge(rec, on="month", how="left"))
    df = df.fillna({"income": 0.0, "opening": 0.0, "expenses": 0.0, "received": 0.0, "carry_note": ""})
    df["income"] = df["income"] + df["received"]
    df["name"] = df["month"].map(lambda i: MONTHS_AR[i - 1])
    df["saving"] = df["income"] - df["expenses"]
    df["rate"] = (df["saving"] / df["income"].replace(0, float("nan")) * 100).fillna(0.0)
    df["closing"] = df["opening"] + df["saving"]
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
    """التصنيفات المقترحة + أي تصنيف كتبته بنفسك من قبل."""
    used = query_df("SELECT DISTINCT category FROM daily_expenses "
                    "WHERE category IS NOT NULL AND category<>''").category.tolist()
    return list(CAT_META) + [c for c in used if c not in CAT_META]


def cat_kind(cat: str) -> str:
    return CAT_META.get(cat, ("🏷️", "want"))[1]


FIN_KINDS = ["مدخرات", "حساب بنكي", "كاش", "ذهب", "استثمارات", "دين لي", "دين عليّ", "أخرى"]
LIABILITY = "دين عليّ"
INCOME_SOURCES = ["راتب", "دخل إضافي", "مكافأة", "حوالة", "أخرى"]


def fin_totals():
    df = query_df("SELECT kind, name, amount, note FROM fin_details ORDER BY id")
    if df.empty:
        return df, 0.0, 0.0
    liab = float(df[df.kind == LIABILITY].amount.sum())
    return df, float(df.amount.sum()) - liab, liab


# ==========================================================
# 3. المرفقات (صور الفواتير ورسائل البنوك)
# ==========================================================
def prepare_file(f) -> tuple:
    """تجهيز الملف المرفوع: تصغير الصور لتوفير المساحة وتسريع قراءتها بالذكاء الاصطناعي."""
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
# 4. الذكاء الاصطناعي (Claude)
# ==========================================================
def get_api_key() -> str:
    key = st.session_state.get("api_key", "")
    if key:
        return key
    try:
        key = st.secrets.get("ANTHROPIC_API_KEY", "")
    except Exception:
        key = ""
    return key or os.environ.get("ANTHROPIC_API_KEY", "")


def ai_ready() -> bool:
    return bool(get_api_key())


def ask_claude(system: str, messages: list, max_tokens: int = 1500) -> str:
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("مكتبة anthropic غير مثبتة. نفّذ: pip install anthropic")
    client = anthropic.Anthropic(api_key=get_api_key())
    resp = client.messages.create(model=get_setting("ai_model", MODEL_DEFAULT) or MODEL_DEFAULT,
                                  max_tokens=max_tokens, system=system, messages=messages)
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def file_block(mime: str, raw: bytes) -> dict:
    data = base64.standard_b64encode(raw).decode()
    kind = "document" if mime == "application/pdf" else "image"
    return {"type": kind, "source": {"type": "base64", "media_type": mime, "data": data}}


EXTRACT_PROMPT = """اقرأ الملفات المرفقة (فاتورة أو رسالة بنك أو إيصال معاملة إلكترونية) واستخرج بيانات العملية.
أرجع JSON فقط بدون أي شرح أو علامات ```، بهذا الشكل بالضبط:
{"amount": رقم أو null, "date": "YYYY-MM-DD" أو null, "details": "اسم المتجر أو وصف قصير", "category": "تصنيف قصير بالعربية", "is_expense": true أو false}
- amount هو المبلغ الإجمالي المدفوع كرقم فقط.
- is_expense تكون false لو العملية تحويل وارد أو إيداع.
- التصنيفات المعتادة: """ + "، ".join(CAT_META) + """ (يمكنك اقتراح غيرها لو أنسب).
لو لم تجد بيانات مالية واضحة ارجع القيم null."""


def ai_extract(files) -> dict:
    blocks = []
    for f in list(files)[:3]:
        _, mime, raw = prepare_file(f)
        blocks.append(file_block(mime, raw))
    blocks.append({"type": "text", "text": EXTRACT_PROMPT})
    text = ask_claude("أنت مساعد دقيق لاستخراج بيانات الفواتير والمعاملات المالية.",
                      [{"role": "user", "content": blocks}], max_tokens=400)
    s, e = text.find("{"), text.rfind("}")
    return json.loads(text[s:e + 1])


def ai_prefill(scope: str, nonce: int) -> None:
    """يُنفَّذ كـ callback قبل رسم الحقول، فيملأها تلقائيًا."""
    ss = st.session_state
    files = ss.get(f"{scope}_files_{nonce}") or []
    try:
        d = ai_extract(files)
    except Exception as ex:
        ss[f"ai_msg_{scope}"] = ("error", f"تعذرت قراءة الصورة: {ex}")
        return
    if d.get("amount"):
        ss[f"{scope}_amount_{nonce}"] = float(d["amount"])
    if d.get("date"):
        try:
            ss[f"{scope}_date_{nonce}"] = date.fromisoformat(str(d["date"])[:10])
        except ValueError:
            pass
    if d.get("details"):
        ss[f"{scope}_details_{nonce}"] = str(d["details"])
    if d.get("category"):
        ss[f"{scope}_cat_{nonce}"] = str(d["category"])
    if d.get("is_expense") is False:
        ss[f"ai_msg_{scope}"] = ("warning", "العملية تبدو تحويلًا وارد (دخل) وليست مصروفًا. راجع البيانات قبل الحفظ.")
    else:
        ss[f"ai_msg_{scope}"] = ("success", "تمت قراءة البيانات. راجعها وعدّل أي خانة قبل الإضافة.")


def build_context() -> str:
    today = date.today()
    ys = year_summary(today.year)
    ys = ys[ys.has_data]
    lines = [f"التاريخ اليوم: {today.isoformat()}", f"العملة: {CUR}", "ملخص شهور السنة الحالية:"]
    for _, r in ys.iterrows():
        lines.append(f"- {r['name']}: استلم {r.income:,.0f} | صرف {r.expenses:,.0f} | ادخر {r.saving:,.0f} | "
                     f"رصيد مرحل {r.opening:,.0f} ({r.carry_note or 'بدون'}) | متبقي {r.closing:,.0f}")
    cats = category_totals(today.year, today.month)
    if not cats.empty:
        lines.append("مصاريف الشهر الحالي حسب التصنيف: " + "، ".join(f"{c}: {a:,.0f}" for c, a in zip(cats.category, cats.amount)))
    b = get_budgets()
    if b:
        lines.append("الميزانيات الشهرية: " + "، ".join(f"{c}: {v:,.0f}" for c, v in b.items() if v > 0))
    g = query_df("SELECT name, target, saved FROM goals")
    if not g.empty:
        lines.append("الأهداف: " + "، ".join(f"{r['name']} ({r.saved:,.0f}/{r.target:,.0f})" for _, r in g.iterrows()))
    fdf, assets, liab = fin_totals()
    if not fdf.empty:
        lines.append(f"التفاصيل المالية: مدخرات وأصول {assets:,.0f} | التزامات {liab:,.0f} | " +
                     "، ".join(f"{r.kind} {r['name']}: {r.amount:,.0f}" for _, r in fdf.iterrows()))
    w = query_df("SELECT name, kind, price, saved, target_date FROM wishes WHERE status='active'")
    if not w.empty:
        lines.append("حاجات يريد شراءها: " + "، ".join(
            f"{r['name']} ({r.kind if pd.notna(r.kind) else 'غير محدد'}) ثمنها {r.price:,.0f} مخصص {r.saved:,.0f}"
            + (f" قبل {r.target_date}" if pd.notna(r.target_date) else "") for _, r in w.iterrows()))
    last = query_df("SELECT expense_date, amount, details, category FROM daily_expenses "
                    "ORDER BY expense_date DESC, id DESC LIMIT 25")
    if not last.empty:
        lines.append("آخر المصاريف:")
        for _, r in last.iterrows():
            lines.append(f"- {r.expense_date} | {r.amount:,.0f} | {r.details} | {r.category}")
    return "\n".join(lines)


# ==========================================================
# 5. التنسيق والمكونات المشتركة
# ==========================================================
def fmt(x: float) -> str:
    return f"{x:,.0f} {CUR}"


def inject_css() -> None:
    st.markdown(f"""
<style>
@import url('[https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;800&display=swap](https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;800&display=swap)');
html, body, .stApp, .stMarkdown, p, h1, h2, h3, h4, h5, label, button, input, textarea,
[data-testid="stMetricValue"], [data-testid="stMetricLabel"], [data-baseweb="select"], [data-baseweb="tab"] {{
    font-family: 'Cairo', sans-serif;
}}
.stApp, section[data-testid="stSidebar"], .block-container {{ direction: rtl; }}
.block-container {{ padding-top: 2rem; max-width: 1280px; }}
h1, h2, h3, h4, p, label, .stMarkdown {{ text-align: right; }}
.app-header {{ background: {C_PRIMARY}; color: #fff; padding: 22px 28px; border-radius: 14px; margin-bottom: 22px; }}
.app-header h1 {{ margin: 0; font-size: 1.7rem; font-weight: 800; color: #fff; }}
.app-header p {{ margin: 4px 0 0; opacity: .88; font-size: .95rem; color: #fff; }}
.kpi {{ background: #fff; border: 1px solid #e3e7ea; border-right: 5px solid {C_PRIMARY};
        border-radius: 10px; padding: 12px 16px; margin-bottom: 10px; color: {C_INK}; }}
.kpi-label {{ font-size: .85rem; color: #5b6b72; font-weight: 600; }}
.kpi-value {{ font-size: 1.4rem; font-weight: 800; line-height: 1.5; color: {C_INK}; }}
.kpi-sub {{ font-size: .78rem; color: #5b6b72; min-height: 1.2em; }}
.kpi.good {{ border-right-color: #16a34a; }} .kpi.bad {{ border-right-color: {C_EXPENSE}; }}
.kpi.warn {{ border-right-color: {C_ACCENT}; }} .kpi.main {{ background: {C_PRIMARY}; border-color: {C_PRIMARY}; }}
.kpi.main .kpi-label, .kpi.main .kpi-value, .kpi.main .kpi-sub {{ color: #fff; }}
.section-title {{ font-weight: 700; font-size: 1.1rem; margin: 18px 0 8px; padding-right: 10px;
                  border-right: 4px solid {C_ACCENT}; }}
section[data-testid="stSidebar"] {{ border-left: 1px solid #e3e7ea; }}
.stButton > button, .stDownloadButton > button {{ border-radius: 8px; font-weight: 600; }}
</style>
""", unsafe_allow_html=True)


def page_header(title: str, subtitle: str = "") -> None:
    sub = f"<p>{subtitle}</p>" if subtitle else ""
    st.markdown(f'<div class="app-header"><h1>{title}</h1>{sub}</div>', unsafe_allow_html=True)


def kpi(label: str, value: str, sub: str = "", tone: str = "") -> None:
    st.markdown(f'<div class="kpi {tone}"><div class="kpi-label">{label}</div>'
                f'<div class="kpi-value">{value}</div><div class="kpi-sub">{sub}</div></div>',
                unsafe_allow_html=True)


def section(title: str) -> None:
    st.markdown(f'<div class="section-title">{title}</div>', unsafe_allow_html=True)


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
    default = opts.index(0 if allow_all else date.today().month) if allow_all else date.today().month - 1
    return (col or st).selectbox("الشهر", opts, index=default, key=key,
                                 format_func=lambda i: "كل الأشهر" if i == 0 else MONTHS_AR[i - 1])


def category_input(label: str, key: str, current: str = ""):
    """خانة تصنيف فاضية: اختر من المقترحات أو اكتب تصنيفك الخاص."""
    if current and key not in st.session_state:
        st.session_state[key] = current
    opts = all_categories()
    val = st.session_state.get(key)
    if val and val not in opts:
        opts.append(val)
    return st.selectbox(label, opts, index=None, key=key,
                        placeholder="اختر تصنيفًا أو اكتب تصنيفك الخاص واضغط Enter",
                        accept_new_options=True)


def donut(df: pd.DataFrame):
    return (alt.Chart(df).mark_arc(innerRadius=60, outerRadius=120)
            .encode(theta="amount:Q",
                    color=alt.Color("category:N", legend=alt.Legend(title=None, orient="bottom")),
                    tooltip=[alt.Tooltip("category:N", title="التصنيف"),
                             alt.Tooltip("amount:Q", title="المبلغ", format=",.0f")])
            .properties(height=320))


def ensure_profile() -> None:
    if get_setting("user_name"):
        return
    page_header("👋 أهلاً بك", "قبل ما نبدأ، اكتب اسم المستخدم اللي تحب يظهر في النظام")
    with st.form("welcome"):
        name = st.text_input("اسم المستخدم", placeholder="مثال: أحمد")
        saved0 = st.number_input("المبلغ المدخر بالفعل (اختياري)", min_value=0.0, step=1000.0, format="%.2f")
        ok = st.form_submit_button("ابدأ الاستخدام", type="primary")
    if ok:
        if name.strip():
            set_setting("user_name", name.strip())
            if saved0 > 0:
                run("INSERT INTO fin_details(kind, name, amount, note) VALUES('مدخرات','مدخرات قبل البدء',?,'')", (saved0,))
            st.rerun()
        else:
            st.error("اكتب اسمك للمتابعة.")
    st.stop()


# ==========================================================
# 6. نموذج إضافة مصروف (يُستخدم في لوحة التحكم وصفحة المصاريف)
# ==========================================================
def render_expense_form(scope: str) -> None:
    ss = st.session_state
    nonce = ss.setdefault(f"nonce_{scope}", 0)

    def k(n):
        return f"{scope}_{n}_{nonce}"

    files = st.file_uploader("📎 ارفع فاتورة أو سكرين رسالة بنك أو معاملة إلكترونية (اختياري)",
                             type=FILE_TYPES, accept_multiple_files=True, key=k("files"))
    if files:
        a, b = st.columns([1, 1])
        a.selectbox("نوع المستند", DOC_KINDS, key=k("kind"))
        if ai_ready():
            b.write("")
            b.button("🤖 اقرأ البيانات تلقائيًا من الصورة", key=k("ai"),
                     on_click=ai_prefill, args=(scope, nonce))
        else:
            b.caption("لتعبئة البيانات تلقائيًا من الصورة، فعّل المساعد الذكي من «الإعدادات».")

    msg = ss.get(f"ai_msg_{scope}")
    if msg:
        getattr(st, msg[0])(msg[1])

    a, b = st.columns(2)
    d = a.date_input("التاريخ", value="today", key=k("date"))
    with b:
        cat = category_input("التصنيف", k("cat"))
    c, e = st.columns(2)
    amount = c.number_input("المبلغ (ج.م)", min_value=0.0, step=10.0, format="%.2f", key=k("amount"))
    details = e.text_input("التفاصيل", placeholder="مثال: بنزين، سوبر ماركت...", key=k("details"))

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
        ss.pop(f"ai_msg_{scope}", None)
        extra = f" مع {n} مرفق" if n else ""
        flash(f"✅ تمت إضافة {fmt(amount)} ({cat_final}){extra}.")
        st.rerun()


def render_edit_panel(eid: int) -> None:
    df = query_df("SELECT * FROM daily_expenses WHERE id=?", (eid,))
    if df.empty:
        return
    r = df.iloc[0]
    k = f"edit_{eid}"
    with st.container(border=True):
        section("✏️ تعديل المصروف")
        a, b = st.columns(2)
        d = a.date_input("التاريخ", value=date.fromisoformat(str(r.expense_date)[:10]), key=f"{k}_date")
        with b:
            cat = category_input("التصنيف", f"{k}_cat", current=r.category or "")
        c, e = st.columns(2)
        amount = c.number_input("المبلغ (ج.م)", min_value=0.0, step=10.0, format="%.2f",
                                value=float(r.amount), key=f"{k}_amount")
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

        s1, s2 = st.columns([1, 1])
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
                st.session_state["log_nonce"] = st.session_state.get("log_nonce", 0) + 1
                flash("✅ تم حفظ التعديلات.")
                st.rerun()
        with s2:
            if st.checkbox("أؤكد حذف هذا المصروف ومرفقاته", key=f"{k}_confirm"):
                if st.button("🗑️ حذف المصروف", key=f"{k}_del"):
                    run_many([("DELETE FROM attachments WHERE expense_id=?", (eid,)),
                              ("DELETE FROM daily_expenses WHERE id=?", (eid,))])
                    st.session_state["log_nonce"] = st.session_state.get("log_nonce", 0) + 1
                    flash("🗑️ تم حذف المصروف.")
                    st.rerun()


# ==========================================================
# 7. الصفحات
# ==========================================================
def carry_forward(year: int, month: int, amount: float, note: str) -> None:
    run("INSERT INTO monthly_data (year, month, basic_salary, cash_income, opening_balance, carry_note) "
        "VALUES (?,?,0,0,?,?) ON CONFLICT(year, month) DO UPDATE SET "
        "opening_balance=excluded.opening_balance, carry_note=excluded.carry_note",
        (year, month, amount, note))
    flash(f"✅ تم ترحيل {fmt(amount)} إلى هذا الشهر.")


def page_dashboard() -> None:
    name = get_setting("user_name")
    page_header(f"أهلاً {name} 👋")

    c1, c2, _ = st.columns([1, 1, 2])
    year = year_picker("dash_year", c1)
    month = month_picker("dash_month", c2)
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
    c = st.columns(5)
    with c[0]:
        kpi("استلمت خلال الشهر", fmt(s["income"]), sub_in, "good")
    with c[1]:
        kpi("صرفت", fmt(s["expenses"]), f"{s['count']} عملية", "bad")
    with c[2]:
        kpi("ادخرت", fmt(s["saving"]), f"{s['rate']:.1f}% من الدخل", rate_tone(s["rate"]))
    with c[3]:
        kpi("الرصيد المرحل", fmt(s["opening"]), s["carry_note"] or "لا يوجد ترحيل")
    with c[4]:
        kpi("متبقي معك", fmt(s["closing"]), "المرحل + المستلم − المصروف", "main")

    section("💼 التفاصيل المالية")
    fdf, assets, liab = fin_totals()
    if fdf.empty:
        st.info("لم تسجّل وضعك المالي الحالي بعد (المبلغ المدخر بالفعل وأي تفاصيل أخرى). "
                "سجّله من صفحة «التفاصيل المالية».")
    else:
        f = st.columns(3)
        with f[0]:
            kpi("مدخراتك وأصولك", fmt(assets), f"{len(fdf)} بند", "good")
        with f[1]:
            kpi("التزامات عليك", fmt(liab), "", "bad" if liab else "")
        with f[2]:
            kpi("إجمالي ما تملكه تقريبًا", fmt(assets - liab + s["closing"]), "التفاصيل المالية + متبقي معك")
        st.caption("لو سجّلت مدخراتك السابقة كرصيد مرحل في أول شهر، لا تسجّلها هنا أيضًا حتى لا تُحسب مرتين.")
        with st.expander("عرض بنود التفاصيل المالية"):
            show_df(pd.DataFrame({"النوع": fdf.kind, "البند": fdf.name, "المبلغ": fdf.amount.map(fmt),
                                  "ملاحظة": fdf.note.fillna("")}))

    with st.expander("➕ إضافة مصروف سريع"):
        render_expense_form("dash")

    df = year_summary(year)
    data = df[df.has_data]
    if data.empty:
        st.info("لا توجد بيانات لهذه السنة بعد. ابدأ بتسجيل ما قبضته من «التفاصيل الشهرية».")
        return

    left, right = st.columns([3, 2])
    with left:
        section(f"استلمت وصرفت شهريًا — {year}")
        long = data[["name", "income", "expenses"]].melt("name", var_name="type", value_name="amount")
        long["type"] = long["type"].map({"income": "استلمت", "expenses": "صرفت"})
        ch = (alt.Chart(long).mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
              .encode(x=alt.X("name:N", sort=MONTHS_AR, title=None), xOffset="type:N",
                      y=alt.Y("amount:Q", title=None),
                      color=alt.Color("type:N", scale=alt.Scale(domain=["استلمت", "صرفت"], range=[C_INCOME, C_EXPENSE]),
                                     legend=alt.Legend(title=None, orient="top")),
                      tooltip=[alt.Tooltip("name:N", title="الشهر"), alt.Tooltip("type:N", title="النوع"),
                               alt.Tooltip("amount:Q", title="المبلغ", format=",.0f")])
              .properties(height=320))
        show_chart(ch)
    with right:
        section(f"مصاريف {MONTHS_AR[month - 1]} حسب التصنيف")
        cats = category_totals(year, month)
        if cats.empty:
            st.info("لا توجد مصاريف لهذا الشهر بعد.")
        else:
            show_chart(donut(cats))


def page_expenses() -> None:
    page_header("💸 سجل المصاريف", "إضافة وتعديل المصاريف اليومية وتتبع المرفقات")

    tab1, tab2 = st.tabs(["➕ إضافة مصروف جديد", "📋 قائمة المصاريف"])

    with tab1:
        render_expense_form("page_exp")

    with tab2:
        c1, c2, c3 = st.columns([1, 1, 2])
        y = year_picker("exp_y", c1)
        m = month_picker("exp_m", c2, allow_all=True)
        query = c3.text_input("🔍 بحث بالتفاصيل أو التصنيف", key="exp_q")

        sql = "SELECT id, expense_date, amount, details, category FROM daily_expenses WHERE year=? "
        params = [y]
        if m > 0:
            sql += "AND month=? "
            params.append(m)
        if query.strip():
            sql += "AND (details LIKE ? OR category LIKE ?) "
            params.extend([f"%{query.strip()}%", f"%{query.strip()}%"])
        sql += "ORDER BY expense_date DESC, id DESC"

        df = query_df(sql, tuple(params))
        if df.empty:
            st.info("لا توجد مصاريف مطابقة للبحث.")
            return

        st.write(f"**إجمالي المصاريف المعروضة:** {fmt(df.amount.sum())} ({len(df)} عملية)")

        df_display = df.copy()
        df_display["amount"] = df_display["amount"].map(fmt)
        df_display.columns = ["م", "التاريخ", "المبلغ", "التفاصيل", "التصنيف"]
        show_df(df_display)

        selected_id = st.selectbox("اختر رقم العملية (م) للتعديل أو الحذف أو عرض المرفقات",
                                   df["id"].tolist(), format_func=lambda x: f"عملية رقم #{x}")
        if selected_id:
            render_edit_panel(selected_id)


def page_monthly_details() -> None:
    page_header("📅 التفاصيل الشهرية", "تسجيل الدخل والراتب والرصيد المرحل لكل شهر")

    c1, c2 = st.columns(2)
    y = year_picker("m_det_y", c1)
    m = month_picker("m_det_m", c2)

    df_m = query_df("SELECT * FROM monthly_data WHERE year=? AND month=?", (y, m))
    salary = float(df_m.basic_salary[0]) if not df_m.empty and df_m.basic_salary[0] else 0.0
    cash = float(df_m.cash_income[0]) if not df_m.empty and df_m.cash_income[0] else 0.0
    opening = float(df_m.opening_balance[0]) if not df_m.empty and df_m.opening_balance[0] else 0.0
    note = str(df_m.carry_note[0]) if not df_m.empty and df_m.carry_note[0] else ""

    section(f"بيانات شهر {MONTHS_AR[m - 1]} {y}")
    with st.form("monthly_form"):
        col1, col2 = st.columns(2)
        new_salary = col1.number_input("الراتب الأساسي", min_value=0.0, value=salary, step=500.0)
        new_cash = col2.number_input("دخل كاش / إضافي", min_value=0.0, value=cash, step=500.0)
        col3, col4 = st.columns(2)
        new_opening = col3.number_input("رصيد مرحل من الشهر السابق", min_value=0.0, value=opening, step=500.0)
        new_note = col4.text_input("ملاحظة الترحيل", value=note)

        if st.form_submit_button("💾 حفظ البيانات الشهرية", type="primary"):
            run("INSERT INTO monthly_data (year, month, basic_salary, cash_income, opening_balance, carry_note) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(year, month) DO UPDATE SET "
                "basic_salary=excluded.basic_salary, cash_income=excluded.cash_income, "
                "opening_balance=excluded.opening_balance, carry_note=excluded.carry_note",
                (y, m, new_salary, new_cash, new_opening, new_note))
            flash("✅ تم حفظ البيانات الشهرية بنجاح.")
            st.rerun()

    section("📥 مقبوضات وأموال واردة أخرى خلال الشهر")
    inc_df = query_df("SELECT id, amount, received_date, source, note FROM incomes WHERE for_year=? AND for_month=?", (y, m))
    if not inc_df.empty:
        inc_disp = inc_df.copy()
        inc_disp["amount"] = inc_disp["amount"].map(fmt)
        show_df(inc_disp)

    with st.expander("➕ إضافة دخل/مقبوضات أخرى"):
        a, b = st.columns(2)
        inc_amt = a.number_input("المبلغ", min_value=0.0, step=100.0)
        inc_src = b.selectbox("المصدر", INCOME_SOURCES)
        inc_dt = st.date_input("تاريخ الاستلام", value=date.today())
        inc_nt = st.text_input("ملاحظات")
        if st.button("➕ تسجيل الدخل"):
            if inc_amt > 0:
                run("INSERT INTO incomes (amount, for_year, for_month, received_date, source, note, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (inc_amt, y, m, inc_dt.isoformat(), inc_src, inc_nt, datetime.now().isoformat()))
                flash("✅ تم تسجيل الدخل الإضافي.")
                st.rerun()


def page_financial_details() -> None:
    page_header("💼 التفاصيل المالية", "إدارة الحسابات البنكية، المدخرات، الذهب، والالتزامات")

    df, assets, liab = fin_totals()

    c1, c2, c3 = st.columns(3)
    c1.metric("إجمالي الأصول والمدخرات", fmt(assets))
    c2.metric("إجمالي الالتزامات (الديون)", fmt(liab))
    c3.metric("صافي الثروة المسجلة", fmt(assets - liab))

    section("البنود المسجلة حاليًا")
    if not df.empty:
        for idx, r in df.iterrows():
            col1, col2, col3, col4, col5 = st.columns([2, 3, 2, 3, 1])
            col1.write(f"**{r.kind}**")
            col2.write(r['name'])
            col3.write(fmt(r.amount))
            col4.write(r.note or "-")
            if col5.button("🗑️", key=f"del_fin_{r.id}"):
                run("DELETE FROM fin_details WHERE id=?", (r.id,))
                flash("تم حذف البند.")
                st.rerun()

    section("➕ إضافة بند جديد")
    with st.form("new_fin"):
        f1, f2 = st.columns(2)
        kind = f1.selectbox("نوع البند", FIN_KINDS)
        name = f2.text_input("اسم البند (مثال: بنك مصر، الذهب، دين لفلان)")
        f3, f4 = st.columns(2)
        amount = f3.number_input("المبلغ / القيمة", min_value=0.0, step=1000.0)
        note = f4.text_input("ملاحظات")

        if st.form_submit_button("إضافة البند", type="primary"):
            if name.strip() and amount > 0:
                run("INSERT INTO fin_details (kind, name, amount, note) VALUES (?,?,?,?)",
                    (kind, name.strip(), amount, note.strip()))
                flash("✅ تمت إضافة البند المالي.")
                st.rerun()


def page_budgets() -> None:
    page_header("🎯 الميزانيات والأهداف", "تحديد الحدود الشهرية للإنفاق وتتبع الأهداف المالية")

    tab1, tab2 = st.tabs(["📉 الميزانيات الشهرية", "🚀 الأهداف المالية"])

    with tab1:
        section("تحديد حد أقصى للإنفاق حسب التصنيف")
        budgets = get_budgets()
        cats = all_categories()

        with st.form("budget_form"):
            new_budgets = {}
            for c in cats:
                current_limit = budgets.get(c, 0.0)
                new_budgets[c] = st.number_input(f"حد الإنفاق لـ ({c})", min_value=0.0, value=float(current_limit), step=500.0)

            if st.form_submit_button("💾 حفظ الميزانيات", type="primary"):
                stmts = []
                for cat_name, limit in new_budgets.items():
                    stmts.append(("INSERT INTO budgets (category, monthly_limit) VALUES (?,?) "
                                  "ON CONFLICT(category) DO UPDATE SET monthly_limit=excluded.monthly_limit",
                                  (cat_name, limit)))
                run_many(stmts)
                flash("✅ تم حفظ الحدود المالية بنجاح.")
                st.rerun()

    with tab2:
        section("الأهداف الحالية")
        goals_df = query_df("SELECT * FROM goals")
        if not goals_df.empty:
            for _, g in goals_df.iterrows():
                pct = min(1.0, g.saved / g.target) if g.target > 0 else 0.0
                st.write(f"**{g['name']}** — {fmt(g.saved)} / {fmt(g.target)}")
                st.progress(pct)

        with st.expander("➕ إضافة هدف جديد"):
            with st.form("new_goal"):
                g_name = st.text_input("اسم الهدف (مثال: شراء سيارة، صندوق الطوارئ)")
                g_target = st.number_input("المبلغ المستهدف", min_value=100.0, step=1000.0)
                g_saved = st.number_input("المبلغ المدخر حاليًا", min_value=0.0, step=500.0)

                if st.form_submit_button("إضافة الهدف"):
                    if g_name.strip() and g_target > 0:
                        run("INSERT INTO goals (name, target, saved, created_at) VALUES (?,?,?,?)",
                            (g_name.strip(), g_target, g_saved, datetime.now().isoformat()))
                        flash("✅ تم إدراج الهدف جديد.")
                        st.rerun()


def page_wishes() -> None:
    page_header("✨ قائمة الأمنيات", "تخطيط الشراء وترتيب الأولويات حسب الاحتياجات والرغبات")

    wishes_df = query_df("SELECT * FROM wishes WHERE status='active' ORDER BY id DESC")

    if not wishes_df.empty:
        for _, w in wishes_df.iterrows():
            with st.container(border=True):
                c1, c2, c3, c4 = st.columns([3, 2, 2, 1])
                c1.write(f"### {w['name']}")
                c1.caption(f"النوع: {w.kind or 'غير محدد'} | ملاحظة: {w.note or '-'}")
                c2.metric("السعر المطلوبة", fmt(w.price))
                c3.metric("الموفر حاليًا", fmt(w.saved))
                if c4.button("✅ تم الشراء", key=f"buy_{w.id}"):
                    run("UPDATE wishes SET status='bought', bought_at=? WHERE id=?",
                        (datetime.now().isoformat(), w.id))
                    flash("🎉 مبروك! تم نقل العنصر إلى المشتريات.")
                    st.rerun()

    section("➕ إضافة أمنية / رغبة جديدة")
    with st.form("new_wish"):
        w1, w2 = st.columns(2)
        w_name = w1.text_input("اسم السلعة / الخدمة")
        w_kind = w2.selectbox("النوع", ["احتياج ضروري", "رغبة ترفيهية", "استثمار/تطوير"])
        w3, w4 = st.columns(2)
        w_price = w3.number_input("السعر المتوقع", min_value=0.0, step=500.0)
        w_saved = w4.number_input("المبلغ المخصص لها حاليًا", min_value=0.0, step=100.0)
        w_note = st.text_input("رابط المنتج أو ملاحظات إضافية")

        if st.form_submit_button("إضافة للقائمة", type="primary"):
            if w_name.strip() and w_price > 0:
                run("INSERT INTO wishes (name, kind, price, saved, note, created_at) VALUES (?,?,?,?,?,?)",
                    (w_name.strip(), w_kind, w_price, w_saved, w_note.strip(), datetime.now().isoformat()))
                flash("✅ تمت الإضافة لقائمة الأمنيات.")
                st.rerun()


def page_ai_advisor() -> None:
    page_header("🤖 المساعد المالي الذكي", "استشارات مالية وتحليل مخصص بناءً على بياناتك")

    if not ai_ready():
        st.warning("⚠️ يرجى إضافة مفتاح Anthropic API من صفحة الإعدادات لاستخدام المساعد الذكي.")
        return

    st.info("💡 المساعد قادِر على الاطلاع على ملخص بياناتك الحالية وتوفير نصائح مخصصة لزيادة ادخارك وتقليل مصاريفك.")

    context_data = build_context()

    if "ai_chat" not in st.session_state:
        st.session_state.ai_chat = []

    for msg in st.session_state.ai_chat:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])

    user_q = st.chat_input("اسأل المساعد المالي شيئًا... (مثال: كيف أوزع مصاريفي الشهر القادم؟)")
    if user_q:
        st.session_state.ai_chat.append({"role": "user", "content": user_q})
        with st.chat_message("user"):
            st.write(user_q)

        sys_prompt = f"أنت مستشار مالي شخصي ذكي وناصح. تعتمد على البيانات المالية المرفقة للمستخدم وتجيب باللغة العربية بأسلوب مشجع ومباشر.\n\nبيانات المستخدم المالية:\n{context_data}"
        messages = [{"role": m["role"], "content": m["content"]} for m in st.session_state.ai_chat]

        with st.chat_message("assistant"):
            with st.spinner("جاري التحليل والرد..."):
                try:
                    reply = ask_claude(sys_prompt, messages)
                    st.write(reply)
                    st.session_state.ai_chat.append({"role": "assistant", "content": reply})
                except Exception as ex:
                    st.error(f"حدث خطأ أثناء التواصل مع المساعد: {ex}")


def page_settings() -> None:
    page_header("⚙️ الإعدادات", "تعديل إعدادات التطبيق والمفتاح البرمجي")

    section("بيانات المستخدم")
    curr_name = get_setting("user_name")
    new_name = st.text_input("اسم المستخدم", value=curr_name)
    if st.button("حفظ الاسم"):
        if new_name.strip():
            set_setting("user_name", new_name.strip())
            flash("✅ تم تحديث الاسم.")
            st.rerun()

    section("إعدادات الذكاء الاصطناعي Anthropic AI")
    curr_key = get_setting("api_key")
    api_key_in = st.text_input("Anthropic API Key", value=curr_key, type="password")
    selected_model = st.selectbox("الموديل المستخدم", [MODEL_DEFAULT, "claude-3-5-sonnet-20241022", "claude-3-haiku-20240307"],
                                  index=0)

    if st.button("حفظ إعدادات الذكاء الاصطناعي", type="primary"):
        set_setting("api_key", api_key_in.strip())
        st.session_state["api_key"] = api_key_in.strip()
        set_setting("ai_model", selected_model)
        flash("✅ تم حفظ إعدادات API.")
        st.rerun()

    section("إدارة البيانات")
    if st.button("🔴 إفراغ كافة البيانات (إعادة ضبط المصنع)"):
        if st.checkbox("أؤكد رغبتي في مسح كافة البيانات المسجلة تمامًا"):
            run_many([
                ("DELETE FROM monthly_data", ()),
                ("DELETE FROM daily_expenses", ()),
                ("DELETE FROM budgets", ()),
                ("DELETE FROM goals", ()),
                ("DELETE FROM attachments", ()),
                ("DELETE FROM incomes", ()),
                ("DELETE FROM fin_details", ()),
                ("DELETE FROM wishes", ()),
                ("DELETE FROM settings", ())
            ])
            flash("⚠️ تم مسح جميع البيانات بنجاح.")
            st.rerun()


# ==========================================================
# 8. التوجيه الرئيسي للبرنامج (Main Loop)
# ==========================================================
def main():
    inject_css()
    get_conn()
    ensure_profile()

    if "flash" in st.session_state and st.session_state["flash"]:
        st.toast(st.session_state["flash"])
        st.session_state["flash"] = None

    pages = {
        "📊 لوحة التحكم": page_dashboard,
        "💸 سجل المصاريف": page_expenses,
        "📅 التفاصيل الشهرية": page_monthly_details,
        "💼 التفاصيل المالية": page_financial_details,
        "🎯 الميزانيات والأهداف": page_budgets,
        "✨ قائمة الأمنيات": page_wishes,
        "🤖 المساعد الذكي": page_ai_advisor,
        "⚙️ الإعدادات": page_settings,
    }

    st.sidebar.title("📌 القائمة الرئيسية")
    choice = st.sidebar.radio("الانتقال إلى", list(pages.keys()))

    st.sidebar.markdown("---")
    st.sidebar.caption("نظامي المالي الشخصي v2.0")

    pages[choice]()


if __name__ == "__main__":
    main()