#!/usr/bin/env python3
"""Plan Meter — Claude Code, Codex, and Z.AI plan usage in the herdr tab bar and popup.

  meter.py bar       one line for herdr's tab bar (a ui.tab_bar_right command entry);
                     refreshes the cache first when it is stale
  meter.py panel     the popup: Tab detail/compact · r refresh · q close
  meter.py setup     the popup that shows (and copies) the config snippet for this install
  meter.py open      open the popup, or `open setup` (plugin actions)
  meter.py refresh   refresh now and print the line
  meter.py shim      (re)write the tab bar shim — runs on every herdr start
  meter.py event     plugin event hook: refresh when an agent turn ends

Credentials come from each tool's own login (Claude Code: macOS Keychain or
~/.claude/.credentials.json; Codex: $CODEX_HOME/auth.json; Z.AI: Pi's
$PI_CODING_AGENT_DIR/auth.json) and go only to that vendor's usage endpoint.
When a Claude access token has expired, the plugin uses the refresh token to
obtain a new one and writes it back to ~/.claude/.credentials.json (or the
macOS Keychain). The cache stores plan names, percentages, and reset times only.

PLAN_METER_DEMO=1 renders sample data without reading credentials or calling anything.
"""
import contextlib
import fcntl
import hashlib
import http.client
import json
import os
import re
import select
import shlex
import shutil
import socket
import subprocess
import sys
import termios
import threading
import time
import tty
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

PLUGIN_ID = "gtrias.plan-meter"
VERSION = "0.2.0"
UA = f"herdr-plan-meter/{VERSION}"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "herdr-plan-meter"
CACHE = CACHE_DIR / "usage.json"
LOCK = CACHE_DIR / "fetch.lock"
BAR_MAX_AGE = 300    # the tab bar redraws every 15 s but fetches at most every 5 minutes —
PANEL_MAX_AGE = 120  # Anthropic's usage endpoint answers 429 when polled much more often
EVENT_MIN_AGE = 120  # an agent turn just ended — usage moved; at most every 2 minutes
MANUAL_MIN_AGE = 30  # `r` in the popup and the refresh action
FETCH_DEADLINE = 15  # herdr kills a tab bar command at 25 s, and urllib's timeout does not cover DNS
CONNECT_TIMEOUT = 3  # per address: a dead IPv6 route must not eat the deadline before IPv4 is tried
STALE_AFTER = 900    # no successful fetch for 15 minutes: prefix the tab bar number with ~
DEMO = os.environ.get("PLAN_METER_DEMO") == "1"
PROVIDERS = ("claude", "codex", "zai")
NAME = {"claude": "Claude", "codex": "Codex", "zai": "Z.AI"}
NERD_ICON = {"claude": "\uec82", "codex": "\uec81", "zai": "Z"}  # Text fallback; no bundled Z.AI glyph.

