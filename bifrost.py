#!/usr/bin/env python3
"""
Bifrost – familjens egen appbutik.

Hämtar APK-filer från GitHub Releases, bygger ett F-Droid-repo av dem och
serverar det över HTTP. Familjens enheter lägger till butiken i en F-Droid-klient
(F-Droid, Droid-ify, Neo Store …) och får sedan nya versioner automatiskt.

Allt sparas under /data:
  /data/fdroid/          fdroid-arbetskatalog (config.yml, nyckel, repo/, metadata/)
  /data/www/             det som serveras (startsida + länkar till repo/ och archive/)
  /data/state.json       vilka release-filer som redan hämtats

Inställningar via miljövariabler (se README):
  BIFROST_URL            adressen enheterna når butiken på, t.ex. http://192.168.1.51:8095
  BIFROST_REPOS          GitHub-repon, t.ex. "NullPointerNecromancer/shieldknapp other/app".
                         Lägg till :mönster för att välja filer, t.ex.
                         "edde746/plezy:*arm64-v8a*" – arkiv (.tar.gz/.zip) packas upp automatiskt
  GITHUB_TOKEN           behövs för privata repon (läsrättighet räcker)
  SYNC_INTERVAL_MIN      hur ofta GitHub kollas (standard 15)
  KEEP_RELEASES          hur många av de senaste releaserna som hämtas per repo (standard 3)
  INCLUDE_PRERELEASES    "true" för att ta med pre-releaser (standard false)
  BIFROST_NAME           butikens namn (standard "Bifrost")
  BIFROST_DESCRIPTION    butikens beskrivning
  PORT                   port inne i containern (standard 8080)
"""

from __future__ import annotations

import fnmatch
import hashlib
import html
import http.server
import json
import os
import re
import secrets
import signal
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

DATA = Path(os.environ.get("BIFROST_DATA", "/data"))
FDROID = DATA / "fdroid"
REPO = FDROID / "repo"
ARCHIVE = FDROID / "archive"
METADATA = FDROID / "metadata"
WWW = DATA / "www"
STATE_FILE = DATA / "state.json"
KEYSTORE = FDROID / "keystore.p12"
CONFIG = FDROID / "config.yml"
APP_DIR = Path(__file__).resolve().parent

KEY_ALIAS = "bifrost"
MANUAL_MARKER = "# bifrost: manual"
MANAGED_HEADER = (
    "# Den här filen skrivs av Bifrost vid varje synk.\n"
    f"# Vill du redigera den själv: lägg till raden \"{MANUAL_MARKER}\" så rör Bifrost den inte.\n"
)

GITHUB_API = os.environ.get("BIFROST_GITHUB_API", "https://api.github.com").rstrip("/")  # kan pekas om vid test


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


# --------------------------------------------------------------------------- #
# Inställningar
# --------------------------------------------------------------------------- #

class Settings:
    def __init__(self) -> None:
        self.url = env("BIFROST_URL", "http://localhost:8095").rstrip("/")
        # Varje post är "ägare/repo" eller "ägare/repo:mönster"
        self.sources: list[tuple[str, str]] = []
        for entry in (e for e in re.split(r"[\s,]+", env("BIFROST_REPOS")) if e):
            repo, _, pattern = entry.partition(":")
            if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
                raise SystemExit(f"Ogiltigt repo-namn i BIFROST_REPOS: {entry!r} (skriv ägare/repo eller ägare/repo:mönster)")
            self.sources.append((repo, pattern or "*.apk"))
        self.repos = [r for r, _ in self.sources]
        self.token = env("GITHUB_TOKEN")
        self.interval_min = max(1, int(env("SYNC_INTERVAL_MIN", "15") or 15))
        self.keep_releases = max(1, int(env("KEEP_RELEASES", "3") or 3))
        self.include_prereleases = env("INCLUDE_PRERELEASES", "false").lower() in ("1", "true", "yes", "ja")
        self.name = env("BIFROST_NAME", "Bifrost")
        self.description = env(
            "BIFROST_DESCRIPTION",
            "Familjens egna appar, byggda på GitHub och serverade hemifrån.",
        )
        self.port = int(env("PORT", "8080") or 8080)



