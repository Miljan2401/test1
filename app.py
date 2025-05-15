import os, json, zipfile, threading, requests, re, smtplib, traceback
from datetime import datetime, date, time as t, timedelta, timezone
from io import BytesIO
from email.message import EmailMessage
from urllib.parse import unquote_plus

import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx   # keeps timers alive

# ───────────────────────── constants ──────────────────────────
DEFAULT_TOKEN = "975449712b54d5463a9bc22eddfacb006D57EA7D1C7F73A4690338D42F14313ED12D0C1F"
BASE_URL_DEFAULT = "https://hst-api.wialon.com/wialon/ajax.html"
DATE_RE = re.compile(r"20\d{6}")               # YYYYMMDD inside file-name
EU_BELGRADE = timezone(timedelta(hours=2))     # CET / CEST

SETTINGS_FILE = "smtp_settings.json"
AUTO_TIMER_KEY = "auto_timer"                  # stored in st.session_state

# ────────────────────────── helpers ───────────────────────────
def show_error(e: Exception | str) -> None:
    st.error(str(e))

def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        try:
            return json.load(open(SETTINGS_FILE, encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_settings(s: dict) -> None:
    json.dump(s, open(SETTINGS_FILE, "w", encoding="utf-8"))

def api_call(svc: str, sid: str, params: dict | None = None, base_url: str = BASE_URL_DEFAULT, get=False):
    try:
        payload = {"svc": svc, "sid": sid}
        if params is not None:
            payload["params"] = json.dumps(params, separators=(",", ":"))
        r = (requests.get if get else requests.post)(base_url, params=payload if get else None, data=payload if not get else None, timeout=20)
        return r.json()
    except Exception as e:
        raise RuntimeError(f"Wialon request failed: {e}")

# ───────────────────────── Wialon api ─────────────────────────
def login_with_token(token: str, base_url: str) -> str | None:
    try:
        r = requests.get(base_url, params={"svc": "token/login", "params": json.dumps({"token": token})}, timeout=20).json()
        if "error" in r:
            raise RuntimeError(f"Wialon error: {r}")
        return r["eid"]
    except Exception as e:
        show_error(e)
        return None

def get_vehicles(sid: str, base_url: str):
    res = api_call(
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
        base_url,
    )
    if "error" in res:
        raise RuntimeError(res)
    return [
        {
            "id": it["id"],
            "name": it.get("nm", "N/A"),
            "reg_number": it.get("prp", {}).get("reg_number", ""),
        }
        for it in res.get("items", [])
    ]

def list_files(sid: str, unit_id: int, day: date, base_url: str):
    res = api_call(
        "file/list",
        sid,
        {
            "itemId": unit_id,
            "storageType": 2,
            "path": "tachograph/",
            "mask": "*",
            "recursive": False,
            "fullPath": False,
        },
        base_url,
    )
    if isinstance(res, dict) and "error" in res:
        raise RuntimeError(res)
    out = []
    for f in res:
        # cr. time
        if "ct" in f and datetime.fromtimestamp(f["ct"], tz=timezone.utc).date() == day:
            out.append(f)
            continue
        # mod. time
        if "mt" in f and datetime.fromtimestamp(f["mt"], tz=timezone.utc).date() == day:
            out.append(f)
            continue
        # by name
        m = DATE_RE.search(f["n"])
        if m and datetime.strptime(m.group(), "%Y%m%d").date() == day:
            out.append(f)
    out.sort(key=lambda x: x.get("mt", x.get("ct", 0)), reverse=True)
    return out

def get_file_content(sid: str, unit_id: int, filename: str, base_url: str) -> bytes | None:
    r = requests.get(
        base_url,
        params={
            "svc": "file/get",
            "sid": sid,
            "params": json.dumps(
                {"itemId": unit_id, "storageType": 2, "path": f"tachograph/{filename}"}
            ),
        },
        timeout=20,
    )
    return r.content if r.status_code == 200 else None

# ─────────────── automatic nightly delivery ──────────────────
def schedule_nightly(base_url: str):
    """Threading timer called whenever settings change."""
    # cancel existing
    timer: threading.Timer | None = st.session_state.get(AUTO_TIMER_KEY)
    if timer and timer.is_alive():
        timer.cancel()

    s = st.session_state["settings"]
    if not s.get("auto_send"):
        return

    now = datetime.now(EU_BELGRADE)
    run_date = now.date() + (timedelta(days=1) if now.time() >= t(2, 5) else timedelta())
    run_dt = datetime.combine(run_date, t(2, 5), tzinfo=EU_BELGRADE)
    delay = (run_dt - now).total_seconds()

    def task():
        try:
            sid = s.get("sid")
            if not sid:    # Need active SID
                return
            vehs = get_vehicles(sid, base_url)
            buf = BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                prev = (datetime.now(EU_BELGRADE) - timedelta(days=1)).date()
                for v in vehs:
                    for f in list_files(sid, v["id"], prev, base_url):
                        content = get_file_content(sid, v["id"], f["n"], base_url)
                        if content:
                            zf.writestr(os.path.join(v["reg_number"] or v["name"], f["n"]), content)
            buf.seek(0)
            send_mail(
                subject=f"DDD fajlovi za {prev.strftime('%d.%m.%Y')}",
                body="Automatski ZIP sa svim jučerašnjim fajlovima.",
                attachment=buf.read(),
                filename=f"DDD_{prev}.zip",
                settings=s,
            )
        except Exception:
            traceback.print_exc()
        finally:
            # reschedule itself
            schedule_nightly(base_url)

    timer = threading.Timer(delay, task, daemon=True)
    add_script_run_ctx(timer)   # make Streamlit aware
    timer.start()
    st.session_state[AUTO_TIMER_KEY] = timer

# ───────────────────── SMTP mail sender ───────────────────────
def send_mail(subject: str, body: str, attachment: bytes | None, filename: str, settings: dict):
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = settings["username"]
        msg["To"] = settings["recipients"]
        msg.set_content(body)
        if attachment:
            msg.add_attachment(attachment, maintype="application", subtype="zip", filename=filename)

        with smtplib.SMTP(settings["server"], int(settings["port"])) as s:
            s.starttls()
            s.login(settings["username"], settings["password"])
            s.send_message(msg)
    except Exception as e:
        show_error(e)

# ────────────────────────── UI logic ──────────────────────────
def main():
    st.set_page_config(page_title="Wialon DDD Manager", layout="wide")
    qs = st.experimental_get_query_params()

    # base_url & sid from URL
    base_url = unquote_plus(qs.get("baseUrl", [BASE_URL_DEFAULT])[0])
    sid_in_url = qs.get("sid", [None])[0]

    # Shared mutable settings
    if "settings" not in st.session_state:
        st.session_state["settings"] = load_settings()

    settings = st.session_state["settings"]

    # keep sid inside settings/session
    if sid_in_url:
        settings["sid"] = sid_in_url
    elif "sid" not in settings:
        settings["sid"] = None

    # ───────── sidebar navigation ─────────
    page = st.sidebar.radio("Navigacija", ["Files", "Admin"])

    if page == "Admin":
        st.header("Admin panel")

        settings["token"] = st.text_input("Wialon token", settings.get("token", DEFAULT_TOKEN), type="password")
        st.markdown("### SMTP")
        settings["server"] = st.text_input("SMTP server", settings.get("server", ""))
        settings["port"] = st.text_input("SMTP port", settings.get("port", "587"))
        settings["username"] = st.text_input("SMTP username", settings.get("username", ""))
        settings["password"] = st.text_input("SMTP password", settings.get("password", ""), type="password")
        settings["recipients"] = st.text_input("Recipients (comma-separated)", settings.get("recipients", ""))

        settings["auto_send"] = st.checkbox(
            "Automatski pošalji jučerašnje fajlove u 02:05h", value=bool(settings.get("auto_send"))
        )

        col1, col2 = st.columns(2)
        with col1:
            if st.button("Sačuvaj"):
                save_settings(settings)
                schedule_nightly(base_url)
                st.success("Sačuvano.")
        with col2:
            if st.button("Pošalji test e-mail"):
                send_mail("Test", "SMTP test", None, "", settings)
                st.success("Poslat.")

        st.write("---")
        st.markdown("### Trenutni status")
        st.json({k: ("***" if k == "password" else v) for k, v in settings.items()})

    # ───────── Files page ─────────
    else:
        # login if no SID yet
        if not settings.get("sid"):
            if st.button("Login with token"):
                sid = login_with_token(settings.get("token", DEFAULT_TOKEN), base_url)
                if sid:
                    settings["sid"] = sid
                    st.success("Log-in uspešan.")
                    save_settings(settings)
                    schedule_nightly(base_url)
                else:
                    st.stop()
            else:
                st.info("Nema aktivnog SID-a. Prvo se prijavite u Admin panelu ili dodajte ?sid=... u URL.")
                st.stop()

        # vehicles
        try:
            vehicles = get_vehicles(settings["sid"], base_url)
        except Exception as e:
            show_error(e)
            st.stop()

        # top controls
        left, mid, right = st.columns([3, 2, 2])
        with left:
            search = st.text_input("Pretraga vozila (naziv / registracija)")
        with mid:
            target_date = st.date_input("Datum", date.today())
        with right:
            st.write("")

        # list vehicles
        rows = []
        for v in vehicles:
            if search.lower() in v["name"].lower() or search.lower() in v["reg_number"].lower():
                rows.append(v)
        if not rows:
            st.warning("Nijedno vozilo.")
            st.stop()

        # vehicle selection
        selected_name = st.selectbox(
            "Vozilo", [f"{v['reg_number']} — {v['name']}" for v in rows], index=0
        )
        selected_vehicle = next(v for v in rows if f"{v['reg_number']} — {v['name']}" == selected_name)

        # pull file list
        try:
            files = list_files(settings["sid"], selected_vehicle["id"], target_date, base_url)
        except Exception as e:
            show_error(e)
            st.stop()

        st.markdown(f"### Fajlovi ({len(files)})")
        if not files:
            st.info("Nema fajlova za taj datum.")
            st.stop()

        checked = st.multiselect(
            "Izaberite fajlove", [f["n"] for f in files], default=[f["n"] for f in files][:3]
        )

        # buttons
        c1, c2 = st.columns(2)
        if checked:
            with c1:
                # download single or zip
                if len(checked) == 1:
                    fname = checked[0]
                    content = get_file_content(settings["sid"], selected_vehicle["id"], fname, base_url)
                    if content:
                        st.download_button("Preuzmi fajl", content, file_name=fname, mime="application/octet-stream")
                else:
                    buf = BytesIO()
                    with zipfile.ZipFile(buf, "w") as zf:
                        for fname in checked:
                            data = get_file_content(settings["sid"], selected_vehicle["id"], fname, base_url)
                            if data:
                                zf.writestr(fname, data)
                    buf.seek(0)
                    st.download_button(
                        "Preuzmi ZIP", buf.read(), file_name=f"{selected_vehicle['reg_number']}_{target_date}.zip", mime="application/zip"
                    )

            with c2:
                if st.button("Pošalji odabrane mailom"):
                    att = None
                    fname = ""
                    if len(checked) == 1:
                        att = get_file_content(settings["sid"], selected_vehicle["id"], checked[0], base_url)
                        fname = checked[0]
                    else:
                        buf = BytesIO()
                        with zipfile.ZipFile(buf, "w") as zf:
                            for f in checked:
                                data = get_file_content(settings["sid"], selected_vehicle["id"], f, base_url)
                                if data:
                                    zf.writestr(f, data)
                        buf.seek(0)
                        att = buf.read()
                        fname = f"{selected_vehicle['reg_number']}_{target_date}.zip"
                    send_mail(
                        subject=f"DDD fajlovi — {selected_vehicle['reg_number']}",
                        body="Izabrani DDD fajlovi u prilogu.",
                        attachment=att,
                        filename=fname,
                        settings=settings,
                    )
                    st.success("Email poslat.")

# ───────────────────────── entrypoint ─────────────────────────
if __name__ == "__main__":
    main()