TEXT = {
    "en": {
        "used": "used", "session": "5h", "weekly": "Weekly", "hours": "{n}h", "days": "{n}d",
        "detail": "Detail", "compact": "Compact", "loading": "Loading…", "refreshing": "Refreshing…",
        "updated": "Updated {ago}", "keys": "Tab view · r refresh · q close",
        "now": "just now", "s": "{n}s ago", "m": "{n}m ago", "h": "{n}h ago", "as_of": "value from {ago}",
        "today": "Today", "weekdays": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
        "expired": "Login expired — run {cli} to renew it", "limited": "Rate limited (429) — retrying later",
        "http": "HTTP {status}", "network": "Network error", "format": "Unexpected response format",
        "nologin": "Not logged in", "unexpected": "Unexpected error: {name}",
        "setup_title": "Add this to {path}, then run `herdr server reload-config`:",
        "setup_keys": "c copy to clipboard · q close", "copied": "Copied.", "no_clipboard": "No clipboard tool found.",
    },
    "ko": {
        "used": "사용", "session": "5시간", "weekly": "주간", "hours": "{n}시간", "days": "{n}일",
        "detail": "상세", "compact": "압축", "loading": "불러오는 중…", "refreshing": "갱신 중…",
        "updated": "{ago} 갱신", "keys": "Tab 보기 전환 · r 새로고침 · q 닫기",
        "now": "방금", "s": "{n}초 전", "m": "{n}분 전", "h": "{n}시간 전", "as_of": "{ago} 값",
        "today": "오늘", "weekdays": ("월", "화", "수", "목", "금", "토", "일"),
        "expired": "로그인 만료 — {cli}를 실행하면 갱신돼요", "limited": "요청 제한(429) — 잠시 뒤 다시 시도",
        "http": "HTTP {status}", "network": "네트워크 오류", "format": "응답 형식이 바뀌었어요",
        "nologin": "로그인 정보 없음", "unexpected": "예상 밖 오류: {name}",
        "setup_title": "{path} 에 아래를 붙여 넣고 `herdr server reload-config` 를 실행하세요:",
        "setup_keys": "c 클립보드에 복사 · q 닫기", "copied": "복사했어요.", "no_clipboard": "클립보드 도구가 없어요.",
    },
}


# ── settings ─────────────────────────────────────────────────────────

def herdr_dir():
    path = os.environ.get("HERDR_CONFIG_PATH")
    return Path(path).expanduser().parent if path else Path.home() / ".config" / "herdr"


def plugin_config_dir():
    # herdr passes this to plugin commands but not to tab bar commands; same path either way
    base = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    return Path(base) if base else herdr_dir() / "plugins" / "config" / PLUGIN_ID


def load_config():
    """Optional config.toml in the plugin config dir (`herdr plugin config-dir gtrias.plan-meter`),
    flat `key = "value"` lines: lang = "auto" | "en" | "ko", icons = "text" | "nerd"."""
    try:
        text = (plugin_config_dir() / "config.toml").read_text()
    except OSError:
        return {}
    return dict(re.findall(r"""(?m)^\s*(\w+)\s*=\s*["']([^"'\n]*)["']""", text))


def pick_lang(setting):
    if setting in TEXT:
        return setting
    if sys.platform == "darwin":  # macOS keeps the UI language here; LANG is often en_US regardless
        try:
            out = subprocess.run(["/usr/bin/defaults", "read", "-g", "AppleLanguages"],
                                 capture_output=True, text=True, timeout=2).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        m = re.search(r"[a-z]{2}", out)
        if m:
            return m.group(0) if m.group(0) in TEXT else "en"
    env = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
    return env[:2] if env[:2] in TEXT else "en"


class View:
    def __init__(self, cfg):
        self.t = TEXT[pick_lang(cfg.get("lang", "auto"))]
        self.nerd = cfg.get("icons") == "nerd"

    def mark(self, p):
        # an extra space after the icon: terminals like Ghostty let an icon spill into the next cell
        return NERD_ICON[p] + " " if self.nerd else NAME[p]


# ── fetching ─────────────────────────────────────────────────────────

class FetchError(Exception):
    def __init__(self, code, retry_after=0, **params):
        super().__init__(code)
        self.code, self.retry_after, self.params = code, retry_after, params


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # never carry the bearer token to wherever a 3xx points; it fails as HTTP 3xx


def connect_any(address, timeout, source_address=None):
    """socket.create_connection, but each address gets CONNECT_TIMEOUT and the families alternate
    (RFC 8305 order). create_connection spends the full request timeout on every address in
    getaddrinfo order, so on a host whose IPv6 route silently drops packets two IPv6 addresses
    cost 20 s and the fetch misses FETCH_DEADLINE before IPv4 is ever tried."""
    host, port = address
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    first = [i for i in infos if i[0] == infos[0][0]]
    rest = [i for i in infos if i[0] != infos[0][0]]
    ordered = [i for pair in zip(first, rest) for i in pair] + first[len(rest):] + rest[len(first):]
    err = OSError(f"no address for {host}")
    for family, kind, proto, _, addr in ordered:
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(min(CONNECT_TIMEOUT, timeout) if timeout else CONNECT_TIMEOUT)
            if source_address:
                sock.bind(source_address)
            sock.connect(addr)
        except OSError as e:
            sock.close()
            err = e
            continue
        sock.settimeout(timeout)
        return sock
    raise err


