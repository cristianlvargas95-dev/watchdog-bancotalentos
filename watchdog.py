"""Watchdog: avisa cuando se habilite "CARGA TU HOJA DE VIDA" en bancodetalentos.com.co.

Hoy (13/10/2026) el botón existe pero al hacer clic muestra un modal:
"Por ahora nuestro banco de talentos está siendo evaluado...".
Cada ejecución abre la página con Chrome (Playwright), hace clic y revisa si ese modal
sigue saliendo. Si no sale (o la página cambia), lanza notificación de Windows + sonido
y abre la página en el navegador.

Uso:  pythonw watchdog.py          (lo corre la tarea programada)
      python  watchdog.py --test   (fuerza una notificación de prueba)
"""
import datetime
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.request
import webbrowser

try:
    from patchright.sync_api import sync_playwright  # Playwright sin huellas de automatización (Cloudflare)
except ImportError:
    from playwright.sync_api import sync_playwright

URL = "https://bancodetalentos.com.co/"
BASE = pathlib.Path(__file__).resolve().parent
PROFILE = BASE / "chrome-profile"   # perfil propio: conserva la cookie de Cloudflare
LOG = BASE / "watchdog.log"
STATE = BASE / "state.json"
STATE_CI = BASE / "ci-state" / "state.json"
BLIND_ALERT_H = 2      # horas sin ver la página antes de avisar "ciego"
BLIND_REPEAT_H = 6     # repetir el aviso de ciego cada N horas
CLOSED_RE =re.compile(r"siendo evaluado|retomemos", re.I)
BUTTON_RE = re.compile(r"hoja de vida", re.I)


def log(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def notify(title, body):
    ps = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$x = $t.GetElementsByTagName('text'); $x.Item(0).InnerText = '{title}'; $x.Item(1).InnerText = '{body}'
$app = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($t))
1..3 | % {{ [System.Media.SystemSounds]::Exclamation.Play(); Start-Sleep -Milliseconds 700 }}
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    webbrowser.open(URL)


def check():
    """Devuelve ('CERRADO'|'ABIERTO'|'CAMBIO'|'ERROR', detalle)."""
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), channel="chrome", headless=False,
            no_viewport=True, args=["--window-position=-32000,-32000"])
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.add_init_script("window.alert=()=>{};window.confirm=()=>false;")
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            # esperar a que pase Cloudflare y renderice el botón
            btn = page.get_by_role("button", name=BUTTON_RE)
            try:
                btn.first.wait_for(state="visible", timeout=45000)
            except Exception:
                page.screenshot(path=str(BASE / "fallo.png"))
                if "Just a moment" in page.title():
                    return "ERROR", "Cloudflare no dejó pasar"
                return "CAMBIO", "ya no aparece el botón 'hoja de vida'"
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            url_before = page.url
            closed = page.locator("[aria-labelledby=registration-closed-modal-title], [role=dialog]",
                                  has_text=CLOSED_RE)
            # 3 intentos: un clic antes de que la app hidrate no hace nada
            for _ in range(3):
                btn.first.scroll_into_view_if_needed()
                btn.first.click()
                try:
                    closed.first.wait_for(state="visible", timeout=8000)
                    return "CERRADO", "modal 'siendo evaluado' sigue saliendo"
                except Exception:
                    if page.url != url_before:
                        break
                    page.wait_for_timeout(2000)
            if CLOSED_RE.search(page.inner_text("body")):
                return "CERRADO", "texto 'siendo evaluado' presente"
            files = page.locator("input[type=file]").count()
            return "ABIERTO", f"sin modal de cierre; url={page.url} (antes {url_before}); inputs file={files}"
        finally:
            ctx.close()


def ntfy(title, body, priority="high"):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers={
        "Title": title.encode("utf-8").decode("latin-1"), "Priority": priority, "Click": URL})
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"::warning::ntfy falló: {e!r}")


def static_fingerprint():
    """Hash de llms.txt + robots.txt: no pasan por el reto de Cloudflare y cambian si redespliegan con cambios."""
    h = hashlib.sha256()
    for path in ("/llms.txt", "/robots.txt"):
        req = urllib.request.Request(URL.rstrip("/") + path, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            h.update(r.read())
    return h.hexdigest()[:16]


def ci_state(status):
    """Estado entre corridas (restaurado con actions/cache): alerta de 'ciego' y de cambios del sitio."""
    STATE_CI.parent.mkdir(exist_ok=True)
    st = json.loads(STATE_CI.read_text()) if STATE_CI.exists() else {}
    now = time.time()
    st.setdefault("last_ok", now)
    if status != "ERROR":
        if st.get("blind_since_alert"):
            ntfy("Watchdog HV: vuelve a ver", "El watchdog recuperó la vista. Banco de Talentos: " + status, "default")
        st["last_ok"], st["blind_since_alert"] = now, None
    else:
        blind_h = (now - st["last_ok"]) / 3600
        last_alert = st.get("blind_since_alert")
        if blind_h >= BLIND_ALERT_H and (not last_alert or now - last_alert >= BLIND_REPEAT_H * 3600):
            ntfy("Watchdog HV CIEGO", f"Cloudflare lo bloquea hace {blind_h:.1f} h. Entra tú a "
                 "bancodetalentos.com.co y prueba el botón CARGA TU HOJA DE VIDA.", "urgent")
            st["blind_since_alert"] = now
    try:
        fp = static_fingerprint()
        if st.get("fingerprint") and fp != st["fingerprint"]:
            ntfy("Banco de Talentos CAMBIÓ", "Cambiaron archivos del sitio (posible despliegue nuevo). "
                 "Entra a revisar si ya abrió la carga de hoja de vida.", "urgent")
        st["fingerprint"] = fp
    except Exception as e:
        print(f"::warning::huella del sitio falló: {e!r}")
    STATE_CI.write_text(json.dumps(st))
    print(f"estado: {st}")


def main():
    if "--test" in sys.argv:
        notify("Banco de Talentos (PRUEBA)", "Así se verá el aviso cuando se habilite.")
        return
    try:
        status, detail = check()
    except Exception as e:
        status, detail = "ERROR", repr(e)[:300]
    log(f"{status}  {detail}")

    if os.environ.get("GITHUB_ACTIONS"):
        ci_state(status)
        # En la nube el aviso es el correo de GitHub por "Run failed": fallar = está abierto
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(f"**{status}** — {detail}\n")
        if status == "ERROR":
            print(f"::warning::{detail}")
        sys.exit(1 if status in ("ABIERTO", "CAMBIO") else 0)

    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if status in ("ABIERTO", "CAMBIO") and state.get("last") != status:
        notify("¡Banco de Talentos habilitado!" if status == "ABIERTO" else "Banco de Talentos cambió",
               "El botón CARGA TU HOJA DE VIDA ya no muestra el aviso de cierre. Entra ya.")
    if status != "ERROR":
        state["last"] = status
    state["errors"] = state.get("errors", 0) + 1 if status == "ERROR" else 0
    if state["errors"] == 6:  # ~1 hora fallando seguido
        notify("Watchdog Banco de Talentos", "Lleva 6 revisiones fallando. Revisa watchdog.log.")
    STATE.write_text(json.dumps(state))


if __name__ == "__main__":
    main()
