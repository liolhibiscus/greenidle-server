from flask import Flask, request, jsonify, render_template_string, redirect, url_for
from datetime import datetime
import uuid

app = Flask(__name__)

# =========================
#   CONFIG GREENIDLE
# =========================
APP_NAME = "GreenIdle"

# 🔐 Admin (dashboard / jobs) : seul toi
ADMIN_TOKEN = "Iletait1fois@33"

# 🔐 Client (anti-squattage) : toutes les machines doivent l’avoir
CLIENT_TOKEN = "GI_CLIENT_2025_!42"

DEBUG = False  # False sur Render


# =========================
#   MINI BDD EN MEMOIRE
# =========================
machines = {}   # machine_id -> dict
jobs = {}       # job_id -> dict
tasks = {}      # task_id -> dict
results = []    # list[dict]
tasks_log = []  # list[dict]


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


def require_client():
    # Accepte le token client en querystring (GET) ou dans le JSON (POST)
    if request.method == "GET":
        token = request.args.get("client_token")
    else:
        data = request.json or {}
        token = data.get("client_token") or request.args.get("client_token")
    return token == CLIENT_TOKEN


# =========================
#   API CLIENTS (PROTEGEES)
# =========================
@app.route("/register", methods=["POST"])
def register():
    if not require_client():
        return jsonify({"error": "unauthorized client"}), 403

    data = request.json or {}
    machine_id = data.get("machine_id")
    client_name = data.get("client_name")

    if not machine_id:
        return jsonify({"error": "machine_id manquant"}), 400

    m = ensure_machine(machine_id, client_name)
    m["last_seen"] = now_iso()
    return jsonify({"status": "ok", "message": "machine enregistree"})


@app.route("/heartbeat", methods=["POST"])
def heartbeat():
    if not require_client():
        return jsonify({"error": "unauthorized client"}), 403

    data = request.json or {}
    machine_id = data.get("machine_id")
    cpu = float(data.get("cpu_percent", 0.0))

    if not machine_id:
        return jsonify({"error": "machine_id manquant"}), 400

    m = ensure_machine(machine_id)
    m["last_seen"] = now_iso()
    m["last_cpu"] = cpu
    return jsonify({"status": "ok"})


@app.route("/task", methods=["GET"])
def get_task():
    if not require_client():
        return jsonify({"error": "unauthorized client"}), 403

    machine_id = request.args.get("machine_id")
    ensure_machine(machine_id)

    # 1) Cherche une tâche utilisateur en attente
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
                "size": t["size"]
            })

    # 2) Sinon tâche démo
    return jsonify({
        "task_id": "demo-task",
        "payload": "demo",
        "max_duration_seconds": 10
    })


@app.route("/report", methods=["POST"])
def report():
    if not require_client():
        return jsonify({"error": "unauthorized client"}), 403

    data = request.json or {}
    machine_id = data.get("machine_id")
    task_id = data.get("task_id")
    seconds = int(data.get("seconds", 0))

    if not machine_id or task_id is None:
        return jsonify({"error": "machine_id ou task_id manquant"}), 400

    m = ensure_machine(machine_id)
    m["total_seconds"] += seconds
    m["last_seen"] = now_iso()

    tasks_log.append({
        "machine_id": machine_id,
        "task_id": task_id,
        "seconds": seconds,
        "reported_at": now_iso()
    })

    # Si c'est une vraie tâche job
    if task_id in tasks:
        t = tasks[task_id]
        t["status"] = "done"
        t["seconds"] = t.get("seconds", 0) + seconds
        t["updated_at"] = now_iso()

        job = jobs.get(t["job_id"])
        if job:
            job["total_seconds"] += seconds
            # Job terminé si toutes ses tasks sont done
            all_done = all(
                (tt["status"] == "done")
                for tt in tasks.values()
                if tt["job_id"] == job["job_id"]
            )
            if all_done:
                job["status"] = "done"

        results.append({
            "job_id": t["job_id"],
            "task_id": task_id,
            "machine_id": machine_id,
            "seconds": seconds,
            "timestamp": now_iso()
        })

    return jsonify({"status": "ok"})