class FastFallbackHTTPS(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = connect_any  # the hook HTTPConnection.connect dials through


class FastFallbackHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(FastFallbackHTTPS, req, context=self._context)


OPENER = urllib.request.build_opener(NoRedirect, FastFallbackHandler)


def http_json(url, headers, cli):
    req = urllib.request.Request(url, headers={**headers, "User-Agent": UA})
    try:
        with OPENER.open(req, timeout=10) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise FetchError("expired", cli=cli)
        if e.code == 429:
            ra = e.headers.get("Retry-After", "")
            raise FetchError("limited", max(300, int(ra) if ra.isdigit() else 0))
        raise FetchError("http", status=e.code)
    except ValueError:
        raise FetchError("format")
    except OSError:  # URLError, timeouts
        raise FetchError("network")


def to_epoch(v):
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e11 else v
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def claude_blobs(cfg):
    if sys.platform == "darwin":
        # Claude Code stores its login in a Keychain item suffixed with the first 8 hex chars
        # of sha256(config dir); older versions used the unsuffixed name.
        scoped = "Claude Code-credentials-" + hashlib.sha256(cfg.encode()).hexdigest()[:8]
        for service in (scoped, "Claude Code-credentials"):
            try:
                r = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
                                   capture_output=True, text=True, timeout=5)
            except (OSError, subprocess.SubprocessError):
                continue
            if r.returncode == 0:
                yield r.stdout
    try:
        yield (Path(cfg) / ".credentials.json").read_text()  # Linux (and a macOS fallback)
    except OSError:
        pass


CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


def claude_oauth():
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    for blob in claude_blobs(cfg):
        try:
            oauth = json.loads(blob).get("claudeAiOauth") or {}
        except (ValueError, AttributeError):
            continue
        if oauth.get("accessToken"):
            return oauth, cfg
    raise FetchError("nologin")


def claude_refresh(oauth, cfg):
    """Use the refresh token to obtain a fresh access token from Anthropic."""
    rt = oauth.get("refreshToken")
    if not rt:
        raise FetchError("expired", cli="Claude Code")
    if oauth.get("refreshTokenExpiresAt") and oauth["refreshTokenExpiresAt"] / 1000 < time.time():
        raise FetchError("expired", cli="Claude Code")
    body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": rt,
                                    "client_id": CLAUDE_CLIENT_ID}).encode()
    req = urllib.request.Request(CLAUDE_TOKEN_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA})
    try:
        with OPENER.open(req, timeout=10) as r:
            tokens = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            raise FetchError("expired", cli="Claude Code")
        raise FetchError("http", status=e.code)
    except (ValueError, OSError):
        raise FetchError("network")
    new_oauth = {**oauth}
    if tokens.get("access_token"):
        new_oauth["accessToken"] = tokens["access_token"]
    if tokens.get("refresh_token"):
        new_oauth["refreshToken"] = tokens["refresh_token"]
    if tokens.get("expires_in"):
        new_oauth["expiresAt"] = int((time.time() + tokens["expires_in"]) * 1000)
    if tokens.get("refresh_token_expires_in"):
        new_oauth["refreshTokenExpiresAt"] = int((time.time() + tokens["refresh_token_expires_in"]) * 1000)
    claude_save(new_oauth, cfg)
    return new_oauth


