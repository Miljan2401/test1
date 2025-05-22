import os, json, zipfile, threading, requests, re, smtplib, traceback, hashlib
from datetime import datetime, date, time as t, timedelta, timezone
from io import BytesIO
from email.message import EmailMessage
from urllib.parse import unquote_plus

import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx

# ───────────── Streamlit init & dark skin ─────────────
st.set_page_config("Wialon DDD Manager", layout="wide")
st.markdown(
    """
    <style>
      body, .stApp { background:#111!important; color:#EEE!important; }
      .stButton>button,.stDownloadButton>button,
      .stTextInput>div>input,.stDateInput>div,
      .stCheckbox>label>div { background:#222!important; color:#EEE!important; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ───────────── konstante & helperi ─────────────
DEFAULT_TOKEN = st.secrets.get("WIALON_TOKEN", "")
DATE_RE       = re.compile(r"20\d{6}")
EU_BG         = timezone(timedelta(hours=2))
USER_DIR      = "user_settings"
TIMERKEY      = "timers"

os.makedirs(USER_DIR, exist_ok=True)
sha = lambda s: hashlib.sha256(s.encode()).hexdigest()
normalize_url = lambda u: u.rstrip("/") + (
    "/wialon/ajax.html" if not u.rstrip("/").endswith("/wialon/ajax.html") else "")

def load_user_settings(tok_hash: str) -> dict:
    fn = os.path.join(USER_DIR, f"{tok_hash}.json")
    if os.path.exists(fn):
        try: return json.load(open(fn, encoding="utf-8"))
        except Exception: pass
    return {}

def save_user_settings(tok_hash: str, d: dict):
    fn = os.path.join(USER_DIR, f"{tok_hash}.json")
    json.dump(d, open(fn, "w", encoding="utf-8"))

# ───────────── Wialon API (neizmenjeno) ─────────────
def login_token(token, base):
    try:
        r = requests.get(base, params={"svc":"token/login",
                        "params":json.dumps({"token":token})}, timeout=20).json()
        if isinstance(r,dict) and "error" in r: raise RuntimeError(r)
        return r["eid"]
    except Exception as e:
        st.error(e); return None

def wialon_call(svc,sid,params,base,*,get=False,retry=True):
    payload={"svc":svc,"sid":sid}
    if params is not None: payload["params"]=json.dumps(params,separators=(",",":"))
    req=requests.get if get else requests.post
    resp=req(base, params=payload if get else None,
             data=payload if not get else None, timeout=20).json()
    if retry and isinstance(resp,dict) and resp.get("error") in (1,5):
        token=st.session_state["token"]
        if token:
            new_sid=login_token(token,base)
            if new_sid:
                st.session_state["sid"]=new_sid
                return wialon_call(svc,new_sid,params,base,get=get,retry=False)
    return resp

def get_units(sid, base):
    res=wialon_call("core/search_items",sid,
        {"spec":{"itemsType":"avl_unit","propName":"sys_name",
                 "propValueMask":"*","sortType":"sys_name"},
         "force":1,"flags":1,"from":0,"to":0}, base)
    if isinstance(res,dict) and "error" in res: raise RuntimeError(res)
    return [{"id":it["id"],"name":it.get("nm","N/A"),
             "reg":it.get("prp",{}).get("reg_number","")} for it in res["items"]]

def list_files(sid, uid, day, base):
    res=wialon_call("file/list",sid,
        {"itemId":uid,"storageType":2,"path":"tachograph/",
         "mask":"*","recursive":False,"fullPath":False}, base)
    if isinstance(res,dict):
        if res.get("error")==5: return []
        raise RuntimeError(res)
    out=[]
    for f in res:
        for k in ("ct","mt"):
            ts=f.get(k)
            if ts and datetime.fromtimestamp(ts,timezone.utc).date()==day:
                out.append(f); break
        else:
            m=DATE_RE.search(f["n"]); 
            if m:
                try:
                    if datetime.strptime(m.group(),"%Y%m%d").date()==day:
                        out.append(f)
                except ValueError: pass
    out.sort(key=lambda x:x.get("mt",x.get("ct",0)), reverse=True)
    return out

def get_file(sid, uid, fname, base):
    r=requests.get(base, params={"svc":"file/get","sid":sid,
        "params":json.dumps({"itemId":uid,"storageType":2,
                             "path":f"tachograph/{fname}"})}, timeout=20)
    return r.content if r.status_code==200 else None

# ───────────── mail & scheduler (neizmenjeno) ─────────────
def send_mail(subj,body,att,fname,cfg):
    try:
        msg=EmailMessage()
        msg["Subject"],msg["From"],msg["To"]=subj,cfg["username"],cfg["recipients"]
        msg.set_content(body)
        if att: msg.add_attachment(att,maintype="application",
                                   subtype="zip",filename=fname)
        with smtplib.SMTP(cfg["server"],int(cfg["port"])) as s:
            s.starttls(); s.login(cfg["username"],cfg["password"]); s.send_message(msg)
    except Exception as e: st.error(f"SMTP greška: {e}")

def schedule_nightly(base,tok_hash,cfg):
    timers=st.session_state.setdefault(TIMERKEY,{})
    if tok_hash in timers and timers[tok_hash].is_alive():
        timers[tok_hash].cancel()
    if not cfg.get("auto_send"): return

    run_dt=datetime.combine((d:=datetime.now(EU_BG)).date()+
            (timedelta(days=1) if d.time()>=t(2,5) else timedelta()),
            t(2,5),tzinfo=EU_BG)
    delay=(run_dt-d).total_seconds()
    def job():
        try:
            sid=st.session_state["sid"]
            if not sid: return
            units=get_units(sid,base)
            prev=(datetime.now(EU_BG)-timedelta(days=1)).date()
            buf=BytesIO()
            with zipfile.ZipFile(buf,"w") as z:
                for u in units:
                    for f in list_files(sid,u["id"],prev,base):
                        d=get_file(sid,u["id"],f["n"],base)
                        if d: z.writestr(os.path.join(u["reg"] or u["name"],f["n"]),d)
            buf.seek(0)
            send_mail(f"DDD fajlovi {prev:%d.%m.%Y}",
                      "Automatski ZIP za sva vozila.",buf.read(),f"DDD_{prev}.zip",cfg)
        finally: schedule_nightly(base,tok_hash,cfg)
    t=threading.Timer(delay,job); t.daemon=True
    add_script_run_ctx(t); t.start(); timers[tok_hash]=t

# ───────────────────── UI / MAIN ──────────────────────────────
def main():
    # logo
    if os.path.exists("app_icon.png"): st.image("app_icon.png", width=220)

    # ----- init session -----
    qs=st.experimental_get_query_params()
    base_url=normalize_url(qs.get("baseUrl",["https://hst-api.wialon.com"])[0])
    if "token" not in st.session_state:
        st.session_state["token"]=qs.get("token",[DEFAULT_TOKEN])[0]
    if "sid" not in st.session_state:
        st.session_state["sid"]=qs.get("sid",[None])[0]

    tok_hash=sha(st.session_state["token"])
    cfg=load_user_settings(tok_hash)

    page=st.sidebar.radio("Navigacija",["Files","Admin"])

    # ===== ADMIN TAB =====
    if page=="Admin":
        st.header("Admin panel")
        st.caption(f"Podešavanja se čuvaju u fajlu: `{tok_hash[:8]}…json`")

        # --- LOGIN PROVERA ---
        admin_key = f"admin_ok_{tok_hash}"
        if cfg.get("admin_pw_hash"):
            if not st.session_state.get(admin_key):
                pw = st.sidebar.text_input("Admin lozinka", type="password")
                if st.sidebar.button("Login"):
                    st.session_state[admin_key] = sha(pw) == cfg["admin_pw_hash"]
                    if not st.session_state[admin_key]:
                        st.sidebar.error("Pogrešna lozinka")
                    st.experimental_rerun()
                st.stop()
        else:
            st.sidebar.info("Postavi početnu admin lozinku")
            new_pw = st.sidebar.text_input("Nova lozinka", type="password")
            if st.sidebar.button("Postavi"):
                if new_pw.strip():
                    cfg["admin_pw_hash"] = sha(new_pw)
                    save_user_settings(tok_hash,cfg)
                    st.sidebar.success("Lozinka sačuvana – prijavi se")
                else:
                    st.sidebar.error("Lozinka ne može biti prazna.")
            st.stop()

        # --- CHANGE PASS ---
        if st.checkbox("Promeni lozinku"):
            npw = st.text_input("Nova lozinka", type="password")
            if st.button("Sačuvaj novu lozinku"):
                cfg["admin_pw_hash"] = sha(npw)
                save_user_settings(tok_hash,cfg)
                st.success("Lozinka promenjena."); st.experimental_rerun()

        # ----- ostatak admin forme -----
        st.subheader("Wialon token (za ovaj nalog)")
        st.session_state["token"] = st.text_input("Token",
                                  st.session_state["token"], type="password")

        st.subheader("SMTP")
        for fld in ("server","port","username","password","recipients"):
            cfg.setdefault(fld, "")
        cfg["server"]     = st.text_input("Server",     cfg["server"])
        cfg["port"]       = st.text_input("Port",       cfg["port"])
        cfg["username"]   = st.text_input("Username",   cfg["username"])
        cfg["password"]   = st.text_input("Password",   cfg["password"], type="password")
        cfg["recipients"] = st.text_input("Recipients", cfg["recipients"])

        cfg["auto_send"]  = st.checkbox("Noćni auto-mail (02:05)", value=cfg.get("auto_send",False))

        col1,col2=st.columns(2)
        if col1.button("Sačuvaj"):
            save_user_settings(tok_hash,cfg); schedule_nightly(base_url,tok_hash,cfg)
            st.success("Sačuvano.")
        if col2.button("Test e-mail"):
            send_mail("Test","SMTP test",None,"",cfg); st.success("Poslat.")

    # ===== FILES TAB =====
    else:
        if not st.session_state.get("sid"):
            if st.button("Login tokenom"):
                sid=login_token(st.session_state["token"],base_url)
                if sid:
                    st.session_state["sid"]=sid
                    schedule_nightly(base_url,tok_hash,cfg); st.experimental_rerun()
            st.info("Prijavi se tokenom."); st.stop()

        try:
            units=get_units(st.session_state["sid"],base_url)
        except Exception as e:
            st.error(e); st.stop()

        l,r=st.columns([1,2])
        with l:
            st.markdown("### Vozila")
            day = st.date_input("Datum", date.today())
            q = st.text_input("Pretraga")
            flt=[u for u in units if q.lower() in (u["reg"]+u["name"]).lower()]
            if not flt: st.warning("Nema vozila."); st.stop()
            label=st.radio("Lista vozila",[f"{u['reg']} — {u['name']}" for u in flt],index=0)
            unit=next(u for u in flt if f"{u['reg']} — {u['name']}"==label)

        with r:
            st.markdown(f"### Fajlovi za **{unit['reg'] or unit['name']}**")
            try: files=list_files(st.session_state["sid"],unit["id"],day,base_url)
            except Exception as e: st.error(e); st.stop()
            if not files: st.info("Nema fajlova."); st.stop()

            picked=[f["n"] for f in files if st.checkbox(f["n"],key=f"{unit['id']}_{f['n']}")]
            if not picked: st.info("Izaberi fajlove."); st.stop()
            st.write("---")

            c1,c2=st.columns(2)
            with c1:
                if len(picked)==1:
                    data=get_file(st.session_state["sid"],unit["id"],picked[0],base_url)
                    if data: st.download_button("Preuzmi fajl",data,picked[0],
                                                mime="application/octet-stream")
                else:
                    buf=BytesIO()
                    with zipfile.ZipFile(buf,"w") as z:
                        for fn in picked:
                            d=get_file(st.session_state["sid"],unit["id"],fn,base_url)
                            if d: z.writestr(fn,d)
                    buf.seek(0)
                    st.download_button("Preuzmi ZIP",buf.read(),
                                       f"{unit['reg']}_{day}.zip",mime="application/zip")
            with c2:
                if st.button("Pošalji e-mail"):
                    if len(picked)==1:
                        att=get_file(st.session_state["sid"],unit["id"],picked[0],base_url)
                        fname=picked[0]
                    else:
                        buf=BytesIO()
                        with zipfile.ZipFile(buf,"w") as z:
                            for fn in picked:
                                d=get_file(st.session_state["sid"],unit["id"],fn,base_url)
                                if d: z.writestr(fn,d)
                        buf.seek(0); att=buf.read()
                        fname=f"{unit['reg']}_{day}.zip"
                    send_mail(f"DDD fajlovi — {unit['reg'] or unit['name']}",
                              "Izabrani fajlovi u prilogu.",att,fname,cfg)
                    st.success("E-mail poslat!")

# ─────────── entrypoint ───────────
if __name__=="__main__":
    main()
