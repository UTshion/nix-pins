#!/usr/bin/env python3
"""nix-pins — dashboard + auto-updater for the Entries this repo Maintains on top
of nixpkgs (overlays/, packages/, pinned flake inputs).

Vocabulary (see CONTEXT.md): an *Entry* is one tracked unit; a *Pin* is an Entry
that fixes a version (derived from versionVar/versionRegex); *Purpose* says why
the Entry exists (version-lag | compat | packaging) and thus when it can be
deleted; *Wired* means flake.nix references it (≠ installed on a host).

Subcommands:
  check   (default) compare every Pin against the upstream latest and the
          flake-locked nixpkgs, evaluate compat Entries' stillNeeded predicates,
          lint for Drift (overlay/package files missing from manifest, and
          manifest entries whose file is gone), print a dashboard.
  update  bump version + hash(es) in place, then verify with `nix build` where the
          current platform can. On build failure the file is reverted (the attempted
          diff and the build log are printed so the phases can be fixed by hand).
          Under --all, major version bumps are skipped unless --major is given.
  sync    warn → pick by number → run the batch (adopt / update / disable / input).
  list    show the entries.
  disable/enable  flip an Entry off/on, reversibly.

Exit codes: 0 = clean, 1 = work to do (outdated pin, drift, review), 2 = the tool
or its configuration is broken (bad manifest entry, failed eval).

The Nix half lives in eval.nix, exposed by the flake output; this script never
composes Nix expressions in strings. Evaluation is pure and uses the flake's own
locked inputs (report/src/drv/installed/overlays all come from the flake).
Everything the updater needs (URLs, hash mode, artifact names) is derived by
evaluating the overlay/package itself, so no URL templates are duplicated here.
Entries come from two places: manifest (via the flake, so `stillNeeded` is a real
function and `enable` is the actual wiring), and the self-describing Auto Pin
data files in overlays/auto/*.json — those are the entry, so they are never
listed in the manifest.

Stdlib only. Requires: nix (flakes enabled), network; `gh` is used for the
GitHub API when available (falls back to plain HTTPS, honoring $GITHUB_TOKEN).

Environment variables:
  NIX_PINS_DIR    path to the directory where this script lives (parent of
                  the current repo root when invoked from a user's repo).
                  When not set, REPO defaults to the parent directory.
"""

import argparse
import base64
import concurrent.futures
import datetime
import difflib
import json
import os
import platform as _platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
# Allow overriding the repo root (for standalone usage outside a user repo).
# When not set, REPO defaults to the parent directory (scripts/nix-pins/… in a user repo).
_NIX_PINS_DIR = os.environ.get("NIX_PINS_DIR", None)
if _NIX_PINS_DIR:
    REPO = Path(_NIX_PINS_DIR).resolve().parent
else:
    REPO = SCRIPT_DIR.parents[1]
MANIFEST = SCRIPT_DIR / "manifest.nix"

# Platform names known to the flake (`.#nix-pins.manifest.platforms`), filled in
# by load_entries(). The platform → nixpkgs-input mapping itself lives in
# flake.nix, next to the inputs it names — not here.
PLATFORMS = []

# nixpkgs mirror:// prefixes we may meet when evaluating src URLs (fetchPypi etc.)
MIRRORS = {
    "mirror://pypi/": "https://files.pythonhosted.org/packages/source/",
}

HTTP_TIMEOUT = 20
HTTP_ATTEMPTS = 3
USE_COLOR = True

# `source = "cmd:..."` runs a shell command out of the manifest, so it is opt-in.
ALLOW_CMD = False

# 0 = clean, 1 = work to do, 2 = the tool/config is broken. Keeping these apart
# lets CI tell "postman is outdated" from "the manifest no longer parses".
EXIT_OK = 0
EXIT_ACTION = 1
EXIT_ERROR = 2

# ── cask 監視(brew-nix 経由のインストール)──────────────────────────────────
# modules/darwin/brew-nix.nix の非コメント `brewCasks.<token>` 行を wired な
# cask とみなす。base は brew-api input(flake.lock で固定されたスナップショット)
# の cask.json、latest は formulae.brew.sh の live API。差分は sync で
# Auto Pin(overlays/auto/<token>.json)として個別に先回りできる。
CASK_MODULE_REL = "modules/darwin/brew-nix.nix"

# Auto Pins: one JSON data file per pin, read by overlays/auto/default.nix.
# The file is the entry — there is no manifest row to keep in sync.
AUTO_REL = "overlays/auto"
AUTO_SCHEMA = 1  # must match `schema` in overlays/auto/default.nix

# Paths nix-pins may rewrite. Mutating subcommands require these to be clean, so
# that `git checkout --` is always a complete undo.
OWNED_PATHS = [
    "manifest.nix",
    "overlays",
    "packages",
    "flake.nix",
    "flake.lock",
]


class ConfigError(RuntimeError):
    """The manifest / pin data is wrong — exit 2, not 1."""


# ── small helpers ────────────────────────────────────────────────────────────


def color(code, s):
    return f"\x1b[{code}m{s}\x1b[0m" if USE_COLOR else s


def green(s):
    return color("32", s)


def red(s):
    return color("1;31", s)


def yellow(s):
    return color("33", s)


def cyan(s):
    return color("36", s)


def dim(s):
    return color("2", s)


def bold(s):
    return color("1", s)


def current_system():
    m = _platform.machine().lower()
    arch = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64", "amd64": "x86_64"}.get(m, m)
    osname = "darwin" if sys.platform == "darwin" else "linux"
    return f"{arch}-{osname}"


def run(cmd, check=True, timeout=None):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=REPO)
    if check and p.returncode != 0:
        raise RuntimeError(
            f"command failed ({p.returncode}): {' '.join(map(shlex.quote, cmd))}\n"
            + p.stderr.strip()[-2000:]
        )
    return p


def require_clean_tree(args):
    """Mutating subcommands want a clean tree for the paths nix-pins writes to,
    so that `git checkout -- <path>` is always a complete undo — including when
    the process is interrupted half-way through a multi-file edit.
    """
    if getattr(args, "allow_dirty", False):
        return True
    p = run(["git", "status", "--porcelain", "--", *OWNED_PATHS], check=False)
    if p.returncode != 0:
        print(yellow("warning: could not read git status; skipping the clean-tree check"),
              file=sys.stderr)
        return True
    dirty = [line for line in p.stdout.splitlines() if line.strip()]
    if not dirty:
        return True
    print(red("✗ the files nix-pins writes to already have uncommitted changes:"))
    for line in dirty[:20]:
        print(red(f"    {line}"))
    if len(dirty) > 20:
        print(red(f"    … and {len(dirty) - 20} more"))
    print("  commit or stash them first, so `git checkout --` can undo this run,")
    print("  or pass --allow-dirty to proceed without that safety net")
    return False


# ── flake evaluation ─────────────────────────────────────────────────────────
# All evaluation goes through the flake's own `nix-pins` output (eval.nix): the
# CLI never composes Nix expressions in strings, evaluation is pure (no
# --impure), and the locked inputs come from the flake itself.


def flake_attr(*parts):
    """Quote an attrpath for the nix CLI: nix-pins.src."raycast-darwin"."aarch64-darwin"."""
    quoted = []
    for part in parts:
        if '"' in part:
            raise ConfigError(f"attrpath component contains a quote: {part!r}")
        quoted.append(part if re.fullmatch(r"[A-Za-z0-9_-]+[A-Za-z0-9_.-]*", part) and "." not in part else f'"{part}"')
    return ".".join(quoted)


def flake_eval(attrpath, raw=False, apply=None):
    mode = "--raw" if raw else "--json"
    cmd = ["nix", "eval", mode, f".#{attrpath}"]
    if apply:
        cmd += ["--apply", apply]
    p = run(cmd, check=False)
    if p.returncode != 0:
        raise RuntimeError(f"nix eval .#{attrpath} failed:\n{p.stderr.strip()[-2000:]}")
    return p.stdout.strip() if raw else json.loads(p.stdout)


def installed_map():
    """`.#nix-pins.installed`, one host at a time: a host whose own config no
    longer evaluates (or whose sources are gone) degrades to a warning instead
    of killing the whole column. Host names are forced without their values."""
    hosts = flake_eval(flake_attr("nix-pins", "installed"), apply="builtins.attrNames")
    out = {}
    for host in hosts:
        try:
            out[host] = flake_eval(flake_attr("nix-pins", "installed", host))
        except RuntimeError as ex:
            tail = str(ex).strip().splitlines()[-1] if str(ex).strip() else "?"
            print(yellow(f"warning: installed check failed for host {host} — the host itself"
                         f" may not evaluate ({tail})"), file=sys.stderr)
            out[host] = {}
    return out


KNOWN_KINDS = {"overlay", "package", "flake-input", "auto-pin"}
KNOWN_PURPOSES = {"version-lag", "compat", "packaging"}
SOURCE_SCHEMES = {"github", "gitlab", "pypi", "npm", "brew-cask", "cmd"}

AUTO_NOTES = (
    "Auto Pin (generated data): outruns the brew-api snapshot for one cask. "
    "Delete the JSON once BASE catches up (DROP?)."
)


