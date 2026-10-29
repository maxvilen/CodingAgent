# CodingAgent

En liten, granskningsstyrd kodagent för Git- och GitHub-repositories. Agenten
använder OpenAI Responses API med din egen API-nyckel och är därför oberoende av
Codex-abonnemangets användningsgränser.

Den här versionen är en vidareutveckling av den tidigare `agent.py` och behåller
ett enkelt terminalgränssnitt. Den viktigaste principen är:

> Modellen får föreslå och skriva kod, men den kan inte själv kringgå
> godkännanden, lämna worktreen, commit:a till en skyddad branch, push:a eller
> skapa/merga pull requests.

## Funktioner

- Automatisk branch och isolerad `git worktree` per uppgift.
- GitHub Issues via `gh` (beskrivning, kommentarer, labels, acceptance criteria,
  beroenden till issues/PRs).
- Plan presenteras och kräver godkännande innan någon fil ändras.
- Varje filskrivning visas som en diff och kräver godkännande.
- Tester körs i en separat Docker-container: icke-root, utan nätverk,
  resursbegränsad och utan värdens hemkatalog, Docker-socket, SSH-nycklar eller
  API-nycklar.
- Automatisk commit endast efter att testerna har passerat.
- Push och draft-PR kräver uttryckligt godkännande. Ingen automatisk merge och
  inga automatiska issue-stängningar.
- Kostnadskontroll per uppgift med `--max-cost`, tokenräkning, cached tokens,
  lokal JSONL-logg och konfigurerbar, versionshanterad prislista.
- `--dry-run` för analys och plan utan branches, worktrees, filändringar,
  commits, kostnadslogg eller PRs.

## Krav

- macOS (Linux fungerar i huvudsak också).
- Python 3.11 eller senare.
- Git.
- Docker Desktop (eller Docker Engine) med en körande daemon.
- GitHub CLI, `gh`.
- En OpenAI API-nyckel med tillgång till Responses API.

Agenten använder **inte** Codex CLI eller Codex-abonnemanget. API-anropen
debiteras separat enligt OpenAI:s API-priser.

## Installation på macOS

### 1. Systemverktyg

```bash
brew install python@3.12 git gh
brew install --cask docker
open -a Docker
```

Vänta tills Docker Desktop har startat och verifiera:

```bash
docker info
gh --version
git --version
```

### 2. Python-miljö

```bash
cd ~/ai-coding-agent
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
```

`requirements-dev.txt` innehåller även pytest. Om du vill kunna starta agenten
som ett kommando kan du dessutom köra:

```bash
pip install -e .
ai-coding-agent --help
```

Det enklaste är annars att alltid köra `python agent.py ...` från
`~/ai-coding-agent` med den virtuella miljön aktiverad.

### 3. GitHub CLI och repository

```bash
gh auth login
gh auth status
git -C /path/to/ProjectA remote -v
```

Det måste finnas en remote som heter `origin`, och `gh` måste vara autentiserad
mot den GitHub-instans som repositoryt tillhör. Agenten återanvänder befintlig
`gh auth` och frågar aldrig efter en GitHub-token i terminalen.

Vid issue-baserat arbete krävs att repositoryt har en GitHub-origin. Agenten
vägrar arbeta mot enbart en lokal katalog.

### 4. OpenAI API

```bash
export OPENAI_API_KEY="sk-..."
# Valfritt:
export OPENAI_MODEL="gpt-5.4"
```

Sätt aldrig nyckeln i repositoryt. Agenten läser den från miljön, skickar den
inte vidare till Docker-containern och skriver den inte till loggar. Den läser
inte `.env` automatiskt.

**Datadelning:** filer, issuebeskrivningar och kommentarer som modellen läser
skickas till OpenAI Responses API som en del av prompten. Agenten använder
`store=False`, men det är inte en garanti för hur leverantören hanterar data
efter anropet. Följ din organisations policy innan du kör agenten mot privat
eller känslig kod.

Kontrollera:

```bash
python agent.py --version
python agent.py --help
```

## Snabbstart

### Analysläge utan sidoeffekter

