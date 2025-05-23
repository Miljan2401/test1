import os, json, zipfile, threading, requests, re, smtplib, traceback, hashlib
from datetime import datetime, date, time, timedelta, timezone
from io import BytesIO
from email.message import EmailMessage
from urllib.parse import unquote_plus

import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx

# ─── Streamlit init & dark skin ─────────────────────────
st.set_page_config("Wialon DDD Manager", layout="wide")
st.markdown(
    """
    <style>
      body,.stApp { background:#111!important; color:#EEE!important; }
      .stButton>button,.stDownloadButton>button,
      .stTextInput>div>input,.stDateInput>div,
      .stCheckbox>label>div { background:#222!important; color:#EEE!important; }
    </style>
    """,
    unsafe_allow_html=True,
)

DEFAULT_TOKEN = ""
DATE_RE       = re.compile(r"20\d{6}")
EU_BG         = timezone(timedelta(hours=2))

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
USER_DIR      = os.path.join(BASE_DIR, "user_settings")
GLOBAL_FILE   = os.path.join(BASE_DIR, "global_settings.json")
TIMERKEY      = "timers"
os.makedirs(USER_DIR, exist_ok=True)

sha  = lambda s: hashlib.sha256(s.encode()).hexdigest()
norm = lambda u: u.rstrip("/") + ("/wialon/ajax.html" if not u.rstrip("/").endswith("/wialon/ajax.html") else "")

def load_global():
    if os.path.exists(GLOBAL_FILE):
        try:
            return json.load(open(GLOBAL_FILE, encoding="utf-8"))
        except Exception:
            pass
    return {"server": "", "port": "587", "username": "", "password": "", "admin_pw_hash": ""}

def save_global(d):
    json.dump(d, open(GLOBAL_FILE, "w", encoding="utf-8"))

def load_user_by_hash(h):
    fn = os.path.join(USER_DIR, f"{h}.json")
    if os.path.exists(fn):
        try: return json.load(open(fn, encoding="utf-8"))
        except Exception: pass
    return {"token": "", "recipients": "", "auto_send": False}

def save_user_by_hash(h, d):
    json.dump(d, open(os.path.join(USER_DIR, f"{h}.json"), "w", encoding="utf-8"))
