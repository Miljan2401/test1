import os, json, zipfile, threading, requests, re, smtplib, traceback, hashlib
from datetime import datetime, date, time as t, timedelta, timezone
from io import BytesIO
from email.message import EmailMessage
from urllib.parse import unquote_plus

import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx

# ─────────── konstante ───────────
DEFAULT_TOKEN = st.secrets.get("WIALON_TOKEN", "")   # ili ostavi "" ako ne koristiš secrets
DATE_RE  = re.compile(r"(20\d{6})(?:_\d{4})?")       # YYYYMMDD uz opciono _HHMM
EU_BG    = timezone(timedelta(hours=2))              # Europe/Belgrade
SETFILE  = "smtp_settings.json"
TIMERKEY = "auto_timer"

# ─────────── pomoćne ────────────
def normalize_base_url(u: str) -> str:
    u = u.rstrip("/")
    return u + "/wialon/ajax.html" if not u.endswith("/wialon/ajax.html") else u

def sha(p: str) -> str:
    return hashlib.sha256(p.encode()).hexdigest()

def load_settings() -> dict:
    if os.path.exists(SETFILE):
        try:
            return json.load(open(SETFILE, encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_settings(s: dict) -> None:
    json.dump(s, open(SETFILE, "w", encoding="utf-8"))

# ─────────── Wialon API wrapper ───────────
def login_token(token: str, base: str) -> str | None:
    try:
        r = requests.get(
            base,
            params={"svc": "token/login",
                    "params": json.dumps({"token": token})},
            timeout=20).json()
        if isinstance(r, dict) and "error" in r:
            raise RuntimeError(r)
        return r["eid"]
    except Exception as e:
        st.error(e)
        return None

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

    # error 5 = invalid SID → probaj da se ponovo uloguješ tokenom
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
        {"spec": {"itemsType": "avl_unit",
                  "propName": "sys_name",
                  "propValueMask": "*",
                  "sortType": "sys_name"},
         "force": 1, "flags": 1, "from": 0, "to": 0},
        base)
    if isinstance(res, dict) and "error" in res:
        raise RuntimeError(res)
    return [{"id": it["id"],
             "name": it.get("nm", "N/A"),
             "reg": it.get("prp", {}).get("reg_number", "")} for it in res["items"]]

def list_files(sid: str, uid: int, day: date, base: str):
    res = wialon_call(
        "file/list", sid,
        {"itemId": uid, "storageType": 2, "path": "tachograph/",
         "mask": "*", "recursive": False, "fullPath": False},
        base)
    if isinstance(res, dict) and "error" in res:
        raise RuntimeError(res)

    out = []
    for f in res:
        # po mt / ct
        for key in ("ct", "mt"):
            if key in f and datetime.fromtimestamp(
                    f[key], tz=timezone.utc).date() == day:
                out.append(f)
                break
        else:
            m = DATE_RE.search(f["n"])
            if m and datetime.strptime(m.group(1), "%Y%m%d").date() == day:
                out.append(f)
    out.sort(key=lambda x: x.get("mt", x.get("ct", 0)), reverse=True)
    return out

def get_file(sid: str, uid: int, fname: str, base: str) -> bytes | None:
    r = requests.get(
        base,
        params={"svc": "file/get",
                "sid": sid,
                "params": json.dumps(
                    {"itemId": uid, "storageType": 2,
                     "path": f"tachograph/{fname}"})},
        timeout=20)
    return r.content if r.status_code == 200 else None

# ─────────── SMTP & scheduler ───────────
def send_mail(subj: str, body: str, att: bytes | None,
              fname: str, s: dict):
    try:
        msg = EmailMessage()
        msg["Subject"] = subj
        msg["From"] = s["username"]
        msg["To"] = s["recipients"]
        msg.set_content(body)
        if att:
            msg.add_attachment(att, maintype="application",
                               subtype="zip", filename=fname)
        with smtplib.SMTP(s["server"], int(s["port"])) as smtp:
            smtp.starttls()
            smtp.login(s["username"], s["password"])
            smtp.send_message(msg)
    except Exception as e:
        st.error(f"SMTP greška: {e}")

def schedule_nightly(base: str):
    # cancel previous timer
    tmr: threading.Timer | None = st.session_state.get(TIMERKEY)
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
            sid, baseu = s.get("sid"), s["base_url"]
            if not sid:
                return
            units = get_units(sid, baseu)
            prev = (datetime.now(EU_BG) - timedelta(days=1)).date()
            buf = BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                for u in units:
                    for f in list_files(sid, u["id"], prev, baseu):
                        d = get_file(sid, u["id"], f["n"], baseu)
                        if d:
                            z.writestr(os.path.join(u["reg"] or u["name"], f["n"]), d)
            buf.seek(0)
            send_mail(f"DDD fajlovi {prev:%d.%m.%Y}",
                      "Automatski ZIP za sva vozila.",
                      buf.read(), f"DDD_{prev}.zip", s)
        except Exception:
            traceback.print_exc()
        finally:
            schedule_nightly(base)

    tmr = threading.Timer(delay, job)
    tmr.daemon = True
    add_script_run_ctx(tmr)
    tmr.start()
    st.session_state[TIMERKEY] = tmr

# ─────────── UI (isti kao ranije, skraćeno za prostor) ───────────
#  … (sav raniji UI kod ostaje identičan) …
#  kopiraj svoj zadnji radni UI blok ili ostavi kako jeste,
#  jer izmene su samo u regex-u i auto-refresh SID logici.

# ─────────── entrypoint ───────────
if __name__ == "__main__":
    main()
