"""
solvo_core.py - business logic for Solvo (no Streamlit code in here).

Solvo = a Finary-style wealth tracker + "Proof-of-Patrimoine":
a privacy-preserving, tamper-evident certificate of solvency.

Modules
-------
1. Portfolio model      : assets, categories, haircuts, net worth, history
2. Freedom Clock        : runway + Monte Carlo projection to financial independence
3. SolvoChain           : a minimal proof-of-work ledger simulating an Ethereum L2 (Base)
4. Merkle commitments   : salted leaves, Merkle root, inclusion proofs
5. Proof-of-Patrimoine  : create / anchor / share / revoke / verify a solvency claim

Everything is pure Python (hashlib, json, random) so the app runs anywhere
with `streamlit run app.py`.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import secrets
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# 0. Helpers
# ---------------------------------------------------------------------------


def sha256(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def canonical(obj) -> str:
    """Deterministic JSON (sorted keys, no spaces) so hashes are reproducible."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def now_ts() -> float:
    return time.time()


def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# 1. Portfolio model
# ---------------------------------------------------------------------------

# category -> (display order, liquidity haircut, annual expected return, annual volatility)
# Haircuts mimic what a lender would apply to collateral: the riskier / less liquid
# the asset, the less of its value counts towards a *liquidity* claim.
CATEGORIES = {
    "Cash & savings":  {"haircut": 0.00, "mu": 0.025, "sigma": 0.00, "liquid": True},
    "Life insurance":  {"haircut": 0.10, "mu": 0.035, "sigma": 0.05, "liquid": True},
    "Stocks & ETFs":   {"haircut": 0.25, "mu": 0.070, "sigma": 0.16, "liquid": True},
    "Crypto":          {"haircut": 0.50, "mu": 0.080, "sigma": 0.60, "liquid": True},
    "Real estate":     {"haircut": 1.00, "mu": 0.020, "sigma": 0.05, "liquid": False},
    "Debt":            {"haircut": 0.00, "mu": 0.000, "sigma": 0.00, "liquid": False},
}
ASSET_CATEGORIES = [c for c in CATEGORIES if c != "Debt"]

SOURCES = {
    "open_banking": "Bank sync (PSD2)",
    "onchain": "Public wallet (on-chain)",
    "declared": "Self-declared",
}
VERIFIED_SOURCES = {"open_banking", "onchain"}


@dataclass
class Asset:
    id: str
    label: str
    institution: str
    category: str
    value: float            # EUR; debts are stored as positive amounts with category "Debt"
    source: str = "declared"

    @property
    def is_debt(self) -> bool:
        return self.category == "Debt"

    @property
    def liquid_value(self) -> float:
        """Value after liquidity haircut (0 for debts and real estate)."""
        if self.is_debt:
            return 0.0
        return self.value * (1 - CATEGORIES[self.category]["haircut"])


def new_asset_id() -> str:
    return "a_" + secrets.token_hex(4)


def demo_portfolio(user: str = "Camille") -> list[Asset]:
    """A realistic young-professional portfolio living in France (demo data).
    Every tester gets a slightly different portfolio (scaled from their name),
    so classmates trying the app at the same time do not all see the same numbers.
    Camille keeps the reference values used in the live demo."""
    rows = [
        ("Compte courant", "BNP Paribas", "Cash & savings", 3_850, "open_banking"),
        ("Livret A", "BNP Paribas", "Cash & savings", 22_950, "open_banking"),
        ("LDDS", "Boursorama", "Cash & savings", 6_400, "open_banking"),
        ("PEA - MSCI World ETF", "Boursorama", "Stocks & ETFs", 18_720, "open_banking"),
        ("Assurance-vie (fonds euros + UC)", "Linxea", "Life insurance", 14_300, "open_banking"),
        ("Ledger - ETH", "0x8f3c...a91b", "Crypto", 4_150, "onchain"),
        ("Studio Lille (share 50%)", "Notary estimate", "Real estate", 95_000, "declared"),
        ("Mortgage - studio", "Crédit Agricole", "Debt", 61_200, "open_banking"),
        ("Student loan", "LCL", "Debt", 7_800, "declared"),
    ]
    if user.strip().lower() == "camille":
        scale = lambda v: v
    else:
        rng = random.Random(sha256("portfolio|" + user.strip().lower()))
        scale = lambda v: round(v * rng.uniform(0.55, 1.6), -1)
    return [Asset(new_asset_id(), l, i, c, float(scale(v)), s) for (l, i, c, v, s) in rows]