def load_manifest():
    """Manifest entries via `.#nix-pins.manifest` — the flake's own view, with
    `systems` resolved, `stillNeeded` reduced to hasStillNeeded, and the
    platform list (defined next to the inputs in flake.nix) alongside."""
    global PLATFORMS
    try:
        envelope = flake_eval(flake_attr("nix-pins", "manifest"))
    except RuntimeError as ex:
        raise ConfigError(f"the flake's nix-pins output does not evaluate:\n{ex}") from ex
    PLATFORMS = envelope["platforms"]
    return envelope["entries"]


def auto_pin_path(token):
    return REPO / AUTO_REL / f"{token}.json"


def read_auto_pin(path):
    """Load one overlays/auto/*.json, rejecting whatever the Nix loader would."""
    rel = path.relative_to(REPO).as_posix()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as ex:
        raise ConfigError(f"{rel}: unreadable Auto Pin ({ex})") from ex
    if data.get("schema") != AUTO_SCHEMA:
        raise ConfigError(
            f"{rel}: schema {data.get('schema')!r}, expected {AUTO_SCHEMA}"
            " — regenerate it with `update`"
        )
    for field, want in (
        ("kind", str),
        ("token", str),
        ("version", str),
        ("url", str),
        ("hash", str),
        ("enabled", bool),
    ):
        if not isinstance(data.get(field), want):
            raise ConfigError(f"{rel}: field `{field}` must be a {want.__name__}")
    if data["kind"] != "brew-cask":
        raise ConfigError(f"{rel}: unknown kind {data['kind']!r} (known: brew-cask)")
    if data["token"] != path.stem:
        raise ConfigError(f"{rel}: token {data['token']!r} does not match the file name")
    return data


def auto_entries():
    """overlays/auto/*.json are self-describing Entries — never in the manifest."""
    out = []
    for path in sorted((REPO / AUTO_REL).glob("*.json")):
        data = read_auto_pin(path)
        out.append(
            {
                "name": data["token"],
                "purpose": ["version-lag"],
                "kind": "auto-pin",
                "file": f"{AUTO_REL}/{path.name}",
                "attr": f"brewCasks.{data['token']}",
                "platform": "darwin",
                "systems": ["aarch64-darwin"],
                "source": f"brew-cask:{data['token']}",
                "tagPattern": None,
                "nixpkgsAttr": None,
                "versionVar": None,
                "versionRegex": None,
                "fixedAgainst": None,
                "update": True,
                "notes": AUTO_NOTES,
                "_auto": data,
            }
        )
    return out


def load_entries():
    entries = load_manifest() + auto_entries()
    validate_entries(entries)
    return entries


def validate_entries(entries):
    """Reject entries the rest of the script cannot handle. A *missing* file is
    not checked here — that is Drift, which `check` reports and works around."""
    problems = []
    seen = {}
    for e in entries:
        name = e.get("name")
        if not name:
            problems.append("an entry has no `name`")
            continue
        if name in seen:
            problems.append(f"{name}: duplicate entry (already defined by {seen[name]})")
        seen[name] = e.get("file")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            problems.append(f"{name!r}: entry names must match [A-Za-z0-9._-]+ (they become attrpaths)")
        if e.get("kind") not in KNOWN_KINDS:
            problems.append(
                f"{name}: unknown kind {e.get('kind')!r}"
                f" (known: {', '.join(sorted(KNOWN_KINDS))})"
            )
        if not is_auto_entry(e) and not isinstance(e.get("enable"), bool):
            problems.append(f"{name}: `enable` must be a boolean — wiring is data")
        for p in e.get("purpose") or []:
            if p not in KNOWN_PURPOSES:
                problems.append(f"{name}: unknown purpose {p!r}")
        if e.get("platform") not in PLATFORMS:
            problems.append(f"{name}: unknown platform {e.get('platform')!r}")
        if e.get("source"):
            scheme = e["source"].split(":", 1)[0]
            if scheme not in SOURCE_SCHEMES:
                problems.append(f"{name}: unknown source scheme {scheme!r}")
        rx = e.get("versionRegex")
        if rx:
            try:
                groups = re.compile(rx).groups
            except re.error as ex:
                problems.append(f"{name}: versionRegex does not compile ({ex})")
            else:
                if groups != 1:
                    problems.append(
                        f"{name}: versionRegex must have exactly one capture group"
                        f" (it has {groups}) — otherwise the match is not a version"
                    )
            if not e.get("versionVar") and e.get("update"):
                problems.append(
                    f"{name}: update = true, but the version is only readable through"
                    " versionRegex — the updater can only rewrite `versionVar` bindings"
                )
    if problems:
        raise ConfigError("invalid entries:\n  - " + "\n  - ".join(problems))


def locked_input_meta(input_name):
    lock = json.loads((REPO / "flake.lock").read_text())
    node_key = lock["nodes"]["root"]["inputs"][input_name]
    return lock["nodes"][node_key]["locked"]


_flake_input_paths = {}


def flake_input_path(name):
    """flake.lock で固定された input のストアパス(eval で解決、キャッシュ)。

    releaseJson を持つ flake-input Entry(barik)がピン版を読むのに使う。
    brew-api だけは .#nix-pins.brewApiPath で引けるが、こちらは任意の input 用。
    """
    if name not in _flake_input_paths:
        p = run(
            ["nix", "eval", "--impure", "--raw", "--expr",
             f'(builtins.getFlake "git+file://{REPO}").inputs.{name}.outPath'],
        )
        _flake_input_paths[name] = Path(p.stdout.strip())
    return _flake_input_paths[name]


_brew_api_cache = None


def brew_api_casks():
    """brew-api input(ロック済みスナップショット)の cask.json → {token: cask}."""
    global _brew_api_cache
    if _brew_api_cache is None:
        store_path = flake_eval(flake_attr("nix-pins", "brewApiPath"), raw=True)
        data = json.loads((Path(store_path) / "cask.json").read_text())
        _brew_api_cache = {c["token"]: c for c in data}
    return _brew_api_cache


def wired_cask_tokens():
    """modules/darwin/brew-nix.nix の非コメント brewCasks.<token> 行を列挙。"""
    tokens = []
    for line in (REPO / CASK_MODULE_REL).read_text().splitlines():
        m = re.match(r"^\s*(#\s*)?brewCasks\.([A-Za-z0-9_-]+)", line)
        if m and not m.group(1) and m.group(2) not in tokens:
            tokens.append(m["token"])
    return tokens


def live_cask(token):
    return http_json(f"https://formulae.brew.sh/api/cask/{token}.json")


def hex_to_sri(digest):
    digest = digest or ""
    if digest.startswith("sha256-"):
        return digest
    if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise RuntimeError(f"expected a 64-character sha256 hex digest, got {digest!r}")
    return "sha256-" + base64.b64encode(bytes.fromhex(digest)).decode()


def is_auto_entry(entry):
    return entry["kind"] == "auto-pin"


def auto_enabled(entry):
    return bool(entry["_auto"]["enabled"])


def auto_cask_token(entry):
    if is_auto_entry(entry):
        return entry["_auto"]["token"]
    src = entry.get("source") or ""
    return src.split(":", 1)[1] if src.startswith("brew-cask:") else None


def adopted_tokens(entries):
    """Tokens whose version an *enabled* Auto Pin governs.

    A disabled pin leaves its cask sitting on BASE, so the cask has to stay in
    the cask table — otherwise disabling a pin would also silence the base-vs-live
    warning for that cask.
    """
    return {
        auto_cask_token(e)
        for e in entries
        if is_auto_entry(e) and auto_enabled(e) and auto_cask_token(e)
    }


def cask_display_version(v):
    """brew の version から表示用の本体を取り出す(",<build>" を落とす)。"""
    return str(v).split(",")[0]


def is_enabled(entry):
    """The `enable` field is the wiring: for overlays flake.nix applies exactly
    the enabled ones (.#nix-pins.overlays), for package/flake-input entries it
    is declared intent. No more grepping flake.nix for uncommented lines."""
    if is_auto_entry(entry):
        return auto_enabled(entry)
    return bool(entry.get("enable"))


def pinned_version(entry):
    """Extract the currently pinned version from the entry's file. → (version, error)."""
    if is_auto_entry(entry):
        # Auto Pins are data: the version is a field, not a regex match.
        return entry["_auto"]["version"], None
    # flake-input with releaseJson: version lives in the locked input's JSON,
    # not in a file of ours — nothing under REPO to read.
    if entry.get("releaseJson"):
        try:
            path = flake_input_path(entry["name"]) / entry["releaseJson"]
            data = json.loads(path.read_text())
            v = data.get("version")
            if not v:
                return None, f"no 'version' in {entry['name']}/{entry['releaseJson']}"
            return str(v), None
        except Exception as ex:  # noqa: BLE001
            return None, f"releaseJson read failed for {entry['name']}: {ex}"
    path = REPO / entry["file"]
    if not path.exists():
        return None, f"{entry['file']} no longer exists — stale manifest entry"
    text = path.read_text()
    if entry.get("versionRegex"):
        rx = entry["versionRegex"]
    else:
        var = entry.get("versionVar") or "version"
        rx = r"\b" + re.escape(var) + r'\s*=\s*"([^"]+)"'
    found = re.findall(rx, text)
    if not found:
        return None, f"no match for version pattern in {entry['file']}"
    if not all(isinstance(v, str) for v in found):
        return None, f"version pattern for {entry['name']} has more than one capture group"
    values = sorted(set(found))
    if len(values) > 1:
        return None, f"ambiguous versions in {entry['file']}: {', '.join(values)}"
    return values[0], None


