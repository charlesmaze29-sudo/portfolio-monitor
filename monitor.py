"""
Portfolio Monitor — Charles
- Récap quotidien à 20h Paris
- Alertes intraday si variation > seuil
- Yahoo Finance pour les prix (gratuit)
- Claude Haiku pour la synthèse narrative (< $0.30/mois)
- Envoi email via Gmail SMTP
"""

import json
import os
import smtplib
import sys
from datetime import datetime, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import anthropic
import yfinance as yf

# ── Config ──────────────────────────────────────────────────────────────────

PORTFOLIO_FILE = "portfolio.json"
PARIS_TZ = ZoneInfo("Europe/Paris")

# Secrets injectés via GitHub Actions secrets (jamais dans le code)
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GMAIL_USER       = os.environ["GMAIL_USER"]
GMAIL_APP_PWD    = os.environ["GMAIL_APP_PASSWORD"]   # App Password Gmail, pas le mdp principal
EMAIL_RECIPIENT  = os.environ.get("EMAIL_RECIPIENT", GMAIL_USER)

# Modèle haiku = ~$0.0008/1K tokens input, $0.0040/1K output — très économique
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS   = 1200   # suffisant pour un récap structuré, limite le coût


# ── Chargement portefeuille ──────────────────────────────────────────────────

def load_portfolio():
    with open(PORTFOLIO_FILE) as f:
        return json.load(f)


# ── Récupération des prix ────────────────────────────────────────────────────

def fetch_prices(positions: list) -> dict:
    """
    Récupère cours actuel + variation J via yfinance.
    Retourne dict {ticker: {price, change_pct, prev_close, currency, name}}
    """
    tickers = [p["ticker"] for p in positions]
    results = {}

    # Batch download — 1 seul appel réseau pour tous les tickers
    data = yf.download(
        tickers,
        period="2d",
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        progress=False,
        threads=True,
    )

    for pos in positions:
        t = pos["ticker"]
        try:
            if len(tickers) == 1:
                closes = data["Close"]
            else:
                closes = data["Close"][t] if t in data["Close"].columns else None

            if closes is None or len(closes) < 2:
                # Fallback : appel individuel
                info = yf.Ticker(t).fast_info
                price = info.last_price
                prev  = info.previous_close
            else:
                vals  = closes.dropna()
                price = float(vals.iloc[-1])
                prev  = float(vals.iloc[-2])

            change_pct = ((price - prev) / prev * 100) if prev else 0.0

            results[t] = {
                "name":       pos["name"],
                "price":      price,
                "prev_close": prev,
                "change_pct": change_pct,
                "currency":   yf.Ticker(t).fast_info.currency or "USD",
                "qty":        pos["qty"],
                "pru_eur":    pos["pru_eur"],
                "envelope":   pos["envelope"],
                "sleeve":     pos["sleeve"],
                "notes":      pos.get("notes", ""),
                "threshold":  pos.get("alert_threshold_override",
                                      None),  # None = on utilisera le défaut config
            }
        except Exception as e:
            print(f"[WARN] Prix non disponible pour {t}: {e}", file=sys.stderr)

    return results


# ── Détection des variations fortes ─────────────────────────────────────────

def detect_movers(prices: dict, default_threshold: float) -> list:
    """Retourne les positions avec |variation| > seuil."""
    movers = []
    for ticker, d in prices.items():
        threshold = d["threshold"] if d["threshold"] else default_threshold
        if abs(d["change_pct"]) >= threshold:
            movers.append({
                "ticker":     ticker,
                "name":       d["name"],
                "change_pct": d["change_pct"],
                "price":      d["price"],
                "sleeve":     d["sleeve"],
                "notes":      d["notes"],
                "threshold":  threshold,
            })
    movers.sort(key=lambda x: abs(x["change_pct"]), reverse=True)
    return movers


# ── Appel Claude (synthèse narrative) ────────────────────────────────────────

def call_claude(prompt: str) -> str:
    """Appel Claude Haiku — optimisé coût."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


def build_daily_prompt(prices: dict, movers: list, portfolio: dict) -> str:
    today = datetime.now(PARIS_TZ).strftime("%d/%m/%Y")
    patrimoine = portfolio["config"]["patrimoine_total_eur"]

    # Résumé compact des positions pour limiter les tokens
    positions_summary = []
    for t, d in prices.items():
        sign = "▲" if d["change_pct"] > 0 else "▼" if d["change_pct"] < 0 else "—"
        positions_summary.append(
            f"- {d['name']} ({t}): {sign}{abs(d['change_pct']):.1f}% | "
            f"cours {d['price']:.2f} | sleeve: {d['sleeve']}"
        )

    movers_summary = ""
    if movers:
        movers_summary = "\nMOUVEMENTS FORTS DU JOUR (> seuil):\n"
        for m in movers:
            movers_summary += f"- {m['name']} ({m['ticker']}): {m['change_pct']:+.1f}% ⚠️\n"
            if m["notes"]:
                movers_summary += f"  → Note: {m['notes']}\n"

    # Catalyseurs proches
    upcoming = []
    today_dt = date.today()
    for cat in portfolio.get("catalysts", []):
        try:
            cat_dt = date.fromisoformat(cat["date"])
            days_ahead = (cat_dt - today_dt).days
            if 0 <= days_ahead <= 7:
                upcoming.append(f"- {cat['date']}: {cat['ticker']} — {cat['event']}")
        except Exception:
            pass
    catalysts_block = ("\nCATALYSEURS DANS LES 7 JOURS:\n" + "\n".join(upcoming)) if upcoming else ""

    prompt = f"""Tu es l'assistant financier personnel de Charles, investisseur avec un patrimoine ~{patrimoine:,}€.
