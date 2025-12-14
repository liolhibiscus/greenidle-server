print(">>> GREENIDLE_SERVER.PY LOADED – DASHBOARD V2 <<<")

from flask import Flask, request, jsonify, render_template_string, redirect, url_for
from datetime import datetime
import uuid

app = Flask(__name__)

# =========================
#   CONFIG
# =========================
APP_NAME = "GreenIdle"
ADMIN_TOKEN = "Iletait1fois@33"
DEBUG = False  # False sur Render


# =========================
#   MINI BDD EN MEMOIRE
# =========================
machines = {}         # machine_id -> dict
machine_configs = {}  # machine_id -> dict config
jobs = {}
tasks = {}
results = []
tasks_log = []


# =========================
#   OUTILS
# =========================
def now_iso():
    return datetime.utcnow().isoformat()


def ensure_machine(machine_id, display_name=None):
    if not machine_id:
        return None

    if machine_id not in machines:
        machines[machine_id] = {
            "machine_id": machine_id,
            "display_name": display_name or machine_id,
            "registered_at": now_iso(),
            "last_seen": None,
            "total_seconds": 0,
            "last_cpu": 0.0,
        }
    else:
        if display_name:
            machines[machine_id]["display_name"] = display_name

    return machines[machine_id]


def require_admin():
    return request.args.get("token") == ADMIN_TOKEN


def default_config():
    return {
        "enabled": True,
        "cpu_pause_threshold": 50.0,
        "heartbeat_every": 15,
        "idle_sleep_seconds": 2,
        "task_max_seconds": 30,
        "post_task_sleep_seconds": 2,
        "night_mode": {
            "enabled": False,
            "start_hour": 23,
            "end_hour": 7,
            "cpu_pause_threshold": 70.0
        }
    }


# =========================
#   API CLIENTS
# =========================
@app.route("/register", methods=["POST"])
def register():
    data = request.json or {}
    machine_id = data.get("machine_id")
    client_name = data.get("client_name")

    if not machine_id:
        return jsonify({"error": "machine_id manquant"}), 400

    m = ensure_machine(machine_id, client_name)
    m["last_seen"] = now_iso()
    return jsonify({"status": "ok"})


@app.route("/heartbeat", methods=["POST"])
def heartbeat():
    data = request.json or {}
    machine_id = data.get("machine_id")
    cpu = float(data.get("cpu_percent", 0.0))

    if not machine_id:
        return jsonify({"error": "machine_id manquant"}), 400

    m = ensure_machine(machine_id)
    m["last_seen"] = now_iso()
    m["last_cpu"] = cpu
    return jsonify({"status": "ok"})


@app.route("/config", methods=["GET"])
def get_config():
    machine_id = request.args.get("machine_id")
    ensure_machine(machine_id)

    cfg = machine_configs.get(machine_id)
    if not cfg:
        cfg = default_config()
        machine_configs[machine_id] = cfg

    return jsonify(cfg)


@app.route("/task", methods=["GET"])
def get_task():
    machine_id = request.args.get("machine_id")
    ensure_machine(machine_id)

    for t in tasks.values():
        if t["status"] == "pending":
            t["status"] = "assigned"
            t["assigned_to"] = machine_id
            t["updated_at"] = now_iso()

            job = jobs.get(t["job_id"])
            if job and job["status"] == "pending":
                job["status"] = "running"

            return jsonify({
                "task_id": t["task_id"],
                "payload": t["task_type"],
                "params": t.get("params", {}),
                "size": t.get("size", 0),
            })

    return ("", 204)


@app.route("/report", methods=["POST"])
def report():
    data = request.json or {}
    machine_id = data.get("machine_id")
    task_id = data.get("task_id")
    seconds = int(data.get("seconds", 0))
    result = data.get("result")

    if not machine_id or task_id is None:
        return jsonify({"error": "machine_id ou task_id manquant"}), 400

    m = ensure_machine(machine_id)
    m["total_seconds"] += seconds
    m["last_seen"] = now_iso()

    if task_id in tasks:
        t = tasks[task_id]
        t["status"] = "done"
        t["seconds"] += seconds
        t["result"] = result
        t["updated_at"] = now_iso()

        job = jobs.get(t["job_id"])
        if job:
            job["total_seconds"] += seconds
            if all(tt["status"] == "done" for tt in tasks.values() if tt["job_id"] == job["job_id"]):
                job["status"] = "done"

    results.append({
        "machine_id": machine_id,
        "task_id": task_id,
        "seconds": seconds,
        "result": result,
        "timestamp": now_iso()
    })

    return jsonify({"status": "ok"})


# =========================
#   ADMIN CONFIG MACHINE
# =========================
@app.route("/machines/<machine_id>/config", methods=["POST"])
def set_machine_config(machine_id):
    if not require_admin():
        return "Accès refusé", 403

    ensure_machine(machine_id)
    cfg = machine_configs.get(machine_id) or default_config()
    data = request.form or request.json or {}

    cfg["enabled"] = "enabled" in data
    cfg["cpu_pause_threshold"] = float(data.get("cpu_pause_threshold", cfg["cpu_pause_threshold"]))
    cfg["task_max_seconds"] = int(data.get("task_max_seconds", cfg["task_max_seconds"]))
    cfg["post_task_sleep_seconds"] = int(data.get("post_task_sleep_seconds", cfg["post_task_sleep_seconds"]))

    cfg["night_mode"] = {
        "enabled": "night_enabled" in data,
        "start_hour": int(data.get("night_start", cfg["night_mode"]["start_hour"])),
        "end_hour": int(data.get("night_end", cfg["night_mode"]["end_hour"])),
        "cpu_pause_threshold": float(data.get("night_cpu", cfg["night_mode"]["cpu_pause_threshold"]))
    }

    machine_configs[machine_id] = cfg
    return redirect(url_for("dashboard", token=request.args.get("token")))