def is_pin(entry):
    """An Entry is a Pin when it fixes a version string in its file (or releaseJson)."""
    if is_auto_entry(entry):
        return True
    return bool(entry.get("versionVar") or entry.get("versionRegex") or entry.get("releaseJson"))


def purposes(entry):
    return entry.get("purpose") or []


PURPOSE_SHORT = {"version-lag": "lag", "compat": "compat", "packaging": "pkg"}


def purpose_label(entry):
    return "+".join(PURPOSE_SHORT.get(p, p) for p in purposes(entry)) or "-"


# ── version comparison (mirrors builtins.compareVersions) ────────────────────


def _chunks(v):
    return re.findall(r"\d+|[A-Za-z]+", v or "")


def compare_versions(a, b):
    ca, cb = _chunks(a), _chunks(b)
    for i in range(max(len(ca), len(cb))):
        x = ca[i] if i < len(ca) else ""
        y = cb[i] if i < len(cb) else ""
        if x == y:
            continue
        dx, dy = x.isdigit(), y.isdigit()
        if dx and dy:
            if int(x) != int(y):
                return -1 if int(x) < int(y) else 1
        elif dx:
            return 1  # numbers sort after letters / absence
        elif dy:
            return -1
        else:
            if x == "":
                return 1 if y == "pre" else -1
            if y == "":
                return -1 if x == "pre" else 1
            return -1 if x < y else 1
    return 0


# ── upstream "latest version" lookup ─────────────────────────────────────────


RETRY_CODES = {403, 429, 500, 502, 503, 504}


def http_json(url):
    """GET + parse JSON, retrying rate limits and transient 5xx with backoff.

    Unauthenticated api.github.com allows 60 requests/hour, which a dashboard of
    ~25 entries can exhaust; without a retry every entry would show as ERROR.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "nix-pins", "Accept": "application/json"})
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        req.add_header("Authorization", f"Bearer {token}")
    for attempt in range(HTTP_ATTEMPTS):
        last = attempt == HTTP_ATTEMPTS - 1
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as ex:
            if last or ex.code not in RETRY_CODES:
                raise
            after = ex.headers.get("Retry-After") if ex.headers else None
            try:
                delay = float(after) if after else 0.0
            except ValueError:
                delay = 0.0
            time.sleep(min(delay or 2.0**attempt, 15))
        except (urllib.error.URLError, TimeoutError):
            if last:
                raise
            time.sleep(2.0**attempt)
    raise RuntimeError(f"unreachable: retries exhausted for {url}")


def strip_v(tag):
    return tag[1:] if len(tag) > 1 and tag[0] in "vV" and tag[1].isdigit() else tag


def _github_latest(owner_repo):
    if shutil.which("gh"):
        p = run(["gh", "api", f"repos/{owner_repo}/releases/latest", "--jq", ".tag_name"], check=False)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
        p = run(["gh", "api", f"repos/{owner_repo}/tags?per_page=1", "--jq", ".[0].name"], check=False)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
        raise RuntimeError(f"github: no releases or tags for {owner_repo}")
    try:
        return http_json(f"https://api.github.com/repos/{owner_repo}/releases/latest")["tag_name"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            tags = http_json(f"https://api.github.com/repos/{owner_repo}/tags?per_page=1")
            if tags:
                return tags[0]["name"]
        raise


def upstream_latest(entry):
    source = entry.get("source")
    if not source:
        return None
    scheme, rest = source.split(":", 1)
    if scheme == "github":
        tag = _github_latest(rest)
    elif scheme == "gitlab":
        proj = urllib.parse.quote(rest, safe="")
        rels = http_json(f"https://gitlab.com/api/v4/projects/{proj}/releases?per_page=1")
        if rels:
            tag = rels[0]["tag_name"]
        else:
            tags = http_json(f"https://gitlab.com/api/v4/projects/{proj}/repository/tags?per_page=1")
            tag = tags[0]["name"] if tags else None
    elif scheme == "pypi":
        return http_json(f"https://pypi.org/pypi/{rest}/json")["info"]["version"]
    elif scheme == "npm":
        return http_json(f"https://registry.npmjs.org/{rest}/latest")["version"]
    elif scheme == "brew-cask":
        return str(http_json(f"https://formulae.brew.sh/api/cask/{rest}.json")["version"]).split(",")[0]
    elif scheme == "cmd":
        # A manifest can therefore run arbitrary shell; never implicit.
        if not ALLOW_CMD:
            raise RuntimeError(
                f"source `cmd:` runs a shell command ({rest!r}); pass --allow-cmd-sources to permit it"
            )
        return run(["bash", "-c", rest]).stdout.strip() or None
    else:
        raise RuntimeError(f"unknown source scheme: {scheme}")
    if tag is None:
        return None
    if entry.get("tagPattern"):
        m = re.search(entry["tagPattern"], tag)
        return m.group(1) if m else tag
    return strip_v(tag)


# ── report / src / drv — all through the flake's nix-pins output ─────────────
# eval.nix owns the Nix side: the nixpkgs comparison column and stillNeeded
# verdicts arrive as one `.#nix-pins.report` eval, an entry's src comes from
# `.#nix-pins.src."<name>"."<system>"` (re-evaluated mid-update against the
# edited working tree — a git+file flake sees tracked files as they are), and
# the verification build targets `.#nix-pins.drv."<name>"."<system>"`.


def nix_pins_report():
    """→ {name: {nixpkgs: version|None, stillNeeded: True|False|None|"error"}}."""
    return flake_eval(flake_attr("nix-pins", "manifest"))


def eval_src(entry, system):
    info = flake_eval(flake_attr("nix-pins", "src", entry["name"], system))
    # dedupe, preserving order (fetchurl exposes the same URL as url and urls[0])
    seen, urls = set(), []
    for u in info.get("urls") or []:
        if u not in seen:
            seen.add(u)
            urls.append(u)
    info["urls"] = urls
    return info


def build_entry_drv(entry, system):
    """`nix build` the entry's derivation via the flake output (pure eval)."""
    return run(
        ["nix", "build", "--no-link",
         f".#{flake_attr('nix-pins', 'drv', entry['name'], system)}"],
        check=False,
    )


def expand_mirror(url):
    for prefix, base in MIRRORS.items():
        if url.startswith(prefix):
            return base + url[len(prefix):]
    return url


def prefetch(entry, src_info, verified):
    """Fetch the (new) artifact and return its SRI hash, matching the fetcher's hash mode.

    `verified` says whether a `nix build` will re-check the result afterwards. It
    gates recursive (NAR) hashing, where our reconstruction of the fetcher's
    unpacking is a guess — a wrong guess that nothing re-checks would leave a
    plausible but bogus hash committed to the file.
    """
    scheme = (entry.get("source") or ":").split(":", 1)[0]
    if src_info.get("rev") and scheme == "github":
        owner_repo = entry["source"].split(":", 1)[1]
        p = run(["nix", "flake", "prefetch", "--json", f"github:{owner_repo}/{src_info['rev']}"])
        j = json.loads(p.stdout)
        return j.get("hash") or j["locked"]["narHash"]
    urls = [expand_mirror(u) for u in src_info["urls"] if not u.startswith("mirror://")
            or any(u.startswith(k) for k in MIRRORS)]
    if not urls:
        raise RuntimeError("src has no fetchable URL")
    cmd = ["nix", "store", "prefetch-file", "--json"]
    if src_info.get("mode") == "recursive":
        # `--unpack` only reproduces plain-NAR fetchers; fetchzip/fetchFromGitHub
        # outside the github fast-path may strip directories, giving a different
        # hash than the real build. Only acceptable if the build re-checks it.
        if not verified:
            raise RuntimeError(
                "src uses recursive (NAR) hashing, whose prefetched hash is only "
                "trustworthy when the `nix build` verification runs — drop --no-build, "
                "or update this entry by hand"
            )
        cmd.append("--unpack")
    if src_info.get("name"):
        cmd += ["--name", src_info["name"]]
    last_err = ""
    for u in urls:
        p = run(cmd + [u], check=False)
        if p.returncode == 0:
            return json.loads(p.stdout)["hash"]
        last_err = p.stderr
    raise RuntimeError(f"prefetch failed for {urls}:\n{last_err.strip()[-800:]}")


# ── check ────────────────────────────────────────────────────────────────────


