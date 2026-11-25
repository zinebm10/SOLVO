"""
Solvo - track your wealth, prove your solvency, reveal nothing else.

    streamlit run app.py

NEOMA MSc Fintech & DeFi - final group project.
Bank and wallet connections are simulated. The cryptography (salted Merkle
commitments, hash-chained ledger, proof verification) is real.
"""

import base64
import copy
import io
import json
import threading
from urllib.parse import urlsplit

import altair as alt
import pandas as pd
import qrcode
import streamlit as st

from solvo_core import (
    ASSET_CATEGORIES, CATEGORIES, CLAIMS, SOURCES, VERIFIED_SOURCES,
    Solvo, SolvoChain, allocation, fmt_ts, history, totals,
)

st.set_page_config(page_title="Solvo", page_icon=":material/verified_user:", layout="wide")

NAVY = "#0F1B3D"
ACCENT = "#2A5BD7"
PALETTE = ["#2A5BD7", "#E07B39", "#1B9E77", "#D9A21B", "#B5579A"]
CAT_SCALE = alt.Scale(domain=ASSET_CATEGORIES, range=PALETTE)

st.markdown(
    f"""
    <style>
      [data-testid="stMainMenu"], [data-testid="stAppDeployButton"], footer {{visibility: hidden;}}
      .block-container {{padding-top: 2.2rem;}}
      h1 {{font-weight: 700; letter-spacing: -0.02em;}}
      .wordmark {{font-size: 1.55rem; font-weight: 800; letter-spacing: -0.03em; color: {NAVY};}}
      .wordmark span {{color: {ACCENT};}}
      .tagline {{color: #5B6475; font-size: 0.85rem; margin-top: -4px;}}
      .eyebrow {{color: #5B6475; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.08em;
                 font-weight: 600; margin-bottom: 2px;}}
      .big-number {{font-size: 2.6rem; font-weight: 700; color: {NAVY}; line-height: 1.1;
                    letter-spacing: -0.02em; margin: 0;}}
      .muted {{color: #5B6475; font-size: 0.9rem;}}
      .up {{color: #1B9E77; font-weight: 600;}}
      .down {{color: #C0392B; font-weight: 600;}}
      .result {{border-radius: 12px; padding: 18px 22px; margin: 8px 0 14px 0;}}
      .result.ok {{background: #EEF8F3; border: 1px solid #1B9E77;}}
      .result.ko {{background: #FCEEEE; border: 1px solid #C0392B;}}
      .result .title {{font-size: 1.15rem; font-weight: 700; margin-bottom: 4px;}}
      .result.ok .title {{color: #137A5C;}}
      .result.ko .title {{color: #A93226;}}
      .proof-code {{font-family: ui-monospace, Menlo, monospace; font-size: 1.7rem; font-weight: 700;
                    letter-spacing: 0.08em; color: {NAVY}; background: #F2F5FB; border-radius: 10px;
                    padding: 10px 16px; display: inline-block;}}
      .hero h1 {{font-size: 2.6rem; line-height: 1.15;}}
      .feature {{font-weight: 600; color: {NAVY}; margin-bottom: 0;}}
    </style>
    """,
    unsafe_allow_html=True,
)


LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="32" viewBox="0 0 120 32">'
    f'<text x="0" y="25" font-family="Helvetica, Arial, sans-serif" font-size="27" font-weight="800" '
    f'letter-spacing="-1" fill="{NAVY}">solvo</text>'
    f'<circle cx="71" cy="22" r="3.6" fill="{ACCENT}"/></svg>'
)
st.logo("data:image/svg+xml;base64," + base64.b64encode(LOGO_SVG.encode()).decode(), size="large")


def eur(x: float) -> str:
    return f"€{x:,.0f}".replace(",", " ")


def source_label(source: str) -> str:
    return ("Verified · " if source in VERIFIED_SOURCES else "Declared · ") + SOURCES[source]


# ---------------------------------------------------------------------------
# Shared state: one ledger for every visitor, one portfolio per session
# ---------------------------------------------------------------------------
@st.cache_resource
def shared_ledger() -> dict:
    return {"chain": SolvoChain(), "proofs": {}, "lock": threading.RLock(),
            "participants": set(), "tamper_backup": None}


