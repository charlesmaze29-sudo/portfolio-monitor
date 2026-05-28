# Portfolio Monitor — Charles

Système de surveillance automatique de portefeuille :
- **Récap quotidien à 20h** (heure Paris) par email
- **Alertes intraday** si une valeur dépasse le seuil de variation configuré
- **0€/mois** (GitHub Actions gratuit) + ~$0.15–0.30/mois en API Claude

---

## 1. Prérequis

- Compte **GitHub** (gratuit)
- Compte **Gmail** avec un App Password activé
- Clé API **Anthropic** (claude.ai → Settings → API Keys)

---

## 2. Setup en 10 minutes

### Étape 1 — Créer le repo GitHub

```bash
# Depuis ton poste local
git clone https://github.com/TON_USERNAME/portfolio-monitor
# OU crée un nouveau repo privé sur github.com et push ces fichiers
```

Structure attendue :
```
portfolio-monitor/
├── .github/
│   └── workflows/
│       └── daily.yml
├── monitor.py
├── portfolio.json
├── requirements.txt
└── README.md
```

### Étape 2 — Créer un Gmail App Password

1. Va sur [myaccount.google.com](https://myaccount.google.com)
2. **Sécurité → Validation en 2 étapes** (doit être activée)
3. **Sécurité → Mots de passe des applications**
4. Crée un mot de passe pour "Portfolio Monitor"
5. Note le mot de passe généré (16 caractères) — il n'est affiché qu'une fois

### Étape 3 — Configurer les secrets GitHub

Sur ton repo → **Settings → Secrets and variables → Actions → New repository secret** :

| Nom du secret       | Valeur                                      |
|---------------------|---------------------------------------------|
| `ANTHROPIC_API_KEY` | Clé API Anthropic (commence par `sk-ant-…`) |
| `GMAIL_USER`        | ton.email@gmail.com                         |
| `GMAIL_APP_PASSWORD`| Le mot de passe de l'étape 2 (16 caract.)   |
| `EMAIL_RECIPIENT`   | Email de réception (peut être différent)    |

⚠️ **Ne jamais mettre ces valeurs dans le code ou dans portfolio.json**

### Étape 4 — Personnaliser ton portefeuille

Édite `portfolio.json` :

```json
"config": {
  "alert_threshold_pct": 5.0,    // Seuil alerte global en %
  "email_recipient": "...",       // Sera surchargé par le secret GitHub
  "patrimoine_total_eur": 470000
}
```

Pour chaque position, tu peux définir un seuil personnalisé :
```json
{
  "ticker": "MU",
  "alert_threshold_override": 4.0   // Seuil spécifique pour Micron (plus sensible)
}
```

### Étape 5 — Activer GitHub Actions

1. Va sur ton repo → onglet **Actions**
2. Si demandé, clique **"I understand my workflows, go ahead and enable them"**
3. Le workflow se lancera automatiquement selon le cron configuré

### Étape 6 — Test manuel

Pour tester immédiatement sans attendre 20h :
1. **Actions → Portfolio Monitor → Run workflow**
2. Sélectionne `daily` ou `alert`
3. Clique **Run workflow**
4. Vérifie que tu reçois l'email dans les 2 minutes

---

## 3. Coût estimé Claude API

| Scénario                  | Tokens/mois | Coût estimé |
|---------------------------|-------------|-------------|
| Récap quotidien (×30)     | ~90 000     | ~$0.09      |
| Alertes intraday (×10/mois)| ~20 000    | ~$0.02      |
| **Total**                 | ~110 000    | **~$0.11**  |

Plafond de sécurité : configure une limite de dépense à $2/mois sur [console.anthropic.com](https://console.anthropic.com) → **Settings → Billing → Usage limits**

---

## 4. Modifier les tickers

Édite `portfolio.json` → tableau `positions`. Format Yahoo Finance pour les tickers :
- Actions US : `MSFT`, `NVDA`, `MU`
- Actions françaises : `AIR.PA`, `MC.PA`, `SU.PA`
- Actions allemandes : `RHM.DE`, `ALV.DE`
- Corée du Sud : `000660.KS` (SK Hynix)
- ADR : `TSM`, `ASML`

Pour vérifier un ticker : [finance.yahoo.com](https://finance.yahoo.com)

---

## 5. Fonctionnement du cron

Le workflow tourne toutes les heures de 9h à 19h Paris (jours ouvrés), plus le récap de 20h.

La logique de sélection daily/alert est dans le workflow :
- Si l'heure Paris == 20 → mode `daily`
- Sinon → mode `alert` (email envoyé **seulement** si un seuil est franchi)

---

## 6. Dépannage

**"No module named anthropic"** → vérifier que `requirements.txt` est bien commité

**"Authentication failed" Gmail** → l'App Password est mal copié, ou la validation 2 étapes n'est pas activée

**Prix manquants pour 000660.KS (SK Hynix)** → les marchés coréens ferment tôt (11h Paris). Normal que le prix ne soit pas dispo en soirée US.

**Email dans les spams** → ajouter l'expéditeur à tes contacts Gmail

---

## 7. Évolutions possibles

- Ajout d'une synthèse hebdomadaire le dimanche soir
- Calcul des PV latentes en EUR (avec taux de change)
- Intégration du calendrier des earnings via earning whispers
- Notifications SMS via Twilio en complément email
