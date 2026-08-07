# tg-claude — pont Telegram ↔ Claude Code ↔ Codex

Un bot Telegram qui transforme une conversation en agent de développement sur un
VPS. On écrit un message (ou on envoie un vocal, ou une photo), le relais le
confie à **Claude Code** dans un dossier de travail donné, et renvoie la réponse
sur Telegram. Quand le quota Claude est épuisé, le relais **bascule
automatiquement vers Codex** sans perdre le fil de la conversation — et revient à
Claude dès que le plafond se libère.

Aucune dépendance à installer : tout est en bibliothèque standard Python. Le
relais ne parle qu'à trois choses — l'API Telegram, les CLI `claude` et `codex`,
et (pour les vocaux) l'API Groq.

---

## 1. Principe général

```
Telegram
   │  message / vocal / photo
   ▼
bot.py ─── file d'attente (1 tâche à la fois)
   │
   ├── moteur choisi : Claude par défaut, Codex si Claude est à plat
   │
   ├── Claude :  claude -p "<prompt>" --resume <session> --model … --effort …
   └── Codex  :  codex exec resume <thread> --model … -c model_reasoning_effort=…
                 (exécuté dans le dossier courant du relais)
   │
   ▼
réponse renvoyée sur Telegram, avec un pied de page
« Opus 4.8 · effort:medium » ou « Codex · gpt-5.6-sol · effort:max »
```

Les tâches sont traitées **séquentiellement** par un unique thread `worker` :
deux agents ne tournent jamais en parallèle sur le même dépôt. Une tâche lancée
n'a **aucune limite de durée** (une compilation ou une analyse longue n'est pas
tuée) ; `/stop` est le seul arrêt manuel.

Deux threads de fond accompagnent le worker :

| Thread | Rôle |
|---|---|
| `relay-worker` | dépile la file et lance le moteur |
| `claude-usage-monitor` | relit toutes les 120 s le snapshot local de quota |

---

## 2. La bascule Claude → Codex (et retour)

C'est le cœur du projet. Claude reste le moteur normal ; Codex est le filet.

### Ce qui déclenche le passage à Codex

1. **Le quota atteint 98 %** (`CLAUDE_LIMIT_PERCENT`) sur l'une des deux
   fenêtres Anthropic — la session de 5 h *ou* le plafond hebdomadaire.
2. **Le CLI Claude renvoie une erreur de quota explicite** (détectée par la
   regex `QUOTA_ERROR`) : la tâche est immédiatement rejouée sur Codex.

Le relais **ne fait aucun appel réseau** pour connaître le quota : il relit un
snapshot JSON produit par un service séparé (`claude-usage-monitor`, chemin
`CLAUDE_USAGE_STATE`). Conséquence importante : si la sonde échoue — typiquement
un HTTP 429 — cela n'a **jamais** pour effet de rendre Claude indisponible. Une
panne de sonde est un problème d'affichage, pas de routage. Un snapshot absent,
invalide ou vieux de plus de 15 min est ignoré de la même façon.

Une tâche **déjà lancée n'est jamais interrompue** par une bascule. Seules les
*nouvelles* tâches changent de moteur.

### Ce qui ramène à Claude

Le retour est gouverné par **la fenêtre qui a causé le blocage**, et pas par
n'importe quel rollover. C'est le point subtil : si l'hebdomadaire est plein, un
simple renouvellement de la session 5 h ne doit pas rendre la main à Claude
devant un mur intact. Claude est réarmé quand sa fenêtre bloquante repasse sous
5 % (`CLAUDE_RECOVERED_PERCENT`) ou que son reset est passé.

### Le relais marche dans les deux sens

Codex peut lui aussi tomber (quota, crédits épuisés). Dans ce cas :

- `CODEX_QUOTA_ERROR` le détecte, Codex est marqué indisponible et le moteur
  préféré redevient Claude ;
- Codex n'expose pas d'heure de reset : il se **réarme seul après 1 h**
  (`CODEX_RETRY_SECONDS`) ;
- si Claude est *aussi* indisponible, la tâche est **mise de côté** (file
  `deferred_q`) et automatiquement reprise à la prochaine fenêtre Claude, avec
  un message d'information sur Telegram. Rien n'est perdu.

### La bascule manuelle : `/switch`

`/switch claude` ou `/switch codex` **épingle** le moteur des prochaines tâches
(`state["manual_engine"]`, persisté dans `state/manual-engine`). L'épingle prime
sur le relais automatique, qui continue de tourner en dessous sans être désarmé :
`preferred_engine` et les quotas restent suivis comme avant. `/switch auto`
retire l'épingle.

Trois points de comportement, tous couverts par les tests :