@app.route("/status", methods=["GET"])
def status():
    total_seconds = sum(m["total_seconds"] for m in machines.values())
    return jsonify({
        "app": APP_NAME,
        "machines_count": len(machines),
        "total_hours": round(total_seconds / 3600, 4),
        "jobs_count": len(jobs),
        "machines": list(machines.values()),
    })


# =========================
#   ADMIN: RENAME + JOBS
# =========================
@app.route("/machines/<machine_id>/rename", methods=["POST"])
def rename_machine(machine_id):
    if not require_admin():
        return "Accès refusé (admin token)", 403

    if machine_id not in machines:
        return "Machine inconnue", 404

    # Form HTML
    new_name = request.form.get("display_name")
    if not new_name:
        # compat API JSON si besoin
        new_name = (request.json or {}).get("display_name")

    if not new_name:
        return "Nom manquant", 400

    machines[machine_id]["display_name"] = new_name

    token = request.args.get("token")
    return redirect(url_for("dashboard", token=token))


@app.route("/submit", methods=["GET", "POST"])
def submit_job():
    if not require_admin():
        return "Accès refusé (admin token)", 403

    token = request.args.get("token")

    if request.method == "POST":
        name = request.form.get("name", "Job sans nom")
        description = request.form.get("description", "")
        task_type = request.form.get("task_type", "demo")  # demo / montecarlo
        total_chunks = int(request.form.get("chunks", 10))
        size = int(request.form.get("size", 10000))

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
            tasks[task_id] = {
                "task_id": task_id,
                "job_id": job_id,
                "task_type": task_type,
                "size": size,
                "status": "pending",
                "assigned_to": None,
                "created_at": now_iso(),
                "updated_at": None,
                "seconds": 0
            }

        return redirect(url_for("jobs_view", token=token))

    html = """
    <h1>Soumettre un job GreenIdle</h1>
    <form method="post">
        Nom du job :<br>
        <input name="name" type="text" value="Simulation"><br><br>

        Description :<br>
        <textarea name="description" rows="3" cols="40"></textarea><br><br>

        Type de tâche :
        <select name="task_type">
            <option value="demo">Démo</option>
            <option value="montecarlo">Monte Carlo</option>
        </select><br><br>

        Chunks :<br>
        <input name="chunks" type="number" value="10"><br><br>

        Taille (size) :<br>
        <input name="size" type="number" value="10000"><br><br>

        <button type="submit">Créer le job</button>
    </form>
    <br>
    <a href="/dashboard?token={{ token }}">Retour dashboard</a>
    """
    return render_template_string(html, token=token)


@app.route("/jobs")
def jobs_view():
    if not require_admin():
        return "Accès refusé (admin token)", 403

    token = request.args.get("token")
    html = """
    <h1>Jobs GreenIdle</h1>
    <p><a href="/dashboard?token={{ token }}">Retour dashboard</a> |
       <a href="/submit?token={{ token }}">Nouveau job</a></p>

    {% if jobs %}
    <table border="1" cellspacing="0" cellpadding="5">
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
    <p>Aucun job.</p>
    {% endif %}
    """
    return render_template_string(html, jobs=list(jobs.values()), token=token)


@app.route("/jobs/<job_id>")
def job_detail(job_id):
    if not require_admin():
        return "Accès refusé (admin token)", 403

    token = request.args.get("token")
    job = jobs.get(job_id)
    if not job:
        return "Job introuvable", 404

    job_tasks = [t for t in tasks.values() if t["job_id"] == job_id]

    html = """
    <h1>Job {{ job.job_id }}</h1>
    <p><b>Nom:</b> {{ job.name }}</p>
    <p><b>Status:</b> {{ job.status }}</p>
    <p><b>Secondes:</b> {{ job.total_seconds }}</p>
    <p><a href="/jobs?token={{ token }}">Retour</a></p>

    <h2>Tâches</h2>
    <table border="1" cellspacing="0" cellpadding="5">
      <tr><th>Task</th><th>Status</th><th>Assignée à</th><th>Secondes</th></tr>
      {% for t in job_tasks %}
        <tr>
          <td>{{ t.task_id }}</td>
          <td>{{ t.status }}</td>
          <td>{{ t.assigned_to }}</td>
          <td>{{ t.seconds }}</td>
        </tr>
      {% endfor %}
    </table>
    """
    return render_template_string(html, job=job, job_tasks=job_tasks, token=token)