SHARED = shared_ledger()
if st.query_params.get("admin") == "1":          # presenter link: ...?admin=1
    st.session_state.admin = True
IS_ADMIN = st.session_state.get("admin", False)


def start_session(name: str) -> None:
    app = Solvo(user=name, chain=SHARED["chain"], proofs=SHARED["proofs"], lock=SHARED["lock"])
    app.load_demo()
    st.session_state.app = app
    SHARED["participants"].add(app.owner)


def welcome() -> None:
    left, right = st.columns([1.15, 1], gap="large")
    with left:
        st.markdown('<div class="wordmark">solvo<span>.</span></div>', unsafe_allow_html=True)
        st.markdown('<div class="hero"><h1>Prove you can pay.<br>Keep the rest private.</h1></div>',
                    unsafe_allow_html=True)
        st.write("Solvo brings all your accounts together and turns them into a solvency certificate "
                 "that a landlord or a bank can verify in seconds, without ever seeing your statements.")
        with st.form("join", border=False):
            name = st.text_input("First name", "Camille" if IS_ADMIN else "", placeholder="e.g. Lucas")
            go = st.form_submit_button("Open my demo account", type="primary", icon=":material/arrow_forward:")
        st.caption("Demo environment: your portfolio is generated for you, bank and wallet connections are simulated.")
        if go and name.strip():
            start_session(name.strip())
            st.rerun()
    with right:
        st.write("")
        for icon, title, text in [
            (":material/account_balance_wallet:", "One view of your wealth",
             "Bank accounts, savings, investments, crypto and property in a single dashboard."),
            (":material/verified_user:", "Targeted proofs",
             "Prove one fact, such as “3 × the rent available”, instead of sending every statement."),
            (":material/lock:", "Tamper-evident by design",
             "Each proof is anchored on a ledger. Change one figure and verification fails."),
        ]:
            with st.container(border=True):
                st.markdown(f'<p class="feature">{title}</p>', unsafe_allow_html=True)
                st.caption(text)


if "app" not in st.session_state:
    welcome()
    st.stop()

app: Solvo = st.session_state.app


# ---------------------------------------------------------------------------
# Feature 1 - Wealth dashboard and account aggregation
# ---------------------------------------------------------------------------
def page_wealth():
    st.title("Wealth")
    overview, accounts = st.tabs([":material/monitoring: Overview", ":material/account_balance: Accounts"])
    with overview:
        wealth_overview()
    with accounts:
        accounts_manager()


