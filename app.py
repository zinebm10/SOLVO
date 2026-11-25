"""
Solvo - the wealth tracker that proves your solvency without exposing your wealth.
Run with:  streamlit run app.py

NEOMA MSc Fintech & DeFi - Final group project (demo app).
All bank / wallet connections are SIMULATED for the demo; the cryptography
(salted Merkle commitments, hash-chained ledger, proof verification) is real.
"""

import copy
import io
import json
import threading
from urllib.parse import urlsplit

import qrcode

import altair as alt
import pandas as pd
import streamlit as st

from solvo_core import (
    ASSET_CATEGORIES, CATEGORIES, CLAIMS, SOURCES, VERIFIED_SOURCES,
    Solvo, SolvoChain, allocation, fire_projection, fmt_ts, history, portfolio_mu_sigma,
    runway_months, totals,
)

st.set_page_config(page_title="Solvo - Proof-of-Patrimoine", page_icon="🛡️", layout="wide")

# Fixed categorical palette (validated colour-blind-safe order), one colour per category.
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
CAT_SCALE = alt.Scale(domain=ASSET_CATEGORIES, range=PALETTE)

st.markdown(
    """
    <style>
      .big-number {font-size: 2.8rem; font-weight: 700; line-height: 1.15; margin: 0 0 4px 0;}
      .muted {color: #6b6b6b; font-size: 0.9rem;}
      .pill {display:inline-block; padding:2px 10px; border-radius:999px; font-size:0.8rem;
             border:1px solid #d0d0d0; margin-right:6px;}
      .ok-banner {background:#e8f6ee; border:1px solid #1baf7a; border-radius:10px; padding:14px 18px;}
      .ko-banner {background:#fdecec; border:1px solid #e34948; border-radius:10px; padding:14px 18px;}
      .code {font-family: monospace; font-size: 1.6rem; font-weight: 700; letter-spacing: 2px;}
    </style>
    """,
    unsafe_allow_html=True,
)


def eur(x: float) -> str:
    return f"€{x:,.0f}"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
@st.cache_resource
def shared_ledger() -> dict:
    """One ledger + proof registry shared by every visitor of the deployed app.
    Each visitor keeps a private portfolio in their own session."""
    return {"chain": SolvoChain(), "proofs": {}, "lock": threading.RLock(),
            "participants": set(), "tamper_backup": None}


SHARED = shared_ledger()
IS_ADMIN = st.query_params.get("admin") == "1"   # presenter link: ...?admin=1


def start_session(name: str) -> None:
    app = Solvo(user=name, chain=SHARED["chain"], proofs=SHARED["proofs"], lock=SHARED["lock"])
    app.load_demo()
    st.session_state.app = app
    SHARED["participants"].add(app.owner)


if "app" not in st.session_state:
    st.markdown("## 🛡️ Welcome to Solvo")
    st.write("Track your wealth. Prove your solvency. Reveal nothing else.")
    with st.form("join"):
        name = st.text_input("Your first name", "Camille" if IS_ADMIN else "",
                             placeholder="e.g. Lucas")
        go = st.form_submit_button("Start the demo", type="primary")
    st.caption("You get your own demo portfolio (simulated data). Everyone shares the same SolvoChain, "
               "so you can verify a classmate's proof with their code.")
    if go and name.strip():
        start_session(name.strip())
        st.rerun()
    st.stop()

app: Solvo = st.session_state.app

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("## 🛡️ Solvo")
    st.caption("Track your wealth. Prove your solvency. Reveal nothing else.")
    page = st.radio(
        "Navigate",
        ["📊 My wealth", "🏦 Accounts", "⏳ Freedom Clock",
         "🛡️ Proof-of-Patrimoine", "✅ Verify a proof", "⛓️ SolvoChain explorer", "📱 Join the demo"],
        label_visibility="collapsed",
    )
    t = totals(app.assets)
    st.divider()
    st.metric("Net worth", eur(t["net_worth"]))
    st.caption(f"Signed in as **{app.user}** · on-chain id `{app.owner[:10]}…`")
    if st.button("↺ Reset my demo data", width="stretch"):
        start_session(app.user)
        st.session_state.pop("last_code", None)
        st.rerun()
    st.caption("Demo build - bank & wallet connections are simulated.")