- **Le contexte suit**, exactement comme pour une bascule automatique : le
  changement de moteur déclenche `needs_handoff`, donc l'écriture de
  `state/handoff.md`, passé au démarrage de la session entrante. Le motif inscrit
  dans la passation indique que la bascule a été demandée à la main — le moteur
  entrant sait qu'il prend la main sur ordre et non sur défaillance de l'autre.
- **Un moteur épinglé mais à plat s'efface**, sans perdre l'épingle : le relais
  assure l'intérim, et l'épingle reprend dès que le moteur choisi est réarmé.
  Un choix explicite ne doit pas être effacé en silence par une panne de quota.
- **L'épingle survit à un redéploiement** (comme les sessions et le `cwd`), et une
  valeur inattendue dans le fichier est ignorée plutôt qu'imposée au routage.

`/switch codex analyse ce bug` épingle *et* enchaîne sur le message, sur le
modèle de `/opus xhigh <message>`.

---

## 3. Continuité du contexte

Chaque moteur conserve **sa propre session native** :

- Claude : `--session-id <uuid>` à la création, puis `--resume <uuid>` ;
- Codex : `codex exec resume <thread_id>`.

Tant qu'on reste sur le même moteur, **rien n'est réinjecté** — il se souvient
tout seul. Les identifiants de session sont écrits dans `state/` et survivent
donc à un redémarrage du service (sans quoi chaque déploiement perdait tout le
contexte).

Au **changement de moteur seulement** — le seul instant où le contexte serait
autrement perdu — `handoff.py` condense le journal JSONL du relais en un fichier
de passation `state/handoff.md` :

- les 12 derniers échanges, budget global 12 000 caractères ;
- les échanges récents sont prioritaires (3500, 2000, 1200, 800 caractères, puis
  400) et les longs textes sont coupés au milieu — le début pose le sujet, la fin
  porte la conclusion ;
- les blocages non résolus sont listés à part.

Ce fichier est passé **au démarrage de la session entrante**, jamais collé dans
la demande de l'utilisateur : `--append-system-prompt-file` pour Claude, entrée
standard pour Codex. Le moteur entrant doit pouvoir distinguer ce qu'on lui
*raconte* de ce qu'on lui *demande*. Il se termine par des consignes explicites :
ce contexte est un résumé, pas la vérité terrain ; en cas de contradiction avec
les fichiers réels, l'observation gagne.

Si une session a disparu côté CLI (purge des rollouts, `/clear`), la regex
`STALE_SESSION` le détecte et la tâche repart à neuf **avec une passation**, pour
ne rien oublier.

---

## 4. Commandes Telegram

| Commande | Effet |
|---|---|
| *(texte libre)* | crée une tâche pour le moteur courant |
| `/help`, `/start` | aide |
| `/status` | dossier, moteur, modèle, efforts, quotas 5 h et semaine, contexte restant, sessions, file d'attente |
| `/pwd`, `/ls`, `/cd <chemin>` | dossier de travail (`/cd` repart sur une conversation neuve) |
| `/switch claude\|codex [message]` | impose le moteur des prochaines tâches, avec passation du contexte; le message éventuel part aussitôt |
| `/switch auto` | retire l'épingle et rend la main au relais automatique |
| `/new` | remet les deux moteurs à zéro (le journal de relais est conservé) |
| `/stop` | interrompt la tâche en cours |
| `/model` | affiche ou change le modèle Claude |
| `/opus`, `/sonnet`, `/haiku`, `/fable` `[effort] [message]` | change de modèle, éventuellement d'effort, et enchaîne sur un message |
| `/effort [claude\|codex] <niveau>` | niveau de raisonnement, par moteur |

**Modèles Claude** : Opus 4.8, Sonnet 5, Haiku 4.5, Fable 5 (Opus par défaut).

**Niveaux d'effort** — volontairement séparés par moteur : Claude reste économe
(`low` → `max`, défaut `medium`), Codex tape haut (`low` → `ultra`, défaut
`max`).

**Vocaux** : le fichier est téléchargé puis transcrit par Groq
(`whisper-large-v3`) et devient une tâche normale.

**Photos** : téléchargées dans `state/photos/`, le chemin est transmis au moteur,
et les images de plus de 14 jours sont purgées automatiquement.

**Messages longs** : Telegram découpe les textes au-delà de ~4096 caractères. Le
relais attend un court silence (4 s, 15 s au maximum) pour rassembler les
fragments en une seule tâche — sinon un prompt collé en trois morceaux devenait
trois tâches.

---

## 5. Sécurité

Trois garde-fous, à comprendre avant de reprendre ce code.