# --------------------------------------------------------------------------- #
# Första start: nyckel och fdroid-konfiguration
# --------------------------------------------------------------------------- #

def ensure_layout(s: Settings) -> None:
    for d in (FDROID, REPO, ARCHIVE, METADATA, WWW, REPO / "icons", ARCHIVE / "icons"):
        d.mkdir(parents=True, exist_ok=True)

    secrets_file = FDROID / ".keystorepass"
    if not KEYSTORE.exists():
        password = secrets.token_urlsafe(24)
        log("Första start: skapar butikens signeringsnyckel (spara en backup av /data/fdroid!)")
        subprocess.run(
            [
                "keytool", "-genkeypair", "-noprompt",
                "-keystore", str(KEYSTORE), "-storetype", "PKCS12",
                "-alias", KEY_ALIAS, "-keyalg", "RSA", "-keysize", "4096",
                "-validity", "36500",
                "-storepass", password, "-keypass", password,
                "-dname", f"CN={s.name}, O=Hemma",
            ],
            check=True, capture_output=True,
        )
        secrets_file.write_text(password)
        secrets_file.chmod(0o600)

    password = secrets_file.read_text().strip()

    # Skriv om config.yml varje start så att ändrade miljövariabler slår igenom.
    config = {
        "repo_url": f"{s.url}/repo",
        "repo_name": s.name,
        "repo_description": s.description,
        "repo_icon": "icon.png",
        "archive_url": f"{s.url}/archive",
        "archive_name": f"{s.name} – äldre versioner",
        "archive_description": "Äldre versioner av apparna i " + s.name + ".",
        "archive_icon": "icon.png",
        "archive_older": 3,
        "repo_keyalias": KEY_ALIAS,
        "keystore": KEYSTORE.name,
        "keystorepass": password,
        "keypass": password,
        "keydname": f"CN={s.name}, O=Hemma",
        "make_current_version_link": False,
    }
    # JSON är giltig YAML, så vi slipper ett YAML-bibliotek för att skriva.
    CONFIG.write_text("".join(f"{k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in config.items()))
    CONFIG.chmod(0o600)

    # Kategorier används inte (gav bara varningar i loggen)
    (FDROID / "config" / "categories.yml").unlink(missing_ok=True)

    icon_src = APP_DIR / "icon.png"
    if icon_src.exists():
        for dest in (FDROID / "icon.png", REPO / "icons" / "icon.png", ARCHIVE / "icons" / "icon.png"):
            if not dest.exists() or dest.read_bytes() != icon_src.read_bytes():
                shutil.copyfile(icon_src, dest)

    # Bara repo/ och archive/ exponeras – aldrig nyckeln eller config.yml.
    for name, target in (("repo", REPO), ("archive", ARCHIVE)):
        link = WWW / name
        if link.is_symlink() or link.exists():
            if link.is_symlink() and Path(os.readlink(link)) == target:
                continue
            if link.is_dir() and not link.is_symlink():
                shutil.rmtree(link)
            else:
                link.unlink()
        link.symlink_to(target, target_is_directory=True)
    if icon_src.exists():
        shutil.copyfile(icon_src, WWW / "icon.png")


def repo_fingerprint() -> str:
    """SHA-256 av butikens certifikat – det som F-Droid-klienter litar på."""
    password = (FDROID / ".keystorepass").read_text().strip()
    der = subprocess.run(
        ["keytool", "-exportcert", "-keystore", str(KEYSTORE), "-storetype", "PKCS12",
         "-alias", KEY_ALIAS, "-storepass", password],
        check=True, capture_output=True,
    ).stdout
    return hashlib.sha256(der).hexdigest().upper()


# --------------------------------------------------------------------------- #
# GitHub
# --------------------------------------------------------------------------- #

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_no_redirect_opener = urllib.request.build_opener(_NoRedirect)


def gh_request(s: Settings, path: str, accept: str = "application/vnd.github+json") -> urllib.request.Request:
    req = urllib.request.Request(GITHUB_API + path)
    req.add_header("Accept", accept)
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "bifrost-appstore")
    if s.token:
        req.add_header("Authorization", f"Bearer {s.token}")
    return req


def gh_json(s: Settings, path: str):
    with urllib.request.urlopen(gh_request(s, path), timeout=30) as resp:
        return json.load(resp)


def download_asset(s: Settings, repo: str, asset_id: int, dest: Path) -> str:
    """Laddar ner en release-fil (fungerar även för privata repon). Returnerar sha256."""
    req = gh_request(s, f"/repos/{repo}/releases/assets/{asset_id}", accept="application/octet-stream")
    try:
        resp = _no_redirect_opener.open(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            raise
        # GitHub skickar vidare till en förinloggad nedladdningslänk. Den ska
        # hämtas UTAN vår token, annars nekar lagringstjänsten anropet.
        location = e.headers["Location"]
        plain = urllib.request.Request(location, headers={"User-Agent": "bifrost-appstore"})
        resp = urllib.request.urlopen(plain, timeout=300)

    sha = hashlib.sha256()
    tmp = dest.with_suffix(dest.suffix + ".part")
    with resp, open(tmp, "wb") as out:
        while chunk := resp.read(1 << 16):
            sha.update(chunk)
            out.write(chunk)
    tmp.replace(dest)
    return sha.hexdigest()


ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar", ".zip")
MAX_APK_BYTES = 1 << 30   # 1 GB – skydd mot trasiga eller elaka arkiv


def is_archive(name: str) -> bool:
    return name.lower().endswith(ARCHIVE_SUFFIXES)


def wanted(name: str, pattern: str) -> bool:
    """Ska den här release-filen med? Bara APK:er och arkiv som matchar mönstret."""
    lower = name.lower()
    if not (lower.endswith(".apk") or is_archive(lower)):
        return False
    return fnmatch.fnmatch(lower, pattern.lower())


def _copy_member(src, dest: Path) -> str:
    sha = hashlib.sha256()
    total = 0
    tmp = dest.with_suffix(dest.suffix + ".part")
    with src, open(tmp, "wb") as out:
        while chunk := src.read(1 << 16):
            total += len(chunk)
            if total > MAX_APK_BYTES:
                raise ValueError("APK:n i arkivet är orimligt stor")
            sha.update(chunk)
            out.write(chunk)
    tmp.replace(dest)
    return sha.hexdigest()


def extract_apk(archive: Path, dest: Path) -> str:
    """
    Plockar ut den enda .apk-filen ur ett arkiv till dest och returnerar dess sha256.
    Inget packas upp till disk förutom själva APK:n, så sökvägar i arkivet spelar ingen roll.
    """
    if archive.name.lower().endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            apks = [i for i in zf.infolist() if not i.is_dir() and i.filename.lower().endswith(".apk")]
            if len(apks) != 1:
                raise ValueError(f"arkivet innehåller {len(apks)} APK-filer, väntade exakt 1")
            return _copy_member(zf.open(apks[0]), dest)

    with tarfile.open(archive, "r:*") as tf:
        apks = [m for m in tf.getmembers() if m.isfile() and m.name.lower().endswith(".apk")]
        if len(apks) != 1:
            raise ValueError(f"arkivet innehåller {len(apks)} APK-filer, väntade exakt 1")
        src = tf.extractfile(apks[0])
        if src is None:
            raise ValueError("kunde inte läsa APK:n i arkivet")
        return _copy_member(src, dest)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"assets": {}, "repos": {}}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    tmp.replace(STATE_FILE)


def fetch_new_apks(s: Settings, state: dict) -> int:
    """Hämtar APK:er från de senaste releaserna. Returnerar antal nya filer."""
    new_files = 0
    state.setdefault("assets", {})
    state.setdefault("repos", {})

    for repo, pattern in s.sources:
        try:
            info = gh_json(s, f"/repos/{repo}")
            releases = gh_json(s, f"/repos/{repo}/releases?per_page=30")
        except urllib.error.HTTPError as e:
            hint = " (privat repo? kolla GITHUB_TOKEN)" if e.code in (401, 403, 404) else ""
            log(f"{repo}: GitHub svarade {e.code}{hint}")
            continue
        except urllib.error.URLError as e:
            log(f"{repo}: kunde inte nå GitHub: {e.reason}")
            continue

        state["repos"][repo] = {
            "description": info.get("description") or "",
            "html_url": info.get("html_url") or f"https://github.com/{repo}",
            "homepage": info.get("homepage") or "",
            "owner": (info.get("owner") or {}).get("login", repo.split("/")[0]),
        }

        picked = [
            r for r in releases
            if not r.get("draft") and (s.include_prereleases or not r.get("prerelease"))
        ][: s.keep_releases]

        if not picked:
            log(f"{repo}: inga releaser ännu")
            continue

        for rel in picked:
            for asset in rel.get("assets", []):
                name = asset.get("name", "")
                if not wanted(name, pattern):
                    continue
                key = str(asset["id"])
                if key in state["assets"]:
                    continue
                safe_repo = repo.replace("/", "_")
                safe_tag = re.sub(r"[^\w.-]", "_", rel.get("tag_name") or "untagged")
                dest = REPO / f"{safe_repo}-{safe_tag}-{asset['id']}.apk"
                log(f"{repo}: hämtar {name} från {rel.get('tag_name')}")
                try:
                    if is_archive(name):
                        with tempfile.TemporaryDirectory(dir=DATA) as tmpdir:
                            archive = Path(tmpdir) / re.sub(r"[^\w.-]", "_", name)
                            download_asset(s, repo, asset["id"], archive)
                            sha = extract_apk(archive, dest)
                        log(f"{repo}: packade upp APK ur {name}")
                    else:
                        sha = download_asset(s, repo, asset["id"], dest)
                except (ValueError, tarfile.TarError, zipfile.BadZipFile) as e:
                    # Filen är trasig eller har fel innehåll – hämta den inte igen varje synk
                    log(f"{repo}: hoppar över {name}: {e}")
                    dest.unlink(missing_ok=True)
                    state["assets"][key] = {"repo": repo, "tag": rel.get("tag_name"), "name": name,
                                            "skipped": str(e)}
                    continue
                except Exception as e:  # noqa: BLE001 – nätverksfel o.d., försök igen nästa gång
                    log(f"{repo}: {name} misslyckades: {e}")
                    dest.unlink(missing_ok=True)
                    continue
                state["assets"][key] = {
                    "repo": repo,
                    "tag": rel.get("tag_name"),
                    "name": name,
                    "sha256": sha,
                    "release_notes": (rel.get("body") or "").strip(),
                }
                new_files += 1
        save_state(state)
    return new_files


# --------------------------------------------------------------------------- #
# F-Droid
# --------------------------------------------------------------------------- #

def run_fdroid_update(create_metadata: bool) -> bool:
    cmd = ["fdroid", "update", "--rename-apks", "--use-date-from-apk"]
    if create_metadata:
        cmd.append("--create-metadata")
    log("Kör: " + " ".join(cmd))
    res = subprocess.run(cmd, cwd=FDROID, capture_output=True, text=True)
    for line in (res.stdout + res.stderr).splitlines():
        if line.strip():
            log("  fdroid: " + line)
    if res.returncode != 0:
        log(f"fdroid update misslyckades (kod {res.returncode})")
        return False
    return True


def read_index() -> dict:
    """Läser fdroids index och returnerar {packageName: {...}} i ett enkelt format."""
    apps: dict[str, dict] = {}
    v1 = REPO / "index-v1.json"
    v2 = REPO / "index-v2.json"
    if v1.exists():
        data = json.loads(v1.read_text())
        meta = {a["packageName"]: a for a in data.get("apps", [])}
        for pkg, versions in data.get("packages", {}).items():
            a = meta.get(pkg, {})
            apps[pkg] = {
                "name": a.get("name") or (a.get("localized", {}).get("en-US", {}).get("name")) or pkg,
                "summary": a.get("summary") or "",
                "icon": f"icons-640/{a['icon']}" if a.get("icon") else "",
                "versions": [
                    {"name": v.get("versionName"), "code": v.get("versionCode"),
                     "file": v.get("apkName"), "sha256": v.get("hash")}
                    for v in versions
                ],
            }
    elif v2.exists():
        data = json.loads(v2.read_text())
        for pkg, p in data.get("packages", {}).items():
            md = p.get("metadata", {})
            name = md.get("name", {})
            summary = md.get("summary", {})
            vers = []
            for sha, v in p.get("versions", {}).items():
                man = v.get("manifest", {})
                vers.append({"name": man.get("versionName"), "code": man.get("versionCode"),
                             "file": v.get("file", {}).get("name", "").lstrip("/"), "sha256": sha})
            icon = md.get("icon", {})
            icon_name = next(iter(icon.values()), {}).get("name", "") if isinstance(icon, dict) and icon else ""
            apps[pkg] = {
                "icon": icon_name.lstrip("/"),
                "name": next(iter(name.values()), pkg) if isinstance(name, dict) else (name or pkg),
                "summary": next(iter(summary.values()), "") if isinstance(summary, dict) else (summary or ""),
                "versions": vers,
            }
    for a in apps.values():
        a["versions"].sort(key=lambda v: v.get("code") or 0, reverse=True)
    return apps


def yaml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def write_metadata(state: dict, index: dict) -> bool:
    """Fyller i beskrivning, källkodslänk m.m. från GitHub. Returnerar True om något ändrades."""
    sha_to_repo = {a["sha256"]: a["repo"] for a in state.get("assets", {}).values() if "sha256" in a}
    changed = False

    for pkg, app in index.items():
        repo = next((sha_to_repo.get(v["sha256"]) for v in app["versions"] if v["sha256"] in sha_to_repo), None)
        if not repo:
            continue
        info = state.get("repos", {}).get(repo, {})
        path = METADATA / f"{pkg}.yml"
        if path.exists() and MANUAL_MARKER in path.read_text():
            continue

        desc = info.get("description", "")
        lines = [
            MANAGED_HEADER,
            f"AuthorName: {yaml_str(info.get('owner', ''))}\n",
            f"SourceCode: {yaml_str(info.get('html_url', ''))}\n",
        ]
        if info.get("homepage"):
            lines.append(f"WebSite: {yaml_str(info['homepage'])}\n")
        if desc:
            summary = desc if len(desc) <= 80 else desc[:77].rstrip() + "…"
            lines.append(f"Summary: {yaml_str(summary)}\n")
            lines.append(f"Description: {yaml_str(desc)}\n")
        content = "".join(lines)

        if not path.exists() or path.read_text() != content:
            path.write_text(content)
            changed = True
            log(f"Metadata uppdaterad för {pkg} ({repo})")
    return changed


# --------------------------------------------------------------------------- #
# Startsida
# --------------------------------------------------------------------------- #

def qr_svg(data: str) -> str:
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return ""
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    svg = img.to_string(encoding="unicode") if hasattr(img, "to_string") else ""
    if isinstance(svg, bytes):
        svg = svg.decode()
    return re.sub(r"<\?xml[^>]*\?>", "", svg)


def write_landing_page(s: Settings, index: dict, fingerprint: str) -> None:
    repo_url = f"{s.url}/repo"
    add_url = f"{repo_url}?fingerprint={fingerprint}"
    scheme = "fdroidrepos" if repo_url.startswith("https://") else "fdroidrepo"
    deep_link = f"{scheme}://{repo_url.split('://', 1)[1]}?fingerprint={fingerprint}"
    e = html.escape

    rows = []
    for pkg, app in sorted(index.items(), key=lambda kv: kv[1]["name"].lower()):
        latest = app["versions"][0] if app["versions"] else {}
        icon = f"repo/{app['icon']}" if app.get("icon") else "icon.png"
        apk = f"repo/{latest.get('file')}" if latest.get("file") else ""
        rows.append(
            f"""<li class="app">
  <img src="{e(icon)}" alt="" onerror="this.onerror=null;this.src='icon.png'">
  <div class="meta"><strong>{e(app['name'])}</strong>
    <span>{e(app['summary'])}</span>
    <small>{e(pkg)} · version {e(str(latest.get('name') or '?'))}</small></div>
  {f'<a class="dl" href="{e(apk)}">APK</a>' if apk else ''}
</li>"""
        )
    apps_html = "\n".join(rows) or '<li class="empty">Inga appar ännu – de dyker upp här när en release finns på GitHub.</li>'

    page = f"""<!doctype html>
<html lang="sv"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(s.name)}</title>
<link rel="icon" href="icon.png">
<style>
:root{{--bg:#11131a;--card:#1b1f2a;--fg:#eceef3;--mut:#9aa3b5;--acc:#8fb8ff}}
@media (prefers-color-scheme:light){{:root{{--bg:#f4f5f8;--card:#fff;--fg:#151821;--mut:#5d6474;--acc:#2f63d6}}}}
*{{box-sizing:border-box}}body{{margin:0;font:16px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg)}}
main{{max-width:760px;margin:0 auto;padding:32px 16px 64px}}
header{{display:flex;gap:16px;align-items:center}}header img{{width:64px;height:64px;border-radius:16px}}
h1{{margin:0;font-size:28px}}p.lead{{margin:4px 0 0;color:var(--mut)}}
section{{background:var(--card);border-radius:16px;padding:20px;margin-top:20px}}
h2{{margin:0 0 12px;font-size:18px}}
.add{{display:flex;gap:20px;align-items:center;flex-wrap:wrap}}
.qr{{background:#fff;border-radius:12px;padding:8px;width:180px;height:180px}}.qr svg{{width:100%;height:100%}}
.btn{{display:inline-block;background:var(--acc);color:#fff;text-decoration:none;padding:10px 16px;border-radius:10px;font-weight:600}}
code{{font-size:13px;word-break:break-all;color:var(--mut)}}
ol{{margin:8px 0 0;padding-left:20px}}
ul.apps{{list-style:none;margin:0;padding:0}}
.app{{display:flex;gap:14px;align-items:center;padding:10px 0;border-top:1px solid rgba(127,127,127,.2)}}
.app:first-child{{border-top:0}}.app img{{width:48px;height:48px;border-radius:12px;flex:none}}
.meta{{flex:1;display:flex;flex-direction:column}}.meta span,.meta small{{color:var(--mut)}}
.dl{{color:var(--acc);text-decoration:none;font-weight:600}}
.sync{{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-top:16px}}.sync small{{color:var(--mut)}}
.sync button{{font:inherit;font-weight:600;border:1px solid var(--acc);color:var(--acc);background:none;border-radius:10px;padding:8px 14px;cursor:pointer}}.empty{{color:var(--mut)}}
</style></head>
<body><main>
<header><img src="icon.png" alt=""><div><h1>{e(s.name)}</h1><p class="lead">{e(s.description)}</p></div></header>

<section><h2>Lägg till butiken</h2>
<div class="add">
  <div class="qr">{qr_svg(add_url)}</div>
  <div>
    <ol>
      <li>Installera <b>F-Droid</b> eller <b>Droid-ify</b> på telefonen.</li>
      <li>Skanna QR-koden – eller tryck på knappen om du läser det här på telefonen.</li>
      <li>Godkänn att lägga till {e(s.name)}.</li>
    </ol>
    <p><a class="btn" href="{e(deep_link)}">Lägg till i appbutiken</a></p>
    <code>{e(add_url)}</code>
  </div>
</div></section>

<section><h2>Appar</h2><ul class="apps">
{apps_html}
</ul>
<form class="sync" method="post" action="sync">
  <button type="submit">Synka nu</button>
  <small>Senast kollat {e(time.strftime("%Y-%m-%d %H:%M"))} · kollar GitHub var {s.interval_min}:e minut</small>
</form></section>
</main></body></html>
"""
    tmp = WWW / "index.html.tmp"
    tmp.write_text(page)
    tmp.replace(WWW / "index.html")


# --------------------------------------------------------------------------- #
# Synk-loop och webbserver
# --------------------------------------------------------------------------- #

def sync_once(s: Settings) -> bool:
    state = load_state()
    new = fetch_new_apks(s, state)
    first_time = not (REPO / "index-v1.json").exists() and not (REPO / "index-v2.json").exists()

    ok = True
    if new or first_time or state.get("needs_update") or state.get("settings_url") != s.url:
        ok = run_fdroid_update(create_metadata=True)
        if ok and write_metadata(state, read_index()):
            ok = run_fdroid_update(create_metadata=False)
        state["needs_update"] = not ok
        if ok:
            state["settings_url"] = s.url
        save_state(state)
    elif write_metadata(state, read_index()):
        ok = run_fdroid_update(create_metadata=False)
        state["needs_update"] = not ok
        save_state(state)

    write_landing_page(s, read_index(), repo_fingerprint())
    log(f"Synk klar: {new} nya filer, {len(read_index())} appar i butiken")
    return ok


# Sätts av "Synka nu" på startsidan och väcker synk-loopen direkt
SYNC_NOW = threading.Event()


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".apk": "application/vnd.android.package-archive",
        ".jar": "application/java-archive",
        ".json": "application/json",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WWW), **kwargs)

    def end_headers(self):
        # Index-filerna ska aldrig cachas, annars ser klienterna inte nya versioner.
        if re.search(r"/(index[^/]*|entry)\.(jar|json|html)$|/$", self.path):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_POST(self):
        if self.path.rstrip("/").endswith("/sync") or self.path == "/sync":
            log("Synk begärd från startsidan")
            SYNC_NOW.set()
            self.send_response(303)
            self.send_header("Location", "./")
            self.end_headers()
            return
        self.send_error(404)

    def log_message(self, fmt, *args):
        if env("BIFROST_ACCESS_LOG").lower() in ("1", "true", "yes"):
            log("http: " + (fmt % args))


