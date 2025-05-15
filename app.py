import os, json, zipfile, threading, requests, re, smtplib, traceback, hashlib
from datetime import datetime, date, time as t, timedelta, timezone
from io import BytesIO
from email.message import EmailMessage
from urllib.parse import unquote_plus

import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx

# ───────────────── constants ─────────────────
DEFAULT_TOKEN = "975449712b54d5463a9bc22eddfacb006D57EA7D1C7F73A4690338D42F14313ED12D0C1F"
DATE_RE = re.compile(r"20\d{6}")               # YYYYMMDD inside filename
EU_BG = timezone(timedelta(hours=2))           # Europe/Belgrade
SETFILE = "smtp_settings.json"                 # settings store
TIMER_KEY = "auto_timer"                       # key in st.session_state

# ───────────────── helpers ───────────────────
def normalize_base_url(u: str) -> str:
    u = u.rstrip("/")
    return u + "/wialon/ajax.html" if not u.endswith("/wialon/ajax.html") else u

def sha(pwd: str) -> str:
    return hashlib.sha256(pwd.encode()).hexdigest()

def load_settings() -> dict:
    if os.path.exists(SETFILE):
        try:
            return json.load(open(SETFILE, encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_settings(s: dict) -> None:
    json.dump(s, open(SETFILE, "w", encoding="utf-8"))

def wialon_call(svc: str, sid: str, params: dict | None, base_url: str, *, get=False):
    payload = {"svc": svc, "sid": sid}
    if params is not None:
        payload["params"] = json.dumps(params, separators=(",", ":"))
    req = requests.get if get else requests.post
    r = req(base_url,
            params=payload if get else None,
            data=payload if not get else None,
            timeout=20)
    return r.json()

# ───────────────── Wialon API ────────────────
def login_token(token: str, base_url: str) -> str | None:
    try:
        r = requests.get(
            base_url,
            params={"svc": "token/login",
                    "params": json.dumps({"token": token})},
            timeout=20).json()
        if "error" in r:
            raise RuntimeError(r)
        return r["eid"]
    except Exception as e:
        st.error(e); return None

def get_units(sid: str, base_url: str):
    res = wialon_call(
        "core/search_items", sid,
        {"spec": {"itemsType": "avl_unit",
                  "propName": "sys_name",
                  "propValueMask": "*",
                  "sortType": "sys_name"},
         "force": 1, "flags": 1, "from": 0, "to": 0},
        base_url)
    if "error" in res:
        raise RuntimeError(res)
    return [
        {"id": it["id"],
         "name": it.get("nm", "N/A"),
         "reg": it.get("prp", {}).get("reg_number", "")}
        for it in res.get("items", [])
    ]

def list_files(sid: str, uid: int, day: date, base_url: str):
    res = wialon_call(
        "file/list", sid,
        {"itemId": uid, "storageType": 2, "path": "tachograph/",
         "mask": "*", "recursive": False, "fullPath": False},
        base_url)
    if isinstance(res, dict) and "error" in res:
        raise RuntimeError(res)
    out = []
    for f in res:
        for key in ("ct", "mt"):
            if key in f and datetime.fromtimestamp(f[key], tz=timezone.utc).date() == day:
                out.append(f); break
        else:
            m = DATE_RE.search(f["n"])
            if m and datetime.strptime(m.group(), "%Y%m%d").date() == day:
                out.append(f)
    out.sort(key=lambda x: x.get("mt", x.get("ct", 0)), reverse=True)
    return out

def get_file(sid: str, uid: int, fname: str, base_url: str) -> bytes | None:
    r = requests.get(
        base_url,
        params={"svc": "file/get",
                "sid": sid,
                "params": json.dumps(
                    {"itemId": uid, "storageType": 2,
                     "path": f"tachograph/{fname}"})},
        timeout=20)
    return r.content if r.status_code == 200 else None

# ───────────── nightly auto-mail ─────────────
def send_mail(subject: str, body: str,
              att: bytes | None, fname: str, s: dict):
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = s["username"]
        msg["To"] = s["recipients"]
        msg.set_content(body)
        if att:
            msg.add_attachment(att, maintype="application",
                               subtype="zip", filename=fname)
        with smtplib.SMTP(s["server"], int(s["port"])) as smtp:
            smtp.starttls(); smtp.login(s["username"], s["password"])
            smtp.send_message(msg)
    except Exception as e:
        st.error(f"SMTP error: {e}")

def schedule_nightly(base_url: str):
    tmr: threading.Timer | None = st.session_state.get(TIMER_KEY)
    if tmr and tmr.is_alive():
        tmr.cancel()

    s = st.session_state["settings"]
    if not s.get("auto_send"):
        return

    now = datetime.now(EU_BG)
    run_dt = datetime.combine(
        now.date() + (timedelta(days=1) if now.time() >= t(2, 5) else timedelta()),
        t(2, 5), tzinfo=EU_BG)
    delay = (run_dt - now).total_seconds()

    def job():
        try:
            sid = s.get("sid"); base = s["base_url"]
            if not sid: return
            units = get_units(sid, base)
            buf = BytesIO()
            prev = (datetime.now(EU_BG) - timedelta(days=1)).date()
            with zipfile.ZipFile(buf, "w") as z:
                for u in units:
                    for f in list_files(sid, u["id"], prev, base):
                        data = get_file(sid, u["id"], f["n"], base)
                        if data:
                            z.writestr(os.path.join(u["reg"] or u["name"], f["n"]), data)
            buf.seek(0)
            send_mail(
                f"DDD fajlovi za {prev.strftime('%d.%m.%Y')}",
                "Automatski ZIP sa svim jučerašnjim fajlovima.",
                buf.read(), f"DDD_{prev}.zip", s)
        except Exception:
            traceback.print_exc()
        finally:
            schedule_nightly(base_url)

    tmr = threading.Timer(delay, job, daemon=True)
    add_script_run_ctx(tmr); tmr.start()
    st.session_state[TIMER_KEY] = tmr

# ────────────────── UI logic ─────────────────
def main():
    st.set_page_config("Wialon DDD Manager", layout="wide")

    # query-string
    qs = st.experimental_get_query_params()
    base_url = normalize_base_url(
        unquote_plus(qs.get("baseUrl", ["https://hst-api.wialon.com"])[0]))
    sid_param = qs.get("sid", [None])[0]

    # session settings
    if "settings" not in st.session_state:
        st.session_state["settings"] = load_settings()
    s = st.session_state["settings"]
    s.setdefault("base_url", base_url)

    # store sid from URL once
    if sid_param: s["sid"] = sid_param

    page = st.sidebar.radio("Navigacija", ["Files", "Admin"])

    # ───────── Admin (password-protected) ─────
    if page == "Admin":
        if s.get("admin_pw_hash"):
            if not st.session_state.get("admin_ok"):
                pw = st.sidebar.text_input("Admin lozinka", type="password")
                if st.sidebar.button("Login"):
                    if sha(pw) == s["admin_pw_hash"]:
                        st.session_state["admin_ok"] = True
                        st.experimental_rerun()
                    else:
                        st.sidebar.error("Pogrešna lozinka")
                st.stop()
        else:
            st.sidebar.info("Postavi početnu admin lozinku")
            npw = st.sidebar.text_input("Nova lozinka", type="password")
            if st.sidebar.button("Postavi"):
                s["admin_pw_hash"] = sha(npw)
                save_settings(s); st.sidebar.success("Sačuvano."); st.stop()

        st.header("Admin panel")
        # promena lozinke
        if st.checkbox("Promeni lozinku"):
            npw = st.text_input("Nova lozinka", type="password")
            if st.button("Sačuvaj novu lozinku"):
                s["admin_pw_hash"] = sha(npw); save_settings(s)
                st.success("Lozinka promenjena."); st.experimental_rerun()

        # Wialon & SMTP
        st.subheader("Wialon token")
        s["token"] = st.text_input("Token", s.get("token", DEFAULT_TOKEN), type="password")
        st.subheader("SMTP")
        s["server"] = st.text_input("Server", s.get("server", ""))
        s["port"] = st.text_input("Port", s.get("port", "587"))
        s["username"] = st.text_input("Username", s.get("username", ""))
        s["password"] = st.text_input("Password", s.get("password", ""), type="password")
        s["recipients"] = st.text_input("Recipients", s.get("recipients", ""))

        s["auto_send"] = st.checkbox("Noćni auto-mail (02:05)",
                                     value=s.get("auto_send", False))

        c1, c2 = st.columns(2)
        if c1.button("Sačuvaj"):
            save_settings(s); schedule_nightly(s["base_url"])
            st.success("Sačuvano.")
        if c2.button("Test e-mail"):
            send_mail("Test", "SMTP test", None, "", s)
            st.success("Poslato.")

    # ───────── Files page ─────────
    else:
        # ensure SID
        if not s.get("sid"):
            if st.button("Login tokenom"):
                sid = login_token(s.get("token", DEFAULT_TOKEN), s["base_url"])
                if sid:
                    s["sid"] = sid; save_settings(s)
                    schedule_nightly(s["base_url"])
                    st.success("Log-in uspešan."); st.experimental_rerun()
                else:
                    st.stop()
            st.info("Dodaj ?sid=... u URL ili se prijavi tokenom.")
            st.stop()

        # fetch units
        try:
            units = get_units(s["sid"], s["base_url"])
        except Exception as e:
            st.error(e); st.stop()

        # search, date
        left, mid, _ = st.columns([3, 2, 1])
        with left:
            q = st.text_input("Pretraga")
        with mid:
            day = st.date_input("Datum", date.today())

        choices = [u for u in units if q.lower() in (u["reg"] + u["name"]).lower()]
        if not choices:
            st.warning("Nijedno vozilo."); st.stop()

        sel = st.selectbox("Vozilo", [f"{u['reg']} — {u['name']}" for u in choices])
        unit = next(u for u in choices if f"{u['reg']} — {u['name']}" == sel)

        # list DDD
        try:
            files = list_files(s["sid"], unit["id"], day, s["base_url"])
        except Exception as e:
            st.error(e); st.stop()

        if not files:
            st.info("Nema fajlova."); st.stop()

        default_files = [files[0]["n"]]  # prvi fajl podrazumevano
        picked = st.multiselect(
            "Izaberi fajlove",
            [f["n"] for f in files],
            default=default_files)

        if picked:
            col1, col2 = st.columns(2)

            # download
            with col1:
                if len(picked) == 1:
                    data = get_file(s["sid"], unit["id"], picked[0], s["base_url"])
                    if data:
                        st.download_button("Preuzmi fajl", data,
                                           picked[0], mime="application/octet-stream")
                else:
                    buf = BytesIO()
                    with zipfile.ZipFile(buf, "w") as z:
                        for fn in picked:
                            d = get_file(s["sid"], unit["id"], fn, s["base_url"])
                            if d: z.writestr(fn, d)
                    buf.seek(0)
                    st.download_button("Preuzmi ZIP", buf.read(),
                                       f"{unit['reg']}_{day}.zip",
                                       mime="application/zip")

            # send mail
            with col2:
                if st.button("Pošalji mail"):
                    if len(picked) == 1:
                        att = get_file(s["sid"], unit["id"], picked[0], s["base_url"])
                        fname = picked[0]
                    else:
                        buf = BytesIO()
                        with zipfile.ZipFile(buf, "w") as z:
                            for fn in picked:
                                d = get_file(s["sid"], unit["id"], fn, s["base_url"])
                                if d: z.writestr(fn, d)
                        buf.seek(0); att = buf.read()
                        fname = f"{unit['reg']}_{day}.zip"
                    send_mail(f"DDD {unit['reg']}",
                              "Izabrani fajlovi",
                              att, fname, s)
                    st.success("Poslato!")

# ───────────────────── entrypoint ───────────────────
if __name__ == "__main__":
    main()
