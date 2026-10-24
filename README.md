# Bifrost

Familjens egen appbutik. Bifrost hämtar APK-filer från dina GitHub Releases, gör ett signerat F-Droid-repo av dem och serverar det från hemservern. Familjens telefoner och TV-boxar lägger till butiken en gång och får sedan nya versioner automatiskt.

```
GitHub (privata repon)                Hemservern (Dockge)                 Familjens enheter
┌────────────────────┐   var 15:e   ┌──────────────────────────┐        ┌──────────────────┐
│ shieldknapp        │   minut      │ Bifrost                  │  http  │ F-Droid /        │
│  └ Release v0.2.0  │ ───────────► │  hämtar nya APK:er       │ ◄───── │ Droid-ify        │
│     shieldknapp.apk│   (token)    │  fdroid update → index   │        │ (hemma eller VPN)│
│ nästa-app …        │              │  webbserver :8095        │        └──────────────────┘
└────────────────────┘              └──────────────────────────┘
```

- Repona kan vara **privata**. Bara servern har GitHub-token, familjens enheter behöver inget konto.
- Inget öppnas mot internet. Servern ringer ut till GitHub, och enheterna når butiken hemma eller via VPN.
- Startsidan på `http://192.168.1.51:8095` visar alla appar och en QR-kod som lägger till butiken i appen.

---

## 1. Lägg upp koden och bygg imagen

1. Skapa repot `bifrost` på GitHub och pusha koden. GitHub Actions bygger då Docker-imagen, testar den och publicerar den som `ghcr.io/nullpointernecromancer/bifrost:latest`.
2. Imagen hamnar under **Packages** på din GitHub-profil. Är repot privat blir även imagen privat. Välj ett av två sätt:
   - **Enklast:** Packages → `bifrost` → Package settings → *Change visibility* → **Public**. Imagen innehåller ingen hemlighet, bara koden.
   - Eller låt den vara privat och logga in på servern en gång: `docker login ghcr.io -u NullPointerNecromancer` med en token som har `read:packages`.

## 2. Skapa en GitHub-token för servern

GitHub → Settings → Developer settings → **Fine-grained tokens** → *Generate new token*:

- **Repository access:** *Only select repositories* → välj de app-repon som ska in i butiken (t.ex. `shieldknapp`)
- **Permissions → Repository → Contents:** *Read-only* (det räcker för att läsa releaser)
- Sätt en lång giltighetstid och skriv upp i kalendern när den går ut.

Lägger du till fler app-repon senare: redigera token och kryssa i de nya repona.

## 3. Starta i Dockge

1. Skapa dataseten `docker/bifrost/data` på `Rodin-27TB-Z2` (samma mönster som dina andra appar).
2. Dockge → **+ Compose** → namn `bifrost` → klistra in `compose.yaml` från det här repot.
3. Fyll i `.env`-rutan i Dockge:
   ```
   GITHUB_TOKEN=github_pat_xxxxxxxx
   ```
4. Kontrollera `BIFROST_URL` (serverns IP + port) och `BIFROST_REPOS`, och tryck sedan **Deploy**.
5. Öppna `http://192.168.1.51:8095` – där ska butiken synas.

Loggen i Dockge visar vad som händer, t.ex. `shieldknapp: hämtar shieldknapp.apk från v0.1.0`.

> **Ta backup på `docker/bifrost/data/fdroid/`!** Där ligger butikens signeringsnyckel (`keystore.p12` + `.keystorepass`). Försvinner den måste alla enheter ta bort och lägga till butiken igen. Det är inte farligt, men det är omständligt. En ZFS-snapshot-policy på dataseten räcker gott.

## 4. Lägg till butiken på enheterna

