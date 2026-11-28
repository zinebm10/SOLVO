"""Run: python test_core.py   (or pytest). Each test is an attack the verifier must reject."""
import copy
from solvo_core import *


def fresh(user="Camille"):
    s = Solvo(user)
    s.load_demo()
    return s


def issue(s, claim=None, mode="minimal", aud="Agence Lille Centre"):
    claim = claim or {"type": "rent", "rent": 850.0, "multiple": 3}
    pid, msg = s.create_proof(claim, aud, 30, mode)
    assert pid, msg
    return pid, copy.deepcopy(s.proofs[pid].proof)


def failed(res):
    return [c["check"] for c in res["checks"] if c["status"] == "fail"]


def test_honest_proofs_valid():
    s = fresh()
    for claim in [{"type": "rent", "rent": 850.0, "multiple": 3}, {"type": "downpayment", "amount": 40000.0},
                  {"type": "liquid", "amount": 20000.0}, {"type": "networth", "amount": 80000.0}]:
        for mode in ("minimal", "full"):
            _, p = issue(s, claim, mode)
            r = s.verify(p, "Agence Lille Centre")
            assert r["valid"], (claim, mode, failed(r))


def test_duplicate_line_rejected():
    s = fresh()
    _, p = issue(s, {"type": "downpayment", "amount": 20000.0})
    big = max((r for r in p["revealed"] if r["leaf"]["kind"] == "asset"), key=lambda r: r["leaf"]["value_cents"])
    p["revealed"] += [copy.deepcopy(big), copy.deepcopy(big)]          # x3 the value
    r = s.verify(p)
    assert not r["valid"] and "Unique indices" in failed(r)


def test_duplicate_with_new_index_rejected():
    s = fresh()
    _, p = issue(s, {"type": "downpayment", "amount": 20000.0})
    big = max((r for r in p["revealed"] if r["leaf"]["kind"] == "asset"), key=lambda r: r["leaf"]["value_cents"])
    dup = copy.deepcopy(big)
    dup["index"] = 99                                                    # out of range
    p["revealed"].append(dup)
    assert not s.verify(p)["valid"]
    dup["index"] = next(i for i in range(1, p["n_leaves"]) if i not in [r["index"] for r in p["revealed"]])
    assert not s.verify(p)["valid"]                                      # path no longer fits the index


def test_changed_value_rejected():
    s = fresh()
    _, p = issue(s)
    r0 = next(r for r in p["revealed"] if r["leaf"]["kind"] == "asset")
    r0["leaf"]["value_cents"] *= 10
    assert not s.verify(p)["valid"]


def test_changed_claim_audience_expiry_rejected():
    s = fresh()
    _, p = issue(s)
    for mut in (lambda q: q["claim"].update(multiple=1), lambda q: q.update(audience="Other"),
                lambda q: q.update(expires_at=q["expires_at"] + 86400)):
        q = copy.deepcopy(p)
        mut(q)
        assert "Anchored on SolvoChain" in failed(s.verify(q))


def test_wrong_audience():
    s = fresh()
    _, p = issue(s)
    assert "Issued for you" in failed(s.verify(p, "Another agency"))


def test_revoked_and_expired():
    s = fresh()
    pid, p = issue(s)
    s.revoke(pid)
    assert "Not revoked" in failed(s.verify(p))
    pid2, p2 = issue(s)
    p2["expires_at"] = 1  # also breaks anchor; check expiry independently below
    s.proofs[pid2].proof["expires_at"] = int(now_ts()) - 5
    assert not s.verify(copy.deepcopy(s.proofs[pid2].proof))["valid"]


def test_kind_swap_debt_to_asset_rejected():
    s = fresh()
    _, p = issue(s, {"type": "networth", "amount": 80000.0})
    d = next(r for r in p["revealed"] if r["leaf"]["kind"] == "debt")
    d["leaf"]["kind"] = "asset"
    assert not s.verify(p)["valid"]


def test_hidden_debt_rejected():
    s = fresh()
    _, p = issue(s, {"type": "networth", "amount": 80000.0})
    p["revealed"] = [r for r in p["revealed"] if r["leaf"]["kind"] != "debt"]
    assert not s.verify(p)["valid"]


def test_index_swap_rejected():
    s = fresh()
    _, p = issue(s, {"type": "downpayment", "amount": 40000.0}, "full")
    a, b = p["revealed"][1], p["revealed"][2]
    a["index"], b["index"] = b["index"], a["index"]
    assert "Merkle inclusion" in failed(s.verify(p))


