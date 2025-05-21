import os, json, zipfile, threading, requests, re, smtplib, traceback, hashlib
from datetime import datetime, date, time as t, timedelta, timezone
from io import BytesIO
from email.message import EmailMessage
from urllib.parse import unquote_plus

import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx

# ─── KONSTANTE ───────────────────────────────────────────────────
DEFAULT_TOKEN = st.secrets.get("WIALON_TOKEN", "")   # ili ostavi "" bez secrets
DATE_RE  = re.compile(r"20\d{6}")                    # hvata TAČNO 8 cifara YYYYMMDD
EU_BG    = timezone(timedelta(hours=2))              # Europe/Belgrade
SETFILE  = "smtp_settings.json"
TIMERKEY = "auto_timer"

# ─── POMOĆNE ─────────────────────────────────────────────────────
def normalize_base_url(u: str) -> str:
    u = u.rstrip("/")
    return u + "/wialon/ajax.html" if not u.endswith("/wialon/ajax.html") else u

sha = lambda s: hashlib.sha256(s.encode()).hexdigest()

def load_settings() -> dict:
    if os.path.exists(SETFILE):
        try: return json.load(open(SETFILE, encoding="utf-8"))
        except Exception: pass
    return {}

def save_settings(s: dict) -> None:
    json.dump(s, open(SETFILE, "w", encoding="utf-8"))

# ─── WIALON API WRAPPER ──────────────────────────────────────────
def login_token(token: str, base: str) -> str | None:
    try:
        r = requests.get(base,
                         params={"svc": "token/login",
                                 "params": json.dumps({"token": token})},
                         timeout=20).json()
        if isinstance(r, dict) and "error" in r: raise RuntimeError(r)
        return r["eid"]
    except Exception as e:
        st.error(e); return None

def wialon_call(svc: str, sid: str, params: dict | None,
                base: str, *, get=False, retry=True):
    payload = {"svc": svc, "sid": sid}
    if params is not None:
        payload["params"] = json.dumps(params, separators=(",", ":"))
    req = requests.get if get else requests.post
    resp = req(base,
               params=payload if get else None,
               data=payload if not get else None,
               timeout=20).json()

    # error 5 = invalid/expired SID → pokušaj login tokenom i ponovi
    if retry and isinstance(resp, dict) and resp.get("error") == 5:
        token = st.session_state["settings"].get("token", "")
        if token:
            new_sid = login_token(token, base)
            if new_sid:
                st.session_state["settings"]["sid"] = new_sid
                save_settings(st.session_state["settings"])
                return wialon_call(svc, new_sid, params, base,
                                   get=get, retry=False)
    return resp

def get_units(sid: str, base: str):
    res = wialon_call(
        "core/search_items", sid,
        {"spec": {"itemsType": "avl_unit", "propName": "sys_name",
                  "propValueMask": "*", "sortType": "sys_name"},
         "force": 1, "flags": 1, "from": 0, "to": 0},
        base)
    if isinstance(res, dict) and "error" in res: raise RuntimeError(res)
    return [{"id": it["id"],
             "name": it.get("nm", "N/A"),
             "reg": it.get("prp", {}).get("reg_number", "")} for it in res["items"]]

def list_files(sid: str, uid: int, day: date, base: str):
    res = wialon_call(
        "file/list", sid,
        {"itemId": uid, "storageType": 2, "path": "tachograph/",
         "mask": "*", "recursive": False, "fullPath": False},
        base)
    if isinstance(res, dict) and "error" in res: raise RuntimeError(res)

    out = []
    for f in res:
        # po ct/mt
        for key in ("ct", "mt"):
            ts = f.get(key)
            if ts and datetime.fromtimestamp(ts, tz=timezone.utc).date() == day:
                out.append(f); break
        else:
            m = DATE_RE.search(f["n"])
            if m:
                try:
                    file_date = datetime.strptime(m.group(), "%Y%m%d").date()
                    if file_date == day:
                        out.append(f)
                except ValueError:
                    pass
    out.sort(key=lambda x: x.get("mt", x.get("ct", 0)), reverse=True)
    return out