```bash
cd ~/ai-coding-agent
source .venv/bin/activate
python agent.py ~/Projects/ProjectA --issue 963 --dry-run
```

I dry-run:

- läses repositoryt och GitHub-issuen,
- ingen Docker krävs,
- ingen branch eller worktree skapas,
- inga filer, commits eller PRs skapas,
- ingen kostnadslogg skrivs (kostnaden visas bara i terminalen),
- inget godkännande begärs.

### Issue-baserad utveckling: ProjectA

```bash
python agent.py ~/Projects/ProjectA \
  --issue 963 \
  --model gpt-5.4 \
  --max-cost 2.00 \
  --reasoning-effort medium
```

### Issue-baserad utveckling: ProjectB

```bash
python agent.py ~/Projects/ProjectB \
  --issue 123 \
  --model gpt-5.4 \
  --max-cost 2.00
```

`ProjectA` och `ProjectB` är neutrala exempel. Byt ut dem och exempelvägarna mot
dina faktiska repositories. Ange `--base-branch` om repositoryts standardbranch
inte kan identifieras automatiskt.

### Fri uppgift utan issue

```bash
python agent.py ~/Projects/ProjectB \
  "Lägg till validering av tomt organisationsnummer och uppdatera testerna" \
  --max-cost 1.50
```

## Arbetsflöde och godkännanden

1. **Förkontroll**: Git-repository, Docker, GitHub CLI/autentisering och
   OpenAI-konfiguration verifieras. Modellpriset måste vara känt.
2. **Issue-analys**: Issue, kommentarer, labels, acceptance criteria och
   refererade issues/PRs hämtas. Innehållet behandlas som opålitlig indata.
3. **Plan**: Agenten undersöker kod och tester och presenterar en konkret plan.
4. **Godkännande av plan**: Inga filer ändras innan du svarar `y`.
5. **Isolerad worktree**: En ny branch skapas, till exempel
   `agent/issue-963-fix-login`, i en worktree utanför din vanliga arbetskopia.
6. **Kodändringar**: Varje `write_file` visar en diff och kräver ett separat
   godkännande.
7. **Tester**: Relevanta tester kan köras under arbetet. Efter avslutat arbete
   kör agenten alltid hela testsviten i Docker.
8. **Resultat**: Testresultat, filstatus och diff-statistik visas. Om testerna
   misslyckas görs **ingen commit** och ändringarna ligger kvar i worktreen.
9. **Commit**: Om testerna passerar skapas en automatisk commit med
   `Refs #<issue>` (aldrig `Fixes`/`Closes`) och en sammanfattning.
10. **Push/PR**: Agenten visar förslag på draft-PR (titel och beskrivning) och
    begär uttryckligt godkännande. Först därefter pushas branchen och en
    **draft**-PR skapas.
11. **Slutrapport**: Branch, commit, worktree, tester, kostnad och eventuell
    PR-länk visas.

Agenten kör aldrig `gh pr merge`, `gh issue close` eller motsvarande. En merge
måste göras manuellt av en människa.

## Git, branches och worktrees

- Branchformat: `agent/issue-<nummer>-<kort-slug>`, till exempel
  `agent/issue-963-fix-login`.
- Om namnet redan finns läggs ett unikt suffix till (`-2`, `-3`, ...).
- Worktrees skapas som standard under:
  `~/.local/state/ai-coding-agent/worktrees/<repo>-<hash>/<branch>`.
- Använd `--worktree-root` för att välja en annan katalog.
- Din ordinarie arbetskopia checkas aldrig ut till agentbranchen och dess
  branch/HEAD ändras inte. Detta verifieras före och efter arbetet.
- Agenten vägrar skriva eller commit:a på `main`, `master`, base-branchen eller
  andra brancher som inte börjar med `agent/`.
- Endast filer som agenten faktiskt har skrivit stageas. Filnamn som `.env`,
  `.env.*` (utom tydliga mallar som `.env.example`), `*.pem`, `*.key`,
  credentials-filer, SSH-nycklar och liknande blockeras.
- Stageade ändringar skannas efter högkonfidensmönster för hemligheter. Vid
  träff avbryts commit och index rensas.
