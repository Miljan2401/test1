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

def save_global(cfg):
    json.dump(cfg, open(GLOBAL_FILE, "w", encoding="utf-8"))

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
            raise RuntimeError(res)
        return res["eid"], res["user"]["id"], res["user"]["nm"]
    except Exception as e:
        st.error(e)
        return None

def get_user_id_name(sid: str, base: str):
    info = requests.get(
        base,
        params={
            "svc": "core/search_items",
            "sid": sid,
            "params": json.dumps({
                "spec": {"itemsType": "avl_user", "propName": "sys_id",
                         "propValueMask": "*", "sortType": "sys_name"},
                "force": 1, "flags": 1, "from": 0, "to": 1
            }),
        },
        timeout=20,
    ).json()

    # ako Wialon vrati error → podigni izuzetak da se korisnik ponovo prijavi
    if isinstance(info, dict) and "error" in info:
        raise RuntimeError(info)

    users = info.get("items", [])
    if not users:
        raise RuntimeError("Nema korisničkih podataka za dati SID")

    u = users[0]
    return u["id"], u.get("nm", "unknown")

def wialon_call(svc, sid, params, base, *, get=False, retry=True):
    payload = {"svc": svc, "sid": sid}
    if params is not None:
        payload["params"] = json.dumps(params, separators=(",", ":"))
    req = requests.get if get else requests.post
    res = req(
        base,
        params=payload if get else None,
        data=payload if not get else None,
        timeout=20,
    ).json()
    if retry and isinstance(res, dict) and res.get("error") in (1, 5):
        token = st.session_state.get("token", "")
        out = login_token(token, base)
        if out:
            new_sid, uid, _ = out
            st.session_state.update({"sid": new_sid, "user_id": uid})
            return wialon_call(svc, new_sid, params, base, get=get, retry=False)
    return res

def get_units(sid, base):
    data = wialon_call(
        "core/search_items",
        sid,
        {
            "spec": {
                "itemsType": "avl_unit",
                "propName": "sys_name",
                "propValueMask": "*",
                "sortType": "sys_name",
            },
            "force": 1,
            "flags": 1,
            "from": 0,
            "to": 0,
        },
        base,
    )
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(data)
    return [
        {
            "id": it["id"],
            "name": it.get("nm", "N/A"),
            "reg": it.get("prp", {}).get("reg_number", ""),
        }
        for it in data["items"]
    ]

def list_files(sid, uid, day, base):
    data = wialon_call(
        "file/list",
        sid,
        {
            "itemId": uid,
            "storageType": 2,
            "path": "tachograph/",
            "mask": "*",
            "recursive": False,
            "fullPath": False,
        },
        base,
    )
    if isinstance(data, dict):
        if data.get("error") == 5:
            return []
        raise RuntimeError(data)

    out = []
    for f in data:
        for k in ("ct", "mt"):
            ts = f.get(k)
            if ts and datetime.fromtimestamp(ts, timezone.utc).date() == day:
                out.append(f)
                break
        else:
            m = DATE_RE.search(f["n"])
            if m:
                ds = m.group()[:8]
                try:
                    if datetime.strptime(ds, "%Y%m%d").date() == day:
                        out.append(f)
                except ValueError:
                    pass
    out.sort(key=lambda x: x.get("mt", x.get("ct", 0)), reverse=True)
    return out

def get_file(sid, uid, fname, base):
    r = requests.get(
        base,
        params={
            "svc": "file/get",
            "sid": sid,
            "params": json.dumps(
                {"itemId": uid, "storageType": 2, "path": f"tachograph/{fname}"}
            ),
        },
        timeout=20,
    )
    return r.content if r.status_code == 200 else None

# ───────── mail & scheduler ─────────────────────
def send_mail(subj, body, att, fname, gcfg, rcpt):
    try:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subj, gcfg["username"], rcpt
        msg.set_content(body)
        if att:
            msg.add_attachment(att, maintype="application", subtype="zip", filename=fname)
        with smtplib.SMTP(gcfg["server"], int(gcfg["port"])) as s:
            s.starttls()
            s.login(gcfg["username"], gcfg["password"])
            s.send_message(msg)
    except Exception as e:
        st.error(f"SMTP greška: {e}")

