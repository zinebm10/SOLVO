# Solvo: financial trust, without financial exposure

NEOMA MSc Fintech & DeFi, Final Group Project. Streamlit prototype.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

Then open http://localhost:8501. Presenter mode (attack simulation, reset, phone QR page):
http://localhost:8501/?admin=1. Set the environment variable `SOLVO_ADMIN_KEY` to change the key.

Run the tests: `python test_core.py` (20 tests: honest proofs, forgeries, revocation, expiry, data minimisation).

## Files

| File | Role |
|---|---|
| `app.py` | Streamlit interface (holder, recipient, ledger explorer) |
| `solvo_core.py` | Business logic and cryptography (no Streamlit code) |
| `certificate.py` | PDF certificate with QR code |
| `test_core.py` | Test suite |
| `.streamlit/config.toml` | Theme |

## Real vs simulated

| Component | Status |
|---|---|
| SHA-256 salted commitments, Merkle tree, inclusion proofs | Real |
| Data-minimising selection of disclosed lines | Real |
| Ed25519 issuer signature bound to Proof ID and Merkle root | Real signature, simulated issuer (demo key) |
| Hash-chained ledger with toy proof-of-work | Real hash chain, single node, in memory: not a blockchain |
| Bank (PSD2) and wallet connections | Simulated (demo data) |
| Zero-knowledge proofs | Not implemented (Merkle selective disclosure reveals exact values of disclosed lines) |

## Demo account

Type the first name **Camille** to get the reference portfolio used in the pitch
(any other name gets a randomised portfolio).

The ledger lives in server memory: if the Streamlit Cloud app restarts, all proofs are lost.
Always create the demo proof live, a few minutes before or during the presentation.
