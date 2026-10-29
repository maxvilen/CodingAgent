# Arkitektur

Målet är en liten, lättgranskad agent – inte ett generellt agentramverk.
Ansvar är uppdelat i moduler med smala gränssnitt. Alla externa kommandon går
via argumentlistor; ingen modellgenererad text når ett shell.

```text
agent.py
  └─ coding_agent.cli
       ├─ GitRepo                 (repository, branch, worktree, commit, push)
       ├─ GitHubClient            (gh issue/pr, autentisering, metadata)
       ├─ PricingCatalog/CostTracker (priser, tokenusage, budget, JSONL-logg)
       ├─ DockerTestRunner        (sanitized copy, image, container, parser)
       └─ CodingWorkflow
            ├─ ResponsesAgent     (OpenAI Responses API-loop)
            ├─ RepositoryTools    (explicit verktygsallowlist)
            └─ Approval callback  (plan, varje skrivning, push/PR)
```

## Moduler

| Fil | Ansvar |
| --- | --- |
| `agent.py` | Tunn entrypoint, `SystemExit(main())`. |
| `coding_agent/cli.py` | Argumentparsning, förkontroller, cost log-plats, OpenAI-klient, interaktiva godkännanden, exitkoder. |
| `coding_agent/workflow.py` | Orkestrering: analys → plan → godkännande → worktree → edit → tester → commit → push/PR → slutrapport. |
| `coding_agent/openai_agent.py` | Responses API-loop, funktionsanrop, statuskontroll, usage-registrering. |
| `coding_agent/tools.py` | Endast de verktyg modellen får använda; path-validering, diff, skrivning, testkörning, summary. |
| `coding_agent/git_ops.py` | Git-repositoryvalidering, branchallokering, worktree, staging, secret-scan, commit, push. |
| `coding_agent/github_ops.py` | `gh`-wrapper, issuehämtning, acceptance criteria, dependency-referenser, draft-PR. |
| `coding_agent/docker_tests.py` | Sanerad testkopia, Dockerfile, resursbegränsad container, pytest-parser, cleanup. |
| `coding_agent/cost.py` | Versionshanterad prislista, tokenusage, cached/long-context-kostnad, budgetguard, säker logg. |
| `coding_agent/security.py` | Känsliga sökvägar, secret-detektion/redigering, path helper, branch-slug, terminal-sanering, URL-sanering. |
| `coding_agent/command.py` | `subprocess.run` med argumentlistor, timeout och sanitizerade miljöer. |
| `coding_agent/errors.py` | Användarvänliga feltyper och exitkoder. |

## Dataflöde

1. **CLI-förkontroll**
   - `GitRepo` verifierar att sökvägen är ett Git working tree.
   - `PricingCatalog` verifierar att modellen har ett explicit pris.
   - `OPENAI_API_KEY` krävs om ingen testklient injicerats.
   - `GitHubClient.check_available()` krävs för `--issue`.
   - `DockerTestRunner.ensure_available()` krävs utanför dry-run.
   - Kostnadsloggen skapas först därefter (inte i dry-run).

2. **Analysfas**
   - `GitHubClient.fetch_issue()` hämtar metadata via `gh`.
   - `RepositoryTools(phase="analysis")` har endast läsverktyg.
   - `ResponsesAgent` kör Responses API med kostnadskontroll före varje anrop.
   - Modellen returnerar en plan. Inga filer kan ändras.

3. **Godkännande och worktree**
   - Användaren måste godkänna planen.
   - `GitRepo.create_worktree()` allokerar en unik `agent/...`-branch och en
     worktree utanför källarbetskopian.
   - Källans branch och HEAD verifieras oförändrade.

4. **Editfas**
   - `RepositoryTools(phase="edit")` har läsverktyg plus `write_file`,
     `run_tests` och `submit_summary`.
   - Varje `write_file` validerar worktree, känsliga filer, path traversal,
     symlinks och secret-mönster, visar en diff och kräver godkännande.
   - `run_tests` kopierar worktreen till en sanitized testkopia och kör Docker.

5. **Avslut**
   - Workflow kör alltid en full testkörning i Docker efter editfasen.
   - Endast vid `TestResult.success` stageas `touched_files` och commit skapas.
   - Commit-meddelandet innehåller sammanfattning, `Refs #issue` och ingen
     stängningsnyckel.
   - Push/PR är en separat approval-gate. PR skapas som draft.
   - Worktree och lokal commit behålls vid fel, avbrott eller nekad push.

## Säkerhetsinvariants

Dessa egenskaper är avsiktliga och testas:

1. **Ingen skrivning utanför worktreen.** `RepositoryTools._resolve()` resolvar
   sökvägen och kräver att den ligger under worktreen. Känsliga delar blockeras.
2. **Ingen commit på skyddad branch.** `GitRepo.assert_agent_branch()` kräver
   `agent/`-prefix och avvisar `main`, `master`, base-branchen och andra
   icke-agentbrancher.
3. **Endast uppgiftens filer stageas.** `touched_files` från godkända skrivningar
   är den enda staginglistan. Oväntade redan stageade filer avbryter commit.