def build_rows(entries):
    rows = []
    for e in entries:
        row = {
            "name": e["name"],
            "purpose": purpose_label(e),
            "pin": is_pin(e),
            "enabled": is_enabled(e),
            "pinned": None,
            "latest": None,
            "nixpkgs": None,
            "stillNeeded": None,
            "status": "?",
            "error": None,
            "entry": e,
        }
        if is_pin(e):
            row["pinned"], err = pinned_version(e)
            if err:
                row["error"] = err
        else:
            row["pinned"] = e.get("fixedAgainst")
        rows.append(row)

    # upstream lookups in parallel
    def fetch(row):
        try:
            return upstream_latest(row["entry"]), None
        except Exception as ex:  # noqa: BLE001 — record and keep going
            return None, f"upstream lookup failed: {ex}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(fetch, r): r for r in rows if r["entry"].get("source")}
        for fut, r in futs.items():
            latest, err = fut.result()
            r["latest"] = latest
            if err and not r["error"]:
                r["error"] = err

    # nixpkgs comparison column + stillNeeded verdicts: one flake eval for all
    # manifest entries (eval.nix batches them; the predicates run in Nix).
    try:
        report = nix_pins_report()
    except Exception as ex:  # noqa: BLE001
        report = {}
        print(yellow(f"warning: .#nix-pins.report eval failed: {ex}"), file=sys.stderr)
    for r in rows:
        e = r["entry"]
        info = report.get(e["name"]) or {}
        if e.get("nixpkgsAttr"):
            r["nixpkgs"] = info.get("nixpkgs")
        if e.get("hasStillNeeded") and "compat" in purposes(e):
            verdict = info.get("stillNeeded")
            if verdict == "error":
                r["error"] = r["error"] or "stillNeeded predicate failed to evaluate (see eval.nix)"
            else:
                r["stillNeeded"] = verdict

    # Auto Pin は比較列(NIXPKGS/BASE)に brew-api input が今くれる版を入れる。
    # version-lag の DROP? 判定(base ≥ latest → ピン不要)がそのまま効く。
    auto_rows = [r for r in rows if is_auto_entry(r["entry"]) and auto_cask_token(r["entry"])]
    if auto_rows:
        try:
            base_map = brew_api_casks()
            for r in auto_rows:
                c = base_map.get(auto_cask_token(r["entry"]))
                if c and c.get("version") is not None:
                    r["nixpkgs"] = cask_display_version(c["version"])
        except Exception as ex:  # noqa: BLE001
            print(yellow(f"warning: brew-api base lookup failed: {ex}"), file=sys.stderr)

    for r in rows:
        r["status"] = classify(r)
    return rows


def classify(row):
    e = row["entry"]
    if row["error"]:
        return "ERROR"
    status = None
    if is_pin(e):
        if not row["pinned"] or not row["latest"]:
            status = "UNKNOWN"
        else:
            c = compare_versions(row["pinned"], row["latest"])
            status = "OUTDATED" if c < 0 else ("OK" if c == 0 else "AHEAD")
            # only a version-lag Pin becomes droppable when nixpkgs catches up
            if (
                "version-lag" in purposes(e)
                and status != "OUTDATED"
                and row["nixpkgs"]
                and compare_versions(row["nixpkgs"], row["latest"]) >= 0
            ):
                status += "+DROP?"
        if status != "OK":
            return status
    if "compat" in purposes(e):
        if row.get("stillNeeded") is False:
            return "FIXED-UPSTREAM"
        if row.get("stillNeeded") is True:
            return "FIX-OK"
        if e.get("fixedAgainst") and row["nixpkgs"]:
            return "FIX-OK" if compare_versions(e["fixedAgainst"], row["nixpkgs"]) == 0 else "REVIEW"
    return status or "-"


STATUS_COLOR = {
    "OK": green,
    "OK+DROP?": cyan,
    "AHEAD": cyan,
    "AHEAD+DROP?": cyan,
    "OUTDATED": red,
    "MISMATCH": yellow,
    "UNKNOWN": yellow,
    "ERROR": red,
    "FIX-OK": green,
    "FIXED-UPSTREAM": cyan,
    "REVIEW": yellow,
    "ROLLING": dim,
    "-": dim,
}


def print_table(rows):
    headers = ["NAME", "PURPOSE", "ENABLED", "PIN/FIX@", "UPSTREAM", "NIXPKGS/BASE", "STATUS"]
    show_installed = any(r.get("installed") is not None for r in rows)
    if show_installed:
        headers.append("INSTALLED")
    if not rows:
        print(dim("(no entries — add one to scripts/nix-pins/manifest)"))
        return
    cells = []
    for r in rows:
        cell = [
            r["name"],
            r["purpose"],
            "yes" if r["enabled"] else "no",
            r["pinned"] or "-",
            r["latest"] or "-",
            r["nixpkgs"] or "-",
            r["status"],
        ]
        if show_installed:
            cell.append(", ".join(r["installed"]) if r.get("installed") else "-")
        cells.append(cell)
    widths = [max(len(h), *(len(c[i]) for c in cells)) for i, h in enumerate(headers)]
    print("  ".join(bold(h.ljust(widths[i])) for i, h in enumerate(headers)))
    for r, c in zip(rows, cells):
        line = []
        for i, cell in enumerate(c):
            padded = cell.ljust(widths[i])
            if i == 6:
                padded = STATUS_COLOR.get(cell, str)(padded)
            elif not r["enabled"]:
                padded = dim(padded)
            line.append(padded)
        print("  ".join(line))


def drift_report(entries):
    """Drift lint in both directions:

    - a file that should have a manifest entry but has none;
    - an Entry whose file is gone (a stale row that used to crash the dashboard).

    Auto Pins are exempt from the first check — the JSON file *is* the Entry, so
    the two can never disagree.
    """
    tracked = {e["file"] for e in entries}
    found = [f"overlays/{p.name}" for p in sorted((REPO / "overlays").glob("*.nix"))]
    found += [
        p.relative_to(REPO).as_posix() for p in sorted((REPO / "packages").glob("*/default.nix"))
    ]
    problems = [
        f"{f} has no entry in the nix-pins manifest — add one"
        for f in found
        if f not in tracked
    ]
    # overlays/auto holds the loader plus JSON data. A stray .nix in there is a
    # leftover from before Auto Pins became data.
    problems += [
        f"{AUTO_REL}/{p.name} is a hand-written Auto Pin — those are"
        f" {AUTO_REL}/<token>.json data files now; delete or convert it"
        for p in sorted((REPO / AUTO_REL).glob("*.nix"))
        if p.name != "default.nix"
    ]
    missing = {}
    for e in entries:
        if not is_auto_entry(e) and not (REPO / e["file"]).exists():
            missing.setdefault(e["file"], []).append(e["name"])
    problems += [
        f"{f} is referenced by manifest entr{'y' if len(names) == 1 else 'ies'}"
        f" {', '.join('`' + n + '`' for n in names)} but does not exist"
        " — delete the entry, or restore the file"
        for f, names in sorted(missing.items())
    ]
    return problems


def build_cask_rows(entries):
    """wired cask(Auto Pin 未採用のもの)の base(brew-api input)vs live API。"""
    tokens = [t for t in wired_cask_tokens() if t not in adopted_tokens(entries)]
    if not tokens:
        return []
    try:
        base_map = brew_api_casks()
    except Exception as ex:  # noqa: BLE001
        print(yellow(f"warning: brew-api input read failed: {ex}"), file=sys.stderr)
        base_map = {}

    def fetch(t):
        try:
            return t, live_cask(t), None
        except Exception as ex:  # noqa: BLE001
            return t, None, str(ex)

    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for t, live, err in pool.map(fetch, tokens):
            base = base_map.get(t, {}).get("version")
            row = {
                "token": t,
                "base": str(base) if base is not None else None,
                "live_version": str(live["version"]) if live else None,
                "status": "?",
                "error": err,
                "live": live,
            }
            if err or live is None:
                row["status"] = "UNKNOWN"
            elif live.get("sha256") == "no_check":
                # rebuild のたびに最新を取りにいく(ハッシュ検証なし)cask。
                # 「決定論的に処理できる範囲」の外なので adopt 対象にしない。
                row["status"] = "ROLLING"
            elif row["base"] is None:
                row["status"] = "UNKNOWN"
                row["error"] = row["error"] or "brew-api input 内に token が見つからない"
            elif row["base"] == row["live_version"]:
                row["status"] = "OK"
            elif (
                compare_versions(
                    cask_display_version(row["base"]),
                    cask_display_version(row["live_version"]),
                )
                > 0
            ):
                # Cask versions are not reliably ordered (dates, build suffixes,
                # vendor-specific schemes), and brew is the source of truth for a
                # cask. So "base looks newer than live" is a mismatch to act on,
                # not something to silently pass as OK.
                row["status"] = "MISMATCH"
            else:
                row["status"] = "OUTDATED"
            rows.append(row)
    rank = {"OUTDATED": 0, "MISMATCH": 1}
    rows.sort(key=lambda r: (rank.get(r["status"], 2), r["token"]))
    return rows


def brew_api_age():
    meta = locked_input_meta("brew-api")
    dt = datetime.datetime.fromtimestamp(meta["lastModified"])
    days = max(0, (datetime.datetime.now() - dt).days)
    return f"{dt:%Y-%m-%d} 取得({days}日前)"


