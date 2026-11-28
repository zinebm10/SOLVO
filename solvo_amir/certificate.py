"""certificate.py - PDF "Solvo Financial Credential" (reportlab + qrcode)."""
import io

import qrcode
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from solvo_core import CLAIM_SHORT, claim_threshold, fmt_date, fmt_ts

NAVY = colors.HexColor("#0F1B3D")
BLUE = colors.HexColor("#2A5BD7")
GREEN = colors.HexColor("#137A5C")
GREY = colors.HexColor("#5B6475")
LIGHT = colors.HexColor("#F4F6FA")


def eur(x: float) -> str:
    return "EUR " + f"{x:,.0f}".replace(",", " ")


def build_certificate_pdf(proof: dict, result: dict, revoked: bool, verify_url: str) -> bytes:
    """`result` is the output of verify_proof() for this proof (used for the status lines)."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    m = 18 * 28.35 / 10  # ~51pt margin

    # header band
    c.setFillColor(NAVY); c.rect(0, H - 90, W, 90, fill=1, stroke=0)
    c.setFillColor(colors.white); c.setFont("Helvetica-Bold", 26); c.drawString(m, H - 52, "solvo")
    c.setFillColor(BLUE); c.circle(m + 74, H - 52, 3.2, fill=1, stroke=0)
    c.setFillColor(colors.white); c.setFont("Helvetica", 9)
    c.drawRightString(W - m, H - 45, "SOLVO FINANCIAL CREDENTIAL")
    c.drawRightString(W - m, H - 58, "DEMO CERTIFICATE - illustrative")

    ev = result.get("eval") or {}
    ok = result.get("valid", False) and not revoked
    y = H - 135
    c.setFillColor(GREEN if ok else colors.HexColor("#A93226")); c.setFont("Helvetica-Bold", 10)
    c.drawString(m, y, "VERIFIED CLAIM" if ok else ("REVOKED" if revoked else "NOT VALID"))
    c.setFillColor(GREY); c.setFont("Helvetica", 9)
    c.drawString(m, y - 14, CLAIM_SHORT[proof["claim"]["type"]].upper())

    thr = claim_threshold(proof["claim"])
    what = "NET WORTH" if proof["claim"]["type"] == "networth" else "ELIGIBLE LIQUID ASSETS"
    c.setFillColor(GREY); c.setFont("Helvetica", 9); c.drawString(m, y - 48, f"PROVEN: {what} OF AT LEAST")
    c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 34)
    c.drawString(m, y - 84, eur(thr))

    # key facts grid
    facts = [("Disclosed value counted", eur(ev.get("proven", 0.0))), ("Issued to", proof["audience"]),
             ("Created", fmt_date(proof["created_at"])), ("Valid until", fmt_date(proof["expires_at"]))]
    gy = y - 130
    for i, (k, v) in enumerate(facts):
        x = m + (i % 2) * 250
        yy = gy - (i // 2) * 42
        c.setFillColor(GREY); c.setFont("Helvetica", 8); c.drawString(x, yy, k.upper())
        c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 12); c.drawString(x, yy - 16, v[:34])

    # status table
    by = {ch["check"]: ch for ch in result.get("checks", [])}

    def st(name, label_ok="Passed"):
        ch = by.get(name)
        if ch is None:
            return "n/a"
        return label_ok if ch["status"] == "pass" else ("Info" if ch["status"] == "info" else "FAILED")

    att = proof.get("attestation")
    rows = [
        ("Cryptographic integrity", "Passed" if result.get("valid") else "FAILED"),
        ("Issuer attestation", "Simulated issuer (Ed25519 signature " + st("Issuer signature", "valid") + ")" if att else "None (self-declared data)"),
        ("Ledger anchoring", "Demo chain, block #%s" % proof.get("anchor_block", "?")),
        ("Revocation status", "Revoked" if revoked else "Not revoked at issue time"),
        ("Privacy", "Full disclosure" if len(proof["revealed"]) == proof["n_leaves"] else "Limited disclosure (minimum needed)"),
    ]
    ty = gy - 110
    c.setFillColor(LIGHT); c.roundRect(m, ty - 100, W - 2 * m, 112, 8, fill=1, stroke=0)
    for i, (k, v) in enumerate(rows):
        c.setFillColor(GREY); c.setFont("Helvetica", 9); c.drawString(m + 14, ty - 8 - i * 20, k)
        c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 9); c.drawString(m + 190, ty - 8 - i * 20, v)

    # disclosed / hidden summary
    rev = [r["leaf"] for r in proof["revealed"] if r["leaf"]["kind"] != "header"]
    hidden = proof["n_leaves"] - 1 - len(rev)
    sy = ty - 140
    c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 10)
    c.drawString(m, sy, f"Disclosed: {len(rev)} line(s)   -   Kept private: {hidden} line(s)")
    c.setFont("Helvetica", 9); c.setFillColor(GREY)
    for i, l in enumerate(rev[:8]):
        c.drawString(m + 8, sy - 18 - i * 14,
                     f"{l['kind'].capitalize():6} {l['category']:16} {eur(l['value_cents'] / 100):>14}   institution & label: hidden")

    # QR + references
    qy = 190
    qr = qrcode.make(verify_url, box_size=6, border=1)
    qb = io.BytesIO(); qr.save(qb, format="PNG"); qb.seek(0)
    c.drawImage(ImageReader(qb), W - m - 110, qy - 20, 110, 110)
    c.setFillColor(GREY); c.setFont("Helvetica", 8)
    c.drawRightString(W - m, qy - 30, "Scan to verify")
    c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 9); c.drawString(m, qy + 70, "PROOF ID")
    c.setFont("Courier-Bold", 13); c.drawString(m, qy + 52, proof["proof_id"])
    c.setFont("Helvetica", 8); c.setFillColor(GREY)
    c.drawString(m, qy + 30, "Merkle root: " + proof["root"][:48] + "...")
    c.drawString(m, qy + 18, "Nonce: " + proof["nonce"] + "   Holder ID: " + proof["owner"][:14] + "...")
    c.drawString(m, qy + 6, "Verify at: " + verify_url[:80])

    # disclaimer
    c.setFillColor(GREY); c.setFont("Helvetica-Oblique", 7.5)
    for i, line in enumerate([
        "Illustrative certificate. Bank / wallet data and the issuer are simulated in this prototype; the ledger is a demo chain.",
        "A demo-chain result is not an independent third-party financial certification. Solvo is not a rent guarantor",
        "nor a regulated credit assessment: it helps verify information, subject to the reliability of its sources.",
    ]):
        c.drawString(m, 70 - i * 10, line)
    c.setStrokeColor(LIGHT); c.line(m, 88, W - m, 88)
    c.showPage(); c.save()
    return buf.getvalue()