def claude_save(oauth, cfg):
    """Write refreshed tokens back to the credentials file."""
    cred_path = Path(cfg) / ".credentials.json"
    try:
        creds = json.loads(cred_path.read_text())
    except (OSError, ValueError):
        creds = {}
    creds["claudeAiOauth"] = oauth
    tmp = cred_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(creds, ensure_ascii=False))
    os.replace(tmp, cred_path)
    if sys.platform == "darwin":
        service = "Claude Code-credentials-" + hashlib.sha256(cfg.encode()).hexdigest()[:8]
        try:
            subprocess.run(["/usr/bin/security", "delete-generic-password", "-s", service],
                           capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            subprocess.run(["/usr/bin/security", "add-generic-password", "-s", service,
                            "-a", "", "-w", json.dumps(creds, ensure_ascii=False), "-U"],
                           capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass


def claude_plan(oauth):
    m = re.search(r"max_(\d+x)", oauth.get("rateLimitTier") or "")
    return f"Max {m.group(1)}" if m else (oauth.get("subscriptionType") or "").title()


def parse_claude(d):
    wins = []
    for key, kind in (("five_hour", "session"), ("seven_day", "weekly")):
        w = d.get(key)
        if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float)):
            wins.append({"kind": kind, "used": w["utilization"], "resets_at": to_epoch(w.get("resets_at"))})
    for lim in d.get("limits") or []:  # per-model weekly caps
        model = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
        if lim.get("kind") == "weekly_scoped" and model and isinstance(lim.get("percent"), (int, float)):
            wins.append({"kind": "weekly", "model": model, "used": lim["percent"],
                         "resets_at": to_epoch(lim.get("resets_at"))})
    if not wins:
        raise FetchError("format")
    return wins


def fetch_claude():
    oauth, cfg = claude_oauth()
    if oauth.get("expiresAt") and oauth["expiresAt"] / 1000 < time.time():
        oauth = claude_refresh(oauth, cfg)
    try:
        d = http_json("https://api.anthropic.com/api/oauth/usage",
                      {"Authorization": f"Bearer {oauth['accessToken']}", "anthropic-beta": "oauth-2025-04-20"},
                      "Claude Code")
    except FetchError as e:
        if e.code == "expired" and oauth.get("refreshToken"):
            oauth = claude_refresh(oauth, cfg)
            d = http_json("https://api.anthropic.com/api/oauth/usage",
                          {"Authorization": f"Bearer {oauth['accessToken']}", "anthropic-beta": "oauth-2025-04-20"},
                          "Claude Code")
        else:
            raise
    return {"plan": claude_plan(oauth), "windows": parse_claude(d)}


def window_kind(secs, fallback):
    hours = (secs or 0) / 3600
    if not hours:
        return {"kind": fallback}
    if round(hours) == 5:
        return {"kind": "session"}
    if 6 <= hours / 24 <= 8:
        return {"kind": "weekly"}
    return {"kind": "hours", "n": round(hours)} if hours < 48 else {"kind": "days", "n": round(hours / 24)}


def parse_codex(d):
    wins = []
    for key, fallback in (("primary_window", "session"), ("secondary_window", "weekly")):
        w = (d.get("rate_limit") or {}).get(key)
        if isinstance(w, dict) and isinstance(w.get("used_percent"), (int, float)):
            wins.append({**window_kind(w.get("limit_window_seconds"), fallback),
                         "used": w["used_percent"], "resets_at": to_epoch(w.get("reset_at"))})
    if not wins:
        raise FetchError("format")
    return wins


def fetch_codex():
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        tokens = json.loads((home / "auth.json").read_text())["tokens"]
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    except (OSError, ValueError, KeyError, TypeError):  # no file, API-key login, …
        raise FetchError("nologin")
    if tokens.get("account_id"):
        headers["ChatGPT-Account-Id"] = tokens["account_id"]
    d = http_json("https://chatgpt.com/backend-api/wham/usage", headers, "Codex")
    return {"plan": (d.get("plan_type") or "").title(), "windows": parse_codex(d)}