4. **Inga kända hemligheter i commits.** Känsliga filnamn blockeras och stagead
   diff skannas. Vid träff rensas index och commit avbryts.
5. **Ingen shell.** Alla kommandon är argumentlistor. Modellens strängar kan
   aldrig bli shell-metatecken.
6. **Inga hemligheter i testcontainern.** Endast en sanerad kopia mountas.
   Hostens hemkatalog, samtliga `.env*`-filer, SSH-nycklar, Docker-socket och
   API-nycklar monteras inte. `docker run` får bara explicita, ofarliga
   `-e`-värden.
7. **Ingen host-testfallback.** Saknas Docker avbryts körningen.
8. **Ingen automatisk merge/close.** Det finns inga anrop till `gh pr merge`
   eller `gh issue close`; PR-texten neutraliserar stängningsnyckelord.
9. **Godkännanden kan inte delegeras till modellen.** Approval-callbacken är
   kod, inte ett verktyg. Plan, varje skrivning och push/PR kräver mänskligt `y`.
10. **Terminal och loggar saneras.** ANSI/control-tecken och högkonfidensmönster
    för hemligheter tas bort innan utskrift. Kostnadsloggen innehåller inga
    prompts eller filinnehåll.

## Kostnadsmodell

`pricing.json` är versionshanterad data:

```json
{
  "version": "2026-10-29",
  "source": "https://developers.openai.com/api/docs/pricing",
  "models": {
    "gpt-5.4": {
      "input": 2.5,
      "cached_input": 0.25,
      "output": 15.0,
      "long_context_threshold": 272000,
      "long_context_input": 5.0,
      "long_context_cached_input": 0.5,
      "long_context_output": 22.5,
      "reasoning_efforts": ["none", "low", "medium", "high", "xhigh"]
    }
  }
}
```

Kostnaden per anrop:

```text
((input_tokens - cached_tokens) * input_price
 + cached_tokens * cached_price
 + output_tokens * output_price) / 1_000_000
```

Long-context-priser används om `input_tokens` passerar tröskeln. Reasoning-tokens
ingår i `output_tokens` och räknas inte dubbelt.

Före varje anrop beräknas en konservativ worst-case-kostnad från serialiserad
prompt (25 % marginal) plus `max_output_tokens`. Om `total + estimate` överstiger
`--max-cost` görs inget anrop. Efter anropet registreras faktisk usage och nästa
anrop blockeras om taket redan är nått.

Detta är en klient-side guard, inte en garanterad faktureringsgräns: ett pågående
anrops faktiska kostnad är okänd i förväg, och providerns debitering kan avvika
från den lokala prislistan.

## Docker-kommando (sammanfattning)

```text
docker run --rm
  --network none
  --cpus <n> --memory <m> --memory-swap <m> --pids-limit <n>
  --ulimit nofile=256:256
  --cap-drop ALL --security-opt no-new-privileges
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=128m
  --mount type=bind,src=<sanitized-copy>,dst=/workspace
  --workdir /workspace --user <hostens icke-root-UID, annars 10001>
  --env HOME=/tmp --env PYTHONDONTWRITEBYTECODE=1 ...
  <image> python -m pytest -v -rA --color=no -p no:cacheprovider ...
```

Testkopian skapas med `os.walk` (utan att följa symlinks), exkluderar känsliga
namn och görs skrivbar för container-uid. Originalrepositoryt rörs aldrig.

## Utbyggnad av testprofiler

`TestProfile` har:

- `base_image`
- `dockerfile_setup()`
- `command(test_paths)`
- `parse(stdout, stderr, exit_code)`

Registrera en ny profil i `DockerTestRunner.profiles` (eller skicka en egen
dict). Git-, GitHub-, kostnads- och approval-lagren är oberoende av
testprofilen. En React/TypeScript-profil kan till exempel använda en Node-image
och en parser för Vitest/Jest.

## Teststrategi

- Enhetstester med fake OpenAI-klient och fake `gh`/Docker-runner.
- Riktig OpenAI SDK-serialisering mot `httpx2.MockTransport`, utan nätverk.
- Lokalt Git-integrationstest med temporärt repository och bare remote.
- Riktigt Docker-test som verifierar icke-root, `--network none`, skrivbar
  testkopia, frånvaro av `OPENAI_API_KEY`, hemkatalog och Docker-socket, samt att
  originalrepositoryt inte ändras.
- Test för avbrott: worktree behålls, ingen commit, källbranch orörd.
- Test för dry-run: inga branches, worktrees, filer eller kostnadsloggar.

## Exitkoder

| Kod | Betydelse |
| --- | --- |
| 0 | Klart, eller användaren nekade plan/push (inga skadliga sidoeffekter). |
| 1 | Allmänt fel, testfel eller ingen commit. |
| 2 | Konfigurationsfel: pris, Docker, API-nyckel, GitHub, budget. |
| 3 | OpenAI API-fel. |
| 130 | Avbruten av användaren (Ctrl-C). |