- Git-hooks och GPG-signering stängs av för agentens automatiska commit
  (`--no-verify`, `commit.gpgsign=false`) så att repository-kod inte körs som en
  del av commit-processen.

Om din ordinarie arbetskopia har ocommittade ändringar varnar agenten. Ändringarna
kopieras inte till worktreen, som utgår från base-branchens HEAD. Committa eller
stasha gärna innan du startar för att planen ska utgå från exakt samma kod som
worktreen.

Worktreen tas inte bort automatiskt, eftersom den kan behövas för granskning
eller återhämtning. Manuell städning:

```bash
git -C /path/to/repo worktree list
git -C /path/to/repo worktree remove /path/to/worktree
git -C /path/to/repo branch -D agent/issue-963-fix-login
```

## GitHub Issues

För `--issue` hämtar agenten:

- nummer, titel, body, state och URL,
- labels,
- kommentarer,
- försök till extrahering av acceptance criteria från rubriker och checkboxar,
- referenser till andra issues/PRs (`#123` och fullständiga GitHub-URL:er) och
  deras titel/state.

Extraherade acceptance criteria är en läshjälp, inte ett kontrakt. Issue-text
och kommentarer är opålitlig indata och får inte styra agentens behörigheter.

När arbetet är klart föreslår agenten en PR-titel och PR-beskrivning. Efter ditt
godkännande skapas en **draft-PR** med `gh pr create --draft`. PR-texten rensas
från stängningsnyckelord som `Closes #123` och använder `Refs #123` i stället.

Saknas `gh` eller autentisering får du installationsinstruktioner. Agenten
försöker inte skapa en egen token eller kringgå `gh auth`.

## Docker-isolerade tester

För varje testkörning:

1. En sanerad, skrivbar kopia av worktreen skapas i en tillfällig katalog.
   `.git`, samtliga `.env*`-filer, credentials, nycklar, `.venv`,
   `node_modules`, `dist`, `build` och liknande kopieras inte. Symboliska länkar
   hoppas över.
2. En minimal image byggs från `python:3.12-slim` med pytest. Projektets
   `requirements.txt`, `requirements-dev.txt` och/eller `pyproject.toml` /
   `setup.py` installeras vid imagebygget.
3. Testcontainern körs med:

   - `--network none`
   - `--user <hostens icke-root-UID>` (eller `10001:10001` om agenten körs som
     root)
   - `--read-only` med skrivbar worktree-mount och tmpfs för `/tmp`
   - `--cap-drop ALL`
   - `--security-opt no-new-privileges`
   - `--cpus`, `--memory`, `--memory-swap`, `--pids-limit` och timeout
   - endast den sanerade testkopian mountad på `/workspace`
   - inga `-e`-variabler med API-nycklar, ingen hemkatalog och ingen
     Docker-socket

4. stdout, stderr och exit code samlas in. Passerade/misslyckade tester och
   sammanfattning visas.
5. Container, image och den tillfälliga testkatalogen städas i `finally`.

Standardgränser:

| Resurs | Standard |
| --- | --- |
| CPU | 1.0 |
| RAM | 512 MB |
| Processer | 128 |
| Testtimeout | 300 s |
| Image build-timeout | 600 s |

Ändra med `--test-cpus`, `--test-memory`, `--test-pids` och `--test-timeout`.

Om Docker saknas eller daemonen inte svarar avbryter agenten med tydliga
installationsinstruktioner. Den kör **aldrig** tester direkt på värddatorn som
fallback.

### Viktig begränsning: imagebygget

Själva testkörningen har inget nätverk. Imagebygget måste däremot kunna nå
paketindex för att installera pytest och projektets beroenden. `pip install`
kan köra projektets byggkod (till exempel `setup.py` eller byggbackend) inuti
byggcontainern. Bygget får inga hemliga miljövariabler, ingen hemkatalog och ingen
Docker-socket, men har nätverk. Kör därför agenten bara mot repositories du
litar på. Om du vill undvika beroendeinstallation kan du använda ett projekt
som inte kräver det, eller anpassa `PytestProfile` i
`coding_agent/docker_tests.py`.

