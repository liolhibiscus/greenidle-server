from flask import Flask, request, jsonify, render_template_string
from datetime import datetime

app = Flask(__name__)

# =========================
#   CONFIG GREENIDLE
# =========================
APP_NAME = "GreenIdle"
DEBUG = True

# Mini "base de données" en mémoire
machines = {}
tasks_log = []

# Tâches envoyées par les utilisateurs
user_tasks = []  # {task_id, size, assigned, done}
results = []     # {task_id, seconds, machine_id, timestamp}


# =========================
#   API POUR LES CLIENTS
# =========================

@app.route("/register", methods=["POST"])
def register():
    """Enregistrement d'une machine GreenIdle."""
    data = request.json or {}
    machine_id = data.get("machine_id")
    if not machine_id:
        return jsonify({"error": "machine_id manquant"}), 400

    if machine_id not in machines:
        machines[machine_id] = {
            "registered_at": datetime.utcnow().isoformat(),
            "total_seconds": 0,
            "last_seen": None
        }

    print(f"[{APP_NAME}] Register: {machine_id}")
    return jsonify({"status": "ok", "message": "machine enregistree"})


@app.route("/task", methods=["GET"])
def get_task():
    """Donne une tâche au client (tâche utilisateur si dispo, sinon tâche démo)."""
    machine_id = request.args.get("machine_id", "unknown")

    # Tâche utilisateur prioritaire (MonteCarlo)
    for task in user_tasks:
        if not task["assigned"] and not task["done"]:
            task["assigned"] = True
            print(f"[{APP_NAME}] Assign user_task {task['task_id']} to {machine_id}")
            return jsonify({
                "task_id": task["task_id"],
                "payload": "montecarlo",
                "size": task["size"]
            })

    # Sinon : tâche démo
    return jsonify({
        "task_id": "demo-task",
        "payload": "demo",
        "max_duration_seconds": 10
    })


@app.route("/report", methods=["POST"])
def report():
    """Rapport envoyé par un client après un calcul."""
    data = request.json or {}
    machine_id = data.get("machine_id")
    task_id = data.get("task_id")
    seconds = data.get("seconds", 0)

    if not machine_id or task_id is None:
        return jsonify({"error": "machine_id ou task_id manquant"}), 400

    if machine_id not in machines:
        machines[machine_id] = {
            "registered_at": datetime.utcnow().isoformat(),
            "total_seconds": 0,
            "last_seen": None
        }

    machines[machine_id]["total_seconds"] += seconds
    machines[machine_id]["last_seen"] = datetime.utcnow().isoformat()

    tasks_log.append({
        "machine_id": machine_id,
        "task_id": task_id,
        "seconds": seconds,
        "reported_at": datetime.utcnow().isoformat()
    })

    # Si c'était une tâche utilisateur, on la marque terminée + stocke le résultat
    for task in user_tasks:
        if task["task_id"] == task_id:
            task["done"] = True
            results.append({
                "task_id": task_id,
                "seconds": seconds,
                "machine_id": machine_id,
                "timestamp": datetime.utcnow().isoformat()
            })
            break

    total_seconds = machines[machine_id]["total_seconds"]
    print(f"[{APP_NAME}] REPORT {machine_id} +{seconds}s (total={total_seconds}s)")

    return jsonify({"status": "ok"})


@app.route("/status", methods=["GET"])
def status():
    """Statut JSON brut du réseau GreenIdle."""
    total_seconds = sum(m["total_seconds"] for m in machines.values())
    return jsonify({
        "machines_count": len(machines),
        "total_hours": round(total_seconds / 3600, 4),
        "machines": machines,
    })


# =========================
#   INTERFACE UTILISATEUR
# =========================

@app.route("/submit", methods=["GET", "POST"])
def submit():
    """Soumettre une tâche utilisateur (MonteCarlo) via un petit formulaire."""
    if request.method == "POST":
        size = int(request.form.get("size", 10000))
        task_id = f"user_task_{len(user_tasks)+1}"
        user_tasks.append({
            "task_id": task_id,
            "size": size,
            "assigned": False,
            "done": False
        })
        print(f"[{APP_NAME}] New user task {task_id} (size={size})")
        return f"Tâche {task_id} ajoutée ! <a href='/dashboard'>Retour Dashboard</a>"

    html = """
    <h1>Soumettre un calcul GreenIdle</h1>
    <form method="post">
        Taille du calcul (nombre de points Monte Carlo) :<br>
        <input name="size" type="number" value="10000"><br><br>
        <button type="submit">Envoyer</button>
    </form>
    <br>
    <a href="/dashboard">Retour dashboard</a>
    """
    return html


@app.route("/")
def home():
    return dashboard()


@app.route("/dashboard")
def dashboard():
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
            table { border-collapse: collapse; width: 100%; max-width: 900px; }
            th, td { border: 1px solid #ccc; padding: 6px 10px; text-align: left; }
            th { background: #f0f0f0; }
            tr:nth-child(even) { background: #fafafa; }
            .small { font-size: 0.9em; color: #666; }
            h2 { margin-top: 1.5em; }
        </style>
    </head>
    <body>
        <h1>{{ app_name }} - Tableau de bord</h1>
        <div class="stats">
            <p><strong>Machines actives :</strong> {{ machines_count }}</p>
            <p><strong>Total d'heures de calcul :</strong> {{ total_hours }}</p>
            <p><a href="/submit">Soumettre un calcul utilisateur</a></p>
        </div>

        <h2>Machines</h2>
        {% if machines %}
        <table>
            <thead>
                <tr>
                    <th>ID Machine</th>
                    <th>Enregistrée le</th>
                    <th>Dernière activité</th>
                    <th>Secondes totales</th>
                    <th>Heures</th>
                </tr>
            </thead>
            <tbody>
                {% for mid, m in machines.items() %}
                <tr>
                    <td>{{ mid }}</td>
                    <td class="small">{{ m.registered_at }}</td>
                    <td class="small">{{ m.last_seen }}</td>
                    <td>{{ m.total_seconds }}</td>
                    <td>{{ (m.total_seconds / 3600) | round(4) }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        {% else %}
        <p>Aucune machine n'est encore connectée.</p>
        {% endif %}

        <h2>Résultats des tâches utilisateur</h2>
        {% if results %}
        <table>
            <thead>
                <tr>
                    <th>ID Tâche</th>
                    <th>Machine</th>
                    <th>Durée (sec)</th>
                    <th>Date</th>
                </tr>
            </thead>
            <tbody>
            {% for r in results %}
                <tr>
                    <td>{{ r.task_id }}</td>
                    <td>{{ r.machine_id }}</td>
                    <td>{{ r.seconds }}</td>
                    <td class="small">{{ r.timestamp }}</td>
                </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}
        <p>Aucun calcul utilisateur encore terminé.</p>
        {% endif %}
    </body>
    </html>
    """

    return render_template_string(
        html,
        app_name=APP_NAME,
        machines=machines,
        machines_count=machines_count,
        total_hours=total_hours,
        results=results
    )


if __name__ == "__main__":
    # host="0.0.0.0" pour être joignable par d'autres PC sur ton réseau.
    app.run(host="0.0.0.0", port=5000, debug=DEBUG)