def totals(assets: list[Asset]) -> dict:
    gross = sum(a.value for a in assets if not a.is_debt)
    debts = sum(a.value for a in assets if a.is_debt)
    liquid = sum(a.liquid_value for a in assets)
    liquid_face = sum(a.value for a in assets if not a.is_debt and CATEGORIES[a.category]["liquid"])
    verified = sum(a.value for a in assets if not a.is_debt and a.source in VERIFIED_SOURCES)
    return {
        "gross": gross,
        "debts": debts,
        "net_worth": gross - debts,
        "liquid": liquid,                 # after haircuts
        "liquid_face": liquid_face,       # before haircuts
        "verified_share": (verified / gross) if gross else 0.0,
    }


def allocation(assets: list[Asset]) -> dict[str, float]:
    out = {c: 0.0 for c in ASSET_CATEGORIES}
    for a in assets:
        if not a.is_debt:
            out[a.category] += a.value
    return {k: v for k, v in out.items() if v > 0}


def history(assets: list[Asset], months: int = 12, seed: int = 7) -> list[dict]:
    """
    Back-filled monthly history ending at today's values (demo only: a real app
    stores daily snapshots from each sync). Each category follows a random walk
    with its own volatility; we then rescale so the last point equals today.
    """
    rng = random.Random(seed)
    alloc = allocation(assets)
    debt = sum(a.value for a in assets if a.is_debt)
    today = datetime.now().replace(day=1)
    series = {}
    for cat, value in alloc.items():
        p = CATEGORIES[cat]
        path = [1.0]
        for _ in range(months):
            m = p["mu"] / 12
            s = p["sigma"] / math.sqrt(12)
            path.append(path[-1] * (1 + m + s * rng.gauss(0, 1)) + 0.012)  # +1.2%/m savings
        scale = value / path[-1]
        series[cat] = [x * scale for x in path]
    rows = []
    for i in range(months + 1):
        d = (today - timedelta(days=30 * (months - i))).strftime("%Y-%m")
        debt_i = debt * (1 + 0.006 * (months - i))  # debt amortises over time
        for cat in series:
            rows.append({"month": d, "category": cat, "value": series[cat][i]})
        rows.append({"month": d, "category": "Debt", "value": -debt_i})
    return rows


# ---------------------------------------------------------------------------
# 2. Freedom Clock
# ---------------------------------------------------------------------------


def runway_months(assets: list[Asset], monthly_expenses: float) -> float:
    if monthly_expenses <= 0:
        return float("inf")
    return totals(assets)["liquid"] / monthly_expenses


def portfolio_mu_sigma(assets: list[Asset]) -> tuple[float, float]:
    """Value-weighted expected return / volatility of investable assets (no real estate)."""
    inv = [a for a in assets if not a.is_debt and a.category != "Real estate"]
    total = sum(a.value for a in inv) or 1.0
    mu = sum(a.value * CATEGORIES[a.category]["mu"] for a in inv) / total
    # simple approximation (assets assumed 50% correlated)
    var_ind = sum((a.value / total * CATEGORIES[a.category]["sigma"]) ** 2 for a in inv)
    sigma = math.sqrt(var_ind + 0.5 * (sum(a.value / total * CATEGORIES[a.category]["sigma"] for a in inv) ** 2 - var_ind))
    return mu, sigma