def wealth_overview():
    t = totals(app.assets)
    hist = pd.DataFrame(history(app.assets))
    nw = hist.groupby("month", as_index=False)["value"].sum().rename(columns={"value": "net_worth"})
    delta = nw["net_worth"].iloc[-1] - nw["net_worth"].iloc[0]

    c1, c2 = st.columns([1.2, 2])
    with c1:
        st.markdown('<div class="eyebrow">Net worth</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="big-number">{eur(t["net_worth"])}</div>', unsafe_allow_html=True)
        cls, arrow = ("up", "+") if delta >= 0 else ("down", "−")
        st.markdown(f'<p class="muted"><span class="{cls}">{arrow}{eur(abs(delta))}</span> over 12 months</p>',
                    unsafe_allow_html=True)
    with c2:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Gross assets", eur(t["gross"]))
        m2.metric("Debts", eur(t["debts"]))
        m3.metric("Liquid value", eur(t["liquid"]),
                  help="What counts in a liquidity proof: cash 100%, life insurance 90%, "
                       "stocks 75%, crypto 50%, real estate 0%.")
        m4.metric("Verified at source", f"{t['verified_share']:.0%}",
                  help="Share of gross assets read directly from a bank (open banking) or a public blockchain.")

    left, right = st.columns([1, 1.4], gap="large")
    with left:
        with st.container(border=True):
            st.markdown("**Allocation**")
            alloc = allocation(app.assets)
            df = pd.DataFrame({"category": list(alloc), "value": list(alloc.values())})
            df["share"] = df["value"] / df["value"].sum()
            donut = (
                alt.Chart(df)
                .mark_arc(innerRadius=62, outerRadius=105, stroke="#ffffff", strokeWidth=2)
                .encode(
                    theta=alt.Theta("value:Q", stack=True),
                    color=alt.Color("category:N", scale=CAT_SCALE,
                                    legend=alt.Legend(title=None, orient="right", labelFontSize=12, symbolSize=110)),
                    tooltip=[alt.Tooltip("category:N", title="Category"),
                             alt.Tooltip("value:Q", title="Value (€)", format=",.0f"),
                             alt.Tooltip("share:Q", title="Share", format=".1%")],
                )
                .properties(height=300)
            )
            st.altair_chart(donut, width="stretch")
    with right:
        with st.container(border=True):
            st.markdown("**Net worth, last 12 months**")
            area = (
                alt.Chart(nw)
                .mark_area(line={"color": ACCENT, "strokeWidth": 2.5}, color=alt.Gradient(
                    gradient="linear", x1=1, x2=1, y1=1, y2=0,
                    stops=[alt.GradientStop(color="#ffffff", offset=0), alt.GradientStop(color="#DCE6FB", offset=1)]))
                .encode(
                    x=alt.X("month:O", title=None, axis=alt.Axis(labelAngle=0, labelOverlap=True)),
                    y=alt.Y("net_worth:Q", title=None, scale=alt.Scale(zero=False), axis=alt.Axis(format="~s")),
                    tooltip=[alt.Tooltip("month:O", title="Month"),
                             alt.Tooltip("net_worth:Q", title="Net worth (€)", format=",.0f")],
                )
                .properties(height=320)
            )
            st.altair_chart(area, width="stretch")

    st.markdown("**Holdings**")
    rows = [{
        "Account": a.label, "Institution": a.institution, "Category": a.category,
        "Value": -a.value if a.is_debt else a.value,
        "Counts in proofs": a.liquid_value,
        "Source": source_label(a.source),
    } for a in app.assets]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                 column_config={"Value": st.column_config.NumberColumn(format="euro"),
                                "Counts in proofs": st.column_config.NumberColumn(format="euro")})


def accounts_manager():
    st.caption("Verified sources are read by Solvo itself (open banking or a public blockchain address). "
               "Only these are attested in a proof; manual entries stay marked as declared.")
    tab1, tab2, tab3, tab4 = st.tabs(["Bank", "Crypto wallet", "Manual entry", "CSV import"])
    with tab1:
        bank = st.selectbox("Bank", ["Société Générale", "Revolut", "Fortuneo", "Crédit Mutuel"])
        if st.button("Connect with open banking", type="primary", icon=":material/link:"):
            app.add_asset("Compte courant", bank, "Cash & savings", 2_480, "open_banking")
            app.add_asset("Livret Jeune", bank, "Cash & savings", 1_600, "open_banking")
            st.success(f"{bank}: 2 accounts synchronised. Read-only access, consent valid 180 days.")
    with tab2:
        addr = st.text_input("Public address (EVM)", "0x4b20993bc481177ec7e8f571cecae8a9e22c02db")
        if st.button("Read balance", icon=":material/search:"):
            a = app.connect_wallet(addr)
            st.success(f"Wallet added: {eur(a.value)}, read from the public ledger. No private key required.")
    with tab3:
        with st.form("manual"):
            c1, c2 = st.columns(2)
            label = c1.text_input("Name", "Cash at home")
            inst = c2.text_input("Institution or note", "-")
            cat = c1.selectbox("Category", list(CATEGORIES))
            val = c2.number_input("Value (€)", min_value=0.0, value=1_000.0, step=100.0)
            if st.form_submit_button("Add"):
                app.add_asset(label, inst, cat, val, "declared")
                st.success("Added as declared.")
    with tab4:
        st.caption("Expected columns: label, institution, category, value")
        up = st.file_uploader("CSV file", type="csv")
        if up is not None and st.button("Import"):
            df = pd.read_csv(up)
            n = 0
            for _, r in df.iterrows():
                if r["category"] in CATEGORIES:
                    app.add_asset(str(r["label"]), str(r["institution"]), r["category"], float(r["value"]), "declared")
                    n += 1
            st.success(f"{n} line(s) imported as declared.")

    st.markdown("**Connected accounts**")
    for a in list(app.assets):
        with st.container(border=True):
            c1, c2, c3, c4, c5 = st.columns([3, 2, 1.6, 2.4, 0.6], vertical_alignment="center")
            c1.markdown(f"**{a.label}**  \n<span class='muted'>{a.institution}</span>", unsafe_allow_html=True)
            c2.write(a.category)
            c3.write(("−" if a.is_debt else "") + eur(a.value))
            if a.source in VERIFIED_SOURCES:
                c4.badge(SOURCES[a.source], icon=":material/check_circle:", color="green")
            else:
                c4.badge("Declared", icon=":material/edit:", color="gray")
            if c5.button("", key="del_" + a.id, icon=":material/delete:", help="Remove"):
                app.assets = [x for x in app.assets if x.id != a.id]
                st.rerun()