def schedule_nightly(base, uid, ucfg, gcfg):
    timers = st.session_state.setdefault(TIMERKEY, {})
    if uid in timers and timers[uid].is_alive():
        timers[uid].cancel()
    if not ucfg.get("auto_send"):
        return

    now = datetime.now(EU_BG)
    tomorrow = now.date() + (timedelta(days=1) if now.time() >= time(2, 5) else timedelta())
    run_dt = datetime.combine(tomorrow, time(2, 5), tzinfo=EU_BG)
    delay = (run_dt - now).total_seconds()

    def job():
        try:
            sid = st.session_state.get("sid")
            if not sid:
                return
            units = get_units(sid, base)
            prev = (datetime.now(EU_BG) - timedelta(days=1)).date()
            buf = BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                for u in units:
                    for f in list_files(sid, u["id"], prev, base):
                        d = get_file(sid, u["id"], f["n"], base)
                        if d:
                            z.writestr(os.path.join(u["reg"] or u["name"], f["n"]), d)
            buf.seek(0)
            send_mail(
                f"DDD fajlovi {prev:%d.%m.%Y}",
                "Automatski ZIP za sva vozila.",
                buf.read(),
                f"DDD_{prev}.zip",
                gcfg,
                ucfg["recipients"],
            )
        finally:
            schedule_nightly(base, uid, ucfg, gcfg)

    t = threading.Timer(delay, job)
    t.daemon = True
    add_script_run_ctx(t)
    t.start()
    timers[uid] = t

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

    # ako već imamo SID a nemamo user_id → dohvati ga
    if st.session_state["sid"] and "user_id" not in st.session_state:
        uid, uname = get_user_id_name(st.session_state["sid"], base_url)
        st.session_state.update({"user_id": uid, "user_name": uname})

    page = st.sidebar.radio("Navigacija", ["Files", "Admin"])

    # ===== ADMIN =====
    if page == "Admin":
        st.header("Admin panel")

        # admin login
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
                    gcfg["admin_pw_hash"] = sha(npw)
                    save_global(gcfg)
                    st.sidebar.success("Lozinka sačuvана – prijavi se")
                else:
                    st.sidebar.error("Lozinka ne može biti prazна.")
            st.stop()

        st.subheader("SMTP (globalno)")
        for fld in ("server", "port", "username", "password"):
            gcfg[fld] = st.text_input(
                fld.capitalize(),
                gcfg[fld],
                type="password" if fld == "password" else "default",
            )

        if st.checkbox("Promeni admin lozinku"):
            n = st.text_input("Nova lozinka", type="password")
            if st.button("Sačuvaj novu lozinku"):
                if n.strip():
                    gcfg["admin_pw_hash"] = sha(n)
                    save_global(gcfg)
                    st.success("Lozinka promenjena.")
                    st.experimental_rerun()
                else:
                    st.error("Lozinka ne može biti prazна.")

        # token & per-user
        st.subheader("Token i primaoci")
        st.session_state["token"] = st.text_input(
            "Token", st.session_state["token"], type="password"
        )

        # ensure user_id
        user_id = st.session_state.get("user_id")
        if not user_id and st.session_state["token"]:
            out = login_token(st.session_state["token"], base_url)
            if out:
                sid, user_id, uname = out
                st.session_state.update({"sid": sid, "user_id": user_id, "user_name": uname})

        if not user_id:
            st.warning("Prvo se prijavi validnim tokenom.")
            st.stop()

        ucfg = load_user(user_id)
        ucfg.setdefault("recipients", "")
        ucfg.setdefault("auto_send", False)

        ucfg["recipients"] = st.text_input("Recipients", ucfg["recipients"])
        ucfg["auto_send"] = st.checkbox("Noćni auto-mail (02:05)", value=ucfg["auto_send"])

        col1, col2 = st.columns(2)
        if col1.button("Sačuvaj"):
            gcfg["last_token"] = st.session_state["token"]
            save_global(gcfg)
            save_user(user_id, ucfg)
            schedule_nightly(base_url, user_id, ucfg, gcfg)
            st.success("Sačuvano.")
            st.experimental_rerun()

        if col2.button("Test e-mail"):
            send_mail("Test", "SMTP test", None, "", gcfg, ucfg["recipients"])
            st.success("Poslat.")
        st.stop()

    # ===== FILES =====
    if "user_id" not in st.session_state:
        st.info("Prijavi se tokenом u Admin panelu.")
        st.stop()

    user_id = st.session_state["user_id"]
    ucfg = load_user(user_id)

    if not st.session_state["sid"]:
        st.info("Prijavi se tokenом u Admin panelu.")
        st.stop()

    try:
        units = get_units(st.session_state["sid"], base_url)
    except Exception as e:
        st.error(e)
        st.stop()

    l, r = st.columns([1, 2])
    with l:
        st.markdown("### Vozila")
        day = st.date_input("Datum", date.today())
        q = st.text_input("Pretraga")
        flt = [u for u in units if q.lower() in (u["reg"] + u["name"]).lower()]
        if not flt:
            st.warning("Nema vozila.")
            st.stop()
        label = st.radio("Lista vozila", [f"{u['reg']} — {u['name']}" for u in flt], index=0)
        unit = next(u for u in flt if f"{u['reg']} — {u['name']}" == label)

    with r:
        st.markdown(f"### Fajlovi za **{unit['reg'] or unit['name']}**")
        try:
            files = list_files(st.session_state["sid"], unit["id"], day, base_url)
        except Exception as e:
            st.error(e)
            st.stop()
        if not files:
            st.info("Nema fajlova.")
            st.stop()

        picked = [
            f["n"]
            for f in files
            if st.checkbox(f["n"], key=f"{unit['id']}_{f['n']}")
        ]
        if not picked:
            st.info("Izaberi fajlove.")
            st.stop()

        st.write("---")
        c1, c2 = st.columns(2)
        with c1:
            if len(picked) == 1:
                data = get_file(st.session_state["sid"], unit["id"], picked[0], base_url)
                if data:
                    st.download_button(
                        "Preuzmi fajl",
                        data,
                        picked[0],
                        mime="application/octet-stream",
                    )
            else:
                buf = BytesIO()
                with zipfile.ZipFile(buf, "w") as z:
                    for fn in picked:
                        d = get_file(st.session_state["sid"], unit["id"], fn, base_url)
                        if d:
                            z.writestr(fn, d)
                buf.seek(0)
                st.download_button(
                    "Preuzmi ZIP",
                    buf.read(),
                    f"{unit['reg']}_{day}.zip",
                    mime="application/zip",
                )
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
                            if d:
                                z.writestr(fn, d)
                    buf.seek(0)
                    att = buf.read()
                    fname = f"{unit['reg']}_{day}.zip"
                send_mail(
                    f"DDD fajlovi — {unit['reg'] or unit['name']}",
                    "Izabrani fajlovi u prilogu.",
                    att,
                    fname,
                    gcfg,
                    ucfg["recipients"],
                )
                st.success("E-mail poslat!")

# ───────── entrypoint ───────────────────────────
if __name__ == "__main__":
    main()
