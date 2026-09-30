"""Benchmark candidate PRNG algorithms for an AstrBot random-number plugin.

Runs inside the astrbot container (Python 3.12). Pure stdlib + numpy variants.
Measures: ns per 64-bit draw for raw generation, and ns per bounded integer draw.
"""

import os
import random
import secrets
import struct
import time

N = 2_000_000
MASK64 = (1 << 64) - 1
MASK32 = (1 << 32) - 1


def bench(fn, n=N, repeat=3):
    best = float("inf")
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn(n)
        dt = time.perf_counter() - t0
        best = min(best, dt)
    return best / n * 1e9  # ns per draw


# ---------------- splitmix64 (stateless-ish, counter based) ----------------
def splitmix64_stream(n):
    x = 0x9E3779B97F4A7C15
    for _ in range(n):
        x = (x + 0x9E3779B97F4A7C15) & MASK64
        z = x
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        z ^ (z >> 31)


# ---------------- xorshift64* ----------------
def xorshift64_stream(n):
    x = 0x2545F4914F6CDD1D
    for _ in range(n):
        x ^= (x >> 12)
        x ^= (x << 25) & MASK64
        x ^= (x >> 27)
        (x * 0x2545F4914F6CDD1D) & MASK64


# ---------------- xoshiro256** ----------------
def xoshiro256ss_stream(n):
    s0, s1, s2, s3 = 1, 2, 3, 4

    def rotl(x, k):
        return ((x << k) | (x >> (64 - k))) & MASK64

    for _ in range(n):
        result = (rotl((s1 * 5) & MASK64, 7) * 9) & MASK64
        t = (s1 << 17) & MASK64
        s2 ^= s0
        s3 ^= s1
        s1 ^= s2
        s0 ^= s3
        s2 ^= t
        s3 = rotl(s3, 45)
        result


# ---------------- wyrand ----------------
def wyrand_stream(n):
    seed = 0x12345678
    for _ in range(n):
        seed = (seed + 0xA0761D6478BD642F) & MASK64
        t = seed * ((seed ^ 0xE7037ED1A0B428DB) & MASK64)
        (t ^ (t >> 32)) & MASK64


# ---------------- Mersenne Twister via random.getrandbits ----------------
def mt_getrandbits(n):
    g = random.getrandbits
    for _ in range(n):
        g(64)


def mt_random_float(n):
    r = random.random
    for _ in range(n):
        r()


def mt_randbelow(n):
    ri = random.randint
    for _ in range(n):
        ri(0, 99)


def mt_choices(n):
    c = random.choices
    for _ in range(n):
        c("abcdefghij", k=1)


def mt_sample_unique(n):
    s = random.sample
    for _ in range(n):
        s(range(1, 101), 5)


# ---------------- secrets / os.urandom ----------------
def sec_randbits(n):
    rb = secrets.randbits
    for _ in range(n):
        rb(64)


def sec_randbelow(n):
    cb = secrets.randbelow
    for _ in range(n):
        cb(100)


# ---------------- numpy ----------------
try:
    import numpy as np

    def np_pcg64_bulk(n):
        rng = np.random.default_rng(12345)
        rng.integers(0, 1 << 63, size=n, dtype=np.int64)

    def np_pcg64_bounded(n):
        rng = np.random.default_rng(12345)
        rng.integers(0, 100, size=n)

    def np_pcg64_raw(n):
        rng = np.random.default_rng(12345)
        rng.bit_generator.random_raw(n)

    def np_mt19937_bulk(n):
        rng = np.random.RandomState(12345)
        rng.randint(0, 1 << 30, size=n)

    HAVE_NUMPY = True
except Exception:
    HAVE_NUMPY = False


def main():
    print(f"python {os.sys.version.split()[0]}  numpy={'yes' if HAVE_NUMPY else 'no'}")
    print(f"{'algorithm':<34}{'ns/draw':>12}{'M draws/s':>12}")
    print("-" * 58)

    cases = [
        ("splitmix64 (pure python)", splitmix64_stream),
        ("xorshift64* (pure python)", xorshift64_stream),
        ("xoshiro256** (pure python)", xoshiro256ss_stream),
        ("wyrand (pure python)", wyrand_stream),
        ("MT19937 random.getrandbits(64)", mt_getrandbits),
        ("MT19937 random.random()", mt_random_float),
        ("MT19937 randbelow(100)", mt_randbelow),
        ("MT19937 choices(k=1)", mt_choices),
        ("MT19937 sample(100,5) unique", mt_sample_unique),
        ("secrets.randbits(64) [CSPRNG]", sec_randbits),
        ("secrets.randbelow(100) [CSPRNG]", sec_randbelow),
    ]
    if HAVE_NUMPY:
        cases += [
            ("numpy PCG64 raw uint64", np_pcg64_raw),
            ("numpy PCG64 integers(2^63)", np_pcg64_bulk),
            ("numpy PCG64 integers(100)", np_pcg64_bounded),
            ("numpy MT19937 randint(2^30)", np_mt19937_bulk),
        ]

    for name, fn in cases:
        n = 200_000 if "pure python" in name else N
        ns = bench(fn, n=n)
        print(f"{name:<34}{ns:>12.1f}{1000.0 / ns:>12.1f}")


if __name__ == "__main__":
    main()