**1. Un seul interlocuteur.** Tout message dont l'expéditeur n'est pas
`TELEGRAM_CHAT_ID` est journalisé et ignoré. Le bot ne répond à personne d'autre.

**2. Confirmation des commandes destructrices** (`confirm_hook.py`). Branché en
hook `PreToolUse` sur l'outil `Bash` de Claude Code. Une liste de motifs
(`rm -rf`, `dd`, `mkfs`, `git push --force`, `git reset --hard`,
`systemctl stop`, `DROP TABLE`, `curl … | sh`, `docker rm`, `kubectl delete`,
fork bomb…) publie une demande dans `state/pending`. **Hublot** l'affiche dans
son interface native et écrit la décision dans `state/decision`. Sans réponse en
5 min, la commande est **bloquée**. Le hook est *fail-safe* : en cas d'erreur
interne sur une commande jugée dangereuse, il bloque.

**3. Liste blanche d'outils** (`ALLOWED_TOOLS`) et permissions restreintes dans
`settings.json` (côté MCP, seul `mcp__claude_ai_Gmail__create_draft` est
autorisé — brouillons uniquement, aucun envoi).

⚠️ **À savoir** : Codex est lancé avec
`--dangerously-bypass-approvals-and-sandbox`, sans le garde-fou de confirmation
qui protège Claude. C'est un choix assumé — le relais est confiné à un VPS dédié
et une exécution non interactive ne peut pas s'arrêter sur un TTY — mais cela
signifie que **le hook de confirmation ne couvre pas les tâches traitées par
Codex**. À revoir avant tout usage sur une machine qui compte.

---

## 6. Fichiers du dépôt

| Fichier | Rôle |
|---|---|
| `bot.py` | le relais : Telegram, file d'attente, routage, quotas, sessions, commandes |
| `handoff.py` | construction du fichier de passation entre moteurs |
| `render.py` | Markdown des moteurs → HTML Telegram, et découpe des messages longs |
| `confirm_hook.py` | hook `PreToolUse` de confirmation des commandes dangereuses |
| `deploy.py` | déploiement du dépôt vers la production, avec tests et retour arrière |
| `settings.json` | réglages Claude Code utilisés par le relais (hook + permissions) |
| `tests/test_bot.py` | 71 tests unitaires (quotas, bascule auto et manuelle, passation, commandes, envoi) |
| `tests/test_render.py` | 26 tests de rendu et de découpe |
| `tests/test_confirm_hook.py` | 3 tests du canal de confirmation Hublot |

Non versionnés (`.gitignore`) : `state/`, `logs/`, `__pycache__/`, `*.bak-*`.

---

## 7. Prérequis

- **Python 3.10+** (le VPS de référence tourne en 3.14) — aucune dépendance pip.
- **Claude Code CLI** (`claude`, testé en 2.1.220), authentifié sur la machine.
- **Codex CLI** (`codex`, testé en 0.145.0), authentifié.
- **Un bot Telegram** (créé via @BotFather) et l'ID du chat autorisé.
- **Une clé Groq**, uniquement si on veut les messages vocaux.
- **`claude-usage-monitor`**, service séparé qui écrit le snapshot de quota lu
  par le relais. Sans lui, le relais fonctionne mais ne bascule que sur erreur
  de quota explicite du CLI, jamais en anticipation.

### Variables d'environnement

Lues depuis `/etc/scripts.env` via `EnvironmentFile` du service systemd.

| Variable | Obligatoire | Rôle |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | oui | token du bot |
| `TELEGRAM_CHAT_ID` | oui | seul chat autorisé |
| `GROQ_API_KEY` | non | transcription des vocaux |
| `CODEX_MODEL` | non | modèle Codex (défaut `gpt-5.6-sol`) |
| `CLAUDE_USAGE_STATE` | non | snapshot de quota (défaut `/root/.claude-usage-monitor/state.json`) |
| `CLAUDE_PROJECTS_DIR` | non | transcripts Claude, pour le calcul du contexte |
| `CODEX_SESSIONS_DIR` | non | rollouts Codex, idem |
| `MESSAGE_BATCH_QUIET_SECONDS` | non | silence avant regroupement des fragments (défaut 4) |
| `MESSAGE_BATCH_MAX_SECONDS` | non | attente maximale (défaut 15) |

---

## 8. Installation

```bash
git clone <url-du-depot> /root/repos/tg-claude
mkdir -p /opt/tg-claude/state /opt/tg-claude/logs /opt/tg-claude/tests
cp /root/repos/tg-claude/{bot.py,handoff.py,confirm_hook.py,settings.json} /opt/tg-claude/
cp /root/repos/tg-claude/tests/test_bot.py /opt/tg-claude/tests/
```