def print_cask_rows(rows):
    if not rows:
        return
    try:
        age = brew_api_age()
    except Exception:  # noqa: BLE001
        age = "?"
    print()
    print(bold(f"Casks — brew-nix 経由(base = brew-api input: {age})"))
    headers = ["CASK", "BASE", "LIVE", "STATUS"]
    cells = [
        [
            r["token"],
            cask_display_version(r["base"]) if r["base"] else "-",
            cask_display_version(r["live_version"]) if r["live_version"] else "-",
            r["status"],
        ]
        for r in rows
    ]
    widths = [max(len(h), *(len(c[i]) for c in cells)) for i, h in enumerate(headers)]
    print("  ".join(bold(h.ljust(widths[i])) for i, h in enumerate(headers)))
    for c in cells:
        line = [c[i].ljust(widths[i]) for i in range(len(headers))]
        line[3] = STATUS_COLOR.get(c[3], str)(line[3])
        print("  ".join(line))


def cmd_check(args):
    entries = load_entries()
    rows = build_rows(entries)
    cask_rows = build_cask_rows(entries)
    drift = drift_report(entries)
    rows.sort(key=lambda r: (not r["pin"], not r["enabled"], r["name"]))

    if getattr(args, "installed", False):
        # Deep reachability: evaluates each host's package list (slow, so opt-in).
        # `installed.<host>.<name>` = true / false / null (not evaluable there).
        try:
            inst = installed_map()
            for r in rows:
                hosts = sorted(h for h, m in inst.items() if m.get(r["name"]) is True)
                r["installed"] = hosts
        except Exception as ex:  # noqa: BLE001
            print(yellow(f"warning: .#nix-pins.installed eval failed: {ex}"), file=sys.stderr)

    if args.json:
        out = {
            "entries": [{k: v for k, v in r.items() if k not in ("entry",)} for r in rows],
            "casks": [{k: v for k, v in c.items() if k != "live"} for c in cask_rows],
            "drift": drift,
        }
        print(json.dumps(out, indent=2))
    else:
        print_table(rows)
        print_cask_rows(cask_rows)
        warned = False
        for r in rows:
            e = r["entry"]
            if r["status"] == "ERROR":
                warned = True
                print(red(f"✗ {r['name']}: {r['error']}"))
            if not r["enabled"]:
                continue
            if r["status"] == "OUTDATED":
                warned = True
                if r["entry"].get("releaseJson"):
                    hint = f" — run: nix flake lock --update-input {r['name']}  (or nix-pins.py update {r['name']})"
                else:
                    hint = f" — run: nix-pins.py update {r['name']}"
                print(
                    red(f"⚠ {r['name']}: pinned {r['pinned']} < upstream {r['latest']}")
                    + hint
                )
            if r["status"].startswith("AHEAD"):
                # Never pass this off as OK: it means either a prerelease pin or a
                # source that reports an older version than we hold (and cask
                # version strings are not reliably ordered in the first place).
                warned = True
                print(
                    yellow(f"ℹ {r['name']}: pinned {r['pinned']} is ahead of upstream {r['latest']}")
                    + " — a prerelease pin, or the source lags; confirm which rather than trusting it"
                )
            if (
                r["pin"]
                and r["pinned"]
                and r["nixpkgs"]
                and compare_versions(r["nixpkgs"], r["pinned"]) > 0
            ):
                warned = True
                print(
                    yellow(f"ℹ {r['name']}: nixpkgs ({r['nixpkgs']}) is newer than the pin ({r['pinned']})")
                    + " — the pin is now holding this package back; update it or drop it"
                )
            if r["status"].endswith("+DROP?"):
                warned = True
                print(
                    cyan(f"ℹ {r['name']}: nixpkgs already ships {r['nixpkgs']} (≥ upstream {r['latest']})")
                    + " — this version-lag pin may be removable"
                )
            if r["status"] == "FIXED-UPSTREAM":
                warned = True
                print(
                    cyan(f"ℹ {r['name']}: stillNeeded predicate reports nixpkgs fixed this")
                    + " — the compat overlay can likely be deleted"
                )
            if r["status"] == "REVIEW":
                warned = True
                note = f" [{e['notes']}]" if e.get("notes") else ""
                print(
                    yellow(
                        f"ℹ {r['name']}: nixpkgs {e['nixpkgsAttr']} moved "
                        f"{e['fixedAgainst']} → {r['nixpkgs']} — re-verify whether the compat overlay is still needed"
                    )
                    + note
                )
        disabled_outdated = [r["name"] for r in rows if r["status"] == "OUTDATED" and not r["enabled"]]
        if disabled_outdated:
            print(dim(f"({len(disabled_outdated)} disabled entries are outdated, table only: "
                      f"{', '.join(disabled_outdated)})"))
        for c in cask_rows:
            if c["status"] == "OUTDATED":
                warned = True
                print(
                    red(
                        f"⚠ cask {c['token']}: base {cask_display_version(c['base'])} "
                        f"< live {cask_display_version(c['live_version'])}"
                    )
                    + " — `sync` で Auto Pin を作るか、`nix flake lock --update-input brew-api` で全体を進める"
                )
            elif c["status"] == "MISMATCH":
                warned = True
                print(
                    yellow(
                        f"⚠ cask {c['token']}: base {cask_display_version(c['base'])} "
                        f"≠ live {cask_display_version(c['live_version'])} and looks newer"
                    )
                    + " — brew is authoritative for casks (a rollback, or an unorderable"
                    " version scheme); re-adopt via `sync` after checking"
                )
            elif c["status"] == "UNKNOWN" and c.get("error"):
                warned = True
                print(yellow(f"⚠ cask {c['token']}: {c['error']}"))
            elif c["status"] == "ROLLING":
                print(dim(f"· cask {c['token']}: sha256=no_check — 決定論的にはピン不可(常に最新を取得する cask)"))
        for f in drift:
            warned = True
            print(red(f"⚠ drift: {f}"))
        if not warned:
            print(green("✓ all pins up to date, nothing to review, no drift"))

    failing = (
        bool(drift)
        or any(r["status"] == "OUTDATED" and r["enabled"] for r in rows)
        or any(c["status"] in ("OUTDATED", "MISMATCH") for c in cask_rows)
    )
    if args.strict:
        failing = failing or any(
            r["status"] in ("UNKNOWN", "ERROR", "REVIEW", "FIXED-UPSTREAM") and r["enabled"] for r in rows
        )
    return EXIT_ACTION if failing else EXIT_OK


# ── update ───────────────────────────────────────────────────────────────────


def bump_version(path, entry, new_version):
    var = entry.get("versionVar") or "version"
    pat = re.compile(r"(\b" + re.escape(var) + r'\s*=\s*")([^"]+)(")')
    text = path.read_text()
    matches = pat.findall(text)
    if not matches:
        raise RuntimeError(f"{entry['name']}: `{var} = \"...\"` not found in {entry['file']}")
    if len({m[1] for m in matches}) > 1:
        raise RuntimeError(f"{entry['name']}: multiple different `{var}` values in {entry['file']}")
    path.write_text(pat.sub(lambda m: m.group(1) + new_version + m.group(3), text))


def _major_bump(current, target):
    ca, cb = _chunks(current), _chunks(target)
    return bool(ca and cb and ca[0].isdigit() and cb[0].isdigit() and int(cb[0]) > int(ca[0]))


def update_release_json_input(entry, args, target_version=None, from_all=False):
    """Bump a flake-input whose version lives in the input's release.json.

    Upstream tags a release and CI rewrites release.json on that repo's main;
    we only need to refresh flake.lock to pick up the new pointer.
    """
    name = entry["name"]
    if not entry.get("update"):
        hint = f" ({entry['notes']})" if entry.get("notes") else ""
        print(yellow(f"→ {name}: auto-update not supported for this entry{hint}"))
        return True
    current, err = pinned_version(entry)
    if err:
        print(red(f"✗ {name}: {err}"))
        return False
    latest = target_version
    if latest is None:
        try:
            latest = upstream_latest(entry)
        except Exception as ex:  # noqa: BLE001
            print(red(f"✗ {name}: upstream lookup failed: {ex}"))
            return False
    if latest is None:
        print(red(f"✗ {name}: no upstream source configured"))
        return False

    c = compare_versions(current, latest)
    if c == 0 and not args.force:
        print(green(f"✓ {name}: already at {current}"))
        return True
    if c > 0 and not args.force:
        print(cyan(f"→ {name}: locked {current} is ahead of upstream tag {latest}; skipping (use --force)"))
        return True
    major = _major_bump(current, latest)
    if major and from_all and not args.major:
        print(yellow(f"→ {name}: major bump {current} → {latest} — skipped under --all "
                     f"(run `update {name}` explicitly or pass --major)"))
        return True
    if args.dry_run:
        tag = yellow(" (major bump)") if major else ""
        print(f"→ {name}: would `nix flake lock --update-input {name}` "
              f"({current} → {bold(latest)}){tag}")
        return True

    print(f"→ {name}: `nix flake lock --update-input {name}` ({current} → {bold(latest)})")
    if major:
        print(yellow(f"   note: this is a major version bump ({current} → {latest})"))
    p = run(["nix", "flake", "lock", "--update-input", name], check=False)
    if p.returncode != 0:
        print(red(f"✗ {name}: flake lock update failed:\n{p.stderr.strip()[-800:]}"))
        return False

    # Re-read after lock refresh; upstream CI may lag the tag briefly.
    new_ver, err = pinned_version(entry)
    if err:
        print(red(f"✗ {name}: lock updated but could not read release.json: {err}"))
        return False
    if new_ver != latest and not args.force:
        print(yellow(f"⚠ {name}: lock now at release.json={new_ver}, upstream tag={latest} "
                     f"(CI may still be bumping release.json on the fork)"))
    systems = entry.get("systems") or [PLATFORM_INPUT[entry["platform"]][1]]
    if args.no_build:
        print(yellow("   build verification skipped (--no-build)"))
    elif current_system() not in systems:
        print(yellow(f"   build not verified: {name} targets {', '.join(systems)}, this host is {current_system()}"))
    else:
        print(f"   verifying: nix build .#{name} …")
        p = run(["nix", "build", f".#{name}", "--no-link"], check=False)
        if p.returncode != 0:
            print(red(f"✗ {name}: build failed:\n{p.stderr.strip()[-1500:]}"))
            return False
        print(green(f"✓ {name}: {current} → {new_ver} (build OK)"))
        return True
    print(green(f"✓ {name}: {current} → {new_ver}"))
    return True