Date: {today}

VARIATIONS DU PORTEFEUILLE AUJOURD'HUI:
{chr(10).join(positions_summary)}
{movers_summary}{catalysts_block}

Produis un récap de soirée CONCIS en français, structuré en 3 blocs:

1. **RÉSUMÉ EN 2 PHRASES** — l'essentiel de la journée pour le portefeuille de Charles
2. **POINTS D'ATTENTION** — uniquement les lignes avec variation notable ou catalyseur proche (bullet points, max 5)
3. **ACTION REQUISE ?** — une seule ligne: oui/non et pourquoi (ex: vérifier stop Micron, renforcer si conditions remplies, etc.)

Sois direct, précis, sans rembourrage. Pas de disclaimer. Ton: analyste senior qui parle à un pair."""

    return prompt


def build_alert_prompt(movers: list) -> str:
    lines = []
    for m in movers:
        lines.append(
            f"- {m['name']} ({m['ticker']}): {m['change_pct']:+.1f}% "
            f"(seuil: {m['threshold']}%) | sleeve: {m['sleeve']}"
        )
        if m["notes"]:
            lines.append(f"  Contexte: {m['notes']}")

    prompt = f"""Alerte portefeuille Charles — {datetime.now(PARIS_TZ).strftime('%d/%m/%Y %H:%M')}

Variations dépassant les seuils d'alerte:
{chr(10).join(lines)}

En 3-4 phrases maximum:
1. Quelle est la cause probable de ce mouvement ?
2. Est-ce que ça déclenche une action concrète (stop, trim, renforcement) ?
3. Urgence: URGENT / SURVEILLER / INFO

