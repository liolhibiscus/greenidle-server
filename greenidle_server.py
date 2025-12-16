print(">>> GREENIDLE_SERVER.PY LOADED – DASHBOARD V2 + JOBS (FINAL + MIN-SEC) <<<")

from flask import Flask, request, jsonify, render_template_string, redirect, url_for, abort
from datetime import datetime
import uuid
import os
import time
import hmac
import hashlib
from functools import wraps

app = Flask(__name__)

# =========================
#   CONFIG
# =========================
APP_NAME = "GreenIdle"

# IMPORTANT: NE JAMAIS METTRE LE TOKEN EN DUR.
# Sur Render => Environment => ADMIN_TOKEN
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# Optionnel: blacklist IPs dans Render => BLACKLIST_IPS="1.2.3.4,5.6.7.8"
BLACKLIST_IPS = set(
    ip.strip() for ip in os.getenv("BLACKLIST_IPS", "").split(",") if ip.strip()
)

DEBUG = False  # False sur Render

# =========================
#   MINI BDD EN MEMOIRE
#   (Render redémarre => mémoire reset, OK pour "minimal")
# =========================
machines = {}         # machine_id -> dict
machine_configs = {}  # machine_id -> config dict
jobs = {}             # job_id -> dict
tasks = {}            # task_id -> dict
results = []          # list[dict] results rows
tasks_log = []        # list[dict] every report

# Auth clients minimal (transition douce)
clients = {}          # client_id -> {"machine_key": "...", "created_at": "..."}
machine_to_client = {}  # machine_id -> client_id (si enregistré)

# =========================
#   OUTILS
# =========================
def now_iso():
    return datetime.utcnow().isoformat()

def get_ip():
    # Render / proxy : X-Forwarded-For existe souvent
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"

# -------------------------
# Rate limit (léger, mémoire)
# -------------------------
_RATE = {}  # {key: [timestamps]}
def rate_limit(key: str, limit=30, window=60):
    now = time.time()
    lst = _RATE.get(key, [])
    lst = [t for t in lst if now - t < window]
    if len(lst) >= limit:
        abort(429)
    lst.append(now)
    _RATE[key] = lst

def is_blacklisted():
    ip = get_ip()
    return ip in BLACKLIST_IPS

# -------------------------
# Admin protection (UX friendly)
# -------------------------
def is_admin():
    if not ADMIN_TOKEN:
        return False
    # 1) header (plus propre)
    if request.headers.get("X-Admin-Token", "") == ADMIN_TOKEN:
        return True
    # 2) query param (compatible avec ton dashboard actuel)
    return request.args.get("token", "") == ADMIN_TOKEN