def get_file(sid: str, uid: int, fname: str, base: str) -> bytes | None:
    r = requests.get(base,
        params={"svc": "file/get", "sid": sid,
                "params": json.dumps({"itemId": uid, "storageType": 2,
                                      "path": f"tachograph/{fname}"})}, timeout=20)
    return r.content if r.status_code == 200 else None

# ─── MAIL & SCHEDULER ───────────────────────────────────────────
def send_mail(subj: str, body: str, att: bytes | None, fname: str, s: dict):
    try:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subj, s["username"], s["recipients"]
        msg.set_content(body)
        if att:
            msg.add_attachment(att, maintype="application",
                               subtype="zip", filename=fname)
        with smtplib.SMTP(s["server"], int(s["port"])) as smtp:
            smtp.starttls(); smtp.login(s["username"], s["password"]); smtp.send_message(msg)
    except Exception as e:
        st.error(f"SMTP greška: {e}")

def schedule_nightly(base: str):
    tmr: threading.Timer | None = st.session_state.get(TIMERKEY)
    if tmr and tmr.is_alive(): tmr.cancel()

    s = st.session_state["settings"]
    if not s.get("auto_send"): return

    now = datetime.now(EU_BG)
    run_dt = datetime.combine(
        now.date() + (timedelta(days=1) if now.time() >= t(2, 5) else timedelta()),
        t(2, 5), tzinfo=EU_BG)
    delay = (run_dt - now).total_seconds()

    def job():
        try:
            sid, baseu = s.get("sid"), s["base_url"]
            if not sid: return
            units = get_units(sid, baseu)
            prev  = (datetime.now(EU_BG) - timedelta(days=1)).date()
            buf   = BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                for u in units:
                    for f in list_files(sid, u["id"], prev, baseu):
                        d = get_file(sid, u["id"], f["n"], baseu)
                        if d: z.writestr(os.path.join(u["reg"] or u["name"], f["n"]), d)
            buf.seek(0)
            send_mail(f"DDD fajlovi {prev:%d.%m.%Y}",
                      "Automatski ZIP za sva vozila.",
                      buf.read(), f"DDD_{prev}.zip", s)
        except Exception:
            traceback.print_exc()
        finally:
            schedule_nightly(base)

    tmr = threading.Timer(delay, job); tmr.daemon = True
    add_script_run_ctx(tmr); tmr.start()
    st.session_state[TIMERKEY] = tmr