def get_token_and_hash():
    token = st.session_state.get("token", DEFAULT_TOKEN)
    ucfg = None
    tok_hash = None

    for fn in os.listdir(USER_DIR):
        path = os.path.join(USER_DIR, fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("token") == token:
                    tok_hash = fn[:-5]
                    ucfg = data
                    break
        except Exception:
            continue

    if ucfg is None:
        tok_hash = sha(token)
        ucfg = {"token": token, "recipients": "", "auto_send": False}
        save_user_by_hash(tok_hash, ucfg)

    st.session_state["token"] = token
    return token, tok_hash, ucfg

def login_token(token, base):
    token = token.strip()
    if len(token) != 64 or any(c not in "0123456789abcdefABCDEF" for c in token):
        return None
    try:
        r = requests.get(base, params={"svc": "token/login", "params": json.dumps({"token": token})}, timeout=20).json()
        if isinstance(r, dict) and "error" in r: raise RuntimeError(r)
        return r["eid"]
    except Exception as e:
        st.error(e); return None

def wialon_call(svc, sid, params, base, *, get=False, retry=True):
    payload = {"svc": svc, "sid": sid}
    if params is not None:
        payload["params"] = json.dumps(params, separators=(",", ":"))
    req = requests.get if get else requests.post
    res = req(base, params=payload if get else None, data=payload if not get else None, timeout=20).json()
    if retry and isinstance(res, dict) and res.get("error") in (1, 5):
        new_sid = login_token(st.session_state["token"], base)
        if new_sid:
            st.session_state["sid"] = new_sid
            return wialon_call(svc, new_sid, params, base, get=get, retry=False)
    return res

def get_units(sid, base):
    res = wialon_call("core/search_items", sid, {"spec": {"itemsType": "avl_unit", "propName": "sys_name", "propValueMask": "*", "sortType": "sys_name"}, "force": 1, "flags": 1, "from": 0, "to": 0}, base)
    if isinstance(res, dict) and "error" in res: raise RuntimeError(res)
    return [{"id": it["id"], "name": it.get("nm", "N/A"), "reg": it.get("prp", {}).get("reg_number", "")} for it in res["items"]]

def list_files(sid, uid, day, base):
    res = wialon_call("file/list", sid, {"itemId": uid, "storageType": 2, "path": "tachograph/", "mask": "*", "recursive": False, "fullPath": False}, base)
    if isinstance(res, dict):
        if res.get("error") == 5:
            return []
        raise RuntimeError(res)
    out = []
    for f in res:
        for k in ("ct", "mt"):
            ts = f.get(k)
            if ts and datetime.fromtimestamp(ts, timezone.utc).date() == day:
                out.append(f); break
        else:
            m = DATE_RE.search(f["n"])
            if m:
                date_str = m.group()[:8]
                try:
                    if datetime.strptime(date_str, "%Y%m%d").date() == day:
                        out.append(f)
                except ValueError:
                    pass
    out.sort(key=lambda x: x.get("mt", x.get("ct", 0)), reverse=True)
    return out

def get_file(sid, uid, fname, base):
    r = requests.get(base, params={"svc": "file/get", "sid": sid, "params": json.dumps({"itemId": uid, "storageType": 2, "path": f"tachograph/{fname}"})}, timeout=20)
    return r.content if r.status_code == 200 else None

def send_mail(subj, body, att, fname, gcfg, rcpt):
    try:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subj, gcfg["username"], rcpt
        msg.set_content(body)
        if att:
            msg.add_attachment(att, maintype="application", subtype="zip", filename=fname)
        with smtplib.SMTP(gcfg["server"], int(gcfg["port"])) as s:
            s.starttls(); s.login(gcfg["username"], gcfg["password"]); s.send_message(msg)
    except Exception as e:
        st.error(f"SMTP greška: {e}")

def schedule_nightly(base, h, ucfg, gcfg):
    timers = st.session_state.setdefault(TIMERKEY, {})
    if h in timers and timers[h].is_alive():
        timers[h].cancel()
    if not ucfg.get("auto_send"): return
    now = datetime.now(EU_BG)
    tomorrow = now.date() + (timedelta(days=1) if now.time() >= time(2, 5) else timedelta())
    run_dt = datetime.combine(tomorrow, time(2, 5), tzinfo=EU_BG)
    delay = (run_dt - now).total_seconds()
    def job():
        try:
            sid = login_token(ucfg["token"], base)
            if not sid: return
            units = get_units(sid, base)
            prev  = (datetime.now(EU_BG) - timedelta(days=1)).date()
            buf   = BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                for u in units:
                    for f in list_files(sid, u["id"], prev, base):
                        d = get_file(sid, u["id"], f["n"], base)
                        if d: z.writestr(os.path.join(u["reg"] or u["name"], f["n"]), d)
            buf.seek(0)
            send_mail(f"DDD fajlovi {prev:%d.%m.%Y}", "Automatski ZIP za sva vozila.", buf.read(), f"DDD_{prev}.zip", gcfg, ucfg["recipients"])
        finally:
            schedule_nightly(base, h, ucfg, gcfg)
    t = threading.Timer(delay, job); t.daemon = True
    add_script_run_ctx(t); t.start(); timers[h] = t

def main():
    logo = os.path.join(BASE_DIR, "app_icon.png")
    if os.path.exists(logo):
        st.image(logo, width=220)
    gcfg = load_global()
    qs = st.experimental_get_query_params()
    base_url = norm(qs.get("baseUrl", ["https://hst-api.wialon.com"])[0])
    st.session_state.setdefault("token", qs.get("token", [DEFAULT_TOKEN])[0])
    st.session_state.setdefault("sid", qs.get("sid", [None])[0])
    page = st.sidebar.radio("Navigacija", ["Files", "Admin"])
    token, tok_hash, ucfg = get_token_and_hash()

    if page == "Admin":
        st.header("Admin panel")
        if gcfg["admin_pw_hash"]:
            if not st.session_state.get("admin_ok"):
                pw = st.sidebar.text_input("Admin lozinka", type="password")
                if st.sidebar.button("Login"):
                    st.session_state["admin_ok"] = sha(pw) == gcfg["admin_pw_hash"]
                    if not st.session_state["admin_ok"]:
                        st.sidebar.error("Pogrešna lozinka")
                    st.experimental_rerun()
                st.stop()
        else:
            st.sidebar.info("Postavi admin lozinku")
            npw = st.sidebar.text_input("Nova lozinka", type="password")
            if st.sidebar.button("Postavi"):
                if npw.strip():
                    gcfg["admin_pw_hash"] = sha(npw); save_global(gcfg)
                    st.sidebar.success("Lozinka sačuvana – prijavi se")
                else:
                    st.sidebar.error("Lozinka ne može biti prazna.")
            st.stop()

        st.subheader("SMTP (globalno)")
        for fld in ("server", "port", "username", "password"):
            gcfg[fld] = st.text_input(fld.capitalize(), gcfg[fld], type="password" if fld == "password" else "default")

        if st.checkbox("Promeni admin lozinku"):
            new_pw = st.text_input("Nova lozinka", type="password")
            if st.button("Sačuvaj novu lozinku"):
                if new_pw.strip():
                    gcfg["admin_pw_hash"] = sha(new_pw); save_global(gcfg)
                    st.success("Lozinka promenjena."); st.experimental_rerun()
                else:
                    st.error("Lozinka ne može biti prazna.")

        st.subheader("Token i primaoci")
        st.session_state["token"] = st.text_input("Token", token, type="password")
        ucfg["recipients"] = st.text_input("Recipients", ucfg["recipients"])
        ucfg["auto_send"]  = st.checkbox("Noćni auto-mail (02:05)", value=ucfg["auto_send"])
        col1, col2 = st.columns(2)
        if col1.button("Sačuvaj"):
            save_global(gcfg); save_user_by_hash(tok_hash, ucfg)
            schedule_nightly(base_url, tok_hash, ucfg, gcfg)
            st.success("Sačuvano."); st.experimental_rerun()
        if col2.button("Test e-mail"):
            send_mail("Test", "SMTP test", None, "", gcfg, ucfg["recipients"])
            st.success("Poslat.")
        st.stop()

    if not st.session_state["sid"]:
        if st.button("Login tokenom"):
            sid = login_token(token, base_url)
            if sid:
                st.session_state["sid"] = sid
                schedule_nightly(base_url, tok_hash, ucfg, gcfg)
                st.experimental_rerun()
        st.info("Prijavi se tokenom."); st.stop()

    try:
        units = get_units(st.session_state["sid"], base_url)
    except Exception as e:
        st.error(e); st.stop()

    l, r = st.columns([1, 2])
    with l:
        st.markdown("### Vozila")
        day = st.date_input("Datum", date.today())
        q   = st.text_input("Pretraga")
        flt = [u for u in units if q.lower() in (u["reg"] + u["name"]).lower()]
        if not flt:
            st.warning("Nema vozila."); st.stop()
        label = st.radio("Lista vozila", [f"{u['reg']} — {u['name']}" for u in flt], index=0)
        unit = next(u for u in flt if f"{u['reg']} — {u['name']}" == label)

    with r:
        st.markdown(f"### Fajlovi za **{unit['reg'] or unit['name']}**")
        try:
            files = list_files(st.session_state["sid"], unit["id"], day, base_url)
        except Exception as e:
            st.error(e); st.stop()
        if not files:
            st.info("Nema fajlova."); st.stop()

        picked = [f["n"] for f in files if st.checkbox(f["n"], key=f"{unit['id']}_{f['n']}")]
        if not picked:
            st.info("Izaberi fajlove."); st.stop()

        st.write("---")
        c1, c2 = st.columns(2)
        with c1:
            if len(picked) == 1:
                data = get_file(st.session_state["sid"], unit["id"], picked[0], base_url)
                if data:
                    st.download_button("Preuzmi fajl", data, picked[0], mime="application/octet-stream")
            else:
                buf = BytesIO()
                with zipfile.ZipFile(buf, "w") as z:
                    for fn in picked:
                        d = get_file(st.session_state["sid"], unit["id"], fn, base_url)
                        if d: z.writestr(fn, d)
                buf.seek(0)
                st.download_button("Preuzmi ZIP", buf.read(), f"{unit['reg']}_{day}.zip", mime="application/zip")
        with c2:
            if st.button("Pošalji e-mail"):
                if len(picked) == 1:
                    att = get_file(st.session_state["sid"], unit["id"], picked[0], base_url)
                    fname = picked[0]
                else:
                    buf = BytesIO()
                    with zipfile.ZipFile(buf, "w") as z:
                        for fn in picked:
                            d = get_file(st.session_state["sid"], unit["id"], fn, base_url)
                            if d: z.writestr(fn, d)
                    buf.seek(0); att = buf.read(); fname = f"{unit['reg']}_{day}.zip"
                send_mail(f"DDD fajlovi — {unit['reg'] or unit['name']}", "Izabrani fajlovi u prilogu.", att, fname, gcfg, ucfg["recipients"])
                st.success("E-mail poslat!")

if __name__ == "__main__":
    main()
