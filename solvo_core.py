"""
solvo_core.py - business logic for Solvo v0.2 (no Streamlit code in here).

Solvo = confidential financial verification: a user turns private wealth data
into a *targeted, verifiable, revocable* proof (e.g. "I can cover 3 x the rent")
without disclosing the rest of the portfolio.

What is REAL in this file
  - SHA-256 salted commitments, Merkle tree, Merkle inclusion proofs
  - Ed25519 digital signature of the issuer attestation
  - hash-chained ledger (toy proof-of-work) + strict proof verification
What is SIMULATED
  - bank / wallet connections (demo data)
  - the issuer (a demo key, not a regulated third party)
  - the ledger (in memory, not a public blockchain)

Sections
  1. Portfolio model         2. Issuer + SolvoChain       3. Merkle commitments
  4. Claims & proof package  5. Strict verification       6. Application state
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

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


def fmt_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d %B %Y")


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _is_hex64(s) -> bool:
    return isinstance(s, str) and bool(_HEX64.match(s))


def _is_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


# ---------------------------------------------------------------------------
# 1. Portfolio model
# ---------------------------------------------------------------------------

# Haircuts mimic what a lender applies: the riskier / less liquid the asset,
# the less of its value counts towards a *liquidity* claim.
CATEGORIES = {
    "Cash & savings": {"haircut": 0.00, "mu": 0.025, "sigma": 0.00, "liquid": True},
    "Life insurance": {"haircut": 0.10, "mu": 0.035, "sigma": 0.05, "liquid": True},
    "Stocks & ETFs":  {"haircut": 0.25, "mu": 0.070, "sigma": 0.16, "liquid": True},
    "Crypto":         {"haircut": 0.50, "mu": 0.080, "sigma": 0.60, "liquid": True},
    "Real estate":    {"haircut": 1.00, "mu": 0.020, "sigma": 0.05, "liquid": False},
    "Debt":           {"haircut": 0.00, "mu": 0.000, "sigma": 0.00, "liquid": False},
}
ASSET_CATEGORIES = [c for c in CATEGORIES if c != "Debt"]

# Trust levels (the app never mixes them up):
#   declared  -> typed by the user, never attested
#   *_sim     -> read from a SIMULATED bank / wallet connection ("demo data");
#                these are the only lines the demo issuer will attest
SOURCES = {
    "declared": "Self-declared",
    "bank_sim": "Bank connection (simulated)",
    "wallet_sim": "Wallet (simulated)",
}
TRUST_LEVEL = {"declared": "Self-declared", "bank_sim": "Source-connected · demo",
               "wallet_sim": "Source-connected · demo"}
ATTESTABLE_SOURCES = {"bank_sim", "wallet_sim"}


@dataclass
class Asset:
    id: str
    label: str
    institution: str
    category: str
    value: float                 # EUR; debts are positive amounts with category "Debt"
    source: str = "declared"
    updated_at: float = 0.0

    def __post_init__(self):
        if not self.updated_at:
            self.updated_at = now_ts()

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
    """Demo data. Every tester gets a slightly different portfolio (scaled from
    the name); Camille keeps the reference values used in the live demo."""
    rows = [
        ("Compte courant", "BNP Paribas", "Cash & savings", 3_850, "bank_sim"),
        ("Livret A", "BNP Paribas", "Cash & savings", 22_950, "bank_sim"),
        ("LDDS", "Boursorama", "Cash & savings", 6_400, "bank_sim"),
        ("PEA - MSCI World ETF", "Boursorama", "Stocks & ETFs", 18_720, "bank_sim"),
        ("Assurance-vie (fonds euros + UC)", "Linxea", "Life insurance", 14_300, "bank_sim"),
        ("Ledger - ETH", "0x8f3c...a91b", "Crypto", 4_150, "wallet_sim"),
        ("Studio Lille (share 50%)", "Notary estimate", "Real estate", 95_000, "declared"),
        ("Mortgage - studio", "Crédit Agricole", "Debt", 61_200, "bank_sim"),
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
    liquid = sum(a.liquid_value for a in assets)                       # after haircuts
    investable = sum(a.value for a in assets if not a.is_debt and CATEGORIES[a.category]["liquid"])
    connected = sum(a.value for a in assets if not a.is_debt and a.source in ATTESTABLE_SOURCES)
    return {
        "gross": gross, "debts": debts, "net_worth": gross - debts,
        "liquid": liquid,                 # "eligible liquid assets": what counts in a proof
        "investable": investable,         # before haircuts (kept separate from liquid on purpose)
        "connected_share": (connected / gross) if gross else 0.0,
    }


def allocation(assets: list[Asset]) -> dict[str, float]:
    out = {c: 0.0 for c in ASSET_CATEGORIES}
    for a in assets:
        if not a.is_debt:
            out[a.category] += a.value
    return {k: v for k, v in out.items() if v > 0}


def history(assets: list[Asset], months: int = 12, seed: int = 7) -> list[dict]:
    """SIMULATED back-filled history ending at today's values. It is NOT a real
    financial history: real snapshots are recorded in Solvo.snapshots."""
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
            path.append(path[-1] * (1 + m + s * rng.gauss(0, 1)) + 0.012)
        scale = value / path[-1]
        series[cat] = [x * scale for x in path]
    rows = []
    for i in range(months + 1):
        d = (today - timedelta(days=30 * (months - i))).strftime("%Y-%m")
        debt_i = debt * (1 + 0.006 * (months - i))
        for cat in series:
            rows.append({"month": d, "category": cat, "value": series[cat][i]})
        rows.append({"month": d, "category": "Debt", "value": -debt_i})
    return rows


# ---------------------------------------------------------------------------
# 2. Issuer (Ed25519) + SolvoChain
# ---------------------------------------------------------------------------


class Issuer:
    """A SIMULATED attestation issuer (stands for a bank / aggregator).
    It signs with a real Ed25519 key; the public key is registered on the ledger."""

    def __init__(self, issuer_id: str = "solvo-demo-issuer"):
        self.issuer_id = issuer_id
        self._sk = Ed25519PrivateKey.generate()
        self.public_key = self._sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()

    def info(self) -> dict:
        return {"issuer_id": self.issuer_id, "public_key": self.public_key, "simulated": True}

    def sign(self, payload: dict) -> str:
        return self._sk.sign(canonical(payload).encode("utf-8")).hex()


def verify_signature(public_key_hex: str, payload: dict, signature_hex: str) -> bool:
    try:
        pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        pk.verify(bytes.fromhex(signature_hex), canonical(payload).encode("utf-8"))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


DIFFICULTY = 3  # leading hex zeros for the toy proof-of-work (pedagogical, NOT secure)

EVENT_LABELS = {
    "GENESIS": "Ledger created",
    "ISSUER_REGISTER": "Issuer registered",
    "ANCHOR_PROOF": "Proof created · Merkle root anchored",
    "ISSUER_ATTEST": "Issuer attestation recorded",
    "PROOF_VERIFIED": "Proof verified",
    "REVOKE": "Proof revoked",
}


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


def _stamp(tx: dict) -> dict:
    tx = dict(tx)
    tx["ts"] = now_ts()
    tx["txid"] = "0x" + sha256(canonical(tx))[:24]
    return tx


class SolvoChain:
    """Append-only hash-chained ledger. In production: a smart contract on an
    Ethereum L2 (Base) anchoring Merkle roots and issue/revoke events.
    A hash chain makes edits *detectable*; it does not by itself make them
    impossible (whoever controls the whole ledger could rebuild it)."""

    def __init__(self, issuers: list[dict] | None = None):
        txs = [_stamp({"type": "GENESIS", "msg": "SolvoChain v0.2"})]
        txs += [_stamp({"type": "ISSUER_REGISTER", **i}) for i in (issuers or [])]
        genesis = Block(0, now_ts(), txs, "0" * 64)
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
        stamped = [_stamp(t) for t in txs]
        prev = self.blocks[-1]
        block = Block(prev.index + 1, now_ts(), stamped, prev.hash)
        self._mine(block)
        self.blocks.append(block)
        return block

    def verify_integrity(self) -> tuple[bool, int | None]:
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

    def trusted_issuer(self, issuer_id: str) -> dict | None:
        _, tx = self.find_tx(lambda t: t["type"] == "ISSUER_REGISTER" and t.get("issuer_id") == issuer_id)
        return tx


# ---------------------------------------------------------------------------
# 3. Merkle commitments
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
            level = level + [level[-1]]
        level = [_node(level[i], level[i + 1]) for i in range(0, len(level), 2)]
        levels.append(level)
    return levels


def merkle_root(leaf_hashes: list[str]) -> str:
    return merkle_levels(leaf_hashes)[-1][0]


def merkle_path(leaf_hashes: list[str], index: int) -> list[dict]:
    path = []
    idx = index
    for level in merkle_levels(leaf_hashes)[:-1]:
        lvl = level + [level[-1]] if len(level) % 2 == 1 else level
        sib = idx ^ 1
        path.append({"hash": lvl[sib], "side": "left" if sib < idx else "right"})
        idx //= 2
    return path


def expected_depth(n_leaves: int) -> int:
    d = 0
    while n_leaves > 1:
        n_leaves = (n_leaves + 1) // 2
        d += 1
    return d


def verify_path_at(lh: str, path: list, root: str, index: int, n_leaves: int) -> bool:
    """Inclusion proof bound to a POSITION: the path length must match the tree
    depth and each step's side must match the bits of `index`. A leaf therefore
    cannot be claimed at a different index than the one it was committed at."""
    if not isinstance(path, list) or len(path) != expected_depth(n_leaves):
        return False
    h, idx = lh, index
    for step in path:
        if not (isinstance(step, dict) and set(step) == {"hash", "side"} and _is_hex64(step["hash"])):
            return False
        side = "left" if idx % 2 else "right"
        if step["side"] != side:
            return False
        h = _node(step["hash"], h) if side == "left" else _node(h, step["hash"])
        idx //= 2
    return h == root


# ---------------------------------------------------------------------------
# 4. Claims & proof package
# ---------------------------------------------------------------------------

CLAIMS = {
    "rent": "Rental application · liquid assets ≥ k × monthly rent",
    "downpayment": "Down payment · liquid assets ≥ amount",
    "liquid": "Liquid assets · liquid assets ≥ amount",
    "networth": "Net worth · assets − liabilities ≥ amount",
}
LIQUID_CLAIMS = {"rent", "downpayment", "liquid"}
CLAIM_SHORT = {"rent": "Rental application", "downpayment": "Down payment",
               "liquid": "Liquid assets", "networth": "Net worth"}
PROOF_VERSION = "solvo-pop/0.2"
MAX_VALID_DAYS = 365


def validate_claim(claim) -> str | None:
    """Return an error message, or None if the claim is well formed."""
    if not isinstance(claim, dict) or claim.get("type") not in CLAIMS:
        return "unknown claim type"
    if claim["type"] == "rent":
        if set(claim) != {"type", "rent", "multiple"}:
            return "rent claim needs exactly: type, rent, multiple"
        if not (_is_num(claim["rent"]) and 0 < claim["rent"] <= 100_000):
            return "invalid rent"
        if not (_is_int(claim["multiple"]) and 1 <= claim["multiple"] <= 24):
            return "invalid multiple"
    else:
        if set(claim) != {"type", "amount"}:
            return "claim needs exactly: type, amount"
        if not (_is_num(claim["amount"]) and 0 < claim["amount"] <= 1_000_000_000):
            return "invalid amount"
    return None


def claim_threshold(claim: dict) -> float:
    if claim["type"] == "rent":
        return claim["rent"] * claim["multiple"]
    return claim["amount"]


def preview_claim(assets: list[Asset], claim: dict) -> dict:
    """What the holder sees before generating: eligible amount vs required."""
    t = totals(assets)
    eligible = t["net_worth"] if claim["type"] == "networth" else t["liquid"]
    thr = claim_threshold(claim)
    return {"eligible": eligible, "required": thr, "remaining": max(0.0, thr - eligible),
            "surplus": max(0.0, eligible - thr), "ok": eligible >= thr}


def build_snapshot(assets: list[Asset], owner: str) -> dict:
    """Commit to the full portfolio. Layout: [header, assets..., debts...].
    The header (leaf 0) fixes the number of leaves and of debts, so debts cannot
    be silently left out of a net-worth proof and lines cannot change kind.
    Labels/institutions stay behind a salted hash: a revealed leaf shows
    category + value + source, never the bank name or account label."""
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


def select_leaves(snapshot: dict, claim: dict, mode: str) -> list[int] | None:
    """Which leaves to reveal. minimal = largest eligible lines first until the
    threshold is cleared (prototype heuristic). full = everything."""
    leaves = snapshot["leaves"]
    thr = claim_threshold(claim)
    debt_idx = [i for i, l in enumerate(leaves) if l["kind"] == "debt"]
    if mode == "full":
        idx = list(range(len(leaves)))
        # still refuse if the claim is not satisfied
        ev = evaluate_claim(claim, [leaves[i] for i in idx])
        return sorted(idx) if ev["ok"] else None
    if claim["type"] == "networth":
        need = thr * 100 + sum(leaves[i]["value_cents"] for i in debt_idx)
        cands = sorted([i for i, l in enumerate(leaves) if l["kind"] == "asset"],
                       key=lambda i: leaves[i]["value_cents"], reverse=True)
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
    """Pure function: does the revealed data satisfy the claim?"""
    thr = claim_threshold(claim)
    if claim["type"] == "networth":
        assets = sum(l["value_cents"] for l in revealed if l["kind"] == "asset") / 100
        debts = sum(l["value_cents"] for l in revealed if l["kind"] == "debt") / 100
        proven = assets - debts
    else:
        proven = sum(_liquid_leaf_value(l) for l in revealed)
    return {"threshold": thr, "proven": proven, "ok": proven >= thr}


def meta_of(proof: dict) -> dict:
    """Everything the ledger commits to (hashed and anchored)."""
    return {k: proof[k] for k in ("version", "proof_id", "owner", "root", "n_leaves", "claim",
                                  "audience", "created_at", "expires_at", "nonce")}


def make_proof_id(root: str, nonce: str) -> str:
    h = sha256("proof-id|" + root + "|" + nonce)[:20].upper()
    return "SOLVO-" + "-".join(h[i:i + 5] for i in range(0, 20, 5))


# ---------------------------------------------------------------------------
# 5. Strict verification (what the landlord / agency / bank runs)
# ---------------------------------------------------------------------------

_PROOF_KEYS = {"version", "proof_id", "owner", "root", "n_leaves", "claim", "audience", "created_at",
               "expires_at", "nonce", "revealed", "attestation", "anchor_block"}
_ASSET_LEAF_KEYS = {"kind", "category", "value_cents", "source", "label_commit", "salt"}
_HEADER_KEYS = {"kind", "owner", "n_leaves", "n_debts", "currency", "snapshot_at", "salt"}
_MAX_CENTS = 10 ** 13


def _leaf_error(leaf, expected_kind: str) -> str | None:
    if not isinstance(leaf, dict):
        return "leaf is not an object"
    if expected_kind == "header":
        if set(leaf) != _HEADER_KEYS or leaf["kind"] != "header":
            return "bad header"
        if not (_is_int(leaf["n_leaves"]) and _is_int(leaf["n_debts"]) and 0 <= leaf["n_debts"] < leaf["n_leaves"]):
            return "bad header counts"
        return None
    if set(leaf) != _ASSET_LEAF_KEYS:
        return "unexpected leaf fields"
    if leaf["kind"] != expected_kind:
        return f"line sits at a {expected_kind} position but is declared '{leaf['kind']}'"
    if expected_kind == "debt" and leaf["category"] != "Debt":
        return "debt line with a non-debt category"
    if expected_kind == "asset" and leaf["category"] not in ASSET_CATEGORIES:
        return f"category '{leaf['category']}' not allowed for an asset"
    if not (_is_int(leaf["value_cents"]) and 0 <= leaf["value_cents"] <= _MAX_CENTS):
        return "negative or invalid value"
    if leaf["source"] not in SOURCES:
        return "unknown source"
    if not (isinstance(leaf["salt"], str) and isinstance(leaf["label_commit"], str)):
        return "bad commitment fields"
    return None


def package_errors(proof) -> list[str]:
    """Shape check of the whole package. Returns a list of problems."""
    if not isinstance(proof, dict):
        return ["package is not a JSON object"]
    errs = []
    missing = (_PROOF_KEYS - {"attestation", "anchor_block"}) - set(proof)
    extra = set(proof) - _PROOF_KEYS
    if missing:
        errs.append("missing fields: " + ", ".join(sorted(missing)))
    if extra:
        errs.append("unexpected fields: " + ", ".join(sorted(extra)))
    if errs:
        return errs
    if proof["version"] != PROOF_VERSION:
        errs.append("unsupported version")
    if not isinstance(proof["proof_id"], str) or not isinstance(proof["owner"], str):
        errs.append("bad proof_id / owner")
    if not _is_hex64(proof["root"]):
        errs.append("bad Merkle root")
    if not (_is_int(proof["n_leaves"]) and 1 <= proof["n_leaves"] <= 10_000):
        errs.append("bad leaf count")
    c = validate_claim(proof["claim"])
    if c:
        errs.append("claim: " + c)
    if not (isinstance(proof["audience"], str) and 1 <= len(proof["audience"].strip()) <= 120):
        errs.append("bad audience")
    if not (_is_int(proof["created_at"]) and _is_int(proof["expires_at"])
            and proof["created_at"] < proof["expires_at"] <= proof["created_at"] + MAX_VALID_DAYS * 86400):
        errs.append("bad validity window")
    if not (isinstance(proof["nonce"], str) and re.fullmatch(r"[0-9a-f]{16}", proof["nonce"])):
        errs.append("bad nonce")
    if not (isinstance(proof["revealed"], list) and proof["revealed"]):
        errs.append("nothing revealed")
    return errs


def verify_proof(proof: dict, chain: SolvoChain, expected_audience: str | None = None) -> dict:
    """Strict verification. A proof is valid only if EVERY check passes."""
    checks: list[dict] = []

    def add(name, ok, detail="", info=False):
        status = "info" if info else ("pass" if ok else "fail")
        checks.append({"check": name, "status": status, "ok": status != "fail", "detail": detail})

    def finish(ev=None, att_share=0.0, att_amount=0.0):
        valid = all(c["status"] != "fail" for c in checks) and ev is not None
        return {"valid": valid, "checks": checks, "eval": ev,
                "attested_share": att_share, "attested_amount": att_amount}

    # --- a) package format -------------------------------------------------
    try:
        errs = package_errors(proof)
    except Exception as e:  # never crash on hostile input
        errs = [f"unreadable package ({type(e).__name__})"]
    add("Package format", not errs, "well-formed" if not errs else "; ".join(errs))
    if errs:
        return finish()

    try:
        root, n = proof["root"], proof["n_leaves"]
        revealed = proof["revealed"]

        # --- b) revealed entries: shape, unique indices, in range -----------
        shape_ok = all(isinstance(r, dict) and set(r) == {"index", "leaf", "path"} and _is_int(r["index"])
                       for r in revealed)
        add("Revealed entries well-formed", shape_ok)
        if not shape_ok:
            return finish()
        indices = [r["index"] for r in revealed]
        add("Unique indices", len(set(indices)) == len(indices),
            "each line is revealed once" if len(set(indices)) == len(indices) else "an index appears several times")
        add("Indices within tree", all(0 <= i < n for i in indices), f"0 ≤ index < {n}")
        if len(set(indices)) != len(indices) or not all(0 <= i < n for i in indices):
            return finish()

        # --- c) header: leaf 0 fixes the layout -----------------------------
        by_index = {r["index"]: r for r in revealed}
        h_err = "header (leaf 0) not revealed" if 0 not in by_index else _leaf_error(by_index[0]["leaf"], "header")
        if not h_err:
            hd = by_index[0]["leaf"]
            if hd["n_leaves"] != n or hd["owner"] != proof["owner"]:
                h_err = "header disagrees with the package (leaf count / owner)"
        add("Header committed", h_err is None, h_err or "leaf count & owner fixed inside the tree")
        if h_err:
            return finish()
        n_debts = hd["n_debts"]
        n_assets = n - 1 - n_debts

        # --- d) every line: valid type for its position, allowed values -----
        line_errs = []
        for i in sorted(indices):
            if i == 0:
                continue
            kind = "asset" if i <= n_assets else "debt"
            e = _leaf_error(by_index[i]["leaf"], kind)
            if e:
                line_errs.append(f"#{i}: {e}")
        add("Line types & values", not line_errs, "; ".join(line_errs[:3]) or "categories, kinds and amounts are valid")

        # --- e) Merkle inclusion, bound to each index -----------------------
        bad = [r["index"] for r in revealed
               if not verify_path_at(leaf_hash(r["leaf"]), r["path"], root, r["index"], n)]
        add("Merkle inclusion", not bad,
            f"{len(revealed)} line(s) match root {root[:12]}…" if not bad else f"line(s) {bad} do not belong at that position")

        # --- f) no repeated line (same content revealed twice) --------------
        hashes = [leaf_hash(r["leaf"]) for r in revealed]
        salts = [r["leaf"]["salt"] for r in revealed]
        uniq = len(set(hashes)) == len(hashes) and len(set(salts)) == len(salts)
        add("No duplicated line", uniq, "every revealed line is distinct" if uniq else "a line is repeated")

        # --- g) net worth: all debts must be disclosed ----------------------
        if proof["claim"]["type"] == "networth":
            n_rev = sum(1 for r in revealed if r["leaf"]["kind"] == "debt")
            add("All debts disclosed", n_rev == n_debts, f"{n_rev}/{n_debts} debt lines revealed")

        structural_ok = all(c["status"] != "fail" for c in checks)

        # --- h) ledger: anchor, integrity, revocation, expiry, audience -----
        meta_hash = sha256(canonical(meta_of(proof)))
        pid = proof["proof_id"]
        block, tx = chain.find_tx(lambda t: t["type"] == "ANCHOR_PROOF" and t.get("proof_id") == pid
                                  and t.get("meta_hash") == meta_hash and t.get("root") == root)
        add("Anchored on SolvoChain", block is not None,
            f"block #{block.index}, tx {tx['txid']}" if block else "no matching anchor: data was altered or never issued")
        ok_chain, bad_blk = chain.verify_integrity()
        add("Ledger integrity", ok_chain, "all block hashes valid" if ok_chain else f"block #{bad_blk} tampered")
        rb, _ = chain.find_tx(lambda t: t["type"] == "REVOKE" and t.get("proof_id") == pid)
        add("Not revoked", rb is None, "no revocation on the ledger" if rb is None else f"revoked in block #{rb.index}")
        add("Not expired", now_ts() < proof["expires_at"], "valid until " + fmt_ts(proof["expires_at"]))
        if expected_audience and expected_audience.strip():
            same = proof["audience"].strip().lower() == expected_audience.strip().lower()
            add("Issued for you", same, f"issued to '{proof['audience']}'")
        else:
            add("Issued for you", True, f"issued to '{proof['audience']}' (enter your organisation to check)", info=True)

        # --- i) issuer attestation (Ed25519) --------------------------------
        att = proof.get("attestation")
        attested_hashes: set[str] = set()
        if att is None:
            add("Issuer signature", True, "no attestation: all disclosed data is self-declared", info=True)
        else:
            ok, detail = _check_attestation(att, proof, chain)
            add("Issuer signature", ok, detail)
            if ok:
                attested_hashes = set(att["payload"]["attested_leaf_hashes"])
                att_hash = sha256(canonical(att))
                ab, _ = chain.find_tx(lambda t: t["type"] == "ISSUER_ATTEST" and t.get("proof_id") == pid
                                      and t.get("attestation_hash") == att_hash)
                add("Attestation recorded on ledger", ab is not None,
                    f"block #{ab.index}" if ab else "attestation not found on the ledger")

        # --- j) the claim itself --------------------------------------------
        if not structural_ok:
            add("Claim satisfied", False, "not evaluated: the proof is structurally invalid")
            return finish()
        leaves = [r["leaf"] for r in revealed]
        ev = evaluate_claim(proof["claim"], leaves)
        add("Claim satisfied", ev["ok"], f"proven €{ev['proven']:,.0f} vs required €{ev['threshold']:,.0f}")

        asset_rev = [l for l in leaves if l["kind"] == "asset"]
        tot = sum(l["value_cents"] for l in asset_rev) or 1
        att_leaves = [l for l in asset_rev if leaf_hash(l) in attested_hashes]
        att_share = sum(l["value_cents"] for l in att_leaves) / tot
        att_amount = sum(_liquid_leaf_value(l) for l in att_leaves)
        return finish(ev, att_share, att_amount)
    except Exception as e:  # any surprise = reject
        add("Verification error", False, f"{type(e).__name__}: proof rejected")
        return finish()


def _check_attestation(att, proof: dict, chain: SolvoChain) -> tuple[bool, str]:
    try:
        if not (isinstance(att, dict) and set(att) == {"payload", "signature"}):
            return False, "malformed attestation"
        p = att["payload"]
        need = {"version", "proof_id", "root", "issuer_id", "issued_at", "valid_until", "source",
                "status", "simulated", "attested_leaf_hashes"}
        if not (isinstance(p, dict) and set(p) == need and isinstance(p["attested_leaf_hashes"], list)):
            return False, "malformed attestation payload"
        issuer = chain.trusted_issuer(p["issuer_id"])
        if issuer is None:
            return False, "issuer not registered on the ledger"
        if not verify_signature(issuer["public_key"], p, att["signature"]):
            return False, "signature does not match the issuer's public key"
        if p["proof_id"] != proof["proof_id"] or p["root"] != proof["root"]:
            return False, "attestation is bound to another proof / Merkle root"
        if p["status"] != "valid":
            return False, f"attestation status: {p['status']}"
        if now_ts() > p["valid_until"]:
            return False, "attestation expired"
        tag = " (simulated issuer: not a real third-party certification)" if p["simulated"] else ""
        return True, f"valid Ed25519 signature by {p['issuer_id']}{tag}"
    except Exception:
        return False, "attestation could not be checked"


# ---------------------------------------------------------------------------
# 6. Application state
# ---------------------------------------------------------------------------


@dataclass
class ProofRecord:
    code: str
    proof: dict
    created_at: float
    owner_name: str = ""
    revoked: bool = False


class Solvo:
    """Application state: portfolio + shared ledger + issued proofs."""

    def __init__(self, user: str = "Camille", chain: SolvoChain | None = None, proofs: dict | None = None,
                 lock=None, issuer: Issuer | None = None):
        self.user = user
        self.owner = "0x" + sha256("solvo-user|" + user + "|" + secrets.token_hex(8))[:40]
        self.assets: list[Asset] = []
        self.issuer = issuer if issuer is not None else Issuer()
        self.chain = chain if chain is not None else SolvoChain([self.issuer.info()])
        self.proofs: dict[str, ProofRecord] = proofs if proofs is not None else {}
        self.lock = lock if lock is not None else threading.RLock()
        self.snapshots: list[dict] = []   # real snapshots, one per change

    # ---- portfolio --------------------------------------------------------
    def _snap(self):
        t = totals(self.assets)
        self.snapshots.append({"ts": now_ts(), "net_worth": t["net_worth"], "gross": t["gross"], "debts": t["debts"]})

    def load_demo(self):
        self.assets = demo_portfolio(self.user)
        self.snapshots = []
        self._snap()

    def add_asset(self, label, institution, category, value, source="declared"):
        if category not in CATEGORIES or float(value) < 0:
            raise ValueError("invalid asset")
        self.assets.append(Asset(new_asset_id(), label, institution, category, float(value), source))
        self._snap()

    def update_asset(self, asset_id: str, label: str, institution: str, category: str, value: float):
        """Editing a connected line turns it into a self-declared one: the value
        is no longer what the source returned, so it must not stay attestable."""
        if category not in CATEGORIES or float(value) < 0:
            raise ValueError("invalid asset")
        for a in self.assets:
            if a.id == asset_id:
                if a.source != "declared" and (float(value) != a.value or category != a.category):
                    a.source = "declared"
                a.label, a.institution, a.category, a.value = label, institution, category, float(value)
                a.updated_at = now_ts()
        self._snap()

    def delete_asset(self, asset_id: str):
        self.assets = [a for a in self.assets if a.id != asset_id]
        self._snap()

    def connect_bank_demo(self, bank: str) -> list[Asset]:
        """SIMULATED open-banking read: fixed demo accounts."""
        new = [Asset(new_asset_id(), "Compte courant", bank, "Cash & savings", 2_480.0, "bank_sim"),
               Asset(new_asset_id(), "Livret Jeune", bank, "Cash & savings", 1_600.0, "bank_sim")]
        self.assets += new
        self._snap()
        return new

    def connect_wallet_demo(self, address: str) -> Asset:
        """SIMULATED wallet read (value derived from the address). Reading a public
        address does NOT prove ownership: a real version needs a signed message."""
        h = int(sha256(address.lower()), 16)
        a = Asset(new_asset_id(), "Wallet " + address[:6] + "…" + address[-4:], address, "Crypto",
                  float(500 + (h % 9500)), "wallet_sim")
        self.assets.append(a)
        self._snap()
        return a

    def my_proofs(self) -> dict[str, ProofRecord]:
        return {c: r for c, r in self.proofs.items() if r.proof["owner"] == self.owner}

    # ---- proofs -----------------------------------------------------------
    def create_proof(self, claim: dict, audience: str, valid_days: int, mode: str = "minimal"):
        err = validate_claim(claim)
        if err:
            return None, "Invalid claim: " + err
        if not audience.strip():
            return None, "Please name the recipient of the proof."
        if not 1 <= valid_days <= MAX_VALID_DAYS:
            return None, f"Validity must be between 1 and {MAX_VALID_DAYS} days."
        snap = build_snapshot(self.assets, self.owner)
        idx = select_leaves(snap, claim, mode)
        if idx is None:
            return None, "Your eligible resources do not satisfy this claim: no proof issued."

        created = round(now_ts())
        nonce = secrets.token_hex(8)
        proof = {
            "version": PROOF_VERSION, "proof_id": make_proof_id(snap["root"], nonce),
            "owner": self.owner, "root": snap["root"], "n_leaves": len(snap["leaves"]),
            "claim": claim, "audience": audience.strip(),
            "created_at": created, "expires_at": created + valid_days * 86400, "nonce": nonce,
            "revealed": [{"index": i, "leaf": snap["leaves"][i], "path": merkle_path(snap["hashes"], i)} for i in idx],
            "attestation": None,
        }
        pid = proof["proof_id"]
        txs = []

        # issuer attestation: signed, and bound to THIS proof id + Merkle root
        att_hashes = [snap["hashes"][i] for i in idx
                      if snap["leaves"][i].get("source") in ATTESTABLE_SOURCES and snap["leaves"][i]["kind"] == "asset"]
        if att_hashes:
            payload = {"version": "solvo-att/0.1", "proof_id": pid, "root": proof["root"],
                       "issuer_id": self.issuer.issuer_id, "issued_at": created,
                       "valid_until": proof["expires_at"], "source": "simulated bank / wallet read",
                       "status": "valid", "simulated": True, "attested_leaf_hashes": sorted(att_hashes)}
            att = {"payload": payload, "signature": self.issuer.sign(payload)}
            proof["attestation"] = att
            txs.append({"type": "ISSUER_ATTEST", "proof_id": pid, "root": proof["root"],
                        "issuer_id": self.issuer.issuer_id, "attestation_hash": sha256(canonical(att))})
        meta_hash = sha256(canonical(meta_of(proof)))
        txs.append({"type": "ANCHOR_PROOF", "proof_id": pid, "owner": self.owner,
                    "root": proof["root"], "meta_hash": meta_hash})
        with self.lock:
            block = self.chain.submit(txs)
            proof["anchor_block"] = block.index
            self.proofs[pid] = ProofRecord(pid, proof, now_ts(), self.user)
        return pid, f"Proof anchored in block #{block.index}."

    def revoke(self, code: str) -> None:
        rec = self.proofs[code]
        if rec.proof["owner"] != self.owner:
            raise PermissionError("Only the holder can revoke a proof.")
        with self.lock:
            self.chain.submit([{"type": "REVOKE", "proof_id": code, "owner": self.owner,
                                "meta_hash": sha256(canonical(meta_of(rec.proof)))}])
        rec.revoked = True

    def lifecycle(self, code: str) -> list[dict]:
        """Created -> Anchored -> Shared? -> Verified -> Expired/Revoked, from the ledger."""
        rec = self.proofs[code]
        verifs = [t for _, t in self.chain.all_txs() if t["type"] == "PROOF_VERIFIED" and t.get("proof_id") == code]
        revoked = self.chain.find_tx(lambda t: t["type"] == "REVOKE" and t.get("proof_id") == code)[0] is not None
        expired = now_ts() >= rec.proof["expires_at"]
        return [
            {"step": "Created", "done": True},
            {"step": "Anchored", "done": True},
            {"step": "Verified", "done": bool(verifs), "detail": f"{len(verifs)} verification(s)" if verifs else ""},
            {"step": "Revoked" if revoked else "Expired" if expired else "Active", "done": revoked or expired,
             "active": not (revoked or expired)},
        ]

    # ---- verification -----------------------------------------------------
    def verify(self, proof: dict, expected_audience: str | None = None) -> dict:
        return verify_proof(proof, self.chain, expected_audience)

    def log_verification(self, proof_id: str, valid: bool, verifier: str = "") -> None:
        if proof_id in self.proofs:
            with self.lock:
                self.chain.submit([{"type": "PROOF_VERIFIED", "proof_id": proof_id, "result": "valid" if valid else "invalid",
                                    "verifier": (verifier or "anonymous")[:60]}])
