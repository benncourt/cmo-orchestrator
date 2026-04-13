"""
CMO Dashboard — Servidor local
================================
Corre este script en VS Code, luego abre dashboard.html en Chrome.

Uso:
    py server.py

Puerto: http://localhost:8765
"""

import os
import json
import sys
import time
import threading
import httpx
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not API_KEY:
    print("❌ Error: define ANTHROPIC_API_KEY primero")
    print("   En PowerShell: $env:ANTHROPIC_API_KEY='sk-ant-...'")
    sys.exit(1)

BASE_URL = "https://api.anthropic.com/v1"
HEADERS = {
    "x-api-key": API_KEY,
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "managed-agents-2026-04-01",
    "content-type": "application/json",
}

# Estado global compartido
app_state = {
    "agents": {},
    "logs": [],
    "session_id": None,
    "session_active": False,
    "session_start": None,
    "runtime_secs": 0,
    "token_cost": 0.0,
    "tasks_done": 0,
    "current_output": "",
}

# Clientes SSE conectados
sse_clients = []
sse_lock = threading.Lock()


def load_agent_ids():
    try:
        with open("agent_ids.json") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def broadcast(event_type: str, data: dict):
    """Envía un evento SSE a todos los clientes conectados."""
    payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    with sse_lock:
        dead = []
        for client in sse_clients:
            try:
                client["wfile"].write(payload.encode())
                client["wfile"].flush()
            except Exception:
                dead.append(client)
        for d in dead:
            sse_clients.remove(d)


def add_log(agent_id: str, message: str, level: str = "info"):
    """Agrega una entrada al log y la transmite."""
    entry = {
        "time": time.strftime("%H:%M:%S"),
        "agent": agent_id,
        "message": message,
        "level": level,
    }
    app_state["logs"].append(entry)
    if len(app_state["logs"]) > 200:
        app_state["logs"] = app_state["logs"][-200:]
    broadcast("log", entry)


def set_agent_status(agent_id: str, status: str, action: str = "", progress: int = 0):
    """Actualiza el estado de un agente y lo transmite."""
    app_state["agents"][agent_id] = {
        "status": status,
        "last_action": action,
        "progress": progress,
    }
    broadcast("agent_update", {
        "agent_id": agent_id,
        "status": status,
        "last_action": action,
        "progress": progress,
    })


def run_session(task: str, agent_id: str, environment_id: str):
    """Crea y ejecuta una sesión con el CMO Orchestrator."""
    try:
        app_state["session_active"] = True
        app_state["session_start"] = time.time()
        app_state["current_output"] = ""

        set_agent_status("cmo_orchestrator", "running", "Iniciando sesión...", 10)
        add_log("cmo_orchestrator", f"Nueva tarea: {task[:80]}...")

        broadcast("session_start", {"task": task})

        # Crear sesión
        resp = httpx.post(
            f"{BASE_URL}/sessions",
            headers=HEADERS,
            json={
                "agent_id": agent_id,
                "environment_id": environment_id,
                "initial_message": task,
            },
            timeout=30,
        )
        resp.raise_for_status()
        session = resp.json()
        session_id = session["id"]
        app_state["session_id"] = session_id

        add_log("cmo_orchestrator", f"Sesión creada: {session_id}", "info")
        set_agent_status("cmo_orchestrator", "running", "Trabajando...", 25)

        # Stream de eventos
        with httpx.stream(
            "GET",
            f"{BASE_URL}/sessions/{session_id}/events/stream",
            headers={**HEADERS, "accept": "text/event-stream"},
            timeout=None,
        ) as response:
            for line in response.iter_lines():
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data: "):
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        event = json.loads(data_str)
                        _handle_event(event)
                    except json.JSONDecodeError:
                        pass

        # Sesión completa
        app_state["tasks_done"] += 1
        app_state["session_active"] = False
        set_agent_status("cmo_orchestrator", "done", "Tarea completada", 100)
        add_log("cmo_orchestrator", "Sesión completada exitosamente", "success")

        broadcast("session_complete", {
            "output": app_state["current_output"],
            "tasks_done": app_state["tasks_done"],
            "duration": round(time.time() - app_state["session_start"], 1),
        })

    except httpx.HTTPStatusError as e:
        error_msg = f"Error API: {e.response.status_code} — {e.response.text[:200]}"
        add_log("cmo_orchestrator", error_msg, "error")
        set_agent_status("cmo_orchestrator", "error", "Error en sesión", 0)
        app_state["session_active"] = False
        broadcast("session_error", {"message": error_msg})

    except Exception as e:
        add_log("cmo_orchestrator", f"Error inesperado: {str(e)}", "error")
        set_agent_status("cmo_orchestrator", "error", "Error", 0)
        app_state["session_active"] = False
        broadcast("session_error", {"message": str(e)})


