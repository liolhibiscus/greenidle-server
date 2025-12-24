import time
import random
import math

def score_function(params: dict, metric: str) -> float:
    """
    Fonction de score maîtrisée (safe).
    Exemple générique : surface convexe avec minimum.
    """
    a = float(params.get("alpha", 0))
    b = float(params.get("beta", 0))
    g = float(params.get("gamma", 0))

    # Exemple de "loss" artificielle mais crédible
    loss = (a - 0.3) ** 2 + (b - 2) ** 2 + (g - 15) ** 2

    if metric == "maximize_score":
        return -loss  # maximiser
    return loss      # minimiser

def run(payload: dict) -> dict:
    start = time.time()

    params = payload.get("params", {})
    metric = payload.get("metric", "minimize_loss")
    seed = payload.get("seed")

    if seed is not None:
        random.seed(int(seed))

    # Sécurité
    if not isinstance(params, dict) or not params:
        return {
            "error": "invalid_params",
            "seconds": 1
        }

    score = score_function(params, metric)

    elapsed = time.time() - start

    return {
        "tested_params": params,
        "metric": metric,
        "score": round(score, 6),
        "seconds": max(1, int(elapsed))
    }

{
  "grid": {
    "alpha": [0.1, 0.2, 0.3, 0.4],
    "beta": [1, 2, 3],
    "gamma": [10, 15, 20]
  },
  "metric": "minimize_loss",
  "seed": 42
}