def require_admin_route(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not is_admin():
            return "Accès refusé", 403
        return f(*args, **kwargs)
    return wrapper

# -------------------------
# Client auth minimal (HMAC)
# Transition douce : si headers présents => vérif, sinon legacy autorisé (rate-limité)
# -------------------------
def _hmac_hex(key: str, body_bytes: bytes) -> str:
    return hmac.new(key.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()

def verify_client_if_present(machine_id: str = None):
    """
    Si le client envoie X-Client-Id + X-Client-Signature => on vérifie.
    Sinon => legacy accepté (mais on rate-limit).
    """
    ip = get_ip()
    if is_blacklisted():
        abort(403)

    client_id = request.headers.get("X-Client-Id", "").strip()
    sig = request.headers.get("X-Client-Signature", "").strip()

    # Si headers absents => mode legacy
    if not client_id and not sig:
        # anti-squattage léger sur endpoints clients (legacy)
        rate_limit(f"legacy:{ip}", limit=60, window=60)
        return {"mode": "legacy", "client_id": None}

    # Headers partiels => refuse
    if not client_id or not sig:
        abort(401)

    c = clients.get(client_id)
    if not c:
        abort(401)

    # signature sur le body brut (request.data) : même pour JSON
    expected = _hmac_hex(c["machine_key"], request.data or b"")
    if not hmac.compare_digest(expected, sig):
        abort(401)

    # petit rate limit par client_id
    rate_limit(f"client:{client_id}", limit=120, window=60)

    # mapping machine_id -> client_id si on l’a
    if machine_id:
        machine_to_client[machine_id] = client_id

    return {"mode": "signed", "client_id": client_id}

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

def ensure_config(machine_id: str):
    cfg = machine_configs.get(machine_id)
    if not cfg:
        cfg = default_config()
        machine_configs[machine_id] = cfg
    return cfg

# =========================
#   API CLIENTS
# =========================
@app.route("/register", methods=["POST"])
def register():
    ip = get_ip()
    if is_blacklisted():
        return jsonify({"error": "blacklisted"}), 403

    # Limite inscriptions (léger) : 10/min par IP
    rate_limit(f"register:{ip}", limit=10, window=60)

    data = request.json or {}
    machine_id = data.get("machine_id")
    client_name = data.get("client_name")

    if not machine_id:
        return jsonify({"error": "machine_id manquant"}), 400

    # Si le client fournit déjà client_id + machine_key => on l'enregistre
    provided_client_id = (data.get("client_id") or "").strip()
    provided_machine_key = (data.get("machine_key") or "").strip()

    if provided_client_id and provided_machine_key:
        clients[provided_client_id] = {
            "machine_key": provided_machine_key,
            "created_at": now_iso(),
        }
        machine_to_client[machine_id] = provided_client_id
        auth_mode = "signed-ready"
        returned_client_id = provided_client_id
        returned_machine_key = None  # on ne le renvoie pas si déjà fourni
    else:
        # Mode transition : on génère un couple et on le renvoie
        generated_client_id = str(uuid.uuid4())
        generated_machine_key = str(uuid.uuid4()) + str(uuid.uuid4())
        clients[generated_client_id] = {
            "machine_key": generated_machine_key,
            "created_at": now_iso(),
        }
        machine_to_client[machine_id] = generated_client_id
        auth_mode = "generated"
        returned_client_id = generated_client_id
        returned_machine_key = generated_machine_key

    m = ensure_machine(machine_id, client_name)
    m["last_seen"] = now_iso()
    ensure_config(machine_id)

    return jsonify({
        "status": "ok",
        "message": "machine enregistree",
        "auth": {
            "mode": auth_mode,
            "client_id": returned_client_id,
            # renvoyé seulement si généré (à stocker côté client)
            "machine_key": returned_machine_key,
            "how_to_sign": "HMAC_SHA256(machine_key, raw_request_body) -> X-Client-Signature",
            "headers": ["X-Client-Id", "X-Client-Signature"]
        }
    })

@app.route("/heartbeat", methods=["POST"])
def heartbeat():
    data = request.json or {}
    machine_id = data.get("machine_id")
    cpu = float(data.get("cpu_percent", 0.0))

    if not machine_id:
        return jsonify({"error": "machine_id manquant"}), 400

    verify_client_if_present(machine_id)

    m = ensure_machine(machine_id)
    m["last_seen"] = now_iso()
    m["last_cpu"] = cpu
    ensure_config(machine_id)
    return jsonify({"status": "ok"})

@app.route("/config", methods=["GET"])
def get_config():
    machine_id = request.args.get("machine_id")
    if not machine_id:
        return jsonify({"error": "machine_id manquant"}), 400

    verify_client_if_present(machine_id)

    ensure_machine(machine_id)
    cfg = ensure_config(machine_id)
    return jsonify(cfg)

@app.route("/task", methods=["GET"])
def get_task():
    machine_id = request.args.get("machine_id")
    if not machine_id:
        return ("", 400)

    verify_client_if_present(machine_id)

    ensure_machine(machine_id)
    cfg = ensure_config(machine_id)

    # Si machine stoppée via dashboard => on ne donne pas de tâche
    if not cfg.get("enabled", True):
        return ("", 204)

    # 1) assign pending tasks
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
                "payload": t["task_type"],      # plugin name
                "params": t.get("params", {}),
                "size": t.get("size", 0),
                "task_max_seconds": cfg.get("task_max_seconds", 30),
                "post_task_sleep_seconds": cfg.get("post_task_sleep_seconds", 2),
            })

    # 2) no task -> 204 (client sleeps)
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

    verify_client_if_present(machine_id)

    m = ensure_machine(machine_id)
    m["total_seconds"] += seconds
    m["last_seen"] = now_iso()
    ensure_config(machine_id)

    tasks_log.append({
        "machine_id": machine_id,
        "task_id": task_id,
        "seconds": seconds,
        "result": result,
        "reported_at": now_iso()
    })

    if task_id in tasks:
        t = tasks[task_id]
        t["status"] = "done"
        t["seconds"] = t.get("seconds", 0) + seconds
        t["result"] = result
        t["updated_at"] = now_iso()

        job = jobs.get(t["job_id"])
        if job:
            job["total_seconds"] += seconds

            all_done = all(
                (tt["status"] == "done")
                for tt in tasks.values()
                if tt["job_id"] == job["job_id"]
            )
            if all_done:
                job["status"] = "done"

            results.append({
                "job_id": job["job_id"],
                "task_id": task_id,
                "machine_id": machine_id,
                "seconds": seconds,
                "timestamp": now_iso(),
                "result": result
            })
    else:
        results.append({
            "job_id": None,
            "task_id": task_id,
            "machine_id": machine_id,
            "seconds": seconds,
            "timestamp": now_iso(),
            "result": result
        })

    return jsonify({"status": "ok"})