def test_negative_and_bad_category():
    s = fresh()
    _, p = issue(s)
    r0 = next(r for r in p["revealed"] if r["leaf"]["kind"] == "asset")
    r0["leaf"]["value_cents"] = -5
    assert not s.verify(p)["valid"]
    _, p = issue(s)
    r0 = next(r for r in p["revealed"] if r["leaf"]["kind"] == "asset")
    r0["leaf"]["category"] = "Gold"
    assert not s.verify(p)["valid"]


def test_malformed_never_crashes():
    s = fresh()
    for junk in [None, [], {}, {"revealed": 3}, "x", {"version": "solvo-pop/0.2"}]:
        assert not s.verify(junk)["valid"]
    _, p = issue(s)
    p["revealed"][0]["path"] = "nope"
    assert not s.verify(p)["valid"]


def test_attestation_bound_to_proof():
    s = fresh()
    _, p1 = issue(s)
    _, p2 = issue(s)
    p2["attestation"] = p1["attestation"]            # reuse another proof's attestation
    assert "Issuer signature" in failed(s.verify(p2))
    _, p3 = issue(s)
    p3["attestation"]["payload"]["attested_leaf_hashes"].append("0" * 64)  # edit signed payload
    assert "Issuer signature" in failed(s.verify(p3))


def test_forged_issuer_key_rejected():
    s = fresh()
    _, p = issue(s)
    evil = Issuer("solvo-demo-issuer")               # same name, different key
    p["attestation"]["signature"] = evil.sign(p["attestation"]["payload"])
    assert "Issuer signature" in failed(s.verify(p))


def test_declared_data_not_attested():
    s = Solvo("Bob")
    s.assets = []
    s.add_asset("Cash at home", "-", "Cash & savings", 5000, "declared")
    pid, p = issue(s, {"type": "liquid", "amount": 1000.0})
    assert p["attestation"] is None
    r = s.verify(p)
    assert r["valid"] and r["attested_share"] == 0


def test_edit_connected_asset_downgrades_trust():
    s = fresh()
    a = next(x for x in s.assets if x.source == "bank_sim")
    s.update_asset(a.id, a.label, a.institution, a.category, a.value + 1000)
    assert next(x for x in s.assets if x.id == a.id).source == "declared"


def test_ledger_tamper_detected():
    s = fresh()
    _, p = issue(s)
    blk = next(b for b in s.chain.blocks if any(t["type"] == "ANCHOR_PROOF" for t in b.txs))
    tx = next(t for t in blk.txs if t["type"] == "ANCHOR_PROOF")
    tx["root"] = "f" + tx["root"][1:]
    r = s.verify(p)
    assert not r["valid"] and "Ledger integrity" in failed(r)


def test_shortfall_refused():
    s = fresh()
    pid, msg = s.create_proof({"type": "downpayment", "amount": 900000.0}, "X", 30)
    assert pid is None
    assert s.create_proof({"type": "rent", "rent": -5, "multiple": 3}, "X", 30)[0] is None


def test_minimal_disclosure_reveals_least_value():
    """Data minimisation: the demo rent proof (850 x 3 = 2,550) must reveal ONE line
    of 3,850 (current account), not the 22,950 savings account."""
    s = fresh()
    _, p = issue(s, {"type": "rent", "rent": 850.0, "multiple": 3})
    rev = [r["leaf"]["value_cents"] for r in p["revealed"] if r["leaf"]["kind"] != "header"]
    assert rev == [385000], rev
    _, p = issue(s, {"type": "downpayment", "amount": 40000.0}, aud="Atlas Courtage")
    rev = sorted(r["leaf"]["value_cents"] / 100 for r in p["revealed"] if r["leaf"]["kind"] != "header")
    assert rev == [6400.0, 14300.0, 22950.0], rev          # 43,650 disclosed, 42,220 counted
    assert s.verify(p, "Atlas Courtage")["valid"]


def test_networth_minimal_still_shows_all_debts():
    s = fresh()
    _, p = issue(s, {"type": "networth", "amount": 80000.0})
    kinds = [r["leaf"]["kind"] for r in p["revealed"]]
    assert kinds.count("debt") == 2
    assert s.verify(p, "Agence Lille Centre")["valid"]


if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); n += 1; print("ok ", k)
    print(n, "tests passed")