# ---------------------------------------------------------------------------
# Feature 2 - Create a proof of solvency (holder side)
# ---------------------------------------------------------------------------
def page_proof():
    st.title("Create a proof")
    st.write("Generate a certificate that a landlord or a bank can check without seeing your full portfolio. "
             "Your holdings are committed in a salted Merkle tree and only its root is anchored on the ledger.")

    claim_type = st.selectbox("What do you need to prove?", list(CLAIMS), format_func=lambda k: CLAIMS[k])
    with st.form("proof"):
        c1, c2, c3 = st.columns(3)
        if claim_type == "rent":
            rent = c1.number_input("Monthly rent (€)", 200, 10_000, 850, 10)
            mult = c2.number_input("Multiple required", 1, 24, 3)
            claim = {"type": "rent", "rent": float(rent), "multiple": int(mult)}
        else:
            amt = c1.number_input("Amount (€)", 1_000, 5_000_000, 40_000 if claim_type == "downpayment" else 80_000, 1_000)
            claim = {"type": claim_type, "amount": float(amt)}
        days = c3.number_input("Valid for (days)", 1, 365, 30)
        audience = st.text_input("Issued to", "Agence Lille Centre" if claim_type == "rent" else "Crédit Agricole")
        mode = st.segmented_control("Disclosure", ["minimal", "full"], default="minimal",
                                    format_func=lambda m: "Minimal: fewest accounts needed" if m == "minimal"
                                    else "Full: every line")
        submitted = st.form_submit_button("Generate and anchor", type="primary", icon=":material/verified:")

    if submitted:
        code, msg = app.create_proof(claim, audience, int(days), mode or "minimal")
        if code is None:
            st.error(msg)
        else:
            st.session_state.last_code = code
            st.toast(msg, icon=":material/check:")

    code = st.session_state.get("last_code")
    if code and code in app.proofs:
        p = app.proofs[code].proof
        a, b = st.columns([1, 1.6], gap="large")
        with a:
            with st.container(border=True):
                st.markdown('<p class="eyebrow">Share this code</p>', unsafe_allow_html=True)
                st.markdown(f'<div class="proof-code">{code}</div>', unsafe_allow_html=True)
                st.caption(f"For {p['audience']} · valid until {fmt_ts(p['expires_at'])}")
                st.download_button("Download proof package", json.dumps(p, indent=2, ensure_ascii=False),
                                   file_name=f"{code}.json", mime="application/json", icon=":material/download:")
        with b:
            revealed = [r["leaf"] for r in p["revealed"] if r["leaf"]["kind"] != "header"]
            hidden = p["n_leaves"] - 1 - len(revealed)
            st.markdown(f"**What the verifier sees:** {len(revealed)} line(s) disclosed, {hidden} kept private")
            st.dataframe(pd.DataFrame([{
                "Type": l["kind"].capitalize(), "Category": l["category"], "Value": l["value_cents"] / 100,
                "Source": source_label(l["source"]), "Institution": "Hidden",
            } for l in revealed]), hide_index=True, width="stretch",
                column_config={"Value": st.column_config.NumberColumn(format="euro")})
            st.caption(f"Merkle root `{p['root'][:24]}…` anchored in block #{p['anchor_block']}")

    mine = app.my_proofs()
    if mine:
        st.markdown("**Issued proofs**")
        for c, rec in sorted(mine.items(), key=lambda kv: -kv[1].created_at):
            with st.container(border=True):
                col1, col2, col3, col4 = st.columns([2, 3, 2.2, 1.2], vertical_alignment="center")
                col1.markdown(f"`{c}`")
                cl = rec.proof["claim"]
                amount = cl.get("amount", cl.get("rent", 0) * cl.get("multiple", 1))
                col2.write(CLAIMS[cl["type"]].split(" (")[0] + f" · {eur(amount)}")
                if rec.revoked:
                    col3.badge("Revoked", icon=":material/block:", color="red")
                else:
                    col3.badge("Active · " + rec.proof["audience"], icon=":material/check_circle:", color="green")
                if not rec.revoked and col4.button("Revoke", key="rev_" + c):
                    app.revoke(c)
                    st.rerun()