def parse_zai(response):
    data = response.get("data") if isinstance(response, dict) else None
    limits = data.get("limits") if isinstance(data, dict) else None
    windows = []
    for limit in limits or []:
        if not isinstance(limit, dict):
            continue
        if limit.get("type") == "TOKENS_LIMIT":
            kind = "session"
        elif limit.get("type") == "CREDIT_LIMIT":
            kind = {(3, 5): "session", (6, 1): "weekly"}.get((limit.get("unit"), limit.get("number")))
        else:
            continue
        percentage = limit.get("percentage")
        if kind and isinstance(percentage, (int, float)) and not isinstance(percentage, bool):
            windows.append({
                "kind": kind,
                "used": percentage,
                "resets_at": to_epoch(limit.get("nextResetTime")),
            })
    if not windows:
        raise FetchError("format")
    return windows


def fetch_zai():
    agent_dir = Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent")
    try:
        key = json.loads((agent_dir / "auth.json").read_text()).get("zai", {}).get("key")
    except (OSError, ValueError, AttributeError):
        raise FetchError("nologin")
    if not isinstance(key, str) or not key:
        raise FetchError("nologin")
    response = http_json(
        "https://api.z.ai/api/monitor/usage/quota/limit",
        {"Authorization": key, "Accept-Language": "en-US,en"},
        "Z.AI",
    )
    return {"plan": "GLM Coding Plan", "windows": parse_zai(response)}


FETCH = {"claude": fetch_claude, "codex": fetch_codex, "zai": fetch_zai}




def attempt(p):
    now = time.time()
    try:
        return {**FETCH[p](), "fetched_at": now, "attempt_at": now, "error": None, "retry_at": 0}
    except FetchError as e:
        return {"attempt_at": now, "error": {"code": e.code, **e.params}, "retry_at": now + e.retry_after}
    except Exception as e:  # keep the last good reading instead of blanking the tab bar
        return {"attempt_at": now, "error": {"code": "unexpected", "name": type(e).__name__}, "retry_at": 0}


def fetch_all(due):
    """attempt() each provider in a daemon thread with one overall deadline. Daemon threads
    never hold up exit, so closing the popup mid-fetch is instant."""
    results = {}
    threads = [threading.Thread(target=lambda p=p: results.__setitem__(p, attempt(p)), daemon=True)
               for p in due]
    for t in threads:
        t.start()
    end = time.time() + FETCH_DEADLINE
    for t in threads:
        t.join(max(0, end - time.time()))
    now = time.time()
    return {p: results.get(p) or {"attempt_at": now, "error": {"code": "network"}, "retry_at": 0} for p in due}


def demo_data(now):
    h, d = 3600, 86400
    return {
        "claude": {"plan": "Max 5x", "fetched_at": now - 20, "windows": [
            {"kind": "session", "used": 42, "resets_at": now + 2 * h + 13 * 60},
            {"kind": "weekly", "used": 67, "resets_at": now + 3 * d + 10 * h},
            {"kind": "weekly", "model": "Fable", "used": 83, "resets_at": now + 3 * d + 10 * h}]},
        "codex": {"plan": "Pro", "fetched_at": now - 20, "windows": [
            {"kind": "session", "used": 12, "resets_at": now + 4 * h + 26 * 60},
            {"kind": "weekly", "used": 31, "resets_at": now + 4 * d + 5 * h}]},
        "zai": {"plan": "GLM Coding Plan", "fetched_at": now - 20, "windows": [
            {"kind": "session", "used": 28, "resets_at": now + 2 * h + 15 * 60},
            {"kind": "weekly", "used": 49, "resets_at": now + 5 * d + 8 * h}]},
    }


def load():
    if DEMO:
        return demo_data(time.time())
    try:
        data = json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return {}
    # tolerate a cache from another version or one damaged by hand
    return {p: v for p, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def save(data):
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, CACHE)