def update_entry(entry, args, target_version=None, from_all=False):
    name = entry["name"]
    path = REPO / entry["file"]

    # Auto Pin(cask)は URL テンプレートを推測せず、live API から再生成する
    if is_auto_entry(entry):
        return readopt_entry(entry, args, target_version, from_all)

    # flake-input + releaseJson: bump by refreshing the lock (CI already moved
    # release.json on the upstream main).
    if entry["kind"] == "flake-input" and entry.get("releaseJson"):
        return update_release_json_input(entry, args, target_version, from_all)

    if not is_pin(entry) or not entry.get("update"):
        hint = f" ({entry['notes']})" if entry.get("notes") else ""
        print(yellow(f"→ {name}: auto-update not supported for this entry{hint}"))
        return True
    current, err = pinned_version(entry)
    if err:
        print(red(f"✗ {name}: {err}"))
        return False
    latest = target_version
    if latest is None:
        try:
            latest = upstream_latest(entry)
        except Exception as ex:  # noqa: BLE001
            print(red(f"✗ {name}: upstream lookup failed: {ex}"))

    if latest is None:
        print(red(f"✗ {name}: no upstream source configured; pass an explicit {name}=<version>"))
        return False

    c = compare_versions(current, latest)
    if c == 0 and not args.force:
        print(green(f"✓ {name}: already at {current}"))
        return True
    if c > 0 and not args.force:
        print(cyan(f"→ {name}: pinned {current} is ahead of upstream {latest}; skipping (use --force)"))
        return True
    major = _major_bump(current, latest)
    if major and from_all and not args.major:
        print(yellow(f"→ {name}: major bump {current} → {latest} — skipped under --all "
                     f"(run `update {name}` explicitly or pass --major)"))
        return True
    if args.dry_run:
        tag = yellow(" (major bump)") if major else ""
        print(f"→ {name}: would update {current} → {bold(latest)} in {entry['file']}{tag}")
        return True

    print(f"→ {name}: updating {current} → {bold(latest)} in {entry['file']}")
    if major:
        print(yellow(f"   note: this is a major version bump ({current} → {latest})"))
    systems = entry["systems"]  # resolved by eval.nix (manifest value or platform default)
    # Whether a `nix build` will re-check the hashes we are about to write.
    will_verify = not args.no_build and current_system() in systems
    original = path.read_text()
    try:
        pre = {}
        for system in systems:
            pre[system] = eval_src(entry, system)
            if not pre[system].get("hash"):
                raise RuntimeError(f"could not evaluate current src hash for {system}")
        bump_version(path, entry, latest)
        for system in systems:
            post = eval_src(entry, system)
            print(f"   [{system}] prefetching {post['urls'][0] if post['urls'] else post.get('rev')}")
            new_hash = prefetch(entry, post, verified=will_verify)
            old_hash = pre[system]["hash"]
            text = path.read_text()
            if old_hash not in text:
                raise RuntimeError(f"old hash {old_hash} not found in {entry['file']}")
            path.write_text(text.replace(old_hash, new_hash))
            if old_hash == new_hash:
                print(yellow(f"   [{system}] hash unchanged — upstream artifact may be identical"))
            else:
                print(f"   [{system}] hash: {old_hash[:24]}… → {new_hash[:24]}…")

        if args.no_build:
            print(yellow("   build verification skipped (--no-build)"))
        elif current_system() not in systems:
            print(yellow(f"   build not verified: {name} targets {', '.join(systems)}, this host is {current_system()}"))
        else:
            print(f"   verifying: nix build ({current_system()}) …")
            p = build_entry_drv(entry, current_system())
            if p.returncode != 0:
                log = Path(tempfile.gettempdir()) / f"nix-pins-{name}-build.log"
                log.write_text(p.stderr)
                raise BuildFailure(f"build failed — full log: {log}\n" + p.stderr.strip()[-1500:])
        print(green(f"✓ {name}: {current} → {latest} updated{'' if args.no_build else ' (build OK)' if current_system() in systems else ''}"))
        return True
    except Exception as ex:  # noqa: BLE001
        attempted = path.read_text()
        is_build = isinstance(ex, BuildFailure)
        if is_build:
            print(red(f"✗ {name}: version/hash were updated but the build failed — "
                      "the derivation likely needs phase changes (installPhase/sourceRoot/etc.)"))
        print(red(f"✗ {name}: {ex}"))
        if args.keep_on_failure:
            print(yellow(f"   edits kept in {entry['file']} (--keep-on-failure); revert with: git checkout -- {entry['file']}"))
        else:
            path.write_text(original)
            if attempted != original:
                diff = "".join(
                    difflib.unified_diff(
                        original.splitlines(True),
                        attempted.splitlines(True),
                        fromfile=f"{entry['file']} (current)",
                        tofile=f"{entry['file']} (attempted, reverted)",
                    )
                )
                print(dim(diff.rstrip()))
                hint = (
                    "reapply the diff above and fix the phases manually"
                    if is_build
                    else "file left untouched"
                )
                print(yellow(f"   {entry['file']} reverted; {hint}"))
        return False


class BuildFailure(RuntimeError):
    pass


def cmd_update(args):
    entries = load_entries()
    by_name = {e["name"]: e for e in entries}
    targets = []  # (entry, explicit-version|None, from_all)
    if args.all:
        targets = [
            (e, None, True)
            for e in entries
            if is_pin(e) and e.get("update") and is_enabled(e)
        ]
    for spec in args.names:
        name, _, ver = spec.partition("=")
        if name not in by_name:
            print(red(f"✗ unknown entry: {name} (see `nix-pins.py list`)"))
            return EXIT_ERROR
        targets.append((by_name[name], ver or None, False))
    if not targets:
        print("nothing to update — pass entry names or --all (see `nix-pins.py list`)")
        return EXIT_OK
    if not args.dry_run and not require_clean_tree(args):
        return EXIT_ERROR
    ok = True
    for entry, ver, from_all in targets:
        ok = update_entry(entry, args, ver, from_all) and ok
    if not args.dry_run:
        print(dim("\nreview with `git diff`, then apply via darwin-rebuild/nixos-rebuild switch"))
    return EXIT_OK if ok else EXIT_ACTION


def cmd_list(_args):
    entries = load_entries()
    for e in entries:
        auto = "auto" if (is_pin(e) and e.get("update")) else "manual"
        state = "on " if is_enabled(e) else "off"
        line = f"{e['name']:<28} {purpose_label(e):<10} {state} {auto:<7} {e['file']}"
        print(line if state == "on " else dim(line))
        if e.get("notes"):
            print(dim(f"{'':28} {e['notes']}"))
    return EXIT_OK


# ── Auto Pin(cask の個別 version-lag ピン)──────────────────────────────────
# Generated output is *data only*: overlays/auto/<token>.json. The override
# mechanism is written once, by hand, in overlays/auto/default.nix. So changing
# that mechanism never means regenerating every pin, and this script never emits
# Nix syntax — no quoting or `${}` escaping hazards to guard against.
# The JSON file is also the Entry itself: nothing is appended to the manifest.


def write_auto_pin(token, live, *, enabled=True, adopted=None, base_at_adoption=None):
    """(Re)generate overlays/auto/<token>.json from a live cask API response."""
    sha = live.get("sha256")
    if not sha or sha == "no_check":
        raise RuntimeError(
            "sha256 is no_check — brew does not verify this artifact, so it cannot be pinned"
        )
    data = {
        "schema": AUTO_SCHEMA,
        "kind": "brew-cask",
        "token": token,
        "enabled": bool(enabled),
        "version": cask_display_version(live["version"]),
        "url": live["url"],
        "hash": hex_to_sri(sha),
        # Provenance: when we started outrunning brew-api, and from where.
        # Preserved across updates so a pin's age stays visible.
        "adopted": adopted or datetime.date.today().isoformat(),
        "baseAtAdoption": base_at_adoption,
    }
    (REPO / AUTO_REL).mkdir(parents=True, exist_ok=True)
    path = auto_pin_path(token)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return path