# ---------------------------------------------------------------------------
# Page 1 - Wealth dashboard (Finary-style)
# ---------------------------------------------------------------------------
def page_wealth():
    st.title("My wealth")
    t = totals(app.assets)
    hist = pd.DataFrame(history(app.assets))
    nw = hist.groupby("month", as_index=False)["value"].sum().rename(columns={"value": "net_worth"})
    delta = nw["net_worth"].iloc[-1] - nw["net_worth"].iloc[0]

    c1, c2 = st.columns([1.3, 2])
    with c1:
        st.markdown('<p class="muted">Net worth</p>', unsafe_allow_html=True)
        st.markdown(f'<div class="big-number">{eur(t["net_worth"])}</div>', unsafe_allow_html=True)
        st.markdown(f'<p class="muted">{"▲" if delta >= 0 else "▼"} {eur(abs(delta))} over 12 months</p>',
                    unsafe_allow_html=True)
    with c2:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Gross assets", eur(t["gross"]))
        m2.metric("Debts", eur(t["debts"]))
        m3.metric("Liquid (after haircuts)", eur(t["liquid"]),
                  help="Value that would count in a solvency proof: cash 100%, life insurance 90%, "
                       "stocks 75%, crypto 50%, real estate 0%.")
        m4.metric("Source-verified", f"{t['verified_share']:.0%}",
                  help="Share of gross assets read directly from a bank API or a public blockchain.")

    st.divider()
    left, right = st.columns([1, 1.4])
    with left:
        st.subheader("Allocation")
        alloc = allocation(app.assets)
        df = pd.DataFrame({"category": list(alloc), "value": list(alloc.values())})
        df["share"] = df["value"] / df["value"].sum()
        donut = (
            alt.Chart(df)
            .mark_arc(innerRadius=75, outerRadius=130, stroke="#ffffff", strokeWidth=2)
            .encode(
                theta=alt.Theta("value:Q", stack=True),
                color=alt.Color("category:N", scale=CAT_SCALE, legend=alt.Legend(title=None, orient="right", labelFontSize=13, symbolSize=120)),
                tooltip=[alt.Tooltip("category:N", title="Category"),
                         alt.Tooltip("value:Q", title="Value (€)", format=",.0f"),
                         alt.Tooltip("share:Q", title="Share", format=".1%")],
            )
            .properties(height=330)
        )
        st.altair_chart(donut, width="stretch")
    with right:
        st.subheader("Net worth - last 12 months")
        line = (
            alt.Chart(nw)
            .mark_line(color="#2a78d6", strokeWidth=2.5, point=alt.OverlayMarkDef(color="#2a78d6", size=40))
            .encode(
                x=alt.X("month:O", title=None, axis=alt.Axis(labelAngle=0, labelOverlap=True)),
                y=alt.Y("net_worth:Q", title="€", scale=alt.Scale(zero=False), axis=alt.Axis(format="~s", grid=True)),
                tooltip=[alt.Tooltip("month:O", title="Month"), alt.Tooltip("net_worth:Q", title="Net worth (€)", format=",.0f")],
            )
            .properties(height=330)
        )
        st.altair_chart(line, width="stretch")
        st.caption("Demo history back-filled from today's balances. In production, one snapshot is stored per sync.")

    st.subheader("Holdings")
    rows = [{
        "Account": a.label, "Institution": a.institution, "Category": a.category,
        "Value (€)": -a.value if a.is_debt else a.value,
        "Counts in proofs (€)": a.liquid_value,
        "Source": ("✅ " if a.source in VERIFIED_SOURCES else "✍️ ") + SOURCES[a.source],
    } for a in app.assets]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                 column_config={"Value (€)": st.column_config.NumberColumn(format="euro"),
                                "Counts in proofs (€)": st.column_config.NumberColumn(format="euro")})


