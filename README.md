# Guggas portefølje på Raspberry Pi

Pi'en henter kurser fra Nordnet hvert 5. minut på hverdage (kl. 7–23), gemmer
historikken og viser et live dashboard på dit hjemmenetværk. Hver aften kl. 22.45
lægger den dagens kurser i mappen `kurser/` i dette GitHub-repository, og
Claude-dashboardet henter dem derfra næste morgen.

Der skal ikke installeres noget ekstra: programmet bruger kun Python, som allerede
er på Raspberry Pi OS.

## Hvad ligger i repository'et?

| Fil/mappe | Indhold | Kommer på GitHub? |
|---|---|---|
| `app.py`, `dashboard.html`, `install.sh` | Programmet | Ja |
| `seed/` | Kurshistorikken fra Claude-dashboardet, så kurverne ikke starter fra nul | Ja |
| `kurser/` | Dagens kurser og kurshistorik (kun kurser, ingen beløb) | Ja, hver aften |
| `config.json` | Dine depotnumre og indstillinger | **Nej** |
| `eksport/` | Dine CSV-eksporter fra Nordnet | **Nej** |
| `data/` | Databasen på Pi'en | **Nej** |

Hvem som helst kan se repository'et, hvis det er offentligt. De ser kun kurserne
på de papirer, du ejer, ikke hvor mange du har eller hvad de er værd.

---

## 1. Læg filerne på GitHub (på din computer, i browseren)

1. Gå til https://github.com/new
2. Navn: `portefolje`. Vælg **Public** (så kan Claude læse kursfilerne uden adgangskode).
3. Klik **Create repository**.
4. Klik **uploading an existing file**, og træk alle filer og mapper fra den udpakkede
   zip-fil ind (`app.py`, `dashboard.html`, `install.sh`, `config.example.json`,
   `README.md`, `.gitignore` og mappen `seed`).
   Tip: `.gitignore` er skjult på nogle computere. Den er ikke afgørende, men sørg for
   aldrig at uploade `config.json` eller dine CSV-filer.
5. Klik **Commit changes**.

## 2. Giv Pi'en lov til at skrive til repository'et

Åbn en terminal på Pi'en (eller log ind med `ssh`) og kør:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/portefolje -N "" -C "raspberry-pi"
cat ~/.ssh/portefolje.pub
```

Kopiér den linje, der kommer frem. På GitHub: dit repository → **Settings** →
**Deploy keys** → **Add deploy key**. Titel: `Raspberry Pi`. Indsæt linjen, sæt
flueben i **Allow write access**, og klik **Add key**.

Fortæl så Pi'en, hvilken nøgle den skal bruge til GitHub:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github-portefolje
  HostName github.com
  User git
  IdentityFile ~/.ssh/portefolje
  IdentitiesOnly yes
EOF
```

## 3. Hent programmet ned på Pi'en

Udskift `DIT-BRUGERNAVN` med dit GitHub-brugernavn:

```bash
cd ~
git clone github-portefolje:DIT-BRUGERNAVN/portefolje.git
cd portefolje
git config user.name "Raspberry Pi"
git config user.email "pi@localhost"
```

Første gang spørger den, om du stoler på github.com. Skriv `yes`.

## 4. Indstillinger

```bash
cp config.example.json config.json
nano config.json
```

Skriv dine depotnumre i stedet for `SKRIV-DEPOTNUMMER-HER`. Du finder dem i kolonnen
**Depot** i Nordnet-eksporten eller på din konto hos Nordnet. Gem med `Ctrl+O`, `Enter`,
`Ctrl+X`.

## 5. Læg dine Nordnet-eksporter ind

Hent **Transaktioner og notaer** som CSV fra Nordnet for hver konto (hele perioden).
Du kan enten kopiere dem til mappen `~/portefolje/eksport/` eller vente til trin 7
og lægge dem ind via dashboardet.

## 6. Start det

```bash
./install.sh
```

Scriptet installerer porteføljen som en tjeneste, der starter af sig selv, når Pi'en
tænder, og skriver adressen på dashboardet, fx `http://192.168.1.50:8080`.

## 7. Åbn dashboardet

Åbn adressen på din telefon eller computer, mens du er på hjemmenetværket. Har du ikke
lagt eksporterne ind endnu, så klik **Vælg CSV-fil** nederst på siden.

Siden opdaterer sig selv hvert minut. Nordnets offentlige kurser er ca. 15 minutter
forsinkede.

**Udefra:** installer [Tailscale](https://tailscale.com/download) på Pi'en og din
telefon, så kan du se dashboardet hvor som helst uden at åbne dit hjemmenetværk.
Lav aldrig portforwarding til Pi'en i din router: dashboardet har ingen adgangskode.

---

## Nyttige kommandoer

```bash
sudo systemctl status portefolje        # kører den?
journalctl -u portefolje -n 50          # seneste log
sudo systemctl restart portefolje       # genstart efter ændring af config.json
python3 app.py hent                     # hent kurser én gang nu
python3 app.py publicer                 # send kurserne til GitHub nu
cd ~/portefolje && git pull && sudo systemctl restart portefolje   # hent en ny version af programmet
```

## Hvis noget ikke virker

- **"Fejl" i statuslinjen øverst på dashboardet:** Nordnet kan have ændret sine
  kursdata. Kopiér fejlteksten til Claude, så kan den rettes.
- **"GitHub: git push fejlede":** tjek at deploy-nøglen har **Allow write access**,
  og kør `ssh -T github-portefolje` (svaret skal nævne dit repository).
- **Tallene passer ikke:** tjek at begge konti har en eksport i `eksport/`, og at
  depotnumrene i `config.json` er rigtige.

Kurserne hentes fra Nordnets offentlige kursdata, ikke et officielt API. Hent ikke
oftere end hvert 5. minut, og husk at Nordnet kan ændre det uden varsel.