@app.route("/status", methods=["GET"])
def status():
    # Public endpoint (tu peux le laisser public)
    total_seconds = sum(m["total_seconds"] for m in machines.values())
    return jsonify({
        "app": APP_NAME,
        "machines_count": len(machines),
        "total_hours": round(total_seconds / 3600, 4),
        "jobs_count": len(jobs),
        "machines": list(machines.values()),
    })

# =========================
#   ADMIN: RENAME + CONFIG + STOP/START
# =========================
@app.route("/machines/<machine_id>/rename", methods=["POST"])
@require_admin_route
def rename_machine(machine_id):
    if machine_id not in machines:
        return "Machine inconnue", 404

    new_name = request.form.get("display_name") or (request.json or {}).get("display_name")
    if not new_name:
        return "Nom manquant", 400

    machines[machine_id]["display_name"] = new_name
    return redirect(url_for("dashboard", token=request.args.get("token")))

@app.route("/machines/<machine_id>/config", methods=["POST"])
@require_admin_route
def set_machine_config(machine_id):
    ensure_machine(machine_id)
    cfg = ensure_config(machine_id)
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
@require_admin_route
def stop_machine(machine_id):
    ensure_machine(machine_id)
    cfg = ensure_config(machine_id)
    cfg["enabled"] = False
    machine_configs[machine_id] = cfg
    return redirect(url_for("dashboard", token=request.args.get("token")))

@app.route("/machines/<machine_id>/start", methods=["POST"])
@require_admin_route
def start_machine(machine_id):
    ensure_machine(machine_id)
    cfg = ensure_config(machine_id)
    cfg["enabled"] = True
    machine_configs[machine_id] = cfg
    return redirect(url_for("dashboard", token=request.args.get("token")))