# ---------------------------------------------------------------------------
# Feature 3 - Verify a proof (landlord / bank side)
# ---------------------------------------------------------------------------
def page_verify():
    st.title("Verify a proof")
    st.write("For landlords, agencies and lenders. No account needed: the check only uses the proof "
             "and the public ledger.")
    c1, c2 = st.columns(2)
    code = c1.text_input("Proof code", st.session_state.get("last_code", ""), placeholder="SOLVO-XXXX-XXXX")
    audience = c2.text_input("Your organisation (optional)", "",
                             help="Checks that the proof was issued to you and not reused from another file.")

    proof = None
    if code in app.proofs:
        proof = copy.deepcopy(app.proofs[code].proof)
    elif code:
        st.warning("Unknown code. You can paste a proof package below instead.")

    with st.expander("Inspect the raw proof package"):
        raw = st.text_area("Proof package (JSON)", json.dumps(proof, indent=2, ensure_ascii=False) if proof else "",
                           height=260, help="Edit any value and verify again to see a forgery being rejected.")
        if raw.strip():
            try:
                proof = json.loads(raw)
            except json.JSONDecodeError:
                st.error("Invalid JSON.")
                proof = None

    if st.button("Verify", type="primary", disabled=proof is None, icon=":material/fact_check:"):
        res = app.verify(proof, audience or None)
        ev = res["eval"]
        if res["valid"]:
            cl = proof["claim"]
            st.markdown(
                f'<div class="result ok"><div class="title">Valid certificate</div>'
                f'The holder proves <b>{eur(ev["proven"])}</b> against a requirement of <b>{eur(ev["threshold"])}</b> '
                f'({CLAIMS[cl["type"]].split(" (")[0].lower()}).<br>'
                f'<span class="muted">{res["verified_share"]:.0%} of the disclosed value is verified at source; '
                f'the rest is declared by the holder.</span></div>', unsafe_allow_html=True)
        else:
            st.markdown('<div class="result ko"><div class="title">Invalid certificate</div>'
                        'At least one check failed. Do not rely on this document.</div>', unsafe_allow_html=True)
        st.dataframe(pd.DataFrame([{"Status": "Pass" if c["ok"] else "Fail", "Check": c["check"], "Detail": c["detail"]}
                                   for c in res["checks"]]), hide_index=True, width="stretch")