# ---------------------------------------------------------------------------
# Page 2 - Accounts (aggregation)
# ---------------------------------------------------------------------------
def page_accounts():
    st.title("Accounts & sources")
    st.write("Solvo aggregates everything you own. **Verified** sources are read by Solvo itself "
             "(bank API under PSD2, or a public blockchain address) - only those can be *attested* in a proof.")

    tab1, tab2, tab3, tab4 = st.tabs(["🏦 Connect a bank", "🪙 Add a crypto wallet", "✍️ Add manually", "📄 Import CSV"])
    with tab1:
        bank = st.selectbox("Bank", ["Société Générale", "Revolut", "Fortuneo", "Crédit Mutuel"])
        if st.button("Connect (simulated PSD2 consent)", type="primary"):
            app.add_asset("Compte courant", bank, "Cash & savings", 2_480, "open_banking")
            app.add_asset("Livret Jeune", bank, "Cash & savings", 1_600, "open_banking")
            st.success(f"{bank}: 2 accounts synchronised (read-only access, 180-day consent).")
    with tab2:
        addr = st.text_input("Public address (EVM)", "0x4b20993bc481177ec7e8f571cecae8a9e22c02db")
        if st.button("Read balance"):
            a = app.connect_wallet(addr)
            st.success(f"Wallet added: {eur(a.value)} - read from the public ledger, no private key needed.")
    with tab3:
        with st.form("manual"):
            c1, c2 = st.columns(2)
            label = c1.text_input("Name", "Cash at home")
            inst = c2.text_input("Institution / note", "-")
            cat = c1.selectbox("Category", list(CATEGORIES))
            val = c2.number_input("Value (€)", min_value=0.0, value=1_000.0, step=100.0)
            if st.form_submit_button("Add"):
                app.add_asset(label, inst, cat, val, "declared")
                st.success("Added as self-declared.")
    with tab4:
        st.caption("Columns: label, institution, category, value")
        up = st.file_uploader("CSV file", type="csv")
        if up is not None and st.button("Import"):
            df = pd.read_csv(up)
            n = 0
            for _, r in df.iterrows():
                if r["category"] in CATEGORIES:
                    app.add_asset(str(r["label"]), str(r["institution"]), r["category"], float(r["value"]), "declared")
                    n += 1
            st.success(f"{n} line(s) imported as self-declared.")

    st.divider()
    st.subheader("Your accounts")
    for a in list(app.assets):
        c1, c2, c3, c4, c5 = st.columns([3, 2, 2, 2, 1])
        c1.write(f"**{a.label}**  \n<span class='muted'>{a.institution}</span>", unsafe_allow_html=True)
        c2.write(a.category)
        c3.write(("−" if a.is_debt else "") + eur(a.value))
        c4.write(("✅ " if a.source in VERIFIED_SOURCES else "✍️ ") + SOURCES[a.source])
        if c5.button("🗑️", key="del_" + a.id, help="Remove"):
            app.assets = [x for x in app.assets if x.id != a.id]
            st.rerun()