def fire_projection(start: float, monthly_saving: float, annual_expenses: float,
                    mu: float, sigma: float, years: int = 40, n_paths: int = 400,
                    swr: float = 0.04, seed: int = 42) -> dict:
    """
    Monte Carlo projection of investable wealth. Target = annual expenses / safe
    withdrawal rate (4% rule -> 25x expenses). Returns percentile bands and the
    median number of years to reach the target.
    """
    rng = random.Random(seed)
    target = annual_expenses / swr if swr > 0 else float("inf")
    paths = []
    hit_years = []
    for _ in range(n_paths):
        w = start
        path = [w]
        hit = None
        for y in range(1, years + 1):
            r = rng.gauss(mu, sigma)
            w = w * (1 + r) + 12 * monthly_saving
            path.append(w)
            if hit is None and w >= target:
                hit = y
        paths.append(path)
        hit_years.append(hit if hit is not None else years + 1)
    bands = []
    for y in range(years + 1):
        col = sorted(p[y] for p in paths)
        bands.append({
            "year": y,
            "p10": col[int(0.10 * (n_paths - 1))],
            "p50": col[int(0.50 * (n_paths - 1))],
            "p90": col[int(0.90 * (n_paths - 1))],
        })
    hit_sorted = sorted(hit_years)
    reached = sum(1 for h in hit_years if h <= years) / n_paths
    return {
        "target": target,
        "bands": bands,
        "median_years": hit_sorted[n_paths // 2],
        "p_reach": reached,
    }


# ---------------------------------------------------------------------------
# 3. SolvoChain - minimal ledger simulating an Ethereum L2
# ---------------------------------------------------------------------------

DIFFICULTY = 3  # leading hex zeros for the toy proof-of-work


@dataclass
class Block:
    index: int
    timestamp: float
    txs: list
    prev_hash: str
    nonce: int = 0
    hash: str = ""

    def header(self) -> str:
        return canonical({
            "index": self.index, "timestamp": self.timestamp,
            "txs_root": sha256(canonical(self.txs)), "prev": self.prev_hash, "nonce": self.nonce,
        })

    def compute_hash(self) -> str:
        return sha256(self.header())


class SolvoChain:
    """Append-only hash-chained ledger. In production this would be a smart
    contract on Base (Ethereum L2); here we simulate it so the demo is offline."""

    def __init__(self):
        genesis = Block(0, now_ts(), [{"type": "GENESIS", "msg": "SolvoChain v0.1"}], "0" * 64)
        self._mine(genesis)
        self.blocks: list[Block] = [genesis]

    @staticmethod
    def _mine(block: Block) -> None:
        block.nonce = 0
        h = block.compute_hash()
        while not h.startswith("0" * DIFFICULTY):
            block.nonce += 1
            h = block.compute_hash()
        block.hash = h

    def submit(self, txs: list[dict]) -> Block:
        """Each submit = one block (we skip a mempool for clarity)."""
        stamped = []
        for tx in txs:
            tx = dict(tx)
            tx["ts"] = now_ts()
            tx["txid"] = "0x" + sha256(canonical(tx))[:24]
            stamped.append(tx)
        prev = self.blocks[-1]
        block = Block(prev.index + 1, now_ts(), stamped, prev.hash)
        self._mine(block)
        self.blocks.append(block)
        return block

    def verify_integrity(self) -> tuple[bool, int | None]:
        """Recompute every hash + link. Returns (ok, first_bad_block_index)."""
        for i, b in enumerate(self.blocks):
            if b.compute_hash() != b.hash or not b.hash.startswith("0" * DIFFICULTY):
                return False, i
            if i > 0 and b.prev_hash != self.blocks[i - 1].hash:
                return False, i
        return True, None

    def find_tx(self, predicate) -> tuple[Block | None, dict | None]:
        for b in self.blocks:
            for tx in b.txs:
                if predicate(tx):
                    return b, tx
        return None, None

    def all_txs(self):
        for b in self.blocks:
            for tx in b.txs:
                yield b, tx


# ---------------------------------------------------------------------------
# 4. Merkle commitments
# ---------------------------------------------------------------------------


def leaf_hash(leaf: dict) -> str:
    return sha256("LEAF:" + canonical(leaf))


def _node(a: str, b: str) -> str:
    return sha256("NODE:" + a + b)


def merkle_levels(leaf_hashes: list[str]) -> list[list[str]]:
    level = list(leaf_hashes)
    levels = [level]
    while len(level) > 1:
        if len(level) % 2 == 1:
            level = level + [level[-1]]  # duplicate last on odd levels
        level = [_node(level[i], level[i + 1]) for i in range(0, len(level), 2)]
        levels.append(level)
    return levels


def merkle_root(leaf_hashes: list[str]) -> str:
    return merkle_levels(leaf_hashes)[-1][0]


def merkle_path(leaf_hashes: list[str], index: int) -> list[dict]:
    """Inclusion proof: sibling hashes from leaf to root."""
    path = []
    levels = merkle_levels(leaf_hashes)
    idx = index
    for level in levels[:-1]:
        lvl = level + [level[-1]] if len(level) % 2 == 1 else level
        sib = idx ^ 1
        path.append({"hash": lvl[sib], "side": "left" if sib < idx else "right"})
        idx //= 2
    return path


def verify_path(lh: str, path: list[dict], root: str) -> bool:
    h = lh
    for step in path:
        h = _node(step["hash"], h) if step["side"] == "left" else _node(h, step["hash"])
    return h == root


# ---------------------------------------------------------------------------
# 5. Proof-of-Patrimoine
# ---------------------------------------------------------------------------

ISSUER = "solvo-aggregator"   # the aggregator attests values it read from a bank API / blockchain

CLAIMS = {
    "downpayment": "Liquid assets ≥ amount (mortgage down payment)",
    "rent": "Liquid assets ≥ k × monthly rent (rental guarantee - voluntary)",
    "networth": "Net worth ≥ amount",
}


def build_snapshot(assets: list[Asset], owner: str) -> dict:
    """
    Commit to the full portfolio. Leaf 0 is a header that fixes the number of
    leaves and of debts (so debts cannot be silently left out of a net-worth proof).
    Labels/institutions are hidden behind a salted hash: revealing a leaf shows
    category + value + source, never the bank name or account label.
    """
    debts = [a for a in assets if a.is_debt]
    items = [a for a in assets if not a.is_debt] + debts
    header = {
        "kind": "header", "owner": owner, "n_leaves": len(items) + 1,
        "n_debts": len(debts), "currency": "EUR", "snapshot_at": round(now_ts()),
        "salt": secrets.token_hex(16),
    }
    leaves = [header]
    for a in items:
        salt = secrets.token_hex(16)
        leaves.append({
            "kind": "debt" if a.is_debt else "asset",
            "category": a.category,
            "value_cents": int(round(a.value * 100)),
            "source": a.source,
            "label_commit": sha256(a.label + "|" + a.institution + "|" + salt),
            "salt": salt,
        })
    hashes = [leaf_hash(l) for l in leaves]
    return {"leaves": leaves, "hashes": hashes, "root": merkle_root(hashes)}


def _liquid_leaf_value(leaf: dict) -> float:
    if leaf["kind"] != "asset":
        return 0.0
    return leaf["value_cents"] / 100 * (1 - CATEGORIES[leaf["category"]]["haircut"])


def claim_threshold(claim: dict) -> float:
    if claim["type"] == "rent":
        return claim["rent"] * claim["multiple"]
    return claim["amount"]


def select_leaves(snapshot: dict, claim: dict, mode: str) -> list[int] | None:
    """
    Choose which leaves to reveal.
    - minimal : the fewest asset leaves whose (haircut) value clears the threshold
    - full    : everything (maximum transparency)
    Returns None if the portfolio cannot satisfy the claim.
    """
    leaves = snapshot["leaves"]
    thr = claim_threshold(claim)
    debt_idx = [i for i, l in enumerate(leaves) if l["kind"] == "debt"]
    if mode == "full":
        idx = list(range(len(leaves)))
    else:
        if claim["type"] == "networth":
            key = lambda i: leaves[i]["value_cents"]
            need = thr * 100 + sum(leaves[i]["value_cents"] for i in debt_idx)
            cands = sorted([i for i, l in enumerate(leaves) if l["kind"] == "asset"], key=key, reverse=True)
            idx, acc = [], 0
            for i in cands:
                if acc >= need:
                    break
                idx.append(i)
                acc += leaves[i]["value_cents"]
            if acc < need:
                return None
            idx = [0] + debt_idx + idx
        else:
            cands = sorted([i for i, l in enumerate(leaves) if _liquid_leaf_value(l) > 0],
                           key=lambda i: _liquid_leaf_value(leaves[i]), reverse=True)
            idx, acc = [], 0.0
            for i in cands:
                if acc >= thr:
                    break
                idx.append(i)
                acc += _liquid_leaf_value(leaves[i])
            if acc < thr:
                return None
            idx = [0] + idx
    return sorted(set(idx))


def evaluate_claim(claim: dict, revealed: list[dict]) -> dict:
    """Pure function used by the verifier: does the revealed data satisfy the claim?"""
    thr = claim_threshold(claim)
    if claim["type"] == "networth":
        assets = sum(l["value_cents"] for l in revealed if l["kind"] == "asset") / 100
        debts = sum(l["value_cents"] for l in revealed if l["kind"] == "debt") / 100
        proven = assets - debts
    else:
        proven = sum(_liquid_leaf_value(l) for l in revealed)
    asset_leaves = [l for l in revealed if l["kind"] == "asset"]
    total_rev = sum(l["value_cents"] for l in asset_leaves) or 1
    verified_rev = sum(l["value_cents"] for l in asset_leaves if l["source"] in VERIFIED_SOURCES)
    return {"threshold": thr, "proven": proven, "ok": proven >= thr,
            "verified_share": verified_rev / total_rev}


def meta_of(proof: dict) -> dict:
    return {"root": proof["root"], "claim": proof["claim"], "audience": proof["audience"],
            "expires_at": proof["expires_at"], "nonce": proof["nonce"]}


@dataclass
class ProofRecord:
    code: str
    proof: dict
    created_at: float
    owner_name: str = ""
    revoked: bool = False


class Solvo:
    """Application state: portfolio + chain + issued proofs."""

    def __init__(self, user: str = "Camille", chain: SolvoChain | None = None,
                 proofs: dict | None = None, lock=None):
        """`chain`, `proofs` and `lock` can be shared between several users (online
        demo): each user keeps a private portfolio, but everybody writes to the same
        ledger, so a proof issued on one phone can be verified on another."""
        self.user = user
        # pseudonymous on-chain id (random part so two "Camille" never collide)
        self.owner = "0x" + sha256("solvo-user|" + user + "|" + secrets.token_hex(8))[:40]
        self.assets: list[Asset] = []
        self.chain = chain if chain is not None else SolvoChain()
        self.proofs: dict[str, ProofRecord] = proofs if proofs is not None else {}
        self.lock = lock if lock is not None else threading.RLock()

    # ---- portfolio --------------------------------------------------------
    def load_demo(self):
        self.assets = demo_portfolio(self.user)

    def my_proofs(self) -> dict[str, "ProofRecord"]:
        return {c: r for c, r in self.proofs.items() if r.proof["owner"] == self.owner}

    def add_asset(self, label, institution, category, value, source="declared"):
        self.assets.append(Asset(new_asset_id(), label, institution, category, float(value), source))

    def connect_wallet(self, address: str) -> Asset:
        """Simulated read of a public wallet balance (deterministic from the address)."""
        h = int(sha256(address.lower()), 16)
        value = 500 + (h % 9500)
        a = Asset(new_asset_id(), "Wallet " + address[:6] + "…" + address[-4:], address, "Crypto", float(value), "onchain")
        self.assets.append(a)
        return a

    # ---- proofs -----------------------------------------------------------
    def create_proof(self, claim: dict, audience: str, valid_days: int, mode: str = "minimal") -> tuple[str | None, str]:
        snap = build_snapshot(self.assets, self.owner)
        idx = select_leaves(snap, claim, mode)
        if idx is None:
            return None, "Your portfolio does not satisfy this claim - no proof issued."

        # 1) issuer attestation: the aggregator vouches for leaves it read itself
        verified_hashes = [snap["hashes"][i] for i, l in enumerate(snap["leaves"])
                           if l.get("source") in VERIFIED_SOURCES]
        if verified_hashes:
            with self.lock:
                self.chain.submit([{"type": "ISSUER_ATTEST", "issuer": ISSUER, "leaf_hashes": verified_hashes}])

        # 2) build the shareable package
        proof = {
            "version": "solvo-pop/0.1",
            "owner": self.owner,
            "root": snap["root"],
            "n_leaves": len(snap["leaves"]),
            "claim": claim,
            "audience": audience.strip(),
            "expires_at": round(now_ts() + valid_days * 86400),
            "nonce": secrets.token_hex(8),
            "revealed": [
                {"index": i, "leaf": snap["leaves"][i], "path": merkle_path(snap["hashes"], i)}
                for i in idx
            ],
        }
        meta_hash = sha256(canonical(meta_of(proof)))

        # 3) anchor root + metadata hash on chain
        with self.lock:
            block = self.chain.submit([{"type": "ANCHOR_PROOF", "owner": self.owner,
                                        "root": snap["root"], "meta_hash": meta_hash}])
            proof["anchor_block"] = block.index
            code = "SOLVO-" + meta_hash[:4].upper() + "-" + meta_hash[4:8].upper()
            self.proofs[code] = ProofRecord(code, proof, now_ts(), self.user)
        return code, f"Proof anchored in block #{block.index}."

    def revoke(self, code: str) -> None:
        rec = self.proofs[code]
        if rec.proof["owner"] != self.owner:
            raise PermissionError("Only the holder can revoke a proof.")
        meta_hash = sha256(canonical(meta_of(rec.proof)))
        with self.lock:
            self.chain.submit([{"type": "REVOKE", "owner": self.owner, "meta_hash": meta_hash}])
        rec.revoked = True

    # ---- verification (what the landlord / bank runs) ---------------------
    def verify(self, proof: dict, expected_audience: str | None = None) -> dict:
        checks = []

        def add(name, ok, detail=""):
            checks.append({"check": name, "ok": bool(ok), "detail": detail})

        try:
            revealed = proof["revealed"]
            root = proof["root"]
            # a) every revealed leaf hashes to the committed root
            paths_ok = all(verify_path(leaf_hash(r["leaf"]), r["path"], root) for r in revealed)
            add("Merkle inclusion", paths_ok, f"{len(revealed)} revealed leaf(s) → root {root[:12]}…")

            # b) header present and consistent; all debts disclosed for net-worth claims
            header = next((r["leaf"] for r in revealed if r["index"] == 0), None)
            header_ok = header is not None and header.get("kind") == "header" \
                and header.get("n_leaves") == proof["n_leaves"] and header.get("owner") == proof["owner"]
            add("Header committed", header_ok, "leaf count & owner fixed inside the tree")
            if proof["claim"]["type"] == "networth" and header_ok:
                n_debts_rev = sum(1 for r in revealed if r["leaf"]["kind"] == "debt")
                add("All debts disclosed", n_debts_rev == header["n_debts"],
                    f"{n_debts_rev}/{header['n_debts']} debt leaves revealed")

            # c) anchored on chain with identical metadata (claim, audience, expiry)
            meta_hash = sha256(canonical(meta_of(proof)))
            block, tx = self.chain.find_tx(lambda t: t["type"] == "ANCHOR_PROOF"
                                           and t["meta_hash"] == meta_hash and t["root"] == root)
            add("Anchored on SolvoChain", block is not None,
                f"block #{block.index}, tx {tx['txid']}" if block else "no matching anchor - data was altered")

            # d) chain integrity
            ok_chain, bad = self.chain.verify_integrity()
            add("Ledger integrity", ok_chain, "all block hashes valid" if ok_chain else f"block #{bad} tampered")

            # e) not revoked / not expired / right audience
            rb, _ = self.chain.find_tx(lambda t: t["type"] == "REVOKE" and t["meta_hash"] == meta_hash)
            add("Not revoked", rb is None, "" if rb is None else f"revoked in block #{rb.index}")
            add("Not expired", now_ts() < proof["expires_at"], "valid until " + fmt_ts(proof["expires_at"]))
            if expected_audience:
                add("Issued for you", proof["audience"].lower() == expected_audience.strip().lower(),
                    f"audience = '{proof['audience']}'")

            # f) claim itself
            ev = evaluate_claim(proof["claim"], [r["leaf"] for r in revealed])
            add("Claim satisfied", ev["ok"], f"proven €{ev['proven']:,.0f} vs required €{ev['threshold']:,.0f}")

            # g) how much of the revealed value is attested by the aggregator
            attested = set()
            for _, t in self.chain.all_txs():
                if t["type"] == "ISSUER_ATTEST" and t["issuer"] == ISSUER:
                    attested.update(t["leaf_hashes"])
            asset_rev = [r["leaf"] for r in revealed if r["leaf"]["kind"] == "asset"]
            tot = sum(l["value_cents"] for l in asset_rev) or 1
            att = sum(l["value_cents"] for l in asset_rev if leaf_hash(l) in attested)
            verified_share = att / tot
        except (KeyError, TypeError, StopIteration) as e:
            add("Well-formed proof", False, f"malformed package: {e}")
            return {"valid": False, "checks": checks, "verified_share": 0.0, "eval": None}

        valid = all(c["ok"] for c in checks)
        return {"valid": valid, "checks": checks, "verified_share": verified_share, "eval": ev}