@app.route("/results")
def results_view():
    if not require_admin():
        return "Accès refusé (admin token)", 403

    token = request.args.get("token")
    html = """
    <h1>Résultats</h1>
    <p><a href="/dashboard?token={{ token }}">Retour dashboard</a></p>
    {% if rows %}
    <table border="1" cellspacing="0" cellpadding="5">
      <tr><th>Job</th><th>Task</th><th>Machine</th><th>Secondes</th><th>Date</th></tr>
      {% for r in rows %}
        <tr>
          <td>{{ r.job_id }}</td>
          <td>{{ r.task_id }}</td>
          <td>{{ r.machine_id }}</td>
          <td>{{ r.seconds }}</td>
          <td>{{ r.timestamp }}</td>
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
@app.route("/")
def home():
    # On n’expose pas le token automatiquement : tu passes par /dashboard?token=...
    return "GreenIdle server OK. Utilise /dashboard?token=... (admin).", 200


@app.route("/dashboard")
def dashboard():
    if not require_admin():
        return "Accès refusé (admin token)", 403

    token = request.args.get("token")
    total_seconds = sum(m["total_seconds"] for m in machines.values())
    total_hours = round(total_seconds / 3600, 4)
    machines_count = len(machines)

    html = """
    <!DOCTYPE html>
    <html lang="fr">
    <head>
        <meta charset="utf-8">
        <title>{{ app_name }} - Dashboard</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; }
            h1 { margin-bottom: 0.2em; }
            .stats { margin-bottom: 1em; }
            table { border-collapse: collapse; width: 100%; max-width: 1100px; }
            th, td { border: 1px solid #ccc; padding: 6px 10px; text-align: left; }
            th { background: #f0f0f0; }
            tr:nth-child(even) { background: #fafafa; }
            .small { font-size: 0.9em; color: #666; }
            form { margin: 0; }
            input[type=text]{ width: 180px; }
        </style>
    </head>
    <body>
        <h1>{{ app_name }} - Tableau de bord</h1>
        <div class="stats">
            <p><strong>Machines actives :</strong> {{ machines_count }}</p>
            <p><strong>Total d'heures de calcul :</strong> {{ total_hours }}</p>
            <p>
              <a href="/submit?token={{ token }}">Nouveau job</a> |
              <a href="/jobs?token={{ token }}">Jobs</a> |
              <a href="/results?token={{ token }}">Résultats</a>
            </p>
        </div>

        <h2>Machines</h2>
        {% if machines %}
        <table>
            <thead>
                <tr>
                    <th>ID interne</th>
                    <th>Nom affiché</th>
                    <th>CPU</th>
                    <th>Enregistrée le</th>
                    <th>Dernière activité</th>
                    <th>Secondes</th>
                    <th>Heures</th>
                    <th>Renommer</th>
                </tr>
            </thead>
            <tbody>
                {% for m in machines %}
                <tr>
                    <td>{{ m.machine_id }}</td>
                    <td><strong>{{ m.display_name }}</strong></td>
                    <td>{{ m.last_cpu }} %</td>
                    <td class="small">{{ m.registered_at }}</td>
                    <td class="small">{{ m.last_seen }}</td>
                    <td>{{ m.total_seconds }}</td>
                    <td>{{ (m.total_seconds / 3600) | round(4) }}</td>
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
        {% else %}
        <p>Aucune machine connectée.</p>
        {% endif %}
    </body>
    </html>
    """
    return render_template_string(
        html,
        app_name=APP_NAME,
        machines=list(machines.values()),
        machines_count=machines_count,
        total_hours=total_hours,
        token=token
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=DEBUG)