# ---------------------------------------------------------------------------
# Page 3 - Freedom Clock
# ---------------------------------------------------------------------------
def page_freedom():
    st.title("Freedom Clock")
    st.write("How long could you live without income - and when does your money work for you?")
    c1, c2, c3 = st.columns(3)
    exp = c1.number_input("Monthly expenses (€)", 300, 20_000, 1_800, 50)
    save = c2.number_input("Monthly savings (€)", 0, 20_000, 900, 50)
    swr = c3.slider("Safe withdrawal rate", 0.025, 0.05, 0.04, 0.005, format="%.3f")

    t = totals(app.assets)
    months = runway_months(app.assets, exp)
    mu, sigma = portfolio_mu_sigma(app.assets)
    proj = fire_projection(t["liquid_face"], save, exp * 12, mu, sigma, swr=swr)

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Runway", f"{months:.0f} months", help="Liquid assets after haircuts ÷ monthly expenses")
    k2.metric("Independence target", eur(proj["target"]), help="Annual expenses ÷ withdrawal rate")
    k3.metric("Median time to target", f"{proj['median_years']} years" if proj["median_years"] <= 40 else "40+ years")
    k4.metric("Chance within 40 years", f"{proj['p_reach']:.0%}")

    df = pd.DataFrame(proj["bands"])
    base = alt.Chart(df).encode(x=alt.X("year:Q", title="Years from now"))
    band = base.mark_area(opacity=0.25, color="#2a78d6").encode(
        y=alt.Y("p10:Q", title="Investable wealth (€)", axis=alt.Axis(format="~s")), y2="p90:Q",
        tooltip=[alt.Tooltip("year:Q"), alt.Tooltip("p10:Q", format=",.0f", title="Pessimistic (P10)"),
                 alt.Tooltip("p50:Q", format=",.0f", title="Median"), alt.Tooltip("p90:Q", format=",.0f", title="Optimistic (P90)")])
    med = base.mark_line(color="#2a78d6", strokeWidth=2).encode(y="p50:Q")
    rule = alt.Chart(pd.DataFrame({"y": [proj["target"]]})).mark_rule(color="#e34948", strokeDash=[6, 4], strokeWidth=2).encode(y="y:Q")
    label = alt.Chart(pd.DataFrame({"y": [proj["target"]], "x": [1], "t": ["Independence target"]})).mark_text(
        align="left", dy=-8, color="#52514e").encode(x="x:Q", y="y:Q", text="t:N")
    st.altair_chart((band + med + rule + label).properties(height=380), width="stretch")
    st.caption(f"Monte Carlo, 400 paths. Portfolio assumption from your allocation: expected return {mu:.1%}, "
               f"volatility {sigma:.1%}. Shaded area = 10th-90th percentile. Not investment advice.")


# ---------------------------------------------------------------------------
# Page 4 - Proof-of-Patrimoine (prover side)
# ---------------------------------------------------------------------------
def page_proof():
    st.title("Proof-of-Patrimoine")
    st.write("Generate a certificate that a landlord or a bank can verify **without seeing your full portfolio**. "
             "Your holdings are committed in a salted Merkle tree; only the root is anchored on-chain.")

    claim_type = st.selectbox("What do you want to prove?", list(CLAIMS), format_func=lambda k: CLAIMS[k])
    with st.form("proof"):
        c1, c2, c3 = st.columns(3)
        if claim_type == "rent":
            rent = c1.number_input("Monthly rent (€)", 200, 10_000, 850, 10)
            mult = c2.number_input("Multiple k", 1, 24, 3)
            claim = {"type": "rent", "rent": float(rent), "multiple": int(mult)}
        else:
            amt = c1.number_input("Amount (€)", 1_000, 5_000_000, 40_000 if claim_type == "downpayment" else 80_000, 1_000)
            c2.empty()
            claim = {"type": claim_type, "amount": float(amt)}
        audience = st.text_input("Issued to (verifier)", "Agence Lille Centre" if claim_type == "rent" else "Crédit Agricole")
        days = c3.number_input("Valid for (days)", 1, 365, 30)
        mode = st.radio("Disclosure", ["minimal", "full"], horizontal=True,
                        format_func=lambda m: "Minimal - reveal the fewest accounts needed" if m == "minimal" else "Full - reveal every line")
        submitted = st.form_submit_button("Generate & anchor proof", type="primary")

    if submitted:
        code, msg = app.create_proof(claim, audience, int(days), mode)
        if code is None:
            st.error(msg)
        else:
            st.session_state.last_code = code
            st.success(msg)

    code = st.session_state.get("last_code")
    if code and code in app.proofs:
        rec = app.proofs[code]
        p = rec.proof
        st.divider()
        a, b = st.columns([1, 1.6])
        with a:
            st.markdown("**Share this code with the verifier**")
            st.markdown(f'<p class="code">{code}</p>', unsafe_allow_html=True)
            st.caption(f"Valid until {fmt_ts(p['expires_at'])} · for “{p['audience']}”")
            st.download_button("⬇ Download proof (.json)", json.dumps(p, indent=2, ensure_ascii=False),
                               file_name=f"{code}.json", mime="application/json")
        with b:
            revealed = [r["leaf"] for r in p["revealed"] if r["leaf"]["kind"] != "header"]
            hidden = p["n_leaves"] - 1 - len(revealed)
            st.markdown(f"**What the verifier sees** - {len(revealed)} line(s) revealed, **{hidden} kept private**")
            st.dataframe(pd.DataFrame([{
                "Type": l["kind"], "Category": l["category"], "Value (€)": l["value_cents"] / 100,
                "Source": SOURCES[l["source"]], "Bank / label": "🔒 hidden (salted hash)",
            } for l in revealed]), hide_index=True, width="stretch",
                column_config={"Value (€)": st.column_config.NumberColumn(format="euro")})
            st.caption(f"Merkle root anchored on-chain: `{p['root'][:32]}…` (block #{p['anchor_block']})")

    mine = app.my_proofs()
    if mine:
        st.divider()
        st.subheader("Issued proofs")
        for c, rec in sorted(mine.items(), key=lambda kv: -kv[1].created_at):
            col1, col2, col3, col4 = st.columns([2, 3, 2, 1.2])
            col1.write(f"`{c}`")
            cl = rec.proof["claim"]
            col2.write(CLAIMS[cl["type"]].split("(")[0] + f" → {eur(cl.get('amount', cl.get('rent', 0) * cl.get('multiple', 1)))}")
            col3.write("⛔ revoked" if rec.revoked else "🟢 active · " + rec.proof["audience"])
            if not rec.revoked and col4.button("Revoke", key="rev_" + c):
                app.revoke(c)
                st.rerun()