def _handle_event(event: dict):
    """Procesa eventos del stream de Managed Agents."""
    etype = event.get("type", "")

    if etype == "text_delta":
        delta = event.get("delta", "")
        app_state["current_output"] += delta
        broadcast("output_delta", {"delta": delta})

    elif etype == "agent_start":
        agent_name = event.get("agent_name", "sub_agent")
        add_log(agent_name, f"Iniciando tarea...")
        set_agent_status(agent_name, "running", "Iniciando...", 20)

    elif etype == "agent_complete":
        agent_name = event.get("agent_name", "sub_agent")
        add_log(agent_name, "Tarea completada", "success")
        set_agent_status(agent_name, "done", "Completado", 100)

    elif etype == "tool_use":
        tool_name = event.get("tool_name", "tool")
        tool_input = event.get("input", {})
        agent = event.get("agent_name", "cmo_orchestrator")

        if tool_name == "web_search":
            query = tool_input.get("query", "")
            msg = f"Buscando: {query[:50]}"
            add_log(agent, msg)
            set_agent_status(agent, "running", msg[:30], 50)

        elif tool_name == "web_fetch":
            url = tool_input.get("url", "")
            msg = f"Leyendo: {url[:50]}"
            add_log(agent, msg)
            set_agent_status(agent, "running", msg[:30], 60)

        elif tool_name == "bash":
            msg = "Ejecutando comando..."
            add_log(agent, msg)
            set_agent_status(agent, "running", msg, 55)

        # Actualizar costo estimado por tokens
        app_state["token_cost"] += 0.0008
        broadcast("cost_update", {
            "token_cost": app_state["token_cost"],
            "runtime_secs": app_state["runtime_secs"],
        })

    elif etype == "error":
        msg = event.get("message", "Error desconocido")
        add_log("cmo_orchestrator", msg, "error")
        broadcast("session_error", {"message": msg})


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Silencia los logs del servidor HTTP

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_GET(self):
        parsed = urlparse(self.path)

        # Servir el dashboard HTML
        if parsed.path == "/" or parsed.path == "/dashboard":
            try:
                with open("dashboard.html", "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self._cors()
                self.end_headers()
                self.wfile.write(content)
            except FileNotFoundError:
                self.send_response(404)
                self.end_headers()
            return

        # SSE stream de eventos
        if parsed.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self._cors()
            self.end_headers()

            client = {"wfile": self.wfile}
            with sse_lock:
                sse_clients.append(client)

            # Enviar estado inicial
            init_data = json.dumps({
                "agents": app_state["agents"],
                "logs": app_state["logs"][-50:],
                "session_active": app_state["session_active"],
                "tasks_done": app_state["tasks_done"],
                "token_cost": app_state["token_cost"],
            })
            try:
                self.wfile.write(f"event: init\ndata: {init_data}\n\n".encode())
                self.wfile.flush()
                # Mantener conexión viva
                while True:
                    time.sleep(15)
                    self.wfile.write(f": heartbeat\n\n".encode())
                    self.wfile.flush()
            except Exception:
                with sse_lock:
                    if client in sse_clients:
                        sse_clients.remove(client)

        # Estado actual
        elif parsed.path == "/state":
            self._json_response({
                "agents": app_state["agents"],
                "session_active": app_state["session_active"],
                "tasks_done": app_state["tasks_done"],
                "token_cost": app_state["token_cost"],
                "runtime_secs": app_state["runtime_secs"],
                "current_output": app_state["current_output"],
            })

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        # Enviar nueva tarea al orquestador
        if parsed.path == "/task":
            task = body.get("task", "").strip()
            if not task:
                self._json_response({"error": "Tarea vacía"}, 400)
                return
            if app_state["session_active"]:
                self._json_response({"error": "Ya hay una sesión activa"}, 409)
                return

            ids = load_agent_ids()
            agent_id = ids.get("AGENT_cmo_orchestrator")
            environment_id = ids.get("environment_id")

            if not agent_id:
                self._json_response({"error": "No encontré agent_ids.json. Corre setup.py primero."}, 500)
                return

            # Resetear estado de agentes
            app_state["agents"] = {}
            app_state["logs"] = []
            broadcast("reset", {})

            # Correr en hilo separado para no bloquear el servidor
            t = threading.Thread(target=run_session, args=(task, agent_id, environment_id), daemon=True)
            t.start()

            self._json_response({"ok": True, "message": "Sesión iniciada"})

        else:
            self.send_response(404)
            self.end_headers()

    def _json_response(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)


# Timer de runtime
def runtime_ticker():
    while True:
        time.sleep(1)
        if app_state["session_active"]:
            app_state["runtime_secs"] += 1
            if app_state["runtime_secs"] % 10 == 0:
                broadcast("cost_update", {
                    "token_cost": app_state["token_cost"],
                    "runtime_secs": app_state["runtime_secs"],
                })


if __name__ == "__main__":
    ticker = threading.Thread(target=runtime_ticker, daemon=True)
    ticker.start()

    PORT = int(os.environ.get("PORT", 8765))
    server = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    print(f"✅ CMO Dashboard Server corriendo en puerto {PORT}")
    print("   Abre dashboard.html en Chrome para ver el dashboard")
    print("   Ctrl+C para detener\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Servidor detenido")