Réponse directe, pas de disclaimer."""

    return prompt


# ── Construction des emails HTML ─────────────────────────────────────────────

def build_email_html(subject_type: str, narrative: str, prices: dict,
                     movers: list, portfolio: dict) -> tuple[str, str]:
    """Retourne (sujet, html)."""
    now = datetime.now(PARIS_TZ)
    date_str = now.strftime("%d/%m/%Y %H:%M")

    # Couleurs
    green  = "#22c55e"
    red    = "#ef4444"
    amber  = "#f59e0b"
    bg     = "#0f172a"
    card   = "#1e293b"
    border = "#334155"
    text   = "#e2e8f0"
    muted  = "#94a3b8"

    # Tableau positions (top movers en tête)
    all_pos = sorted(prices.values(), key=lambda x: abs(x["change_pct"]), reverse=True)
    rows = ""
    for d in all_pos:
        chg = d["change_pct"]
        color = green if chg > 0 else red if chg < 0 else muted
        flag = " ⚠️" if abs(chg) >= (d["threshold"] if d["threshold"] else
                                       portfolio["config"]["alert_threshold_pct"]) else ""
        # Valeur position en cours
        val_eur = d["qty"] * d["price"]  # approximation — currency mix
        rows += f"""
        <tr>
          <td style="padding:6px 10px;color:{text};font-weight:500">{d['name']}</td>
          <td style="padding:6px 10px;color:{muted};font-size:12px">{d['sleeve']}</td>
          <td style="padding:6px 10px;color:{color};font-weight:700;text-align:right">
            {chg:+.1f}%{flag}
          </td>
          <td style="padding:6px 10px;color:{muted};text-align:right">{d['price']:.2f}</td>
          <td style="padding:6px 10px;color:{muted};text-align:right">{d['envelope']}</td>
        </tr>"""

    # Narrative formatée (markdown basique → HTML)
    narrative_html = narrative.replace("\n", "<br>")
    for marker in ["**RÉSUMÉ", "**POINTS", "**ACTION"]:
        narrative_html = narrative_html.replace(
            marker, f'<span style="color:{amber};font-weight:700">{marker}')
    narrative_html = narrative_html.replace("**", "</span>")

    if subject_type == "daily":
        subject = f"📊 Portefeuille — Récap {now.strftime('%d/%m/%Y')}"
        badge_color = "#3b82f6"
        badge_text = "RÉCAP QUOTIDIEN"
    else:
        worst = movers[0] if movers else {}
        subject = f"⚠️ Alerte {worst.get('name','')}: {worst.get('change_pct',0):+.1f}% — {now.strftime('%H:%M')}"
        badge_color = "#ef4444"
        badge_text = f"ALERTE — {len(movers)} VALEUR{'S' if len(movers)>1 else ''}"

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body style="margin:0;padding:0;background:{bg};font-family:'Segoe UI',Arial,sans-serif;color:{text}">
  <div style="max-width:680px;margin:0 auto;padding:24px 16px">

    <!-- Header -->
    <div style="display:flex;align-items:center;justify-content:space-between;
                border-bottom:1px solid {border};padding-bottom:16px;margin-bottom:24px">
      <div>
        <div style="font-size:11px;letter-spacing:2px;color:{muted};text-transform:uppercase
                    ;margin-bottom:4px">{badge_text}</div>
        <div style="font-size:22px;font-weight:700;color:{text}">{now.strftime('%d %B %Y')}</div>
        <div style="font-size:12px;color:{muted}">{date_str} (heure Paris)</div>
      </div>
      <div style="background:{badge_color};color:white;padding:6px 14px;border-radius:6px;
                  font-size:12px;font-weight:700">CHARLES</div>
    </div>

    <!-- Narrative Claude -->
    <div style="background:{card};border:1px solid {border};border-radius:10px;
                padding:20px;margin-bottom:24px;line-height:1.7;font-size:14px">
      {narrative_html}
    </div>

    <!-- Tableau positions -->
    <div style="font-size:11px;color:{muted};text-transform:uppercase;letter-spacing:1px;
                margin-bottom:10px">Variations du jour</div>
    <div style="background:{card};border:1px solid {border};border-radius:10px;overflow:hidden;
                margin-bottom:24px">
      <table style="width:100%;border-collapse:collapse">
        <thead>
          <tr style="background:#263348;font-size:11px;color:{muted};text-transform:uppercase">
            <th style="padding:8px 10px;text-align:left">Valeur</th>
            <th style="padding:8px 10px;text-align:left">Sleeve</th>
            <th style="padding:8px 10px;text-align:right">Var. J</th>
            <th style="padding:8px 10px;text-align:right">Cours</th>
            <th style="padding:8px 10px;text-align:right">Enveloppe</th>
          </tr>
        </thead>
        <tbody>
          {rows}
        </tbody>
      </table>
    </div>

    <!-- Footer -->
    <div style="font-size:11px;color:{muted};text-align:center;
                border-top:1px solid {border};padding-top:16px">
      Généré automatiquement par Portfolio Monitor · Claude Haiku API ·
      <a href="https://github.com" style="color:{muted}">modifier les alertes</a>
    </div>

  </div>
</body>
</html>"""

    return subject, html


# ── Envoi email ──────────────────────────────────────────────────────────────

def send_email(subject: str, html: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = EMAIL_RECIPIENT
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PWD)
        server.sendmail(GMAIL_USER, EMAIL_RECIPIENT, msg.as_string())

    print(f"[OK] Email envoyé: {subject}")


# ── Points d'entrée principaux ───────────────────────────────────────────────

def run_daily():
    """Récap quotidien complet — lancé à 20h Paris."""
    print("[daily] Démarrage récap quotidien...")
    portfolio = load_portfolio()
    prices    = fetch_prices(portfolio["positions"])
    movers    = detect_movers(prices, portfolio["config"]["alert_threshold_pct"])

    print(f"[daily] {len(prices)} positions récupérées, {len(movers)} movers")

    prompt    = build_daily_prompt(prices, movers, portfolio)
    narrative = call_claude(prompt)

    subject, html = build_email_html("daily", narrative, prices, movers, portfolio)
    send_email(subject, html)
    print("[daily] Terminé.")


def run_alert():
    """Check intraday — envoi email seulement si variation > seuil."""
    print("[alert] Vérification intraday...")
    portfolio = load_portfolio()
    prices    = fetch_prices(portfolio["positions"])
    movers    = detect_movers(prices, portfolio["config"]["alert_threshold_pct"])

    if not movers:
        print("[alert] Aucun mover — pas d'email.")
        return

    print(f"[alert] {len(movers)} mover(s) détecté(s) — génération email...")
    prompt    = build_alert_prompt(movers)
    narrative = call_claude(prompt)

    subject, html = build_email_html("alert", narrative, prices, movers, portfolio)
    send_email(subject, html)
    print("[alert] Terminé.")


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "daily"
    if mode == "daily":
        run_daily()
    elif mode == "alert":
        run_alert()
    else:
        print(f"Usage: python monitor.py [daily|alert]", file=sys.stderr)
        sys.exit(1)