Testprofilen är utbyggbar. `DockerTestRunner` tar emot en `TestProfile`; nya
profiler för React/TypeScript kan registreras utan att ändra Git-, GitHub- eller
kostnadsmodulerna. Endast `pytest` är registrerad i den här versionen.

## Kostnadskontroll

```bash
python agent.py ~/Projects/ProjectA \
  --issue 963 \
  --model gpt-5.4 \
  --max-cost 2.00 \
  --reasoning-effort medium
```

- Standardtak: **2.00 USD** per uppgift.
- `--max-cost` måste vara ett positivt decimaltal.
- `--model` väljer modell. Standard är `OPENAI_MODEL` eller `gpt-5.4`.
- `--reasoning-effort` skickas bara om modellens prispost deklarerar stöd för
  det. Annars avbryts körningen före första API-anropet.
- Före varje API-anrop görs en konservativ worst-case-uppskattning: uppskattade
  inputtokens (25 % marginal plus request-overhead) och maximalt antal
  outputtokens. Om uppskattningen skulle överskrida återstående budget görs
  inget anrop.
- Efter varje anrop visas input-, cached- och outputtokens samt kostnad och
  ackumulerad summa.
- Cached input-tokens räknas som en delmängd av inputtokens och prissätts med
  cached-priset.
- Långa kontexter kan ha separat prisnivå (till exempel gpt-5.4 över 272 000
  inputtokens). Prislistan innehåller sådana nivåer där de är kända.
- Om API:et inte returnerar usage används den konservativa förhandsuppskattningen
  och en varning visas.

### Prislista och versionshantering

Priserna ligger i `coding_agent/pricing.json`, inte hårdkodade i Python-koden.
Filen innehåller:

- `version`: `2026-10-29`
- `source`: OpenAI:s officiella prissida
- `currency`: USD
- `unit`: per 1M tokens
- pris per modell samt eventuella long-context- och reasoning-inställningar

Okända modeller ger ett tydligt fel och antas **aldrig** kosta noll. Uppdatera
prislistan manuellt när OpenAI ändrar priser, och höj `version` samt
`checked_at`. Du kan också peka på en egen fil:

```bash
python agent.py ... --pricing-file /path/to/pricing.json
```

### Kostnadslogg

Som standard skrivs en JSONL-logg per uppgift under:

```text
~/.local/state/ai-coding-agent/costs/
```

Loggen innehåller start-/slutposter, modell, prisversion, pre-call-uppskattning
och faktisk usage-kostnad. Den innehåller **inte** prompttext, filinnehåll eller
API-nycklar. Filen skapas med behörighet `0600`; nya kataloger skapas med
`0700`.
`--cost-log-dir` ändrar platsen, men måste ligga utanför målrepositoryt så att
agenten inte skriver i din ordinarie arbetskopia. Dry-run skriver ingen
kostnadslogg.

### Uppskattad kostnad är inte en garanterad debiteringsgräns

Kostnaden beräknas från API:ets rapporterade tokenusage och den lokala
prislistan. Det kan skilja sig från OpenAI:s faktiska faktura på grund av
prisändringar, long-context/prioritetsnivåer, cache-beteende, reasoning-tokens,
valuta/avgifter eller andra villkor. Kostnaden för ett pågående API-anrop är
inte känd i förväg. `--max-cost` är därför en **konservativ klient-side guard**,
inte en garanterad debiteringsgräns. Om ett anrop ändå går över taket stoppas
alla efterföljande anrop.

## CLI-referens