@app.route("/machines/<machine_id>/stop", methods=["POST"])
def stop_machine(machine_id):
    if not require_admin():
        return "Accès refusé", 403

    ensure_machine(machine_id)
    cfg = machine_configs.get(machine_id) or default_config()
    cfg["enabled"] = False
    machine_configs[machine_id] = cfg

    return redirect(url_for("dashboard", token=request.args.get("token")))


@app.route("/machines/<machine_id>/start", methods=["POST"])
def start_machine(machine_id):
    if not require_admin():
        return "Accès refusé", 403

    ensure_machine(machine_id)
    cfg = machine_configs.get(machine_id) or default_config()
    cfg["enabled"] = True
    machine_configs[machine_id] = cfg

    return redirect(url_for("dashboard", token=request.args.get("token")))

# =========================
#   DASHBOARD (ADMIN)
# =========================
@app.route("/dashboard")
def dashboard():
    if not require_admin():
        return "Accès refusé", 403

    token = request.args.get("token")
    total_seconds = sum(m["total_seconds"] for m in machines.values())
    total_hours = round(total_seconds / 3600, 4)

    html = """
    <!DOCTYPE html>
    <html lang="fr">
    <head>
        <meta charset="utf-8">
        <title>{{ app_name }} - Dashboard</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; }
            table { border-collapse: collapse; width: 100%; max-width: 1200px; }
            th, td { border: 1px solid #ccc; padding: 8px; vertical-align: top; }
            th { background: #f0f0f0; }
            tr:nth-child(even) { background: #fafafa; }
            button { cursor: pointer; }
            .stop { background:#c0392b; color:white; border:none; padding:4px 8px; }
            .start { background:#27ae60; color:white; border:none; padding:4px 8px; }
            .cfg { font-size: 0.9em; }
        </style>
    </head>
    <body>
    <h2 style="color:red;">DASHBOARD V2 – CONFIG MACHINE ACTIVE</h2>
    <h1>{{ app_name }} – Tableau de bord</h1>
    <p>
      <strong>Machines :</strong> {{ machines|length }} |
      <strong>Heures totales :</strong> {{ total_hours }}
    </p>

    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>Nom</th>
          <th>CPU</th>
          <th>Secondes</th>
          <th>Contrôle</th>
          <th>Paramètres</th>
        </tr>
      </thead>
      <tbody>
      {% for m in machines %}
        {% set cfg = configs.get(m.machine_id, {}) %}
        {% set nm = cfg.get("night_mode", {}) %}
        <tr>
          <td>{{ m.machine_id }}</td>
          <td><strong>{{ m.display_name }}</strong></td>
          <td>{{ m.last_cpu }} %</td>
          <td>{{ m.total_seconds }}</td>

          <!-- STOP / START -->
          <td>
            {% if cfg.get("enabled", True) %}
              <form method="post" action="/machines/{{ m.machine_id }}/stop?token={{ token }}">
                <button class="stop" type="submit">⏹ STOP</button>
              </form>
            {% else %}
              <form method="post" action="/machines/{{ m.machine_id }}/start?token={{ token }}">
                <button class="start" type="submit">▶ START</button>
              </form>
            {% endif %}
          </td>

          <!-- PARAMÈTRES -->
          <td class="cfg">
            <form method="post" action="/machines/{{ m.machine_id }}/config?token={{ token }}">
              <label>
                <input type="checkbox" name="enabled"
                  {% if cfg.get("enabled", True) %}checked{% endif %}>
                Machine active
              </label><br>

              CPU max :
              <input type="number" name="cpu_pause_threshold"
                     value="{{ cfg.get('cpu_pause_threshold',50) }}"
                     min="10" max="95" step="5"> %<br>

              Durée max tâche :
              <input type="number" name="task_max_seconds"
                     value="{{ cfg.get('task_max_seconds',30) }}"
                     min="5" max="300"> s<br>

              Pause après tâche :
              <input type="number" name="post_task_sleep_seconds"
                     value="{{ cfg.get('post_task_sleep_seconds',2) }}"
                     min="0" max="30"> s<br>

              <hr>

              <label>
                <input type="checkbox" name="night_enabled"
                  {% if nm.get("enabled") %}checked{% endif %}>
                Mode nuit
              </label><br>

              Nuit début :
              <input type="number" name="night_start"
                     value="{{ nm.get('start_hour',23) }}"
                     min="0" max="23">
              fin :
              <input type="number" name="night_end"
                     value="{{ nm.get('end_hour',7) }}"
                     min="0" max="23"><br>

              CPU nuit :
              <input type="number" name="night_cpu"
                     value="{{ nm.get('cpu_pause_threshold',70) }}"
                     min="20" max="100" step="5"> %<br><br>

              <button type="submit">Appliquer</button>
            </form>
          </td>
        </tr>
      {% endfor %}
      </tbody>
    </table>

    </body>
    </html>
    """

    return render_template_string(
        html,
        app_name=APP_NAME,
        machines=list(machines.values()),
        total_hours=total_hours,
        configs=machine_configs,
        token=token
    )

@app.route("/")
def home():
    return "GreenIdle server OK"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=DEBUG)