def serve(s: Settings) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer(("0.0.0.0", s.port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"Webbservern lyssnar på port {s.port} – öppna {s.url}")
    return server


def main(argv: list[str]) -> int:
    # Som PID 1 i Docker ignoreras SIGTERM annars, och containern dödas först
    # efter 10 s ("exited with code 137"). Avsluta snyggt direkt i stället.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    s = Settings()
    ensure_layout(s)
    fp = repo_fingerprint()
    log(f"{s.name} startar. Repo: {s.url}/repo  Fingeravtryck: {fp}")
    if not s.repos:
        log("BIFROST_REPOS är tomt – butiken startar utan appar")
    for repo, pattern in s.sources:
        log(f"Källa: {repo}" + ("" if pattern == "*.apk" else f"  (filer: {pattern})"))
    if s.repos and not s.token:
        log("GITHUB_TOKEN saknas – bara publika repon kan läsas")

    if "--once" in argv:
        return 0 if sync_once(s) else 1

    serve(s)
    while True:
        try:
            sync_once(s)
        except Exception as e:  # noqa: BLE001 – loopen får aldrig dö
            log(f"Synk misslyckades: {e!r}")
        # Vänta till nästa intervall – eller tills någon trycker "Synka nu"
        SYNC_NOW.wait(timeout=s.interval_min * 60)
        SYNC_NOW.clear()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