# =========================
#   JOBS (ADMIN)
# =========================
@app.route("/submit", methods=["GET", "POST"])
@require_admin_route
def submit_job():
    token = request.args.get("token")

    if request.method == "POST":
        name = request.form.get("name", "Job sans nom")
        description = request.form.get("description", "")
        task_type = request.form.get("task_type", "montecarlo_pi")
        total_chunks = int(request.form.get("chunks", 5))
        size = int(request.form.get("size", 200000))

        job_id = str(uuid.uuid4())[:8]
        jobs[job_id] = {
            "job_id": job_id,
            "name": name,
            "description": description,
            "task_type": task_type,
            "total_chunks": total_chunks,
            "created_at": now_iso(),
            "status": "pending",
            "total_seconds": 0
        }

        for i in range(total_chunks):
            task_id = f"{job_id}_part_{i+1}"
            params = {}

            if task_type in ("montecarlo_pi", "montecarlo"):
                params = {"n": size, "seed": i + 1}

            tasks[task_id] = {
                "task_id": task_id,
                "job_id": job_id,
                "task_type": task_type,
                "size": size,
                "params": params,
                "status": "pending",
                "assigned_to": None,
                "created_at": now_iso(),
                "updated_at": None,
                "seconds": 0,
                "result": None
            }

        return redirect(url_for("jobs_view", token=token))

    html = """
    <h1>Soumettre un job GreenIdle</h1>
    <p><a href="/dashboard?token={{ token }}">⬅ Retour dashboard</a></p>

    <form method="post">
        Nom du job :<br>
        <input name="name" type="text" value="Estimation de PI"><br><br>

        Description :<br>
        <textarea name="description" rows="3" cols="60">Test calcul distribué</textarea><br><br>

        Type de tâche :
        <select name="task_type">
            <option value="montecarlo_pi">Plugin: montecarlo_pi</option>
            <option value="montecarlo">Plugin: montecarlo</option>
        </select><br><br>

        Chunks :<br>
        <input name="chunks" type="number" value="5" min="1" max="200"><br><br>

        Taille (n) :<br>
        <input name="size" type="number" value="200000" min="1000"><br><br>

        <button type="submit">Créer le job</button>
    </form>
    """
    return render_template_string(html, token=token)

@app.route("/jobs")
@require_admin_route
def jobs_view():
    token = request.args.get("token")
    html = """
    <h1>Jobs GreenIdle</h1>
    <p>
      <a href="/dashboard?token={{ token }}">⬅ Dashboard</a> |
      <a href="/submit?token={{ token }}">➕ Nouveau job</a>
    </p>

    {% if jobs %}
    <table border="1" cellspacing="0" cellpadding="6">
      <tr>
        <th>ID</th><th>Nom</th><th>Type</th><th>Status</th><th>Chunks</th><th>Secondes</th><th>Créé le</th><th>Détail</th>
      </tr>
      {% for j in jobs %}
      <tr>
        <td>{{ j.job_id }}</td>
        <td>{{ j.name }}</td>
        <td>{{ j.task_type }}</td>
        <td>{{ j.status }}</td>
        <td>{{ j.total_chunks }}</td>
        <td>{{ j.total_seconds }}</td>
        <td>{{ j.created_at }}</td>
        <td><a href="/jobs/{{ j.job_id }}?token={{ token }}">Voir</a></td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
      <p>Aucun job (après redeploy Render, la mémoire repart à zéro). Clique sur “Nouveau job”.</p>
    {% endif %}
    """
    return render_template_string(html, jobs=list(jobs.values()), token=token)

