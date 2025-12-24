# optimizer_grid.py
# =========================================================
# GreenIdle plugin: optimizer_grid
# - mode 1: "params" (évalue une seule combinaison)
# - mode 2: "grid" + "chunk_index/chunk_count" (évalue une tranche de la grille)
# Retourne le meilleur candidat trouvé + stats.
# =========================================================

import time
import random
import itertools
from typing import Dict, Any, List, Tuple


def score_function(params: dict, metric: str) -> float:
    """
    Fonction de score maîtrisée (safe).
    Exemple générique : surface convexe avec minimum exact en:
      alpha=0.3, beta=2, gamma=15  => loss = 0
    """
    a = float(params.get("alpha", 0))
    b = float(params.get("beta", 0))
    g = float(params.get("gamma", 0))

    loss = (a - 0.3) ** 2 + (b - 2) ** 2 + (g - 15) ** 2

    # Convention:
    # - metric="minimize_loss" => plus petit est meilleur (loss)
    # - metric="maximize_score" => plus grand est meilleur (on renvoie -loss)
    if metric == "maximize_score":
        return -loss
    return loss


def _as_list(v) -> List[Any]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, tuple):
        return list(v)
    # autorise un scalaire => 1 valeur
    return [v]


def _make_grid_combos(grid: dict) -> List[dict]:
    """
    Convertit:
      {"alpha":[...], "beta":[...], "gamma":[...]}
    en liste de dicts params [{alpha:..., beta:..., gamma:...}, ...]
    Ordre stable (alpha->beta->gamma).
    """
    if not isinstance(grid, dict) or not grid:
        return []

    keys = list(grid.keys())
    values_lists = [_as_list(grid.get(k)) for k in keys]

    if any(len(vs) == 0 for vs in values_lists):
        return []

    combos = []
    for vals in itertools.product(*values_lists):
        combos.append({k: vals[i] for i, k in enumerate(keys)})
    return combos


def _chunk_slice(n_total: int, chunk_index: int, chunk_count: int) -> Tuple[int, int]:
    """
    Retourne [start, end) pour un chunk 1-based.
    Répartition la plus équilibrée possible.
    """
    if chunk_count < 1:
        chunk_count = 1
    if chunk_index < 1:
        chunk_index = 1
    if chunk_index > chunk_count:
        chunk_index = chunk_count

    base = n_total // chunk_count
    rem = n_total % chunk_count

    # chunks 1..rem ont base+1
    # chunks rem+1..chunk_count ont base
    if chunk_index <= rem:
        start = (chunk_index - 1) * (base + 1)
        end = start + (base + 1)
    else:
        start = rem * (base + 1) + (chunk_index - rem - 1) * base
        end = start + base

    return start, min(end, n_total)


def run(payload: dict) -> dict:
    start = time.time()

    if not isinstance(payload, dict):
        return {"error": "invalid_payload", "seconds": 1}

    metric = payload.get("metric", "minimize_loss")
    seed = payload.get("seed")

    if seed is not None:
        try:
            random.seed(int(seed))
        except Exception:
            random.seed(str(seed))

    # -----------------------------------------------------
    # Mode 1: évaluation d'un seul "params"
    # -----------------------------------------------------
    params = payload.get("params")
    if isinstance(params, dict) and params:
        score = score_function(params, metric)
        elapsed = time.time() - start
        return {
            "mode": "single",
            "tested_params": params,
            "metric": metric,
            "score": round(float(score), 6),
            "evaluated": 1,
            "seconds": max(1, int(elapsed)),
        }

    # -----------------------------------------------------
    # Mode 2: grid search (distribué) via "grid" + chunking
    # -----------------------------------------------------
    grid = payload.get("grid")
    if not isinstance(grid, dict) or not grid:
        return {"error": "invalid_params_or_grid", "hint": "Provide payload.params or payload.grid", "seconds": 1}

    combos = _make_grid_combos(grid)
    if not combos:
        return {"error": "invalid_grid", "seconds": 1}

    chunk_index = int(payload.get("chunk_index", 1))
    chunk_count = int(payload.get("chunk_count", 1))

    total = len(combos)
    start_i, end_i = _chunk_slice(total, chunk_index, chunk_count)
    subset = combos[start_i:end_i]

    best_params = None
    best_score = None

    # Pour minimisation: best = score le plus petit
    # Pour maximisation: best = score le plus grand (score_function gère déjà le signe si maximize_score)
    for p in subset:
        s = float(score_function(p, metric))
        if best_score is None:
            best_score = s
            best_params = p
        else:
            if metric == "maximize_score":
                if s > best_score:
                    best_score = s
                    best_params = p
            else:
                if s < best_score:
                    best_score = s
                    best_params = p

    elapsed = time.time() - start

    return {
        "mode": "grid",
        "metric": metric,
        "chunk_index": chunk_index,
        "chunk_count": chunk_count,
        "grid_total": total,
        "grid_slice": {"start": start_i, "end": end_i, "count": len(subset)},
        "best_params": best_params,
        "best_score": round(float(best_score if best_score is not None else 0.0), 6),
        "evaluated": len(subset),
        "seconds": max(1, int(elapsed)),
    }