@contextlib.contextmanager
def cache_lock():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def refresh(max_age):
    """Fetch the providers not tried in the last max_age seconds and merge the results into
    the cache. The attempt is recorded before the network call, so a fetch that hangs or gets
    killed still counts and no one retries early. The lock covers file I/O only, so the tab
    bar never waits on another process's network call. A failure keeps the last good reading
    and records the error."""
    if DEMO:
        return load()
    with cache_lock():
        data, now = load(), time.time()
        due = [p for p in PROVIDERS
               if now - data.get(p, {}).get("attempt_at", 0) >= max_age
               and now >= data.get(p, {}).get("retry_at", 0)]
        if not due:
            return data
        for p in due:
            data[p] = {**data.get(p, {}), "attempt_at": now}
        save(data)
    results = fetch_all(due)
    with cache_lock():
        data = load()
        for p, res in results.items():
            data[p] = {**data.get(p, {}), **res}
        save(data)
        return data


# ── rendering ────────────────────────────────────────────────────────

def error_of(st):
    err = st.get("error")
    return err if isinstance(err, dict) else None


def shown(data):
    # hide a provider the user never signed into; keep one that is loading or failing
    return [p for p in PROVIDERS
            if windows_now(data.get(p, {}), 0) or (error_of(data.get(p, {})) or {}).get("code") != "nologin"]


def windows_now(st, now):
    # skip malformed entries; a window whose reset time has passed has started over — show 0
    wins = st.get("windows") if isinstance(st.get("windows"), list) else []
    return [{**w, "used": 0, "resets_at": None} if w.get("resets_at") and w["resets_at"] <= now else w
            for w in wins if isinstance(w, dict) and isinstance(w.get("used"), (int, float))]


def headline(wins):
    return max(wins, key=lambda w: (w["used"], w.get("resets_at") or 0))


def left(ts, now):
    s = max(0, int(ts - now))
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"


def clock(ts, now, t):
    at = datetime.fromtimestamp(ts)
    day = t["today"] if at.date() == datetime.fromtimestamp(now).date() else t["weekdays"][at.weekday()]
    return f"{day} {at:%H:%M}"


