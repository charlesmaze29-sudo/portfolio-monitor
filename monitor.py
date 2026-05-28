"""
Portfolio Monitor — Charles
- Récap quotidien à 20h Paris (Claude Sonnet — qualité analytique)
- Alertes intraday si variation > seuil (Claude Haiku — coût minimal)
- Récap mensuel le dernier jour du mois à 20h30 (Claude Sonnet)
- Yahoo Finance pour les prix + taux EUR/USD en temps réel
- Variation totale PTF en € par enveloppe et par sleeve
- Envoi email HTML via Gmail SMTP
"""

import json
import os
import smtplib
import sys
from calendar import monthrange
from datetime import datetime, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import anthropic
import yfinance as yf

# ── Config ───────────────────────────────────────────────────────────────────

PORTFOLIO_FILE  = "portfolio.json"
BASELINE_FILE   = "monthly_baseline.json"
PARIS_TZ        = ZoneInfo("Europe/Paris")

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GMAIL_USER        = os.environ["GMAIL_USER"]
GMAIL_APP_PWD     = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_RECIPIENT   = os.environ.get("EMAIL_RECIPIENT", GMAIL_USER)

# Modèles — hybride qualité/coût
MODEL_SONNET = "claude-sonnet-4-6"   # daily + monthly (~$0.55/mois)
MODEL_HAIKU  = "claude-haiku-4-5-20251001"  # alertes intraday (~$0.05/mois)
MAX_TOKENS   = 1800


# ── Chargement portefeuille ──────────────────────────────────────────────────

def load_portfolio():
    with open(PORTFOLIO_FILE) as f:
        return json.load(f)

def load_baseline():
    if os.path.exists(BASELINE_FILE):
        with open(BASELINE_FILE) as f:
            return json.load(f)
    return {}