| Flagga | Beskrivning |
| --- | --- |
| `repo` | Sökväg till Git-repositoryt |
| `task ...` | Fri uppgift (valfri om `--issue` anges) |
| `--issue N` | GitHub-issue att arbeta med |
| `--dry-run` | Endast analys/plan, inga sidoeffekter |
| `--model` | OpenAI-modell (standard `gpt-5.4` eller `OPENAI_MODEL`) |
| `--max-cost USD` | Kostnadstak, standard 2.00 |
| `--reasoning-effort` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh` |
| `--pricing-file` | Egen versionshanterad prislista |
| `--base-branch` | Base-branch för agentbranchen |
| `--worktree-root` | Katalog för worktrees |
| `--cost-log-dir` | Katalog för kostnadsloggar |
| `--test-timeout` | Testtimeout i sekunder (standard 300) |
| `--test-cpus` | CPU-gräns (standard 1.0) |
| `--test-memory` | Minnesgräns (standard 512m) |
| `--test-pids` | Processgräns (standard 128) |
| `--test-profile` | Testprofil, för närvarande `pytest` |
| `--version` | Version |

Det finns medvetet inga flaggor för att automatiskt godkänna plan, filskrivning,
push eller PR. Godkännanden sker interaktivt.

## Säkerhet och kända begränsningar

### Skyddsåtgärder

- Endast uttryckligen definierade verktyg: `list_files`, `read_file`,
  `search_code`, `git_status`, `write_file`, `run_tests`, `submit_summary`.
  Inget generellt shellverktyg.
- Alla externa kommandon körs med argumentlistor, aldrig `shell=True`.
- Sökvägar normaliseras och måste ligga inom worktreen. Path traversal,
  symlink-escape och känsliga filnamn blockeras.
- Filskrivningar kräver godkännande och får bara ske på `agent/`-brancher.
- Repository-, issue- och kommentarsinnehåll behandlas som opålitlig indata i
  systemprompten. Modellgenererad text kan inte godkänna sig själv.
- Terminalutskrifter saneras från ANSI/control-tecken och kända hemligheter.
- Git- och gh-subprocesser får inte OpenAI-nyckeln i sin miljö. Docker får inga
  hemliga miljövariabler alls.
- Commit-hooks och GPG-signering stängs av för agentens commit.
- Push kräver godkännande; PR skapas som draft; merge/close finns inte i
  kodbasen.
- Inga automatiska commits vid testfel eller avbruten körning.

### Begränsningar du måste känna till

- **Docker image build har nätverk** och installerar projektberoenden. Det är
  nödvändigt för att kunna köra tester men innebär att projektets byggkod körs i
  byggcontainern. Använd betrodda repositories.
- **Testkoden kan läsa hela den sanerade projektkopian.** Den kan inte se
  värdens hemkatalog, Docker-socket, SSH-nycklar eller miljövariabler med
  API-nycklar, men kan försöka exfiltrera projektets egen källkod om den har
  nätverk. Testkörningen har `--network none`, men imagebygget har nätverk.
  Detta förutsätter också att hemligheter inte har checkats in i vanliga
  projektfiler; agenten blockerar kända filnamn och mönster men kan inte göra
  anspråk på att hitta varje möjlig hemlighet.
- **Docker-daemonen är betrodd.** Agenten monterar inte socketen, men en
  användare som kan köra Docker på värden har ändå stora befogenheter.
- **Prompt injection kan påverka modellens förslag** och slösa API-budget, men
  kan inte kringgå de hårda godkännandena, verktygslistan, worktree-gränsen,
  Docker-isoleringen eller push/PR-spärrarna.
- **Kostnadstaket är inte faktureringsgaranti** (se ovan). Ett pågående anrop
  kan kosta mer än uppskattningen.
- **Modellen kan göra felaktiga ändringar.** Granska diffen och testerna innan
  du pushar eller mergar.
- **Docker build-cache kan behålla lager** från den tillfälliga testkopian även
  efter att image-taggen tagits bort. Rensa vid behov med `docker builder prune`;
  det påverkar även andra Docker-byggen på maskinen.
- **Worktrees ligger kvar** efter körningen och kan behöva städas manuellt.
- **Python 3.12 + pytest** är den enda färdiga testprofilen. React/TypeScript
  kräver en framtida `TestProfile`.
- **Repositories med enbart `pyproject.toml`** kan behöva ett fungerande
  byggbackend och Python-versionen i testimagen. Saknade systembibliotek ger ett
  tydligt Docker-build-fel; lägg då till dem i projektets miljö eller anpassa
  profilen.
- **Base-branchen kan vara äldre än `origin`.** Agenten gör ingen automatisk
  `git fetch`; kör det själv om du vill utgå från senaste remote-commit.
- **Ingen automatisk återupptagning.** Efter avbrott visas worktree och branch;
  du kan fortsätta manuellt eller köra en ny uppgift.

## Felsökning

### `Not a valid Git working tree`

Kontrollera att sökvägen finns och är ett Git-repository:

```bash
git -C /path/to/repo rev-parse --show-toplevel
```

### `GitHub CLI (gh) is not installed`

```bash
brew install gh
gh auth login
gh auth status
```

### `GitHub CLI is not authenticated`

```bash
gh auth login
```

Agenten använder din befintliga `gh`-autentisering och lagrar inga tokens själv.

### `Docker was not found` eller `daemon is not available`

```bash
brew install --cask docker
open -a Docker
docker info
```

Agenten faller aldrig tillbaka på att köra tester på värddatorn.

### Docker image build failed

Vanliga orsaker:

- projektets beroenden kräver systempaket som inte finns i `python:3.12-slim`,
- `pyproject.toml` kräver en annan Python-version eller byggbackend,
- nätverket når inte paketindex.

Byggfelet visas i terminalen. Ändringarna ligger kvar i worktreen och ingen
commit görs.

### `No tests collected` / pytest exit code 5

Agenten kräver minst ett passerat test för att automatiskt committa. Lägg till
tester eller kör en annan testprofil när sådan finns.

### `No pricing is configured for model ...`

Lägg till modellen i `coding_agent/pricing.json` med version, källa och pris,
eller använd en modell som finns i prislistan. Okända priser antas aldrig vara
noll.

### `Cost limit reached` / `would exceed the remaining budget`

Höj `--max-cost` eller använd en billigare modell. Meddelandet är en
konservativ skyddsgräns, inte en faktureringsgaranti.

### `Git author identity is not configured`

```bash
git config --global user.name "Ditt Namn"
git config --global user.email "du@example.com"
```

### `uncommitted changes` i källarbetskopian

Agenten varnar och worktreen utgår från HEAD, inte från de ocommittade
ändringarna. Committa eller stasha innan du kör för bästa resultat.

### `ModuleNotFoundError: coding_agent` när du kör pytest

Kör från repositoryroten med:

```bash
python -m pytest -q
```

### Push/PR misslyckas

Kontrollera:

```bash
git -C /path/to/repo remote -v
gh auth status
git -C /path/to/worktree status
```

Commiten och worktreen finns kvar även om push eller PR misslyckas. Ingen merge
eller issue-stängning görs av agenten.

## Tester

Enhetstesterna mockar OpenAI- och GitHub-anrop. Det finns även ett lokalt
Git-integrationstest och ett riktigt Docker-test som verifierar att containern
kör som icke-root, saknar nätverk, inte får `OPENAI_API_KEY` och inte kan skriva
i originalrepositoryt. Kommandoargumenten verifieras dessutom för att ingen
hemkatalog, Docker-socket eller hemlig miljövariabel monteras.

```bash
source .venv/bin/activate
python -m pytest -q
python -m pytest -q -m docker
```

Docker-testet hoppas över automatiskt om Docker saknas. Inga riktiga
OpenAI-anrop görs i testsviten; en riktig API-körning kräver din
`OPENAI_API_KEY`.

## Arkitektur

Se [ARCHITECTURE.md](ARCHITECTURE.md) för moduluppdelning, dataflöde,
säkerhetsinvariants och utbyggnad av testprofiler.

## Licens

MIT, se [LICENSE](LICENSE).

## Säkerhetsrapportering

Se [SECURITY.md](SECURITY.md). Rapportera säkerhetsproblem via GitHub:s privata
säkerhetsrapportering, inte som ett öppet issue.

## Manuella installationssteg

Agenten installerar inte systemverktyg eller skapar autentisering åt dig. Följ
därför:

1. Installera Git, Python 3.12, Docker Desktop och `gh` enligt ovan.
2. Starta Docker Desktop och verifiera `docker info`.
3. Kör `gh auth login` och säkerställ att repositoryt har en `origin`.
4. Sätt `OPENAI_API_KEY` i aktuell shell.
5. Kör `pip install -r requirements-dev.txt`.
6. Börja med `--dry-run` för att granska planen utan sidoeffekter.