Le code tourne depuis `/opt/tg-claude` (constante `BASE` dans `bot.py`) ; le
dossier de travail par défaut des agents est `/root/repos` (`REPOS_BASE`).

Unité systemd `/etc/systemd/system/tg-claude.service` :

```ini
[Unit]
Description=Pont Telegram <-> Claude Code
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/scripts.env
Environment=PATH=/usr/local/bin:/usr/bin:/bin
WorkingDirectory=/opt/tg-claude
ExecStart=/usr/bin/python3 /opt/tg-claude/bot.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Un drop-in `tg-claude.service.d/credentials.conf` ajoute
`UnsetEnvironment=CLAUDE_CODE_OAUTH_TOKEN` : le token statique de
`/etc/scripts.env` est ancien et n'a pas le scope `user:profile`. Claude Code
doit utiliser et rafraîchir ses propres credentials OAuth locaux.

```bash
systemctl enable --now tg-claude
journalctl -u tg-claude -f
```

---

## 9. Déploiement et tests

```bash
python3 -m unittest discover -s tests -v
```

Le déploiement passe par `deploy.py`, qui ne se contente pas de copier :

1. tests sur le code du dépôt **avant** toute copie ;
2. sauvegarde de la production dans `/opt/tg-claude/backup-<horodatage>/` ;
3. copie, puis **les mêmes tests sur le code déployé** ;
4. redémarrage du service, vérification que le PID a bien été renouvelé ;
5. notification Telegram du résultat ;
6. **retour arrière automatique** vers la sauvegarde à la moindre erreur.

Un détail à connaître : redémarrer le service tue la tâche en cours — donc le
processus Claude qui est peut-être en train de répondre sur Telegram. C'est
pourquoi `deploy.py` est prévu pour être lancé hors bande (`systemd-run`) une
fois la réponse partie.

---

## 10. État sur disque

Tout est dans `/opt/tg-claude/state/` :

| Fichier | Contenu |
|---|---|
| `offset` | dernier `update_id` Telegram traité (pas de message rejoué au redémarrage) |
| `model`, `claude-effort`, `codex-effort` | préférences persistées |
| `claude-session-id`, `codex-session-id`, `last-engine`, `cwd` | sessions natives et dossier, pour survivre à un redémarrage |
| `manual-engine` | moteur épinglé par `/switch` (absent = relais automatique) |
| `relay-journal.jsonl` | journal des échanges, source du fichier de passation |
| `handoff.md` | dernière passation générée |
| `pending`, `decision` | dialogue avec `confirm_hook.py` |
| `photos/` | images reçues (purge à 14 jours) |

---

## 11. À savoir avant de reprendre le code

- Les chemins sont **codés en dur** pour ce VPS : `/opt/tg-claude`,
  `/root/repos`, `/etc/scripts.env`. Un portage passe par là.
- Le modèle Opus est référencé par l'alias `opus` (et non par un identifiant
  figé), tandis que le libellé affiché reste « Opus 4.8 » : l'alias suivra une
  future version d'Opus alors que le libellé, lui, ne bougera pas tout seul.
- `setup-composio.sh` et `composio-setup/` existent sur le serveur mais ne sont
  pas versionnés (le second embarque un `node_modules`). Ils servent à brancher
  Composio en mode clé API, sans OAuth.
- Aucun secret n'est présent dans le dépôt : tout vient de l'environnement du
  service.
- **Le rendu des réponses passe par `render.py`, jamais par `sendMessage` en
  direct.** Trois règles y sont verrouillées par les tests :
  - Telegram n'accepte que `<b> <i> <u> <s> <a> <code> <pre> <blockquote>` et
    **rejette le message entier** (HTTP 400) sur toute autre balise : tout
    fragment de texte est donc échappé, et `post()` renvoie le message en texte
    nu si l'API refuse le HTML. Un défaut de rendu ne doit jamais faire perdre
    une réponse.
  - une coupe ne tombe qu'à une frontière de mot, **hors balise et hors
    entité**, et les balises encore ouvertes sont refermées puis rouvertes dans
    le fragment suivant — sinon un bloc de code de 6 000 caractères produit deux
    messages invalides.
  - `_italique_` n'est pas reconnu, volontairement : `horizontal_accuracy`
    passerait en italique. Seul `*x*` l'est.
- Au-delà de `MAX_RESPONSE_CHUNKS` fragments, la réponse part en pièce jointe
  `.txt` (le texte **complet**, Markdown d'origine) après les deux premiers
  messages. Si l'envoi du fichier échoue, les fragments restants sont postés :
  aucun contenu ne disparaît.