def aggregate_job_result(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return None

    if job.get("task_type") not in ("montecarlo_pi", "montecarlo"):
        return None

    inside_sum = 0
    total_sum = 0
    for t in tasks.values():
        if t["job_id"] == job_id and t.get("result"):
            r = t["result"] or {}
            inside_sum += int(r.get("inside", 0))
            total_sum += int(r.get("total", 0))

    if total_sum <= 0:
        return {"pi": None, "inside": inside_sum, "total": total_sum}

    pi_est = 4.0 * inside_sum / float(total_sum)
    return {"pi": pi_est, "inside": inside_sum, "total": total_sum}

@app.route("/jobs/<job_id>")
@require_admin_route
def job_detail(job_id):
    token = request.args.get("token")
    job = jobs.get(job_id)
    if not job:
        return "Job introuvable", 404

    job_tasks = [t for t in tasks.values() if t["job_id"] == job_id]
    agg = aggregate_job_result(job_id)

    html = """
    <h1>Job {{ job.job_id }}</h1>
    <p><a href="/jobs?token={{ token }}">⬅ Retour</a></p>

    <p><b>Nom:</b> {{ job.name }}</p>
    <p><b>Type:</b> {{ job.task_type }}</p>
    <p><b>Status:</b> {{ job.status }}</p>
    <p><b>Secondes:</b> {{ job.total_seconds }}</p>

    {% if agg %}
      <h2>Résultat agrégé</h2>
      <p><b>PI estimé:</b> {{ agg.pi }}</p>
      <p>inside={{ agg.inside }} / total={{ agg.total }}</p>
    {% endif %}

    <h2>Tâches</h2>
    <table border="1" cellspacing="0" cellpadding="6">
      <tr><th>Task</th><th>Status</th><th>Assignée à</th><th>Secondes</th><th>Result</th></tr>
      {% for t in job_tasks %}
        <tr>
          <td>{{ t.task_id }}</td>
          <td>{{ t.status }}</td>
          <td>{{ t.assigned_to }}</td>
          <td>{{ t.seconds }}</td>
          <td><pre style="margin:0; white-space:pre-wrap;">{{ t.result }}</pre></td>
        </tr>
      {% endfor %}
    </table>
    """
    return render_template_string(html, job=job, job_tasks=job_tasks, token=token, agg=agg)

@app.route("/results")
@require_admin_route
def results_view():
    token = request.args.get("token")
    html = """
    <h1>Résultats</h1>
    <p><a href="/dashboard?token={{ token }}">⬅ Dashboard</a></p>

    {% if rows %}
    <table border="1" cellspacing="0" cellpadding="6">
      <tr><th>Job</th><th>Task</th><th>Machine</th><th>Secondes</th><th>Date</th><th>Result</th></tr>
      {% for r in rows %}
        <tr>
          <td>{{ r.get('job_id') }}</td>
          <td>{{ r.get('task_id') }}</td>
          <td>{{ r.get('machine_id') }}</td>
          <td>{{ r.get('seconds') }}</td>
          <td>{{ r.get('timestamp') }}</td>
          <td><pre style="margin:0; white-space:pre-wrap;">{{ r.get('result') }}</pre></td>
        </tr>
      {% endfor %}
    </table>
    {% else %}
      <p>Aucun résultat.</p>
    {% endif %}
    """
    return render_template_string(html, rows=results, token=token)

# =========================
#   DASHBOARD (ADMIN)
# =========================
@app.route("/dashboard")
@require_admin_route
def dashboard():
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

    <h2 style="color:red;">DASHBOARD V2 – CONFIG + JOBS (FINAL + MIN-SEC)</h2>

    <h1>{{ app_name }} – Tableau de bord</h1>
    <p>
      <strong>Machines :</strong> {{ machines|length }} |
      <strong>Heures totales :</strong> {{ total_hours }}
    </p>

    <p>
      <a href="/submit?token={{ token }}">➕ Nouveau job</a> |
      <a href="/jobs?token={{ token }}">📦 Jobs</a> |
      <a href="/results?token={{ token }}">📊 Résultats</a>
    </p>
    <hr>

    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>Nom</th>
          <th>CPU</th>
          <th>Secondes</th>
          <th>Contrôle</th>
          <th>Paramètres</th>
          <th>Renommer</th>
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

          <td>
            <form method="post" action="/machines/{{ m.machine_id }}/rename?token={{ token }}" style="display:flex; gap:6px;">
              <input type="text" name="display_name" placeholder="Nouveau nom" required>
              <button type="submit">OK</button>
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