# ---------------------------------------------------------------------------
# Feature 4 - Ledger explorer (tamper-evident record)
# ---------------------------------------------------------------------------
def page_chain():
    st.title("Ledger")
    st.write("Every proof, attestation and revocation is recorded in a hash-linked chain of blocks. "
             "This prototype simulates the ledger locally; in production it would be a smart contract on an "
             "Ethereum layer 2 (Base).")
    ok, bad = app.chain.verify_integrity()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Users", len(SHARED["participants"]))
    c2.metric("Blocks", len(app.chain.blocks))
    c3.metric("Proofs issued", len(app.proofs))
    c4.metric("Integrity", "Valid" if ok else f"Broken at #{bad}")
    st.button("Refresh", icon=":material/refresh:")

    rows = [{
        "Block": b.index, "Time": fmt_ts(b.timestamp), "Transactions": ", ".join(t["type"] for t in b.txs),
        "Nonce": b.nonce, "Hash": b.hash[:18] + "…", "Previous hash": b.prev_hash[:18] + "…",
    } for b in reversed(app.chain.blocks)]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    with st.expander("Block details"):
        i = st.number_input("Block", 0, len(app.chain.blocks) - 1, len(app.chain.blocks) - 1)
        st.json(app.chain.blocks[int(i)].txs)

    if not IS_ADMIN:
        return
    # The ledger is shared by every user, so only the presenter can alter it.
    st.subheader("Attack simulation")
    st.caption("Alter one character of an anchored Merkle root, as an attacker with database access would. "
               "This breaks the shared ledger for everyone until it is restored.")
    a, b, c = st.columns(3)
    anchors = [blk for blk in app.chain.blocks if any(t["type"] == "ANCHOR_PROOF" for t in blk.txs)]
    if a.button("Tamper with latest anchor", icon=":material/warning:",
                disabled=not anchors or SHARED["tamper_backup"] is not None):
        blk = anchors[-1]
        tx = next(t for t in blk.txs if t["type"] == "ANCHOR_PROOF")
        SHARED["tamper_backup"] = (blk.index, tx["root"])
        tx["root"] = ("f" if tx["root"][0] != "f" else "e") + tx["root"][1:]
        st.rerun()
    if b.button("Restore", icon=":material/undo:", disabled=SHARED["tamper_backup"] is None):
        idx, root = SHARED["tamper_backup"]
        tx = next(t for t in app.chain.blocks[idx].txs if t["type"] == "ANCHOR_PROOF")
        tx["root"] = root
        SHARED["tamper_backup"] = None
        st.rerun()
    if c.button("Reset ledger", icon=":material/restart_alt:"):
        with SHARED["lock"]:
            SHARED["chain"].__init__()
            SHARED["proofs"].clear()
            SHARED["participants"].clear()
            SHARED["tamper_backup"] = None
        st.session_state.clear()
        st.session_state.admin = True
        st.rerun()


# ---------------------------------------------------------------------------
# Presenter screen - QR code for the audience
# ---------------------------------------------------------------------------
def page_live():
    st.title("Try Solvo on your phone")
    parts = urlsplit(st.context.url or "")
    link = f"{parts.scheme}://{parts.netloc}/" if parts.netloc else ""
    link = st.text_input("App link", link)
    a, b = st.columns([1, 1.2], gap="large")
    with a:
        if link:
            img = qrcode.make(link, box_size=12, border=2)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            st.image(buf.getvalue(), width=380)
    with b:
        st.markdown(f"### {link.replace('https://', '')}")
        st.markdown("1. Scan the code and enter your first name\n"
                    "2. Open **Create a proof** and generate one\n"
                    "3. Give your code to your neighbour, who checks it in **Verify a proof**\n"
                    "4. Try to cheat: edit a value in the raw package and verify again")
        k1, k2 = st.columns(2)
        k1.metric("Users connected", len(SHARED["participants"]))
        k2.metric("Proofs issued", len(app.proofs))
        st.button("Refresh", icon=":material/refresh:", key="refresh_live")


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------
pages = {
    "Holder": [
        st.Page(page_wealth, title="Wealth", icon=":material/account_balance_wallet:", url_path="wealth", default=True),
        st.Page(page_proof, title="Create a proof", icon=":material/verified_user:", url_path="prove"),
    ],
    "Verifier": [
        st.Page(page_verify, title="Verify a proof", icon=":material/fact_check:", url_path="verify"),
    ],
    "Network": [
        st.Page(page_chain, title="Ledger", icon=":material/deployed_code:", url_path="ledger"),
    ],
}
if IS_ADMIN:
    pages["Presentation"] = [st.Page(page_live, title="Live demo", icon=":material/qr_code_2:", url_path="live")]
nav = st.navigation(pages)

with st.sidebar:
    t = totals(app.assets)
    with st.container(border=True):
        st.caption(f"Signed in as **{app.user}**")
        st.markdown(f"**{eur(t['net_worth'])}** net worth")
        st.caption(f"ID `{app.owner[:12]}…`")
    if st.button("Reset my data", icon=":material/restart_alt:", width="stretch"):
        start_session(app.user)
        st.session_state.pop("last_code", None)
        st.rerun()
    st.caption("Prototype · bank and wallet connections are simulated")

nav.run()
