import os, json, sys, time, threading, httpx
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not API_KEY:
    print("Error: define ANTHROPIC_API_KEY"); sys.exit(1)

BASE_URL = "https://api.anthropic.com/v1"
HEADERS = {
    "x-api-key": API_KEY,
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "managed-agents-2026-04-01",
    "content-type": "application/json",
}

state = {
    "agents": {}, "logs": [], "session_active": False,
    "runtime_secs": 0, "token_cost": 0.0, "tasks_done": 0,
    "current_output": "", "last_error": ""
}
lock = threading.Lock()

def load_ids():
    env = os.environ.get("AGENT_IDS")
    if env:
        try: return json.loads(env)
        except: pass
    try:
        with open("agent_ids.json") as f: return json.load(f)
    except: return {}

def log(agent, msg, level="info"):
    with lock:
        state["logs"].append({"time": time.strftime("%H:%M:%S"), "agent": agent, "message": msg, "level": level})
        if len(state["logs"]) > 200: state["logs"] = state["logs"][-200:]

def set_agent(agent, status, action="", progress=0):
    with lock:
        state["agents"][agent] = {"status": status, "last_action": action, "progress": progress}

def poll_until_done(session_id):
    """Espera polling hasta que session.status_idle aparezca, luego retorna el output."""
    log("cmo_orchestrator", "Esperando respuesta del agente...")
    max_wait = 600  # 10 minutos máximo
    interval = 5    # revisar cada 5 segundos
    elapsed = 0

    while elapsed < max_wait:
        time.sleep(interval)
        elapsed += interval

        try:
            r = httpx.get(f"{BASE_URL}/sessions/{session_id}/events", headers=HEADERS, timeout=30)
            if r.status_code != 200:
                continue

            data = r.json().get("data", [])
            
            # Buscar si ya terminó
            idle = any(e.get("type") == "session.status_idle" for e in data)
            error = next((e for e in data if e.get("type") == "session.error"), None)
            
            if error:
                raise Exception(f"Error del agente: {error.get('message','error desconocido')}")

            # Actualizar progreso visual
            progress = min(20 + (elapsed // 5) * 3, 90)
            set_agent("cmo_orchestrator", "running", f"Pensando... ({elapsed}s)", progress)
            with lock: state["token_cost"] += 0.001

            if idle:
                # Extraer output de agent.message
                parts = []
                for evt in data:
                    if evt.get("type") == "agent.message":
                        for block in evt.get("content", []):
                            if isinstance(block, dict) and block.get("type") == "text":
                                txt = block.get("text", "")
                                if txt:
                                    parts.append(txt)
                return "".join(parts)

        except httpx.RequestError:
            continue

    return ""

def run(task, agent_id, env_id):
    with lock:
        state.update({"session_active": True, "current_output": "", "last_error": "",
                      "agents": {}, "logs": [], "runtime_secs": 0, "token_cost": 0.0})
    set_agent("cmo_orchestrator", "running", "Iniciando...", 10)
    log("cmo_orchestrator", f"Tarea: {task[:80]}...")
    try:
        # 1. Crear sesión
        r = httpx.post(f"{BASE_URL}/sessions", headers=HEADERS, json={
            "agent": agent_id,
            "environment_id": env_id,
        }, timeout=30)
        if r.status_code != 200:
            raise Exception(f"Error creando sesión {r.status_code}: {r.text[:300]}")
        session_id = r.json()["id"]
        log("cmo_orchestrator", f"Sesión: {session_id}")
        set_agent("cmo_orchestrator", "running", "Sesión lista...", 15)

        # 2. Mandar mensaje
        ev = httpx.post(f"{BASE_URL}/sessions/{session_id}/events", headers=HEADERS, json={
            "events": [{
                "type": "user.message",
                "content": [{"type": "text", "text": task}]
            }]
        }, timeout=30)
        if ev.status_code != 200:
            raise Exception(f"Error enviando mensaje {ev.status_code}: {ev.text[:300]}")
        log("cmo_orchestrator", "Agente procesando tarea...")
        set_agent("cmo_orchestrator", "running", "Agente pensando...", 20)

        # 3. Polling hasta que termine
        output = poll_until_done(session_id)

        with lock:
            state["current_output"] = output if output else "El agente completó pero no generó texto visible."
            state["tasks_done"] += 1
            state["session_active"] = False

        set_agent("cmo_orchestrator", "done", "Completado ✓", 100)
        log("cmo_orchestrator", "Completado exitosamente", "success")

    except Exception as ex:
        err = str(ex)
        with lock:
            state["session_active"] = False
            state["last_error"] = err
        set_agent("cmo_orchestrator", "error", err[:60], 0)
        log("cmo_orchestrator", err, "error")

def ticker():
    while True:
        time.sleep(1)
        if state["session_active"]:
            with lock: state["runtime_secs"] += 1

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
    def do_OPTIONS(self):
        self.send_response(200); self.cors(); self.end_headers()
    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/dashboard"):
            try:
                c = open("dashboard.html", "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(c)))
                self.cors(); self.end_headers(); self.wfile.write(c)
            except:
                self.send_response(404); self.end_headers()
        elif p == "/state":
            with lock: d = json.dumps(state).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(d)))
            self.cors(); self.end_headers(); self.wfile.write(d)
        else:
            self.send_response(404); self.end_headers()
    def do_POST(self):
        p = urlparse(self.path).path
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n)) if n else {}
        if p == "/task":
            task = body.get("task", "").strip()
            if not task:
                self.respond({"error": "Tarea vacía"}, 400); return
            if state["session_active"]:
                self.respond({"error": "Sesión activa, espera"}); return
            ids = load_ids()
            aid = ids.get("AGENT_cmo_orchestrator")
            eid = ids.get("environment_id")
            if not aid:
                self.respond({"error": "No encontré AGENT_IDS"}); return
            threading.Thread(target=run, args=(task, aid, eid), daemon=True).start()
            self.respond({"ok": True})
        else:
            self.send_response(404); self.end_headers()
    def respond(self, data, status=200):
        b = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.cors(); self.end_headers(); self.wfile.write(b)

if __name__ == "__main__":
    threading.Thread(target=ticker, daemon=True).start()
    PORT = int(os.environ.get("PORT", 8765))
    srv = HTTPServer(("0.0.0.0", PORT), H)
    print(f"✅ CMO Dashboard Server corriendo en puerto {PORT}")
    print("   Ctrl+C para detener\n")
    try: srv.serve_forever()
    except KeyboardInterrupt: print("\n👋 Servidor detenido")
