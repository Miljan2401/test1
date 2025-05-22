import os, json, zipfile, threading, requests, re, smtplib, hashlib, traceback
from datetime import datetime, date, time, timedelta, timezone
from io import BytesIO
from email.message import EmailMessage

import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx

# ───────── Streamlit init & dark theme ─────────
st.set_page_config("Wialon DDD Manager", layout="wide")
st.markdown(
    """
    <style>
      body,.stApp{background:#111!important;color:#EEE!important;}
      .stButton>button,.stDownloadButton>button,
      .stTextInput>div>input,.stDateInput>div,
      .stCheckbox>label>div{background:#222!important;color:#EEE!important;}
    </style>
    """,
    unsafe_allow_html=True,
)

# ───────── constants & paths ───────────────────
DATE_RE = re.compile(r"20\d{6}")
EU_BG   = timezone(timedelta(hours=2))

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
GLOBAL_FILE = os.path.join(BASE_DIR, "global_settings.json")
USER_DIR    = os.path.join(BASE_DIR, "user_settings")
TIMERKEY    = "timers"
os.makedirs(USER_DIR, exist_ok=True)

sha  = lambda s: hashlib.sha256(s.encode()).hexdigest()
norm = lambda u: u.rstrip("/") + ("/wialon/ajax.html"
                                  if not u.rstrip("/").endswith("/wialon/ajax.html") else "")

# ───────── helpers: global settings ────────────
def load_global():
    if os.path.exists(GLOBAL_FILE):
        try:
            return json.load(open(GLOBAL_FILE, encoding="utf-8"))
        except Exception:
            pass
    return {
        "server": "", "port": "587", "username": "", "password": "",
        "admin_pw_hash": "", "last_token": ""
    }

def save_global(cfg): json.dump(cfg, open(GLOBAL_FILE, "w", encoding="utf-8"))

# ───────── helpers: per-user settings ───────────
def user_file(uid: int):
    return os.path.join(USER_DIR, f"user_{uid}.json")

def load_user(uid: int):
    fn = user_file(uid)
    if os.path.exists(fn):
        try:
            return json.load(open(fn, encoding="utf-8"))
        except Exception:
            pass
    return {"recipients": "", "auto_send": False}

def save_user(uid: int, cfg: dict):
    json.dump(cfg, open(user_file(uid), "w", encoding="utf-8"))

# ───────── Wialon API helpers ───────────────────
def login_token(token: str, base: str):
    token = token.strip()
    if len(token) != 64 or any(c not in "0123456789abcdefABCDEF" for c in token):
        return None
    try:
        res = requests.get(
            base,
            params={"svc": "token/login", "params": json.dumps({"token": token})},
            timeout=20,
        ).json()
        if isinstance(res, dict) and "error" in res:
            return None
        return res["eid"], res["user"]["id"], res["user"]["nm"]
    except Exception:
        return None

def get_user_id_name(sid: str, base: str):
    """Vrati (id, ime) ili None ako SID nije validan."""
    try:
        info = requests.get(
            base,
            params={
                "svc": "core/search_items",
                "sid": sid,
                "params": json.dumps({
                    "spec":{"itemsType":"avl_user","propName":"sys_id",
                            "propValueMask":"*","sortType":"sys_name"},
                    "force":1,"flags":1,"from":0,"to":1}),
            },
            timeout=20,
        ).json()
        if isinstance(info, dict) and "error" in info:
            return None
        users = info.get("items", [])
        if not users:
            return None
        u = users[0]
        return u["id"], u.get("nm","unknown")
    except Exception:
        return None

def wialon_call(svc,sid,params,base,*,get=False,retry=True):
    payload={"svc":svc,"sid":sid}
    if params is not None:
        payload["params"]=json.dumps(params,separators=(",",":"))
    req=requests.get if get else requests.post
    res=req(base, params=payload if get else None,
            data=payload if not get else None, timeout=20).json()
    if retry and isinstance(res,dict) and res.get("error") in (1,5):
        token=st.session_state.get("token","")
        out=login_token(token,base)
        if out:
            new_sid,uid,_=out
            st.session_state.update({"sid":new_sid,"user_id":uid})
            return wialon_call(svc,new_sid,params,base,get=get,retry=False)
    return res

# (get_units, list_files, get_file ostaju isti kao u prethodnoj verziji,
#  nisu menjani - za kratkoću koda ovde nisu ponovljeni. Kopiraj ih iz
#  prethodne kompletne verzije bez promena.)

# …………………………………………………………………………………
#  Preskočeni delovi:  get_units, list_files, get_file,
#  send_mail, schedule_nightly  – ostaju identični
# …………………………………………………………………………………

# ───────── main UI ─────────────────────────────
def main():
    logo_path = os.path.join(BASE_DIR, "app_icon.png")
    if os.path.exists(logo_path):
        st.image(logo_path, width=220)

    gcfg = load_global()

    qs = st.experimental_get_query_params()
    base_url = norm(qs.get("baseUrl", ["https://hst-api.wialon.com"])[0])
    default_token = qs.get("token", [gcfg.get("last_token", "")])[0]
    st.session_state.setdefault("token", default_token)
    st.session_state.setdefault("sid", qs.get("sid", [None])[0])

    # proveri da li sid još važi
    if st.session_state["sid"] and "user_id" not in st.session_state:
        res = get_user_id_name(st.session_state["sid"], base_url)
        if res:
            uid, uname = res
            st.session_state.update({"user_id": uid, "user_name": uname})
        else:
            st.session_state["sid"] = None    # SID ne važi – neka se user prijavi
            st.info("Sesija je istekla. Prijavi se tokenom u Admin panelu.")

    page = st.sidebar.radio("Navigacija", ["Files", "Admin"])

    # (ostatak koda – Admin & Files sekcije – ostaje identičan kao
    #  u prošloj kompletnoj verziji; nije menjan.)
    # …………………………………………………………………………………

# ───────── entrypoint ───────────────────────────
if __name__ == "__main__":
    main()