def save_baseline(data: dict):
    with open(BASELINE_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── Taux de change EUR/USD ───────────────────────────────────────────────────

def fetch_eurusd() -> float:
    """Taux EUR/USD en temps réel via Yahoo Finance."""
    try:
        data = yf.download("EURUSD=X", period="2d", interval="1d",
                           progress=False, auto_adjust=True)
        if not data.empty:
            close = data["Close"]
            # Selon la version de yfinance, Close peut être Series ou DataFrame
            if hasattr(close, "iloc"):
                val = close.iloc[-1]
                # Si c'est encore une Series (MultiIndex), prendre le premier élément
                if hasattr(val, "iloc"):
                    val = float(val.iloc[0])
                return float(val)
    except Exception as e:
        print(f"[WARN] Taux EUR/USD non disponible: {e}", file=sys.stderr)
    return 1.08  # fallback raisonnable


# ── Récupération des prix ────────────────────────────────────────────────────

def fetch_prices(positions: list, eurusd: float) -> dict:
    """
    Récupère cours actuel + variation J via yfinance.
    Calcule variation en € en convertissant USD→EUR au taux du jour.
    Utilise des appels individuels pour éviter que les tickers invalides
    ne cassent le batch entier.
    """
    results = {}

    for pos in positions:
        t = pos["ticker"]
        try:
            data = yf.download(
                t,
                period="2d",
                interval="1d",
                auto_adjust=True,
                progress=False,
            )

            if data.empty or len(data) < 2:
                # Fallback fast_info
                info  = yf.Ticker(t).fast_info
                price = float(info.last_price)
                prev  = float(info.previous_close)
            else:
                # Close peut être Series ou DataFrame selon la version yfinance
                close = data["Close"]
                if hasattr(close, "columns"):
                    # DataFrame multi-colonnes → prendre la première
                    close = close.iloc[:, 0]
                vals  = close.dropna()
                if len(vals) < 2:
                    raise ValueError("Pas assez de données")
                price = float(vals.iloc[-1])
                prev  = float(vals.iloc[-2])

            change_pct = ((price - prev) / prev * 100) if prev else 0.0

            ticker_info = yf.Ticker(t).fast_info
            currency    = getattr(ticker_info, "currency", None) or "USD"

            fx         = (1 / eurusd) if currency == "USD" else 1.0
            price_eur  = price * fx
            prev_eur   = prev  * fx
            change_eur = (price_eur - prev_eur) * pos["qty"]

            results[t] = {
                "name":        pos["name"],
                "price":       price,
                "price_eur":   price_eur,
                "prev_close":  prev,
                "change_pct":  change_pct,
                "change_eur":  change_eur,
                "currency":    currency,
                "qty":         pos["qty"],
                "pru_eur":     pos["pru_eur"],
                "valeur_eur":  price_eur * pos["qty"],
                "envelope":    pos["envelope"],
                "sleeve":      pos["sleeve"],
                "notes":       pos.get("notes", ""),
                "threshold":   pos.get("alert_threshold_override", None),
            }
            print(f"[OK] {t}: {price:.2f} {currency} ({change_pct:+.1f}%)")

        except Exception as e:
            print(f"[WARN] Prix non disponible pour {t}: {e}", file=sys.stderr)

    return results


# ── Agrégation par enveloppe et sleeve ──────────────────────────────────────

def aggregate(prices: dict) -> dict:
    """Calcule variation €/jour par enveloppe et par sleeve."""
    by_envelope = {}
    by_sleeve   = {}
    total_change_eur = 0.0
    total_valeur_eur = 0.0

    for d in prices.values():
        env = d["envelope"]
        slv = d["sleeve"]
        chg = d["change_eur"]
        val = d["valeur_eur"]

        by_envelope[env] = by_envelope.get(env, 0.0) + chg
        by_sleeve[slv]   = by_sleeve.get(slv, 0.0)   + chg
        total_change_eur += chg
        total_valeur_eur += val

    return {
        "total_change_eur": total_change_eur,
        "total_valeur_eur": total_valeur_eur,
        "by_envelope":      dict(sorted(by_envelope.items(), key=lambda x: x[1])),
        "by_sleeve":        dict(sorted(by_sleeve.items(),   key=lambda x: x[1])),
    }


# ── Calcul performance mensuelle ────────────────────────────────────────────

def compute_monthly_perf(prices: dict, baseline: dict) -> dict:
    """Compare cours actuels vs baseline début de mois."""
    perfs = {}
    total_gain = 0.0
    total_base = 0.0

    for ticker, d in prices.items():
        if ticker in baseline:
            base_price_eur = baseline[ticker]["price_eur"]
            curr_price_eur = d["price_eur"]
            qty            = d["qty"]
            gain_eur       = (curr_price_eur - base_price_eur) * qty
            base_val       = base_price_eur * qty
            pct            = ((curr_price_eur / base_price_eur) - 1) * 100 if base_price_eur else 0
            perfs[ticker]  = {
                "name":      d["name"],
                "gain_eur":  gain_eur,
                "pct":       pct,
                "sleeve":    d["sleeve"],
            }
            total_gain += gain_eur
            total_base += base_val

    total_pct = (total_gain / total_base * 100) if total_base else 0
    return {"positions": perfs, "total_gain": total_gain, "total_pct": total_pct}


# ── Appels Claude ────────────────────────────────────────────────────────────

def call_claude(prompt: str, model: str, use_web_search: bool = False) -> str:
    """
    Appel Claude avec web search optionnel.
    Web search actif pour daily/monthly (Sonnet) — acces aux news du jour.
    Desactive pour alertes Haiku (cout + vitesse).
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    kwargs = {
        "model":      model,
        "max_tokens": MAX_TOKENS,
        "messages":   [{"role": "user", "content": prompt}],
    }
    if use_web_search:
        kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]

    msg = client.messages.create(**kwargs)
    text_parts = [block.text for block in msg.content if hasattr(block, "text")]
    return "\n".join(text_parts)


def build_daily_prompt(prices: dict, movers: list, agg: dict, portfolio: dict) -> str:
    today      = datetime.now(PARIS_TZ).strftime("%d/%m/%Y")
    patrimoine = portfolio["config"]["patrimoine_total_eur"]
    total_chg  = agg["total_change_eur"]
    total_val  = agg["total_valeur_eur"]
    sign_total = "+" if total_chg >= 0 else ""
    chg_pct    = total_chg / total_val * 100 if total_val else 0.0

    variation_bloc = f"VARIATION TOTALE: {sign_total}{total_chg:,.0f} EUR ({sign_total}{chg_pct:.2f}%)\n\n"
    variation_bloc += "Par enveloppe:\n"
    for env, chg in agg["by_envelope"].items():
        s = "+" if chg >= 0 else ""
        variation_bloc += f"  {env}: {s}{chg:,.0f} EUR\n"
    variation_bloc += "\nPar sleeve:\n"
    for slv, chg in agg["by_sleeve"].items():
        s = "+" if chg >= 0 else ""
        variation_bloc += f"  {slv}: {s}{chg:,.0f} EUR\n"

    positions_lines = []
    for t, d in sorted(prices.items(), key=lambda x: x[1]["change_pct"], reverse=True):
        sign = "+" if d["change_pct"] > 0 else ""
        positions_lines.append(
            f"- {d['name']} ({t}): {sign}{d['change_pct']:.1f}% | "
            f"{sign}{d['change_eur']:,.0f} EUR | cours {d['price']:.2f} {d['currency']} | {d['sleeve']}"
        )

    movers_bloc = ""
    if movers:
        movers_bloc = "\nMOUVEMENTS > SEUIL D'ALERTE:\n"
        for m in movers:
            movers_bloc += f"- {m['name']} ({m['ticker']}): {m['change_pct']:+.1f}% ALERTE"
            if m.get("notes"):
                movers_bloc += f" -- {m['notes']}"
            movers_bloc += "\n"

    upcoming = []
    today_dt = date.today()
    for cat in portfolio.get("catalysts", []):
        try:
            days_ahead = (date.fromisoformat(cat["date"]) - today_dt).days
            if 0 <= days_ahead <= 7:
                upcoming.append(f"- J+{days_ahead}: {cat['ticker']} -- {cat['event']}")
        except Exception:
            pass
    catalysts_bloc = ("\nCATALYSEURS DANS LES 7 JOURS:\n" + "\n".join(upcoming)) if upcoming else ""

    big_movers = [
        f"{d['name']} ({t}, {d['change_pct']:+.1f}%)"
        for t, d in prices.items()
        if abs(d["change_pct"]) >= 3.0
    ]
    big_movers_str = ", ".join(big_movers) if big_movers else "aucune variation > 3%"

    return f"""Tu es l'analyste financier personnel de Charles. Patrimoine ~{patrimoine:,} EUR, horizon 5+ ans, fortement expose chaine de valeur IA.
Date: {today}

{variation_bloc}
POSITIONS DU JOUR (tri decroissant par variation):
{chr(10).join(positions_lines)}
{movers_bloc}{catalysts_bloc}

VARIATIONS > 3% A INVESTIGUER: {big_movers_str}

INSTRUCTIONS — utilise le web search pour:
1. Identifier la cause precise de chaque mouvement > 3% (news du jour, annonces, macro)
2. Pour chaque grosse variation: contexte des 3-5 derniers jours (continuation? retournement? gap?)
3. Contexte macro du jour pertinent pour ce portefeuille

Produis le recap en francais, 4 blocs:

**BILAN DU JOUR**
Variation totale en EUR et %, 2 phrases sur ce qui a domine avec les vrais chiffres.

**POINTS D'ATTENTION**
Max 6 bullets. Pour chaque ligne notable:
- Variation du jour + cause precise (via web search)
- Contexte des derniers jours
- Implication concrete pour le portefeuille (stops, sizing, these)

**CONTEXTE DE MARCHE**
2-3 phrases sur le macro/sectoriel du jour. Lien direct avec les expositions de Charles.

**ACTION REQUISE ?**
OUI ou NON, une ligne tranchee. Si OUI: action precise sur quelle ligne et pourquoi maintenant.

Ton: analyste senior. Direct, chiffre, source. Zero rembourrage."""


def build_alert_prompt(movers: list) -> str:
    lines = []
    for m in movers:
        lines.append(
            f"- {m['name']} ({m['ticker']}): {m['change_pct']:+.1f}% "
            f"(seuil: {m['threshold']}%) | {m['change_eur']:+,.0f} EUR | {m['sleeve']}"
        )
        if m.get("notes"):
            lines.append(f"  Note: {m['notes']}")

    return f"""Alerte portefeuille Charles -- {datetime.now(PARIS_TZ).strftime('%d/%m/%Y %H:%M')}

Variations depassant les seuils:
{chr(10).join(lines)}

3-4 phrases:
1. Cause probable (news, macro, technique)
2. Action concrete declenchee ? (stop, trim, renforcement, rien)
3. Urgence: URGENT / SURVEILLER / INFO

Direct."""


# ── Construction emails HTML ─────────────────────────────────────────────────

def build_email_html(email_type: str, narrative: str, prices: dict,
                     movers: list, portfolio: dict,
                     agg: dict = None, perf: dict = None,
                     month_label: str = "") -> tuple[str, str]:
    now    = datetime.now(PARIS_TZ)
    green  = "#22c55e"
    red    = "#ef4444"
    amber  = "#f59e0b"
    bg     = "#0f172a"
    card   = "#1e293b"
    card2  = "#162032"
    border = "#334155"
    text   = "#e2e8f0"
    muted  = "#94a3b8"

    # ── Bloc variation totale (daily + monthly) ──
    variation_html = ""
    if agg:
        total_chg = agg["total_change_eur"]
        total_val = agg["total_valeur_eur"]
        chg_color = green if total_chg >= 0 else red
        chg_pct   = total_chg / total_val * 100 if total_val else 0.0

        env_rows = ""
        for env, chg in agg["by_envelope"].items():
            c = green if chg >= 0 else red
            env_rows += f"""<tr>
              <td style="padding:5px 10px;color:{muted};font-size:12px">{env}</td>
              <td style="padding:5px 10px;color:{c};font-weight:600;text-align:right;font-family:monospace">
                {chg:+,.0f} €</td></tr>"""

        slv_rows = ""
        for slv, chg in agg["by_sleeve"].items():
            c = green if chg >= 0 else red
            slv_rows += f"""<tr>
              <td style="padding:5px 10px;color:{muted};font-size:12px">{slv}</td>
              <td style="padding:5px 10px;color:{c};font-weight:600;text-align:right;font-family:monospace">
                {chg:+,.0f} €</td></tr>"""

        variation_html = f"""
        <div style="margin-bottom:24px">
          <div style="font-size:11px;color:{muted};text-transform:uppercase;letter-spacing:1px;margin-bottom:10px">
            Variation totale du jour</div>
          <div style="background:{card};border:1px solid {border};border-radius:10px;padding:16px 20px;
                      margin-bottom:12px;text-align:center">
            <div style="font-size:32px;font-weight:700;color:{chg_color};font-family:monospace">
              {total_chg:+,.0f} €</div>
            <div style="font-size:14px;color:{chg_color};margin-top:4px">{chg_pct:+.2f}% de la valeur suivie</div>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
            <div style="background:{card};border:1px solid {border};border-radius:10px;overflow:hidden">
              <div style="font-size:10px;color:{muted};text-transform:uppercase;letter-spacing:1px;
                          padding:8px 10px;border-bottom:1px solid {border}">Par enveloppe</div>
              <table style="width:100%;border-collapse:collapse">{env_rows}</table>
            </div>
            <div style="background:{card};border:1px solid {border};border-radius:10px;overflow:hidden">
              <div style="font-size:10px;color:{muted};text-transform:uppercase;letter-spacing:1px;
                          padding:8px 10px;border-bottom:1px solid {border}">Par sleeve</div>
              <table style="width:100%;border-collapse:collapse">{slv_rows}</table>
            </div>
          </div>
        </div>"""

    # ── Tableau positions ──
    all_pos = sorted(prices.values(), key=lambda x: x["change_pct"], reverse=True)
    rows = ""
    for d in all_pos:
        chg      = d["change_pct"]
        chg_eur  = d["change_eur"]
        c        = green if chg > 0 else red if chg < 0 else muted
        threshold = d["threshold"] if d["threshold"] else portfolio["config"]["alert_threshold_pct"]
        flag     = " ⚠️" if abs(chg) >= threshold else ""
        rows += f"""<tr style="border-bottom:1px solid {border}">
          <td style="padding:6px 10px;color:{text};font-weight:500;font-size:13px">{d['name']}</td>
          <td style="padding:6px 10px;color:{muted};font-size:11px">{d['sleeve']}</td>
          <td style="padding:6px 10px;color:{c};font-weight:700;text-align:right;font-size:13px">
            {chg:+.1f}%{flag}</td>
          <td style="padding:6px 10px;color:{c};text-align:right;font-size:12px;font-family:monospace">
            {chg_eur:+,.0f}€</td>
          <td style="padding:6px 10px;color:{muted};text-align:right;font-size:11px">{d['envelope']}</td>
        </tr>"""

    # ── Narrative HTML ──
    narrative_html = narrative.replace("\n", "<br>")
    for marker, color in [("**📊", amber), ("**🔍", amber), ("**🌍", amber),
                           ("**⚡", amber), ("**📅", amber), ("**🏆", amber),
                           ("**🔄", amber), ("**📋", amber)]:
        narrative_html = narrative_html.replace(
            marker, f'<span style="color:{color};font-weight:700">{marker}')
    narrative_html = narrative_html.replace("**", "</span>")

    # ── Header / subject ──
    if email_type == "daily":
        subject      = f"📊 PTF — {now.strftime('%d/%m/%Y')} | {agg['total_change_eur']:+,.0f}€"
        badge_color  = "#3b82f6"
        badge_text   = "RÉCAP QUOTIDIEN"
    elif email_type == "monthly":
        subject      = f"📅 Bilan mensuel — {month_label}"
        badge_color  = "#8b5cf6"
        badge_text   = f"BILAN {month_label.upper()}"
    else:
        worst        = movers[0] if movers else {}
        subject      = f"⚠️ Alerte {worst.get('name','')}: {worst.get('change_pct',0):+.1f}% — {now.strftime('%H:%M')}"
        badge_color  = "#ef4444"
        badge_text   = f"ALERTE — {len(movers)} VALEUR{'S' if len(movers)>1 else ''}"

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:{bg};font-family:'Segoe UI',Arial,sans-serif;color:{text}">
<div style="max-width:680px;margin:0 auto;padding:24px 16px">

  <div style="display:flex;align-items:center;justify-content:space-between;
              border-bottom:1px solid {border};padding-bottom:16px;margin-bottom:24px">
    <div>
      <div style="font-size:11px;letter-spacing:2px;color:{muted};text-transform:uppercase;margin-bottom:4px">
        {badge_text}</div>
      <div style="font-size:22px;font-weight:700;color:{text}">{now.strftime('%d %B %Y')}</div>
      <div style="font-size:12px;color:{muted}">{now.strftime('%H:%M')} heure Paris</div>
    </div>
    <div style="background:{badge_color};color:white;padding:6px 14px;border-radius:6px;
                font-size:12px;font-weight:700">CHARLES</div>
  </div>

  {variation_html}

  <div style="background:{card};border:1px solid {border};border-radius:10px;
              padding:20px;margin-bottom:24px;line-height:1.8;font-size:14px">
    {narrative_html}
  </div>

  <div style="font-size:11px;color:{muted};text-transform:uppercase;letter-spacing:1px;margin-bottom:10px">
    Détail positions</div>
  <div style="background:{card};border:1px solid {border};border-radius:10px;overflow:hidden;margin-bottom:24px">
    <table style="width:100%;border-collapse:collapse">
      <thead>
        <tr style="background:{card2};font-size:11px;color:{muted};text-transform:uppercase">
          <th style="padding:8px 10px;text-align:left">Valeur</th>
          <th style="padding:8px 10px;text-align:left">Sleeve</th>
          <th style="padding:8px 10px;text-align:right">Var. %</th>
          <th style="padding:8px 10px;text-align:right">Var. €</th>
          <th style="padding:8px 10px;text-align:right">Enveloppe</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </div>

  <div style="font-size:11px;color:{muted};text-align:center;border-top:1px solid {border};padding-top:16px">
    Portfolio Monitor · Claude Sonnet 4.6 · EUR/USD live · Données Yahoo Finance
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


# ── Modes principaux ─────────────────────────────────────────────────────────

def run_daily():
    print("[daily] Démarrage...")
    portfolio = load_portfolio()
    eurusd    = fetch_eurusd()
    prices    = fetch_prices(portfolio["positions"], eurusd)
    movers    = [
        {**{"ticker": t}, **{k: v for k, v in d.items()}}
        for t, d in prices.items()
        if abs(d["change_pct"]) >= (d["threshold"] or portfolio["config"]["alert_threshold_pct"])
    ]
    movers.sort(key=lambda x: abs(x["change_pct"]), reverse=True)
    agg = aggregate(prices)

    print(f"[daily] {len(prices)} positions, variation totale: {agg['total_change_eur']:+,.0f}€")

    # Mise à jour baseline si 1er du mois
    today = date.today()
    if today.day == 1:
        baseline = {t: {"price_eur": d["price_eur"], "date": str(today)}
                    for t, d in prices.items()}
        save_baseline(baseline)
        print("[daily] Baseline mensuelle mise à jour")

    prompt    = build_daily_prompt(prices, movers, agg, portfolio)
    narrative = call_claude(prompt, MODEL_SONNET, use_web_search=True)
    subject, html = build_email_html("daily", narrative, prices, movers, portfolio, agg=agg)
    send_email(subject, html)
    print("[daily] Terminé.")


def run_alert():
    print("[alert] Vérification intraday...")
    portfolio = load_portfolio()
    eurusd    = fetch_eurusd()
    prices    = fetch_prices(portfolio["positions"], eurusd)
    threshold = portfolio["config"]["alert_threshold_pct"]

    movers = []
    for t, d in prices.items():
        thr = d["threshold"] or threshold
        if abs(d["change_pct"]) >= thr:
            movers.append({**{"ticker": t}, **d})
    movers.sort(key=lambda x: abs(x["change_pct"]), reverse=True)

    if not movers:
        print("[alert] Aucun mover — pas d'email.")
        return

    print(f"[alert] {len(movers)} mover(s) détecté(s)...")
    agg       = aggregate(prices)
    prompt    = build_alert_prompt(movers)
    narrative = call_claude(prompt, MODEL_HAIKU)  # Haiku pour les alertes
    subject, html = build_email_html("alert", narrative, prices, movers, portfolio, agg=agg)
    send_email(subject, html)
    print("[alert] Terminé.")


def run_monthly():
    print("[monthly] Démarrage bilan mensuel...")
    portfolio = load_portfolio()
    baseline  = load_baseline()

    if not baseline:
        print("[monthly] Pas de baseline disponible — skipping.")
        return

    eurusd = fetch_eurusd()
    prices = fetch_prices(portfolio["positions"], eurusd)
    agg    = aggregate(prices)
    perf   = compute_monthly_perf(prices, baseline)

    now         = datetime.now(PARIS_TZ)
    month_label = now.strftime("%B %Y")

    prompt    = build_monthly_prompt(prices, perf, agg, portfolio, month_label)
    narrative = call_claude(prompt, MODEL_SONNET, use_web_search=True)
    subject, html = build_email_html("monthly", narrative, prices, [], portfolio,
                                     agg=agg, perf=perf, month_label=month_label)
    send_email(subject, html)
    print("[monthly] Terminé.")


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "daily"
    if   mode == "daily":   run_daily()
    elif mode == "alert":   run_alert()
    elif mode == "monthly": run_monthly()
    else:
        print(f"Usage: python monitor.py [daily|alert|monthly]", file=sys.stderr)
        sys.exit(1)
