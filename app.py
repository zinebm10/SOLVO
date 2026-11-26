"""
Solvo - financial trust, without financial exposure.

    streamlit run app.py

NEOMA MSc Fintech & DeFi - final group project.
REAL: salted Merkle commitments, Ed25519 issuer signature, hash-chained ledger,
strict proof verification.   SIMULATED: bank/wallet connections, the issuer, the ledger.
"""

import base64
import copy
import io
import json
import threading
from datetime import datetime
from urllib.parse import urlsplit

import altair as alt
import pandas as pd
import qrcode
import streamlit as st

from certificate import build_certificate_pdf
from solvo_core import (
    ASSET_CATEGORIES, ATTESTABLE_SOURCES, CATEGORIES, CLAIM_SHORT, EVENT_LABELS, SOURCES,
    TRUST_LEVEL, Issuer, Solvo, SolvoChain, allocation, claim_threshold, fmt_ts, history,
    preview_claim, totals,
)

st.set_page_config(page_title="Solvo", page_icon=":material/verified_user:", layout="wide")

NAVY, ACCENT, GREEN = "#0F1B3D", "#2A5BD7", "#137A5C"
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
      .eyebrow {{color: #5B6475; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.08em;
                 font-weight: 600; margin-bottom: 2px;}}
      .big-number {{font-size: 2.6rem; font-weight: 700; color: {NAVY}; line-height: 1.1; letter-spacing: -0.02em;}}
      .muted {{color: #5B6475; font-size: 0.9rem;}}
      .hero h1 {{font-size: 2.6rem; line-height: 1.15;}}
      .step {{font-size: .72rem; font-weight: 700; letter-spacing: .08em; color: {ACCENT};}}
      .feature {{font-weight: 600; color: {NAVY}; margin-bottom: 0;}}
      .result {{border-radius: 12px; padding: 18px 22px; margin: 8px 0 14px 0;}}
      .result.ok {{background: #EEF8F3; border: 1px solid #1B9E77;}}
      .result.ko {{background: #FCEEEE; border: 1px solid #C0392B;}}
      .result .title {{font-size: 1.2rem; font-weight: 700; margin-bottom: 4px;}}
      .result.ok .title {{color: {GREEN};}} .result.ko .title {{color: #A93226;}}
      .cert {{border: 1px solid #D9DEEA; border-radius: 14px; overflow: hidden; background: #fff;}}
      .cert .band {{background: {NAVY}; color: #fff; padding: 12px 22px; display: flex;
                    justify-content: space-between; font-size: .75rem; letter-spacing: .06em;}}
      .cert .body {{padding: 18px 22px;}}
      .cert .amount {{font-size: 2.4rem; font-weight: 800; color: {NAVY}; letter-spacing: -0.02em;}}
      .cert .ok {{color: {GREEN}; font-weight: 700; font-size: .8rem; letter-spacing: .08em;}}
      .cert .ko {{color: #A93226; font-weight: 700; font-size: .8rem; letter-spacing: .08em;}}
      .proof-code {{font-family: ui-monospace, Menlo, monospace; font-size: 1.25rem; font-weight: 700;
                    color: {NAVY}; background: #F2F5FB; border-radius: 10px; padding: 8px 14px; display: inline-block;}}
      .flow {{display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 6px 0 12px 0;}}
      .flow .chip {{background: #F2F5FB; border: 1px solid #D9DEEA; border-radius: 8px; padding: 6px 10px;
                    font-size: .82rem; color: {NAVY}; font-weight: 600;}}
      .flow .arrow {{color: #5B6475;}}
      .note {{font-size: .82rem; color: #5B6475;}}
    </style>
    """,
    unsafe_allow_html=True,
)

LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="32" viewBox="0 0 120 32">'
    f'<text x="0" y="25" font-family="Helvetica, Arial, sans-serif" font-size="27" font-weight="800" '
    f'letter-spacing="-1" fill="{NAVY}">solvo</text><circle cx="71" cy="22" r="3.6" fill="{ACCENT}"/></svg>'
)
st.logo("data:image/svg+xml;base64," + base64.b64encode(LOGO_SVG.encode()).decode(), size="large")


def eur(x: float) -> str:
    return f"€{x:,.0f}".replace(",", " ")


def trust_badge(col, source: str):
    if source in ATTESTABLE_SOURCES:
        col.badge("Connected · demo data", icon=":material/link:", color="blue")
    else:
        col.badge("Self-declared", icon=":material/edit:", color="gray")


# ---------------------------------------------------------------------------
# Shared state: one ledger + one demo issuer for every visitor
# ---------------------------------------------------------------------------
@st.cache_resource
def shared_ledger() -> dict:
    issuer = Issuer()
    return {"issuer": issuer, "chain": SolvoChain([issuer.info()]), "proofs": {},
            "lock": threading.RLock(), "participants": set(), "tamper_backup": None}


SHARED = shared_ledger()
# Demo-only presenter switch. NOT authentication: a real product needs real accounts.
if st.query_params.get("admin") == "1":
    st.session_state.admin = True
IS_ADMIN = st.session_state.get("admin", False)


def make_session(name: str) -> Solvo:
    return Solvo(user=name, chain=SHARED["chain"], proofs=SHARED["proofs"], lock=SHARED["lock"],
                 issuer=SHARED["issuer"])


def start_holder(name: str) -> None:
    app = make_session(name)
    app.load_demo()
    st.session_state.app = app
    st.session_state.mode = "holder"
    SHARED["participants"].add(app.owner)


def start_verifier() -> None:
    st.session_state.app = make_session("Verifier")
    st.session_state.mode = "verifier"


def base_url() -> str:
    parts = urlsplit(st.context.url or "")
    return f"{parts.scheme}://{parts.netloc}/" if parts.netloc else ""


def verify_link(pid: str) -> str:
    return f"{base_url()}verify?proof={pid}"


# ---------------------------------------------------------------------------
# Welcome: the whole product in 4 steps
# ---------------------------------------------------------------------------
def welcome() -> None:
    st.markdown('<div class="wordmark">solvo<span>.</span></div>', unsafe_allow_html=True)
    st.markdown('<div class="hero"><h1>Financial trust,<br>without financial exposure.</h1></div>',
                unsafe_allow_html=True)
    st.write("Prove you have the financial means for a rental, a purchase or a visa file, "
             "**without handing over your whole wealth.** The recipient verifies the claim, not your accounts.")
    steps = [("STEP 1", "Build your profile", "Connect or add accounts, savings, investments, property, debts."),
             ("STEP 2", "Select your purpose", "Rental application, down payment, liquid assets or net worth."),
             ("STEP 3", "Generate your proof", "Solvo picks what must be disclosed, commits the rest and anchors the proof."),
             ("STEP 4", "Share & verify", "The recipient checks amount, recipient, expiry, revocation and attestation.")]
    cols = st.columns(4)
    for col, (s, t, d) in zip(cols, steps):
        with col.container(border=True, height=185):
            st.markdown(f'<div class="step">{s}</div><p class="feature">{t}</p>', unsafe_allow_html=True)
            st.caption(d)

    left, right = st.columns(2, gap="large")
    with left:
        with st.container(border=True):
            st.markdown("**I want to prove my financial capacity**")
            with st.form("join", border=False):
                name = st.text_input("First name", "Camille" if IS_ADMIN else "", placeholder="e.g. Lucas")
                go = st.form_submit_button("Open my demo account", type="primary", icon=":material/arrow_forward:")
            if go and name.strip():
                start_holder(name.strip())
                st.rerun()
    with right:
        with st.container(border=True):
            st.markdown("**I received a proof and want to check it**")
            st.caption("No account needed. Enter the Proof ID or scan the QR code on the certificate.")
            if st.button("Verify a proof", icon=":material/fact_check:"):
                start_verifier()
                st.rerun()
    st.caption("Demo environment: bank and wallet connections are simulated and the issuer is a demo issuer. "
               "Illustrative data, not a real financial assessment.")


if "app" not in st.session_state:
    if st.query_params.get("proof"):          # arrived through a certificate's QR code
        start_verifier()
        st.rerun()
    welcome()
    st.stop()

app: Solvo = st.session_state.app
IS_HOLDER = st.session_state.get("mode") == "holder"


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------
def page_overview():
    st.title("Overview")
    t = totals(app.assets)
    c1, c2 = st.columns([1.2, 2])
    with c1:
        st.markdown('<div class="eyebrow">Estimated net worth</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="big-number">{eur(t["net_worth"])}</div>', unsafe_allow_html=True)
        st.markdown(f'<p class="muted">Last change recorded {fmt_ts(app.snapshots[-1]["ts"])}</p>',
                    unsafe_allow_html=True)
    with c2:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Gross assets", eur(t["gross"]))
        m2.metric("Liabilities", eur(t["debts"]))
        m3.metric("Eligible liquid assets", eur(t["liquid"]),
                  help="What counts in a liquidity proof, after haircuts: cash 100%, life insurance 90%, "
                       "stocks 75%, crypto 50%, real estate 0%. Different from net worth.")
        m4.metric("Source-connected", f"{t['connected_share']:.0%}",
                  help="Share of gross assets coming from a (simulated) connected source, and therefore "
                       "attestable. Manual entries are never attested.")
    st.caption("Net worth = what you own minus what you owe. Eligible liquid assets = what a recipient can count "
               "as readily available. Illustrative demo data.")

    left, right = st.columns([1, 1.4], gap="large")
    with left:
        with st.container(border=True):
            st.markdown("**Allocation**")
            alloc = allocation(app.assets)
            df = pd.DataFrame({"category": list(alloc), "value": list(alloc.values())})
            df["share"] = df["value"] / df["value"].sum()
            donut = (alt.Chart(df).mark_arc(innerRadius=62, outerRadius=105, stroke="#ffffff", strokeWidth=2)
                     .encode(theta=alt.Theta("value:Q", stack=True),
                             color=alt.Color("category:N", scale=CAT_SCALE,
                                             legend=alt.Legend(title=None, orient="right", labelFontSize=12)),
                             tooltip=[alt.Tooltip("category:N"), alt.Tooltip("value:Q", format=",.0f"),
                                      alt.Tooltip("share:Q", format=".1%")]).properties(height=300))
            st.altair_chart(donut, width="stretch")
    with right:
        with st.container(border=True):
            st.markdown("**Net worth, last 12 months** · simulated history")
            hist = pd.DataFrame(history(app.assets))
            nw = hist.groupby("month", as_index=False)["value"].sum().rename(columns={"value": "net_worth"})
            area = (alt.Chart(nw).mark_area(line={"color": ACCENT, "strokeWidth": 2.5}, color="#DCE6FB")
                    .encode(x=alt.X("month:O", title=None, axis=alt.Axis(labelAngle=0, labelOverlap=True)),
                            y=alt.Y("net_worth:Q", title=None, scale=alt.Scale(zero=False), axis=alt.Axis(format="~s")),
                            tooltip=[alt.Tooltip("month:O"), alt.Tooltip("net_worth:Q", format=",.0f")])
                    .properties(height=320))
            st.altair_chart(area, width="stretch")
            st.caption(f"This curve is back-filled from today's values for the demo: it is **not** a real history. "
                       f"Real snapshots are recorded at each change ({len(app.snapshots)} so far).")

    st.markdown("**Holdings**")
    st.dataframe(pd.DataFrame([{
        "Account": a.label, "Institution": a.institution, "Category": a.category,
        "Value": -a.value if a.is_debt else a.value, "Counts in proofs": a.liquid_value,
        "Trust level": TRUST_LEVEL[a.source], "Updated": datetime.fromtimestamp(a.updated_at).strftime("%d/%m %H:%M"),
    } for a in app.assets]), width="stretch", hide_index=True,
        column_config={"Value": st.column_config.NumberColumn(format="euro"),
                       "Counts in proofs": st.column_config.NumberColumn(format="euro")})


# ---------------------------------------------------------------------------
# My assets: connect, add, edit, delete
# ---------------------------------------------------------------------------
def page_assets():
    st.title("My assets")
    st.caption("Three trust levels: **self-declared** (typed by you, never attested), **source-connected** "
               "(read from a bank or wallet) and **issuer-attested** (a signed attestation attached to a proof). "
               "In this prototype the connections below are simulated, so connected lines are labelled demo data.")
    tab1, tab2, tab3, tab4 = st.tabs(["Bank (simulated)", "Crypto wallet (simulated)", "Manual entry", "CSV import"])
    with tab1:
        bank = st.selectbox("Institution", ["Société Générale", "Revolut", "Fortuneo", "Crédit Mutuel"])
        if st.button("Connect (demo open banking)", type="primary", icon=":material/link:"):
            app.connect_bank_demo(bank)
            st.success(f"{bank}: 2 demo accounts added. Simulated read-only connection.")
    with tab2:
        addr = st.text_input("Public address (EVM)", "0x4b20993bc481177ec7e8f571cecae8a9e22c02db")
        st.caption("Reading a public address does not prove you own it. A real integration would require "
                   "a message signed by the wallet.")
        if st.button("Read balance (simulated)", icon=":material/search:"):
            a = app.connect_wallet_demo(addr)
            st.success(f"Wallet added: {eur(a.value)} (demo value).")
    with tab3:
        with st.form("manual"):
            c1, c2 = st.columns(2)
            label = c1.text_input("Name", "Cash at home")
            inst = c2.text_input("Institution or note", "-")
            cat = c1.selectbox("Category", list(CATEGORIES))
            val = c2.number_input("Value (€)", min_value=0.0, value=1_000.0, step=100.0)
            if st.form_submit_button("Add as self-declared"):
                app.add_asset(label, inst, cat, val, "declared")
                st.rerun()
    with tab4:
        st.caption("Expected columns: label, institution, category, value")
        up = st.file_uploader("CSV file", type="csv")
        if up is not None and st.button("Import"):
            df = pd.read_csv(up)
            n = 0
            for _, r in df.iterrows():
                try:
                    if r["category"] in CATEGORIES and float(r["value"]) >= 0:
                        app.add_asset(str(r["label"]), str(r["institution"]), r["category"], float(r["value"]), "declared")
                        n += 1
                except (KeyError, ValueError):
                    continue
            st.success(f"{n} line(s) imported as self-declared.")

    st.markdown("**Your accounts**")
    for a in list(app.assets):
        with st.container(border=True):
            c1, c2, c3, c4, c5 = st.columns([3, 1.8, 1.6, 2.4, 0.7], vertical_alignment="center")
            c1.markdown(f"**{a.label}**  \n<span class='muted'>{a.institution}</span>", unsafe_allow_html=True)
            c2.write(a.category)
            c3.write(("−" if a.is_debt else "") + eur(a.value))
            trust_badge(c4, a.source)
            c4.caption("Updated " + datetime.fromtimestamp(a.updated_at).strftime("%d/%m/%Y %H:%M"))
            if c5.button("", key="del_" + a.id, icon=":material/delete:", help="Remove"):
                app.delete_asset(a.id)
                st.rerun()
        with st.expander("Edit", icon=":material/edit:"):
            with st.form("edit_" + a.id, border=False):
                e1, e2, e3, e4 = st.columns(4)
                nl = e1.text_input("Name", a.label)
                ni = e2.text_input("Institution", a.institution)
                nc = e3.selectbox("Category", list(CATEGORIES), index=list(CATEGORIES).index(a.category))
                nv = e4.number_input("Value (€)", 0.0, value=float(a.value), step=100.0)
                if st.form_submit_button("Save"):
                    app.update_asset(a.id, nl, ni, nc, nv)
                    st.rerun()
            if a.source != "declared":
                st.caption("Changing the value or category turns this line into self-declared: it is no longer "
                           "what the source returned, so it will not be attested.")


# ---------------------------------------------------------------------------
# Proof Center (main product flow)
# ---------------------------------------------------------------------------
PURPOSES = {
    "rent": ("Rental application", "Financial capacity for rent"),
    "downpayment": ("Down payment", "Available funds for a purchase"),
    "liquid": ("Liquid assets", "Minimum financial resources"),
    "networth": ("Net worth", "Assets minus liabilities"),
}


def page_proof():
    st.title("Proof Center")
    st.write("**How can I prove I have the means, without sharing everything?** Choose what to prove; "
             "Solvo discloses only what is needed.")

    st.markdown('<div class="step">STEP 1 · WHAT DO YOU NEED TO PROVE?</div>', unsafe_allow_html=True)
    ctype = st.radio("Purpose", list(PURPOSES), horizontal=True, label_visibility="collapsed",
                     format_func=lambda k: f"{PURPOSES[k][0]} · {PURPOSES[k][1]}")

    st.markdown('<div class="step">STEP 2 · AMOUNT, RECIPIENT, VALIDITY</div>', unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3)
    if ctype == "rent":
        rent = c1.number_input("Monthly rent (€)", 200, 10_000, 850, 10)
        mult = c2.number_input("Months of rent to cover", 1, 24, 3)
        claim = {"type": "rent", "rent": float(rent), "multiple": int(mult)}
    else:
        default = {"downpayment": 40_000, "liquid": 20_000, "networth": 80_000}[ctype]
        amt = c1.number_input("Amount (€)", 1_000, 5_000_000, default, 1_000)
        claim = {"type": ctype, "amount": float(amt)}
    days = c3.number_input("Valid for (days)", 1, 365, 30)
    audience = st.text_input("Issued to (recipient)", "Agence Lille Centre" if ctype == "rent" else "Crédit Agricole")

    pv = preview_claim(app.assets, claim)
    st.markdown('<div class="step">STEP 3 · WHAT SOLVO CAN PROVE</div>', unsafe_allow_html=True)
    m1, m2, m3 = st.columns(3)
    m1.metric("Eligible " + ("net worth" if ctype == "networth" else "liquid assets"), eur(pv["eligible"]))
    m2.metric("Required amount", eur(claim_threshold(claim)))
    m3.metric("Remaining to justify" if not pv["ok"] else "Surplus", eur(pv["remaining"] if not pv["ok"] else pv["surplus"]))
    if not pv["ok"]:
        st.warning("Your eligible resources do not cover this amount yet. Add or connect assets, or lower the amount.")

    st.markdown('<div class="step">STEP 4 · DISCLOSURE LEVEL</div>', unsafe_allow_html=True)
    mode = st.radio("Disclosure", ["minimal", "full"], horizontal=True, label_visibility="collapsed",
                    format_func=lambda m: "Limited: only the lines needed" if m == "minimal" else "Full: every line")
    if st.button("Generate certificate", type="primary", icon=":material/verified:", disabled=not pv["ok"]):
        pid, msg = app.create_proof(claim, audience, int(days), mode)
        if pid is None:
            st.error(msg)
        else:
            st.session_state.last_code = pid
            st.toast(msg, icon=":material/check:")

    code = st.session_state.get("last_code")
    if code and code in app.proofs:
        show_certificate(code)

    mine = app.my_proofs()
    if mine:
        st.markdown("**Issued proofs**")
        for c, rec in sorted(mine.items(), key=lambda kv: -kv[1].created_at):
            with st.container(border=True):
                a, b, d, e = st.columns([2.8, 2.6, 2.4, 1], vertical_alignment="center")
                a.markdown(f"`{c}`")
                cl = rec.proof["claim"]
                b.write(f"{CLAIM_SHORT[cl['type']]} · {eur(claim_threshold(cl))}")
                life = app.lifecycle(c)
                if rec.revoked:
                    d.badge("Revoked", icon=":material/block:", color="red")
                elif not life[-1].get("active"):
                    d.badge("Expired", icon=":material/schedule:", color="orange")
                else:
                    d.badge("Active · " + rec.proof["audience"], icon=":material/check_circle:", color="green")
                if not rec.revoked and e.button("Revoke", key="rev_" + c):
                    app.revoke(c)
                    st.rerun()
                st.caption(" → ".join(("✓ " if s["done"] or s.get("active") else "○ ") + s["step"] +
                                      (f" ({s['detail']})" if s.get("detail") else "") for s in life))


def show_certificate(code: str):
    rec = app.proofs[code]
    p = rec.proof
    res = app.verify(p, p["audience"])
    ev = res["eval"] or {"proven": 0}
    ok = res["valid"] and not rec.revoked
    st.divider()
    left, right = st.columns([1.7, 1], gap="large")
    with left:
        att = "Simulated issuer · Ed25519 signed" if p["attestation"] else "None · self-declared data"
        st.markdown(
            f'<div class="cert"><div class="band"><span>SOLVO FINANCIAL CREDENTIAL</span><span>DEMO CERTIFICATE</span></div>'
            f'<div class="body"><div class="{"ok" if ok else "ko"}">{"VERIFIED CLAIM" if ok else "NOT VALID"}</div>'
            f'<div class="muted">{CLAIM_SHORT[p["claim"]["type"]].upper()}</div><br>'
            f'<div class="eyebrow">Eligible financial resources</div><div class="amount">{eur(ev["proven"])}</div>'
            f'<p class="muted">Required <b>{eur(claim_threshold(p["claim"]))}</b> · Issued to <b>{p["audience"]}</b> · '
            f'Valid until <b>{fmt_ts(p["expires_at"])}</b></p>'
            f'<p class="note">Cryptographic integrity: <b>{"Passed" if res["valid"] else "Failed"}</b> · '
            f'Issuer attestation: <b>{att}</b> · Ledger anchoring: <b>Demo chain, block #{p["anchor_block"]}</b> · '
            f'Revocation: <b>{"Revoked" if rec.revoked else "Not revoked"}</b> · Privacy: <b>Limited disclosure</b></p>'
            f'<div class="proof-code">{code}</div></div></div>', unsafe_allow_html=True)
        st.caption("Illustrative certificate. A demo-chain result is not an independent third-party financial certification.")
    with right:
        link = verify_link(code)
        buf = io.BytesIO()
        qrcode.make(link, box_size=7, border=2).save(buf, format="PNG")
        st.image(buf.getvalue(), width=170, caption="Scan to verify")
        st.download_button("Download PDF certificate", build_certificate_pdf(p, res, rec.revoked, link),
                           file_name=f"{code}.pdf", mime="application/pdf", icon=":material/picture_as_pdf:",
                           width="stretch")
        st.download_button("Download proof package (JSON)", json.dumps(p, indent=2, ensure_ascii=False),
                           file_name=f"{code}.json", mime="application/json", icon=":material/data_object:",
                           width="stretch")

    revealed = [r["leaf"] for r in p["revealed"] if r["leaf"]["kind"] != "header"]
    hidden = p["n_leaves"] - 1 - len(revealed)
    gross = totals(app.assets)["gross"] or 1
    shown_assets = sum(l["value_cents"] for l in revealed if l["kind"] == "asset") / 100
    with st.expander("What is disclosed, what stays private, and why", expanded=True):
        a, b = st.columns([1.6, 1])
        with a:
            st.markdown(f"**{len(revealed)} line(s) disclosed · {hidden} kept private**")
            rows = []
            for rank, l in enumerate(sorted(revealed, key=lambda l: -l["value_cents"]), 1):
                why = ("Debt: all liabilities must be shown for a net-worth claim" if l["kind"] == "debt"
                       else f"Largest eligible line #{rank}: selected until the amount is covered"
                       if p["claim"]["type"] != "networth" or rank else "")
                rows.append({"Type": l["kind"].capitalize(), "Category": l["category"], "Value": l["value_cents"] / 100,
                             "Trust": TRUST_LEVEL[l["source"]], "Institution": "Hidden", "Why disclosed": why})
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                         column_config={"Value": st.column_config.NumberColumn(format="euro")})
        with b:
            private_share = max(0.0, 1 - shown_assets / gross)
            st.metric("Gross assets kept private", f"≈ {private_share:.0%}")
            st.caption("Prototype rule: largest eligible lines first, fewest lines possible. "
                       "Bank names and account labels are never disclosed, only category, value and trust level.")
    st.caption(f"Merkle root `{p['root'][:24]}…` · anchored in block #{p['anchor_block']} · nonce `{p['nonce']}`")


# ---------------------------------------------------------------------------
# Verify a proof (recipient side)
# ---------------------------------------------------------------------------
ATTACKS = {
    "none": "No attack (genuine proof)",
    "value": "Multiply a disclosed amount by 10",
    "duplicate": "Repeat the largest line 3 times",
    "reindex": "Re-label a line with another index",
    "audience": "Change the recipient",
}


def apply_attack(p: dict, kind: str) -> dict:
    p = copy.deepcopy(p)
    assets = [r for r in p["revealed"] if r["leaf"]["kind"] == "asset"]
    if kind == "value" and assets:
        assets[0]["leaf"]["value_cents"] *= 10
    elif kind == "duplicate" and assets:
        big = max(assets, key=lambda r: r["leaf"]["value_cents"])
        p["revealed"] += [copy.deepcopy(big), copy.deepcopy(big)]
    elif kind == "reindex" and assets:
        assets[0]["index"] = (assets[0]["index"] % (p["n_leaves"] - 1)) + 1 if p["n_leaves"] > 2 else 1
        if len({r["index"] for r in p["revealed"]}) == len(p["revealed"]):
            assets[0]["index"] = max(r["index"] for r in p["revealed"]) + 1
    elif kind == "audience":
        p["audience"] = "Another agency"
    return p


def page_verify():
    st.title("Verify a proof")
    st.write("For agencies, landlords and lenders. No account needed, and no need to read any JSON: "
             "enter the Proof ID printed on the certificate (or scan its QR code).")
    qp = st.query_params.get("proof", "")
    c1, c2 = st.columns(2)
    code = c1.text_input("Proof ID", qp or st.session_state.get("last_code", ""), placeholder="SOLVO-XXXXX-XXXXX-XXXXX-XXXXX").strip()
    org = c2.text_input("Your organisation", "", help="Checks the proof was issued to you and not reused elsewhere.")

    proof = None
    if code in app.proofs:
        proof = copy.deepcopy(app.proofs[code].proof)
    elif code:
        st.warning("Unknown Proof ID. You can paste a proof package under Technical details.")

    with st.expander("Technical details · raw proof package and forgery demo"):
        attack = st.selectbox("Simulate an attack before verifying", list(ATTACKS), format_func=ATTACKS.get)
        raw = st.text_area("Proof package (JSON)", json.dumps(proof, indent=2, ensure_ascii=False) if proof else "",
                           height=240, help="Edit any value, then verify: the forgery is rejected.")
        if raw.strip():
            try:
                proof = json.loads(raw)
            except json.JSONDecodeError:
                st.error("Invalid JSON.")
                proof = None
        if proof is not None and attack != "none":
            proof = apply_attack(proof, attack)

    if st.button("Verify", type="primary", disabled=proof is None, icon=":material/fact_check:"):
        res = app.verify(proof, org or None)
        anchored = any(c["check"] == "Anchored on SolvoChain" and c["status"] == "pass" for c in res["checks"])
        if anchored and isinstance(proof, dict):
            app.log_verification(proof.get("proof_id", ""), res["valid"], org)
        report(proof, res)


def report(proof, res):
    ev = res["eval"]
    if res["valid"]:
        cl = proof["claim"]
        st.markdown(
            f'<div class="result ok"><div class="title">Claim satisfied</div>'
            f'The presented proof satisfies the threshold: <b>{eur(ev["proven"])}</b> proven against '
            f'<b>{eur(ev["threshold"])}</b> required ({CLAIM_SHORT[cl["type"]].lower()}).<br>'
            f'<span class="muted">{res["attested_share"]:.0%} of the disclosed assets are covered by the issuer '
            f'attestation (simulated issuer); the rest is declared by the holder.</span></div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="result ko"><div class="title">Claim not verified</div>'
                    'At least one check failed. Do not rely on this document.</div>', unsafe_allow_html=True)
    st.markdown("**Verification report**")
    for c in res["checks"]:
        a, b, d = st.columns([2.6, 1.2, 5], vertical_alignment="center")
        a.write(c["check"])
        if c["status"] == "pass":
            b.badge("Passed", icon=":material/check_circle:", color="green")
        elif c["status"] == "info":
            b.badge("Info", icon=":material/info:", color="gray")
        else:
            b.badge("Failed", icon=":material/cancel:", color="red")
        d.caption(c["detail"])
    st.caption("Issuer signatures are real Ed25519 signatures, but the issuer is simulated: this is not a "
               "third-party financial certification.")


# ---------------------------------------------------------------------------
# Activity & Security (ledger explorer)
# ---------------------------------------------------------------------------
def page_activity():
    st.title("Activity & Security")
    ok, bad = app.chain.verify_integrity()
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Blocks", len(app.chain.blocks))
    k2.metric("Proofs issued", len(app.proofs))
    k3.metric("Users", len(SHARED["participants"]))
    k4.metric("Ledger integrity", "Valid" if ok else f"Broken at #{bad}")
    st.button("Refresh", icon=":material/refresh:")

    events = []
    for b, tx in app.chain.all_txs():
        events.append({"Event": EVENT_LABELS.get(tx["type"], tx["type"]), "Proof ID": tx.get("proof_id", ""),
                       "Block": b.index, "Time": fmt_ts(tx["ts"]), "Tx ID": tx["txid"], "Block hash": b.hash[:16] + "…"})
    ev_df = pd.DataFrame(events[::-1])
    st.markdown("**Events**")
    st.dataframe(ev_df, hide_index=True, width="stretch")

    st.markdown("**From financial data to verification**")
    st.markdown('<div class="flow"><span class="chip">Financial data (private, off-chain)</span><span class="arrow">→</span>'
                '<span class="chip">Salted hashes</span><span class="arrow">→</span><span class="chip">Merkle root</span>'
                '<span class="arrow">→</span><span class="chip">Ledger transaction</span><span class="arrow">→</span>'
                '<span class="chip">Verification</span></div>', unsafe_allow_html=True)
    mine = app.my_proofs() or app.proofs
    if mine:
        pid = st.selectbox("Inspect a proof", list(mine))
        p = mine[pid].proof
        blk, tx = app.chain.find_tx(lambda t: t["type"] == "ANCHOR_PROOF" and t.get("proof_id") == pid)
        a, b = st.columns(2)
        with a:
            st.markdown(f"**Off-chain:** {p['n_leaves']} committed leaves, {len(p['revealed']) - 1} disclosed")
            st.caption(f"Merkle root `{p['root']}`")
        with b:
            if tx:
                st.markdown("**On the ledger** (no balance, no bank name, no personal data):")
                st.json(tx)
    with st.expander("Open any transaction"):
        n = st.number_input("Block", 0, len(app.chain.blocks) - 1, len(app.chain.blocks) - 1)
        blk = app.chain.blocks[int(n)]
        st.caption(f"hash `{blk.hash}` · previous `{blk.prev_hash[:24]}…` · nonce {blk.nonce}")
        st.json(blk.txs)

    with st.expander("What is real and what is simulated"):
        st.dataframe(pd.DataFrame([
            ("SHA-256, salted commitments, Merkle tree + inclusion proofs", "Real"),
            ("Strict verification (unique lines, index-bound paths, debts, expiry, revocation, audience)", "Real"),
            ("Issuer attestation signature (Ed25519), bound to the Proof ID and Merkle root", "Real signature · simulated issuer"),
            ("Hash-chained ledger + toy proof-of-work (3 hex zeros)", "Simulation: educational, not Bitcoin-grade"),
            ("Decentralised consensus, smart contract on Base", "Not implemented"),
            ("Bank (PSD2) and wallet connections", "Simulated"),
            ("Zero-knowledge proofs", "Not implemented: Merkle commitments are not ZKPs"),
            ("User accounts, persistence, per-user data isolation", "Not implemented (prototype session state)"),
        ], columns=["Component", "Status"]), hide_index=True, width="stretch")
        st.caption("A hash chain makes edits detectable but does not guarantee immutability on its own: whoever controls "
                   "the whole ledger could rebuild it. In production only Merkle roots and issue/revoke events would be "
                   "anchored on a shared chain, never balances or personal data.")

    if not IS_ADMIN:
        return
    st.subheader("Attack simulation (presenter)")
    st.caption("Alters an anchored Merkle root as an attacker with database access would. "
               "Breaks the shared ledger for everyone until restored.")
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
        next(t for t in app.chain.blocks[idx].txs if t["type"] == "ANCHOR_PROOF")["root"] = root
        SHARED["tamper_backup"] = None
        st.rerun()
    if c.button("Reset ledger", icon=":material/restart_alt:"):
        with SHARED["lock"]:
            SHARED["chain"].__init__([SHARED["issuer"].info()])
            SHARED["proofs"].clear()
            SHARED["participants"].clear()
            SHARED["tamper_backup"] = None
        st.session_state.clear()
        st.session_state.admin = True
        st.rerun()


def page_live():
    st.title("Try Solvo on your phone")
    link = st.text_input("App link", base_url())
    a, b = st.columns([1, 1.2], gap="large")
    with a:
        if link:
            buf = io.BytesIO()
            qrcode.make(link, box_size=12, border=2).save(buf, format="PNG")
            st.image(buf.getvalue(), width=380)
    with b:
        st.markdown("1. Scan the code and enter your first name\n2. Open **Proof Center** and generate a certificate\n"
                    "3. Your neighbour scans the certificate QR or types its Proof ID in **Verify a proof**\n"
                    "4. Try to cheat: pick an attack under *Technical details*")
        k1, k2 = st.columns(2)
        k1.metric("Users connected", len(SHARED["participants"]))
        k2.metric("Proofs issued", len(app.proofs))
        st.button("Refresh", key="refresh_live", icon=":material/refresh:")


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------
verify_page = st.Page(page_verify, title="Verify a Proof", icon=":material/fact_check:", url_path="verify",
                      default=not IS_HOLDER)
if IS_HOLDER:
    pages = {"Holder": [
        st.Page(page_overview, title="Overview", icon=":material/monitoring:", url_path="overview", default=True),
        st.Page(page_assets, title="My Assets", icon=":material/account_balance_wallet:", url_path="assets"),
        st.Page(page_proof, title="Proof Center", icon=":material/verified_user:", url_path="prove"),
    ], "Recipient": [verify_page],
        "Network": [st.Page(page_activity, title="Activity & Security", icon=":material/deployed_code:", url_path="activity")]}
    if IS_ADMIN:
        pages["Presentation"] = [st.Page(page_live, title="Live demo", icon=":material/qr_code_2:", url_path="live")]
else:
    pages = {"Recipient": [verify_page],
             "Network": [st.Page(page_activity, title="Activity & Security", icon=":material/deployed_code:", url_path="activity")]}
nav = st.navigation(pages)

with st.sidebar:
    if IS_HOLDER:
        t = totals(app.assets)
        with st.container(border=True):
            st.caption(f"Signed in as **{app.user}**")
            st.markdown(f"**{eur(t['net_worth'])}** net worth")
            st.caption(f"ID `{app.owner[:12]}…`")
        if st.button("Reset my demo data", icon=":material/restart_alt:", width="stretch"):
            start_holder(app.user)
            st.session_state.pop("last_code", None)
            st.rerun()
    else:
        st.caption("Recipient mode · no account needed")
        if st.button("Back to start", icon=":material/arrow_back:", width="stretch"):
            st.session_state.pop("app", None)
            st.rerun()
    st.caption("Prototype · bank, wallet and issuer are simulated")

nav.run()
