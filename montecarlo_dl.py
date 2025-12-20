import random
import time

def run(payload: dict) -> dict:
    n = int(payload.get("n", 200_000))
    seed = payload.get("seed")
    if seed is not None:
        random.seed(int(seed))

    start = time.time()
    inside = 0

    for _ in range(n):
        x = random.random()
        y = random.random()
        if x*x + y*y <= 1.0:
            inside += 1

    elapsed = time.time() - start
    pi_estimate = 4.0 * inside / float(n)

    return {
        "inside": inside,
        "total": n,
        "pi_estimate": pi_estimate,
        "seconds": max(1, int(elapsed))
    }