# ---------------------------------------------------------------------------
# Page 5 - Verifier portal (landlord / bank side)
# ---------------------------------------------------------------------------
def page_verify():
    st.title("Verify a proof")
    st.write("The verifier's view (landlord, agency, bank). No Solvo account needed: the check only uses "
             "the proof package and the public ledger. **Try a classmate's code!**")
    c1, c2 = st.columns([1, 1])
    code = c1.text_input("Proof code", st.session_state.get("last_code", ""))
    audience = c2.text_input("Your organisation (optional - checks the proof was issued to you)", "")

    proof = None
    if code in app.proofs:
        proof = copy.deepcopy(app.proofs[code].proof)
    elif code:
        st.warning("Unknown code. You can paste the JSON package below instead.")

    with st.expander("🧪 Advanced: inspect / edit the raw proof package (try tampering with a value!)"):
        raw = st.text_area("Proof JSON", json.dumps(proof, indent=2, ensure_ascii=False) if proof else "", height=260)
        if raw.strip():
            try:
                proof = json.loads(raw)
            except json.JSONDecodeError:
                st.error("Invalid JSON.")
                proof = None

    if st.button("Verify", type="primary", disabled=proof is None):
        res = app.verify(proof, audience or None)
        ev = res["eval"]
        if res["valid"]:
            cl = proof["claim"]
            st.markdown(
                f'<div class="ok-banner"><b>✅ VALID</b> - the holder proves <b>{eur(ev["proven"])}</b> '
                f'against a requirement of <b>{eur(ev["threshold"])}</b> - {CLAIMS[cl["type"]].split(" (")[0]}.<br>'
                f'<span class="muted">{res["verified_share"]:.0%} of the revealed value is attested by the aggregator '
                f'(bank API / blockchain); the rest is self-declared.</span></div>', unsafe_allow_html=True)
        else:
            st.markdown('<div class="ko-banner"><b>❌ INVALID</b> - do not rely on this certificate.</div>',
                        unsafe_allow_html=True)
        st.write("")
        st.dataframe(pd.DataFrame([{"": "✅" if c["ok"] else "❌", "Check": c["check"], "Detail": c["detail"]}
                                   for c in res["checks"]]), hide_index=True, width="stretch")