**Telefon:** installera [F-Droid](https://f-droid.org) eller Droid-ify. Öppna startsidan i mobilens webbläsare och tryck *Lägg till i appbutiken*, eller skanna QR-koden från datorn.

**Android TV / Shield:** F-Droid-klienten går att installera på TV:n och styra med pilknapparna, men den är inte gjord för TV. För TV kan det vara enklast att installera första gången med `adb install`. Har APK:n samma signering, vilket alla dina appar får från sina repon, kan klienten sedan uppdatera den.

**Borta hemifrån:** slå på VPN:en till hemmanätet så fungerar allt som vanligt.

## 5. Lägga till fler appar

1. Appens repo behöver en GitHub Release med en `.apk` som bifogad fil. ShieldKnapp gör det automatiskt när du pushar en tagg, t.ex. `git tag v0.2.0 && git push origin v0.2.0`.
2. Lägg till repot i `BIFROST_REPOS` (mellanslag mellan repona) och i token-behörigheten.
3. Tryck **Deploy** i Dockge igen. Appen dyker upp inom en minut.

> Varje ny version måste ha ett högre `versionCode` än den förra. Det är så Android och butiken vet vad som är nyast. ShieldKnapp använder byggets körnummer, så det sköter sig självt.

## Appar från andra (open source på GitHub)

Bifrost kan hämta appar direkt från utvecklarens egna GitHub-releaser – ingen Play Butik, ingen mellanhand. Appen behåller utvecklarens signering.

Många projekt släpper flera filer per version (en per processortyp, eller packade i arkiv). Lägg då till ett **mönster** efter ett kolon:

```yaml
BIFROST_REPOS: >-
  NullPointerNecromancer/shieldknapp
  edde746/plezy:*arm64-v8a*
```

- Mönstret matchas mot filnamnen i releasen (`*` = vad som helst, skiftläget spelar ingen roll).
- Matchar det ett arkiv (`.tar.gz`, `.tgz`, `.tar`, `.zip`) packar Bifrost upp APK:n automatiskt. Arkivet måste innehålla exakt en APK.
- Utan mönster tas alla `.apk`-filer.
- Utkast (draft) hoppas alltid över, och pre-releaser om inte `INCLUDE_PRERELEASES=true`.
- `arm64-v8a` passar alla moderna telefoner (alla Pixel med GrapheneOS) och Shield. Osäker? `adb shell getprop ro.product.cpu.abilist`.

Publika repon behöver ingen extra behörighet på token.

## Synka nu

Bifrost kollar GitHub var 15:e minut. Vill du inte vänta: tryck **Synka nu** längst ned på startsidan.

## Inställningar

| Variabel | Standard | Betydelse |
|---|---|---|
| `BIFROST_URL` | – | Adressen enheterna når butiken på. Bestämmer också QR-koden. |
| `BIFROST_REPOS` | – | `ägare/repo` eller `ägare/repo:mönster`, med mellanslag emellan |
| `GITHUB_TOKEN` | – | Krävs för privata repon |
| `SYNC_INTERVAL_MIN` | `15` | Hur ofta GitHub kollas |
| `KEEP_RELEASES` | `3` | Hur många av de senaste releaserna som hämtas per app |
| `INCLUDE_PRERELEASES` | `false` | `true` för att ta med pre-releaser (t.ex. betor) |
| `BIFROST_NAME` | `Bifrost` | Butikens namn i apparna |
| `BIFROST_DESCRIPTION` | … | Butikens beskrivning |
| `BIFROST_ACCESS_LOG` | `false` | `true` loggar varje webbanrop |

Byter du `BIFROST_URL` senare (t.ex. när du får lokal DNS) behöver enheterna lägga till butiken igen med den nya adressen.

## Beskrivningar och ikoner

Bifrost hämtar appens beskrivning och källkodslänk från GitHub-repots *About*-text. Ikonen läses ur APK:n, så appen behöver PNG-ikoner (`mipmap-*/ic_launcher.png`). Vektorikoner kan F-Droid inte visa.

Vill du skriva en längre beskrivning själv: redigera `data/fdroid/metadata/<paketnamn>.yml` och lägg till raden `# bifrost: manual` överst. Då låter Bifrost filen vara.

## Felsökning

| Problem | Prova |
|---|---|
| `GitHub svarade 404` i loggen | Repot är privat och token saknar behörighet till just det repot. |
| `GitHub svarade 401` | Token har gått ut eller är felinskriven. |
| Appen syns inte | Finns det en Release (inte bara en tagg) med en `.apk` bifogad? Pre-releaser hoppas över som standard. |
| Ny version syns inte i appen | Dra nedåt för att uppdatera i F-Droid. Kolla att `versionCode` faktiskt ökat. |
| Kör en synk direkt | `docker exec bifrost python3 /app/bifrost.py --once` |

## Filer

```
bifrost.py                     allt: GitHub-synk, fdroid update, metadata, startsida, webbserver
Dockerfile                     Debian + fdroidserver
compose.yaml                   stack för Dockge
.github/workflows/docker.yml   bygger, röktestar och publicerar imagen
icon.png                       butikens ikon
```