# ─── UI / MAIN ─────────────────────────────────────────────────
def main():
    st.set_page_config("Wialon DDD Manager", layout="wide")

    qs = st.experimental_get_query_params()
    base_url = normalize_base_url(unquote_plus(qs.get("baseUrl", ["https://hst-api.wialon.com"])[0]))
    sid_qs   = qs.get("sid", [None])[0]

    if "settings" not in st.session_state:
        st.session_state["settings"] = load_settings()
    s = st.session_state["settings"]
    s.setdefault("base_url", base_url)
    if sid_qs: s["sid"] = sid_qs

    page = st.sidebar.radio("Navigacija", ["Files", "Admin"])

    # ─── ADMIN TAB ───
    if page == "Admin":
        if s.get("admin_pw_hash"):
            if not st.session_state.get("admin_ok"):
                pwd = st.sidebar.text_input("Admin lozinka", type="password")
                if st.sidebar.button("Login"):
                    st.session_state["admin_ok"] = (sha(pwd) == s["admin_pw_hash"])
                    if not st.session_state["admin_ok"]: st.sidebar.error("Pogrešna lozinka")
                    st.experimental_rerun()
                st.stop()
        else:
            st.sidebar.info("Postavi početnu lozinku")
            npw = st.sidebar.text_input("Nova lozinka", type="password")
            if st.sidebar.button("Postavi"):
                s["admin_pw_hash"] = sha(npw); save_settings(s); st.sidebar.success("Sačuvano."); st.stop()

        st.header("Admin panel")
        if st.checkbox("Promeni lozinku"):
            npw = st.text_input("Nova lozinka", type="password")
            if st.button("Sačuvaj novu lozinku"):
                s["admin_pw_hash"] = sha(npw); save_settings(s)
                st.success("Lozinka promenjena."); st.experimental_rerun()

        st.subheader("Wialon token")
        s["token"] = st.text_input("Token", s.get("token", DEFAULT_TOKEN), type="password")
        st.subheader("SMTP")
        s["server"]     = st.text_input("Server",     s.get("server", ""))
        s["port"]       = st.text_input("Port",       s.get("port", "587"))
        s["username"]   = st.text_input("Username",   s.get("username", ""))
        s["password"]   = st.text_input("Password",   s.get("password", ""), type="password")
        s["recipients"] = st.text_input("Recipients", s.get("recipients", ""))

        s["auto_send"] = st.checkbox("Noćni auto-mail (02:05)", value=s.get("auto_send", False))

        c1, c2 = st.columns(2)
        if c1.button("Sačuvaj"):
            save_settings(s); schedule_nightly(s["base_url"]); st.success("Sačuvano.")
        if c2.button("Test e-mail"):
            send_mail("Test", "SMTP test", None, "", s); st.success("Poslat.")

    # ─── FILES TAB ───
    else:
        if not s.get("sid"):
            if st.button("Login tokenom"):
                sid = login_token(s.get("token", DEFAULT_TOKEN), s["base_url"])
                if sid:
                    s["sid"] = sid; save_settings(s); schedule_nightly(s["base_url"]); st.experimental_rerun()
            st.info("Dodaj ?sid=... u URL ili se prijavi tokenom."); st.stop()

        try:
            units = get_units(s["sid"], s["base_url"])
        except Exception as e:
            st.error(e); st.stop()

        col_left, col_right = st.columns([1, 2])

        # LEFT pane
        with col_left:
            st.markdown("### Vozila")
            day = st.date_input("Datum", date.today())
            q   = st.text_input("Pretraga")
            flt = [u for u in units if q.lower() in (u["reg"]+u["name"]).lower()]
            if not flt: st.warning("Nema vozila."); st.stop()
            sel_lbl = st.radio("Lista vozila",
                               [f"{u['reg']}  —  {u['name']}" for u in flt], index=0)
            unit = next(u for u in flt if f"{u['reg']}  —  {u['name']}" == sel_lbl)

        # RIGHT pane
        with col_right:
            st.markdown(f"### Fajlovi za **{unit['reg'] or unit['name']}**")
            try:
                files = list_files(s["sid"], unit["id"], day, s["base_url"])
            except Exception as e:
                st.error(e); st.stop()
            if not files: st.info("Nema fajlova."); st.stop()

            picked = []
            for f in files:
                if st.checkbox(f["n"], key=f"{unit['id']}_{f['n']}"):
                    picked.append(f["n"])

            st.write("---")
            if not picked:
                st.info("Izaberi fajlove."); st.stop()

            c1, c2 = st.columns(2)

            # DOWNLOAD
            with c1:
                if len(picked) == 1:
                    data = get_file(s["sid"], unit["id"], picked[0], s["base_url"])
                    if data:
                        st.download_button("Preuzmi fajl", data, picked[0], mime="application/octet-stream")
                else:
                    buf = BytesIO()
                    with zipfile.ZipFile(buf, "w") as z:
                        for fn in picked:
                            d = get_file(s["sid"], unit["id"], fn, s["base_url"])
                            if d: z.writestr(fn, d)
                    buf.seek(0)
                    st.download_button("Preuzmi ZIP", buf.read(),
                                       f"{unit['reg']}_{day}.zip", mime="application/zip")

            # EMAIL
            with c2:
                if st.button("Pošalji e-mail"):
                    if len(picked) == 1:
                        att   = get_file(s["sid"], unit["id"], picked[0], s["base_url"])
                        fname = picked[0]
                    else:
                        buf = BytesIO()
                        with zipfile.ZipFile(buf, "w") as z:
                            for fn in picked:
                                d = get_file(s["sid"], unit["id"], fn, s["base_url"])
                                if d: z.writestr(fn, d)
                        buf.seek(0); att = buf.read()
                        fname = f"{unit['reg']}_{day}.zip"

                    send_mail(f"DDD fajlovi — {unit['reg'] or unit['name']}",
                              "Izabrani fajlovi u prilogu.",
                              att, fname, s)
                    st.success("E-mail poslat!")

# ─── ENTRYPOINT ────────────────────────────────────────────────
if __name__ == "__main__":
    main()