# ---------------------------------------------------------------------------
# Page 6 - Chain explorer
# ---------------------------------------------------------------------------
def page_chain():
    st.title("SolvoChain explorer")
    st.write("Simulated Ethereum L2 ledger (in production: a smart contract on Base). Each block is hash-linked "
             "to the previous one and sealed with a toy proof-of-work.")
    ok, bad = app.chain.verify_integrity()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Participants", len(SHARED["participants"]))
    c2.metric("Blocks", len(app.chain.blocks))
    c3.metric("Proofs issued", len(app.proofs))
    c4.metric("Integrity", "✅ valid" if ok else f"❌ broken at #{bad}")
    if st.button("🔄 Refresh"):
        st.rerun()

    rows = [{
        "#": b.index, "Time": fmt_ts(b.timestamp), "Tx types": ", ".join(t["type"] for t in b.txs),
        "Nonce": b.nonce, "Hash": b.hash[:18] + "…", "Prev": b.prev_hash[:18] + "…",
    } for b in reversed(app.chain.blocks)]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    with st.expander("Block details"):
        i = st.number_input("Block #", 0, len(app.chain.blocks) - 1, len(app.chain.blocks) - 1)
        st.json(app.chain.blocks[int(i)].txs)

    # The ledger is shared by everyone, so only the presenter (?admin=1) can tamper with it.
    if not IS_ADMIN:
        st.caption("Tip: on the Verify page, open the raw proof package and change one value to see a forgery rejected.")
        return
    st.subheader("Tamper test (presenter only)")
    st.caption("Change a single character in an anchored Merkle root, as an attacker with database access would. "
               "⚠️ This breaks the shared ledger for everyone until you click Undo.")
    a, b, c = st.columns(3)
    anchors = [blk for blk in app.chain.blocks if any(t["type"] == "ANCHOR_PROOF" for t in blk.txs)]
    if a.button("😈 Tamper with the latest anchor", disabled=not anchors or SHARED["tamper_backup"] is not None):
        blk = anchors[-1]
        tx = next(t for t in blk.txs if t["type"] == "ANCHOR_PROOF")
        SHARED["tamper_backup"] = (blk.index, tx["root"])
        tx["root"] = ("f" if tx["root"][0] != "f" else "e") + tx["root"][1:]
        st.rerun()
    if b.button("↩ Undo tamper", disabled=SHARED["tamper_backup"] is None):
        idx, root = SHARED["tamper_backup"]
        tx = next(t for t in app.chain.blocks[idx].txs if t["type"] == "ANCHOR_PROOF")
        tx["root"] = root
        SHARED["tamper_backup"] = None
        st.rerun()
    if c.button("🧹 Reset the shared ledger"):
        with SHARED["lock"]:
            SHARED["chain"].__init__()
            SHARED["proofs"].clear()
            SHARED["participants"].clear()
            SHARED["tamper_backup"] = None
        st.session_state.clear()
        st.rerun()


# ---------------------------------------------------------------------------
# Page 7 - Join the demo (QR code to show on the projector)
# ---------------------------------------------------------------------------
def page_join():
    st.title("Try Solvo on your phone")
    parts = urlsplit(st.context.url or "")
    link = f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.netloc else ""
    link = st.text_input("App link", link, help="Filled in automatically once the app is online.")
    a, b = st.columns([1, 1.2])
    with a:
        if link:
            img = qrcode.make(link, box_size=12, border=2)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            st.image(buf.getvalue(), width=380)
    with b:
        st.markdown(f"### {link}")
        st.markdown("1. Scan the code and enter your first name\n"
                    "2. Go to **Proof-of-Patrimoine** and generate a proof\n"
                    "3. Give your code to your neighbour: they check it on **Verify a proof**\n"
                    "4. Try to cheat: edit a value in the raw proof and verify again")
        k1, k2 = st.columns(2)
        k1.metric("People connected", len(SHARED["participants"]))
        k2.metric("Proofs issued", len(app.proofs))
        if st.button("🔄 Refresh counters"):
            st.rerun()


PAGES = {
    "📊 My wealth": page_wealth,
    "🏦 Accounts": page_accounts,
    "⏳ Freedom Clock": page_freedom,
    "🛡️ Proof-of-Patrimoine": page_proof,
    "✅ Verify a proof": page_verify,
    "⛓️ SolvoChain explorer": page_chain,
    "📱 Join the demo": page_join,
}
PAGES[page]()