def ago(ts, now, t):
    s = int(now - ts)
    if s < 10:
        return t["now"]
    if s < 60:
        return t["s"].format(n=s)
    return t["m"].format(n=s // 60) if s < 3600 else t["h"].format(n=s // 3600)


def label(w, t):
    kind = w.get("kind")
    base = t[kind].format(n=w.get("n")) if kind in ("session", "weekly", "hours", "days") else str(kind)
    return f"{base} · {w['model']}" if w.get("model") else base


ERRORS = ("expired", "limited", "http", "network", "format", "nologin", "unexpected")


def error_text(err, t):
    code = err.get("code")
    try:
        return t[code].format(**err) if code in ERRORS else t["unexpected"].format(name=code)
    except (KeyError, IndexError):
        return str(code)


def bar_text(data, now, view):
    parts = []
    for p in shown(data):
        st = data.get(p, {})
        wins = windows_now(st, now)
        if not wins:
            parts.append(f"{view.mark(p)} –")
            continue
        w = headline(wins)
        stale = "~" if now - st.get("fetched_at", 0) > STALE_AFTER else ""
        reset = f" {left(w['resets_at'], now)}" if w["resets_at"] else ""
        parts.append(f"{view.mark(p)} {stale}{round(w['used'])}% {view.t['used']}{reset}")
    return "   ".join(parts)


RGB = {"fg": (192, 202, 245), "dim": (110, 118, 150), "track": (59, 66, 97), "green": (158, 206, 106),
       "yellow": (224, 175, 104), "red": (247, 118, 142), "claude": (217, 119, 87),
       "codex": (236, 236, 241), "zai": (75, 124, 255)}
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def fg(name, s):
    return "\x1b[38;2;%d;%d;%dm%s\x1b[39m" % (*RGB[name], s)


def bold(s):
    return f"\x1b[1m{s}\x1b[22m"


def sev(used):
    return "green" if used < 50 else "yellow" if used < 80 else "red"


def width(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in ANSI.sub("", s))


def pad(s, n):
    return s + " " * max(0, n - width(s))


def row(lhs, rhs, W):
    return "  " + lhs + " " * max(1, W - 4 - width(lhs) - width(rhs)) + rhs


def window_row(w, now, W, t):
    used = w["used"]
    bw = max(6, W - 47)  # indent 4 + label 14 + 1 + bar + 1 + pct 4 + 2 + reset 19 + margin 2
    fill = max(1 if used > 0 else 0, round(bw * min(100, used) / 100))
    track = fg(sev(used), "━" * fill) + fg("track", "━" * (bw - fill))
    reset = f"{left(w['resets_at'], now)} · {clock(w['resets_at'], now, t)}" if w["resets_at"] else ""
    return ("    " + fg("dim", pad(label(w, t), 14)) + " " + track + " "
            + fg("fg", f"{round(used):>3}%") + "  " + fg("dim", reset))


def panel_lines(data, now, mode, busy, W, view):
    t = view.t
    seg = "".join("\x1b[48;2;41;46;66m" + bold(fg("fg", f" {t[m]} ")) + "\x1b[49m" if m == mode
                  else fg("dim", f" {t[m]} ") for m in ("detail", "compact"))
    lines = [row(fg("dim", "↻ r"), seg, W), ""]
    for p in shown(data):
        st = data.get(p, {})
        wins = windows_now(st, now)
        icon = fg(p, NERD_ICON[p]) + "  " if view.nerd else ""
        title = f"{icon}{bold(fg('fg', NAME[p]))}  {fg('dim', st.get('plan') or '')}"
        if wins:
            h = headline(wins)
            reset = fg("dim", left(h["resets_at"], now)) + "  " if h["resets_at"] else ""
            lines.append(row(title, reset + bold(fg(sev(h["used"]), f"{round(h['used']):>3}%")), W))
            if mode == "detail":
                lines += [window_row(w, now, W, t) for w in wins]
        else:
            lines.append(row(title, fg("dim", t["loading"] if busy else "–"), W))
        if error_of(st) and (mode == "detail" or not wins):
            old = fg("dim", " · " + t["as_of"].format(ago=ago(st["fetched_at"], now, t))) if st.get("fetched_at") else ""
            lines.append("    " + fg("red", "⚠ " + error_text(error_of(st), t)) + old)
        if mode == "detail":
            lines.append("")
    fetched = [data[p]["fetched_at"] for p in PROVIDERS if data.get(p, {}).get("fetched_at")]
    status = t["refreshing"] if busy else t["updated"].format(ago=ago(min(fetched), now, t)) if fetched else ""
    lines.append("  " + fg("dim", " · ".join(s for s in (status, t["keys"]) if s)))
    return lines


class Screen:
    """Alternate screen in cbreak mode, restored on exit."""

    def __enter__(self):
        self.fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[?7l")  # alt screen · hide cursor · no autowrap
        return self

    def __exit__(self, *exc):
        sys.stdout.write("\x1b[?7h\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        return exc[0] is KeyboardInterrupt

    def draw(self, lines):
        _, H = os.get_terminal_size()
        footer = lines[-1]
        lines = (lines[:-1] + [""] * H)[:H - 1] + [footer]  # key hints stick to the bottom row
        sys.stdout.write("\x1b[H" + "\r\n".join(l + "\x1b[K" for l in lines))
        sys.stdout.flush()

    def key(self, timeout):
        return os.read(self.fd, 32) if select.select([self.fd], [], [], timeout)[0] else b""


QUIT = (b"q", b"Q", b"\x1b", b"\x03")


def panel(view):
    ui = {"mode": "detail", "busy": False}

    def kick(max_age):
        if ui["busy"]:
            return
        ui["busy"] = True

        def run():
            try:
                refresh(max_age)
            finally:
                ui["busy"] = False
        threading.Thread(target=run, daemon=True).start()

    with Screen() as screen:
        kick(PANEL_MAX_AGE)
        last = time.time()
        while True:
            now = time.time()
            if now - last >= PANEL_MAX_AGE:
                kick(PANEL_MAX_AGE)
                last = now
            W, H = os.get_terminal_size()
            data = load()
            lines = panel_lines(data, now, ui["mode"], ui["busy"], W, view)
            if ui["mode"] == "detail" and len(lines) > H:  # clipped rows would hide a provider
                ui["mode"] = "compact"
                lines = panel_lines(data, now, "compact", ui["busy"], W, view)
            screen.draw(lines)
            key = screen.key(0.5)
            if key in QUIT:
                break
            if key in (b"r", b"R"):
                kick(MANUAL_MIN_AGE)
                last = time.time()
            elif key in (b"\t", b"\x1b[Z", b"\x1b[C", b"\x1b[D"):
                ui["mode"] = "compact" if ui["mode"] == "detail" else "detail"


def write_shim():
    """config.toml points the tab bar at this file, and this file points at wherever the plugin
    is installed now. herdr's managed checkout path is an internal detail that has changed
    before; the plugin config dir is the documented stable place. Rewritten by setup and by
    the startup hook on every herdr start."""
    path = plugin_config_dir() / "tab-bar.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("#!/bin/sh\n# Written by Plan Meter on setup and on every herdr start. Do not edit.\n"
                   f"exec python3 {shlex.quote(str(Path(__file__).resolve()))} bar\n")
    tmp.chmod(0o755)
    os.replace(tmp, path)  # the tab bar may run it at any moment; never let it see half a file
    return path


def snippet(shim):
    return (
        "[ui]\n"
        'tab_bar_position = "bottom"  # optional\n'
        "tab_bar_right = [\n"
        f'  {{ type = "command", command = {json.dumps(shlex.quote(str(shim)))}, interval_seconds = 15, timeout_seconds = 25 }},\n'
        "]\n\n"
        "[[keys.command]]\n"
        'key = "prefix+u"\n'
        'type = "plugin_action"\n'
        f'command = "{PLUGIN_ID}.open"\n'
        'description = "plan usage"\n'
    )


def copy(text):
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-b", "-i"]):
        if shutil.which(cmd[0]):
            return subprocess.run(cmd, input=text, text=True).returncode == 0
    return False


def setup(view):
    t, text, note = view.t, snippet(write_shim()), ""
    with Screen() as screen:
        while True:
            head = t["setup_title"].format(path=herdr_dir() / "config.toml")
            body = ["  " + fg("fg", head), ""] + ["  " + fg("dim", l) for l in text.splitlines()]
            screen.draw(body + ["", "  " + fg("green", note), "  " + fg("dim", t["setup_keys"])])
            key = screen.key(1)
            if key in QUIT:
                break
            if key in (b"c", b"C"):
                note = t["copied"] if copy(text) else t["no_clipboard"]


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "bar"
    if cmd == "bar":
        print(bar_text(refresh(BAR_MAX_AGE), time.time(), View(load_config())))
    elif cmd == "refresh":
        print(bar_text(refresh(MANUAL_MIN_AGE), time.time(), View(load_config())))
    elif cmd == "event":
        # a status that leaves "working" means a turn finished (done, idle, blocked on a prompt)
        event = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON") or "{}")
        if event.get("agent_status") not in (None, "working"):
            refresh(EVENT_MIN_AGE)
    elif cmd == "panel":
        panel(View(load_config()))
    elif cmd == "setup":
        setup(View(load_config()))
    elif cmd == "shim":
        write_shim()
    elif cmd == "open":  # open [panel|setup]
        herdr = os.environ.get("HERDR_BIN_PATH") or "herdr"
        entry = sys.argv[2] if len(sys.argv) > 2 else "panel"
        sys.exit(subprocess.run([herdr, "plugin", "pane", "open", "--plugin", PLUGIN_ID,
                                 "--entrypoint", entry]).returncode)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