def write_auto_pin_data(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def stage(path):
    """Track a generated file: a git+file flake cannot see untracked ones."""
    rel = path.relative_to(REPO).as_posix()
    p = run(["git", "add", "--", str(path)], check=False)
    if p.returncode == 0:
        print(dim(f"   staged {rel} — a git+file flake only sees tracked files"))
    else:
        print(yellow(f"   `git add {rel}` failed; darwin-rebuild will not see this pin"))


def build_cask(token):
    """flake 経由で 1 cask をビルド検証(darwinOverlays 適用後の実物)。"""
    return run(
        ["nix", "build", "--no-link",
         f'.#darwinConfigurations.default.pkgs.brewCasks."{token}"'],
        check=False,
    )


def adopt_cask(token, live, base, args):
    """Grow an Auto Pin for an OUTDATED cask. One data file, nothing else."""
    rel = f"{AUTO_REL}/{token}.json"
    live_disp = cask_display_version(live["version"])
    base_disp = cask_display_version(base) if base else "?"
    if args.dry_run:
        print(f"→ {token}: would adopt {base_disp} → {bold(live_disp)} ({rel})")
        return True
    if auto_pin_path(token).exists():
        print(red(f"✗ {token}: {rel} already exists — use `update {token}` instead"))
        return False
    print(f"→ {token}: adopting {base_disp} → {bold(live_disp)} ({rel})")
    path = None
    try:
        path = write_auto_pin(token, live, enabled=True, base_at_adoption=base_disp if base else None)
        stage(path)
        if args.no_build:
            print(yellow("   build verification skipped (--no-build)"))
        else:
            print(f"   verifying: nix build brewCasks.{token} …")
            pr = build_cask(token)
            if pr.returncode != 0:
                log = Path(tempfile.gettempdir()) / f"nix-pins-adopt-{token}-build.log"
                log.write_text(pr.stderr)
                raise BuildFailure(f"build failed — full log: {log}\n" + pr.stderr.strip()[-1500:])
        print(green(f"✓ {token}: Auto Pin {live_disp} created" + ("" if args.no_build else " (build OK)")))
        return True
    except Exception as ex:  # noqa: BLE001
        print(red(f"✗ {token}: {ex}"))
        if args.keep_on_failure and path is not None:
            print(yellow(f"   kept {rel} (--keep-on-failure)"))
            return False
        if path is not None and path.exists():
            path.unlink()
            run(["git", "rm", "-q", "--cached", "-f", "--", str(path)], check=False)
            print(yellow(f"   rolled back: {rel} deleted and unstaged"))
        return False


def readopt_entry(entry, args, target_version=None, from_all=False):
    """Updating an Auto Pin = regeneration from the live API (never a URL guess)."""
    name = entry["name"]
    token = auto_cask_token(entry)
    path = REPO / entry["file"]
    if not token:
        print(red(f"✗ {name}: Auto Pin without a brew-cask token"))
        return False
    current, err = pinned_version(entry)
    if err:
        print(red(f"✗ {name}: {err}"))
        return False
    try:
        live = live_cask(token)
    except Exception as ex:  # noqa: BLE001
        print(red(f"✗ {name}: live API lookup failed: {ex}"))
        return False
    latest = cask_display_version(live["version"])
    if target_version and target_version != latest:
        print(red(f"✗ {name}: an Auto Pin can only be regenerated at the live version "
                  f"({latest}) — to hold a different one, move it to a hand-written "
                  "overlay in overlays/"))
        return False
    c = compare_versions(current, latest)
    if c == 0 and not args.force:
        print(green(f"✓ {name}: already at {current}"))
        return True
    if c > 0 and not args.force:
        # Not silently "fine": brew is authoritative for casks, so a pin that
        # looks newer means a rollback or an unorderable version scheme.
        print(yellow(f"⚠ {name}: pinned {current} does not match live {latest} and looks newer; "
                     "skipping — re-run with --force to take the live version"))
        return True
    major = _major_bump(current, latest)
    if major and from_all and not args.major:
        print(yellow(f"→ {name}: major bump {current} → {latest} — skipped under --all "
                     f"(run `update {name}` explicitly or pass --major)"))
        return True
    if args.dry_run:
        tag = yellow(" (major bump)") if major else ""
        print(f"→ {name}: would re-adopt {current} → {bold(latest)} in {entry['file']}{tag}")
        return True
    print(f"→ {name}: re-adopting {current} → {bold(latest)} in {entry['file']}")
    if major:
        print(yellow(f"   note: this is a major version bump ({current} → {latest})"))
    original = path.read_text()
    try:
        # Provenance from the first adoption is carried over, so the pin's age
        # and the base it originally outran stay readable.
        write_auto_pin(
            token,
            live,
            enabled=auto_enabled(entry),
            adopted=entry["_auto"].get("adopted"),
            base_at_adoption=entry["_auto"].get("baseAtAdoption"),
        )
        stage(path)
        if args.no_build:
            print(yellow("   build verification skipped (--no-build)"))
        else:
            print(f"   verifying: nix build brewCasks.{token} …")
            pr = build_cask(token)
            if pr.returncode != 0:
                log = Path(tempfile.gettempdir()) / f"nix-pins-{name}-build.log"
                log.write_text(pr.stderr)
                raise BuildFailure(f"build failed — full log: {log}\n" + pr.stderr.strip()[-1500:])
        print(green(f"✓ {name}: {current} → {latest} updated" + ("" if args.no_build else " (build OK)")))
        return True
    except Exception as ex:  # noqa: BLE001
        print(red(f"✗ {name}: {ex}"))
        if args.keep_on_failure:
            print(yellow(f"   edits kept in {entry['file']} (--keep-on-failure)"))
        else:
            path.write_text(original)
            print(yellow(f"   {entry['file']} reverted"))
        return False


# ── disable / enable ─────────────────────────────────────────────────────────


def set_manifest_enable(entry, enable):
    """Flip `enable = true|false;` inside the entry's block in manifest.

    A single literal-value edit, scoped between this entry's `name = "…";` and
    the next one — never structural surgery, and never a commented-out line in
    flake.nix (flake.nix builds its overlay lists from this field).
    """
    name = entry["name"]
    text = MANIFEST.read_text()
    anchor = re.search(r'name = "' + re.escape(name) + r'";', text)
    if not anchor:
        print(red(f"✗ {name}: entry not found in {MANIFEST.name}"))
        return False
    nxt = re.compile(r'name = "').search(text, anchor.end())
    end = nxt.start() if nxt else len(text)
    seg = text[anchor.end():end]
    m = re.search(r"(\benable\s*=\s*)(true|false)(\s*;)", seg)
    if not m:
        print(red(f"✗ {name}: no `enable` field found in its {MANIFEST.name} block"))
        return False
    if (m.group(2) == "true") == enable:
        print(cyan(f"→ {name}: already {'enabled' if enable else 'disabled'}"))
        return True
    new_seg = seg[: m.start()] + m.group(1) + ("true" if enable else "false") + m.group(3) + seg[m.end():]
    MANIFEST.write_text(text[: anchor.end()] + new_seg + text[end:])
    if entry["kind"] == "overlay":
        mech = f"flake.nix now {'applies' if enable else 'stops applying'} the overlay"
    else:
        mech = "declared intent for the dashboard; the consuming module is untouched"
    print(green(f"✓ {name}: {'enabled' if enable else 'disabled'} (`enable` in {MANIFEST.name} — {mech})"))
    return True


def toggle_entry(entry, enable):
    """Flip an Entry's enable state. Always a data edit: the Auto Pin JSON field,
    or the `enable` field in manifest — flake.nix is never touched."""
    name = entry["name"]
    if is_auto_entry(entry):
        path = REPO / entry["file"]
        data = dict(entry["_auto"])
        if bool(data["enabled"]) == enable:
            print(cyan(f"→ {name}: already {'enabled' if enable else 'disabled'}"))
            return True
        data["enabled"] = enable
        write_auto_pin_data(path, data)
        print(green(f"✓ {name}: {'enabled' if enable else 'disabled'} (`enabled` in {entry['file']})"))
        return True
    return set_manifest_enable(entry, enable)


def cmd_toggle(args, enable):
    entries = load_entries()
    by_name = {e["name"]: e for e in entries}
    if args.name not in by_name:
        print(red(f"✗ unknown entry: {args.name} (see `nix-pins.py list`)"))
        return EXIT_ERROR
    if not require_clean_tree(args):
        return EXIT_ERROR
    ok = toggle_entry(by_name[args.name], enable)
    if ok:
        print(dim("反映は darwin-rebuild / nixos-rebuild switch"))
    return EXIT_OK if ok else EXIT_ACTION


# ── sync(警告 → 選択 → 一括実行)────────────────────────────────────────────


def parse_selection(sel, actions, allow_major):
    sel = (sel or "").strip().lower()
    if sel in ("", "none", "n"):
        return []
    if sel in ("all", "a"):
        return [
            i
            for i, a in enumerate(actions)
            if a["kind"] != "input" and (allow_major or not a["major"])
        ]
    idxs = []
    for part in re.split(r"[,\s]+", sel):
        if not part:
            continue
        if not part.isdigit() or not (1 <= int(part) <= len(actions)):
            raise ValueError(f"不正な選択: {part}")
        i = int(part) - 1
        if i not in idxs:
            idxs.append(i)
    return idxs


def cmd_sync(args):
    args.force = False  # update_entry と引数を共有するための既定値
    entries = load_entries()
    by_name = {e["name"]: e for e in entries}
    print(dim("上流と base を照会中…"))
    rows = build_rows(entries)
    cask_rows = build_cask_rows(entries)

    actions = []
    for c in cask_rows:
        if c["status"] in ("OUTDATED", "MISMATCH"):
            base_v = cask_display_version(c["base"])
            live_v = cask_display_version(c["live_version"])
            # A MISMATCH is offered too — brew is authoritative for casks — but
            # it is flagged, and never swept up by "all".
            major = _major_bump(base_v, live_v) or c["status"] == "MISMATCH"
            note = " (mismatch: base looks newer)" if c["status"] == "MISMATCH" else ""
            actions.append(
                {
                    "kind": "adopt",
                    "name": c["token"],
                    "major": major,
                    "cask": c,
                    "label": f"[adopt]   brewCasks.{c['token']}: {base_v} → {live_v} の Auto Pin を新規作成"
                    + (" (major)" if _major_bump(base_v, live_v) else "")
                    + note,
                }
            )
        elif c["status"] == "ROLLING":
            print(dim(f"· {c['token']}: sha256=no_check — 決定論的にピン不可のためスキップ"))
    for r in rows:
        e = r["entry"]
        if r["status"].startswith("OUTDATED") and r["enabled"] and is_pin(e) and e.get("update"):
            major = bool(r["pinned"] and r["latest"] and _major_bump(r["pinned"], r["latest"]))
            actions.append(
                {
                    "kind": "update",
                    "name": r["name"],
                    "major": major,
                    "label": f"[update]  {r['name']}: {r['pinned']} → {r['latest']}"
                    + (" (major)" if major else ""),
                }
            )
    for r in rows:
        e = r["entry"]
        if not r["enabled"]:
            continue
        if r["status"].endswith("+DROP?") and is_auto_entry(e):
            actions.append(
                {
                    "kind": "disable",
                    "name": r["name"],
                    "major": False,
                    "label": f"[disable] {r['name']}: base({r['nixpkgs']})が追いついた — Auto Pin を無効化",
                }
            )
        elif r["status"] == "FIXED-UPSTREAM" and e["kind"] == "overlay" and not is_auto_entry(e):
            actions.append(
                {
                    "kind": "disable",
                    "name": r["name"],
                    "major": False,
                    "label": f"[disable] {r['name']}: nixpkgs 側で解消 — flake.nix の import 行をコメントアウト",
                }
            )
    if any(a["kind"] == "adopt" for a in actions):
        try:
            age = brew_api_age()
        except Exception:  # noqa: BLE001
            age = "?"
        actions.append(
            {
                "kind": "input",
                "name": "brew-api",
                "major": False,
                "label": f"[input]   brew-api input({age})を最新スナップショットへ — 全 cask が一斉に動く(個別ピンの代替)",
            }
        )

    if not actions:
        print(green("✓ adopt / update / disable の候補なし"))
        return EXIT_OK
    print()
    print(bold("実行候補(警告 → 選択 → 実行):"))
    for i, a in enumerate(actions, 1):
        print(f"  {i:>2}. {a['label']}")
    print(dim('  選択: 番号(スペース/カンマ区切り)/ "all"(major と [input] は除く)/ "none"'))
    if args.dry_run:
        print(dim("(--dry-run: 選択・実行なし)"))
        return EXIT_ACTION
    sel = args.select
    if sel is None:
        if not sys.stdin.isatty():
            print(red("✗ 非対話環境では --select を指定すること(例: --select all / --select '1 3')"))
            return EXIT_ERROR
        try:
            sel = input("> ")
        except EOFError:
            sel = "none"
    try:
        chosen = parse_selection(sel, actions, allow_major=args.major)
    except ValueError as ex:
        print(red(f"✗ {ex}"))
        return EXIT_ERROR
    if not chosen:
        print("何も選択されなかった — 終了")
        return EXIT_OK
    if not require_clean_tree(args):
        return EXIT_ERROR
    ok = True
    for i in chosen:
        a = actions[i]
        if a["kind"] == "adopt":
            c = a["cask"]
            ok = adopt_cask(a["name"], c["live"], c["base"], args) and ok
        elif a["kind"] == "update":
            ok = update_entry(by_name[a["name"]], args) and ok
        elif a["kind"] == "disable":
            ok = toggle_entry(by_name[a["name"]], enable=False) and ok
        elif a["kind"] == "input":
            print("→ brew-api input を更新中…")
            pr = run(["nix", "flake", "lock", "--update-input", "brew-api"], check=False)
            if pr.returncode == 0:
                print(green("✓ brew-api input updated ")
                      + dim("(全 cask の base が動いた — check で Auto Pin の DROP? を確認)"))
            else:
                ok = False
                print(red(f"✗ flake lock failed:\n{pr.stderr.strip()[-800:]}"))
    print(dim("\nreview with `git diff`, then apply via darwin-rebuild/nixos-rebuild switch"))
    return EXIT_OK if ok else EXIT_ACTION


def main():
    global USE_COLOR, ALLOW_CMD
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    common.add_argument("--allow-cmd-sources", action="store_true",
                        help="permit `source = \"cmd:...\"` entries, which run a shell command")
    dirty = argparse.ArgumentParser(add_help=False)
    dirty.add_argument("--allow-dirty", action="store_true",
                       help="write even when the affected files have uncommitted changes")
    parser = argparse.ArgumentParser(
        prog="nix-pins.py",
        description="dashboard + updater for version pins in overlays/ and packages/",
        parents=[common],
        epilog="exit codes: 0 = clean, 1 = work to do, 2 = tool/config error",
    )
    sub = parser.add_subparsers(dest="cmd")

    p_check = sub.add_parser("check", parents=[common],
                             help="show the dashboard and warn on drift (default)")
    p_check.add_argument("--json", action="store_true", help="machine-readable output")
    p_check.add_argument("--strict", action="store_true",
                         help="also fail on UNKNOWN/ERROR/REVIEW/FIXED-UPSTREAM")
    p_check.add_argument("--installed", action="store_true",
                         help="also evaluate each host's package list and show which hosts"
                              " actually install every entry (slow: full host evals)")

    p_update = sub.add_parser("update", parents=[common, dirty],
                              help="bump version+hash in place, verify build")
    p_update.add_argument("names", nargs="*", metavar="NAME[=VERSION]",
                          help="entries to update (explicit version optional)")
    p_update.add_argument("--all", action="store_true", help="update all active auto-updatable pins")
    p_update.add_argument("--dry-run", action="store_true", help="only show what would change")
    p_update.add_argument("--no-build", action="store_true", help="skip the nix build verification")
    p_update.add_argument("--keep-on-failure", action="store_true",
                          help="keep file edits when the verification build fails")
    p_update.add_argument("--force", action="store_true", help="re-pin even if already up to date")
    p_update.add_argument("--major", action="store_true",
                          help="allow major version bumps under --all (explicit names always allow them)")

    sub.add_parser("list", parents=[common], help="list entries (manifest + Auto Pins)")

    p_sync = sub.add_parser("sync", parents=[common, dirty],
                            help="警告 → 選択 → 一括実行(cask の adopt / pin の update / disable / input 更新)")
    p_sync.add_argument("--select", metavar="SEL",
                        help='非対話選択: "all" / "none" / "1 3 4"(未指定なら対話プロンプト)')
    p_sync.add_argument("--dry-run", action="store_true", help="候補一覧のみ表示して終了")
    p_sync.add_argument("--no-build", action="store_true", help="nix build 検証をスキップ")
    p_sync.add_argument("--major", action="store_true", help='"all" 選択に major bump を含める')
    p_sync.add_argument("--keep-on-failure", action="store_true",
                        help="検証ビルド失敗時も生成/編集を残す")

    p_disable = sub.add_parser("disable", parents=[common, dirty],
                               help="Entry を無効化(Auto Pin: enabled=false / overlay: flake.nix の行をコメント)")
    p_disable.add_argument("name")
    p_enable = sub.add_parser("enable", parents=[common, dirty], help="disable の逆")
    p_enable.add_argument("name")

    argv = sys.argv[1:] or ["check"]
    args = parser.parse_args(argv)
    USE_COLOR = sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ
    ALLOW_CMD = args.allow_cmd_sources

    try:
        if args.cmd == "update":
            return cmd_update(args)
        if args.cmd == "list":
            return cmd_list(args)
        if args.cmd == "sync":
            return cmd_sync(args)
        if args.cmd == "disable":
            return cmd_toggle(args, enable=False)
        if args.cmd == "enable":
            return cmd_toggle(args, enable=True)
        return cmd_check(args)
    except ConfigError as ex:
        # Config is broken, which is not the same as "there is work to do".
        print(red(f"✗ {ex}"), file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
