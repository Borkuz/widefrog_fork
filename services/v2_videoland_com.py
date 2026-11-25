import base64
import builtins
import json
import re
import threading
import time
from os.path import join

import browser_cookie3
import requests
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from utils.constants.macros import ERR_MSG, USER_ERROR
from utils.main_service import main_service
from utils.structs import BaseElement, BaseService, CustomException
from utils.tools.args import check_range
from utils.tools.common import get_valid_filename


ALLOWED_HOSTS = {"videoland.com", "www.videoland.com", "v2.videoland.com"}
REQUEST_TIMEOUT = 15

# ---------------------------------------------------------------------------
# Videoland / M6 Bedrock endpoints and identifiers (verified via HAR capture)
# ---------------------------------------------------------------------------
BFF_HOST        = "https://layout.videoland.bedrock.tech"
LAYOUT_API      = BFF_HOST + "/front/v1/{customer}/{platform}/main/{device}/{entity}/layout"
CUSTOMER        = "rtlnl"
PLATFORM        = "m6group_web"
DEFAULT_DEVICE  = "token-web-31"
CLIENT_RELEASE  = "6.55.7"

# DRMtoday Widevine license proxy (confirmed via HAR capture).
# Auth is an HS512 JWT sent in the 'x-dt-auth-token' header, minted by the
# Videoland backend; its payload contains merchant "m6_videoland".
LICENSE_URL = "https://lic.drmtoday.com/license-proxy-widevine/cenc/"
MERCHANT    = "m6_videoland"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:160.0) "
    "Gecko/20100101 Firefox/160.0"
)

# Login via Firefox cookies (same chain the web app runs):
#   glt_<apikey> cookie --accounts.getAccountInfo--> UID + UIDSignature
#   --front-auth getJwt--> Bearer JWT (~30 min)
#   Bearer JWT --drm upfront-token--> x-dt-auth-token for DRMtoday (per clip)
GIGYA_ACCOUNT_INFO = "https://gigya-merge.videoland.com/accounts.getAccountInfo"
GET_JWT_URL = "https://front-auth.videoland.bedrock.tech/v2/platforms/{platform}/getJwt"
PROFILES_URL = "https://users.videoland.bedrock.tech/v2/platforms/{platform}/users/{uid}/profiles"
UPFRONT_TOKEN_URL = (
    "https://drm.videoland.bedrock.tech/v1/customers/{customer}/platforms/{platform}"
    "/services/videoland/users/{uid}/videos/{clip_id}/upfront-token"
)
TOKEN_MARGIN = 120  # refresh the Bearer JWT when it has less than this many seconds left


# ============================ indexing (unchanged) ==========================

class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts = []
        self.metadata = {}
        self.links = []
        self.current_section = None
        self._title_open = False
        self._heading_parts = None
        self._anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self._title_open = True
        elif tag in {"h2", "h3"}:
            self._heading_parts = []
        elif tag == "meta":
            key = attrs.get("property") or attrs.get("name")
            value = attrs.get("content")
            if key and value:
                self.metadata[key.lower()] = value.strip()
        elif tag == "link" and "canonical" in attrs.get("rel", "").lower():
            if attrs.get("href"):
                self.metadata["canonical"] = attrs["href"]
        elif tag == "a":
            self._anchor = {"attrs": attrs, "parts": [], "section": self.current_section}

    def handle_endtag(self, tag):
        if tag == "title":
            self._title_open = False
        elif tag in {"h2", "h3"} and self._heading_parts is not None:
            heading = " ".join(self._heading_parts).strip()
            if heading:
                self.current_section = heading
            self._heading_parts = None
        elif tag == "a" and self._anchor is not None:
            attrs = self._anchor["attrs"]
            text = " ".join(self._anchor["parts"]).strip()
            href = attrs.get("href")
            if href:
                self.links.append({
                    "href": href,
                    "label": attrs.get("aria-label", "").strip() or text,
                    "text": text,
                    "section": self._anchor["section"],
                })
            self._anchor = None

    def handle_data(self, data):
        value = data.strip()
        if not value:
            return
        if self._title_open:
            self.title_parts.append(value)
        if self._heading_parts is not None:
            self._heading_parts.append(value)
        if self._anchor is not None:
            self._anchor["parts"].append(value)


def _validate_url(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError("URL must be an HTTPS Videoland page.")
    return parsed


def _same_site_url(base_url, href):
    resolved = urljoin(base_url, href)
    parsed = urlparse(resolved)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        return None
    return resolved


def _episode_from_link(link, base_url):
    label = link["label"]
    match = re.match(r"^\s*(\d+)\.\s*(.+?)\s*$", label)
    duration_match = re.search(r"\b\d+\s*(?:min|uur)\b", link["text"], re.IGNORECASE)
    section = link["section"] or ""
    if (
        match is None
        or duration_match is None
        or re.match(r"^(?:seizoen|season)\s+\d+", section, re.IGNORECASE) is None
    ):
        return None

    href = _same_site_url(base_url, link["href"])
    if href is None:
        return None

    return {
        "number": int(match.group(1)),
        "title": match.group(2).strip(),
        "duration": duration_match.group(0),
        "section": section,
        "url": href,
        "requires_subscription": urlparse(href).path.startswith("/abonnementen"),
    }


def parse_videoland_page(html, url):
    _validate_url(url)
    parser = _PageParser()
    parser.feed(html)

    raw_title = parser.metadata.get("og:title") or " ".join(parser.title_parts)
    title = re.sub(r"\s*\|\s*Videoland\s*$", "", raw_title, flags=re.IGNORECASE)
    description = parser.metadata.get("description") or parser.metadata.get(
        "og:description"
    )
    image = parser.metadata.get("og:image")
    canonical = parser.metadata.get("canonical")
    if canonical:
        canonical = _same_site_url(url, canonical)

    episodes = []
    seen_episodes = set()
    links = []
    seen_links = set()
    for link in parser.links:
        episode = _episode_from_link(link, url)
        if episode is not None:
            episode_key = (episode["number"], episode["title"].casefold())
            if episode_key not in seen_episodes:
                episodes.append(episode)
                seen_episodes.add(episode_key)

        link_url = _same_site_url(url, link["href"])
        if (
            link_url
            and re.search(r"(?:-p_\d+|-c_\d+)(?:/|$)", urlparse(link_url).path)
            and link_url not in seen_links
        ):
            links.append({"title": link["label"] or link["text"], "url": link_url})
            seen_links.add(link_url)

    return {
        "url": url,
        "canonical_url": canonical or url,
        "title": title or None,
        "description": description,
        "image": image,
        "episodes": episodes,
        "links": links,
    }


def _index_videoland_page(url, timeout=REQUEST_TIMEOUT):
    _validate_url(url)
    request = Request(
        url,
        headers={
            "Accept": "text/html",
            "Accept-Language": "nl,en;q=0.8",
            "User-Agent": "VideolandCatalogIndexer/1.0",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            final_url = response.geturl()
            _validate_url(final_url)
            html = response.read().decode(
                response.headers.get_content_charset() or "utf-8",
                errors="replace",
            )
    except HTTPError as error:
        raise RuntimeError(
            f"Videoland returned HTTP {error.code} for {url}"
        ) from error
    except URLError as error:
        raise RuntimeError(f"Could not fetch Videoland page {url}: {error.reason}") from error

    return parse_videoland_page(html, final_url)


# ============================ DRM / stream helpers =========================

def _jwt_payload(token):
    """Decode the payload of a JWT without verifying its signature."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except (IndexError, ValueError):
        return {}


def _clip_id_from_url(url):
    """'.../schiphol-diamantroof-p_5841/aflevering-1-c_102367' -> 'clip_102367'"""
    match = re.search(r"-c_(\d+)", urlparse(url).path)
    if not match:
        raise CustomException(ERR_MSG.format(
            type=USER_ERROR,
            url=url,
            reason="Could not find a clip id (-c_NNNN) in the URL",
            solution="Use an episode URL of the form "
                     ".../schiphol-diamantroof-p_5841/aflevering-1-c_102367",
        ))
    return f"clip_{match.group(1)}"


def _fetch_layout(token, clip_id, page_url, entity_type="video"):
    """
    Call the Bedrock layout BFF. This is the same request the web player
    makes; its Bearer JWT is short-lived (~30 minutes) and tied to a
    logged-in, subscribed account.
    """
    url = LAYOUT_API.format(
        customer=CUSTOMER,
        platform=PLATFORM,
        device=DEFAULT_DEVICE,
        entity=f"{entity_type}/{clip_id}",
    )
    request = Request(url + "?blockPage=1&nbPages=2", headers={
        "Accept": "*/*",
        "Accept-Language": "nl",
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {token}",
        "X-Customer-Name": CUSTOMER,
        "X-Client-Release": CLIENT_RELEASE,
        "X-Location": page_url,
        "Origin": "https://v2.videoland.com",
        "Referer": "https://v2.videoland.com/",
        "Request-Timeout": "10000",
    })
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except HTTPError as error:
        detail = ""
        if error.code in (401, 403, 498):
            detail = " (token probably expired or not entitled)"
        raise CustomException(ERR_MSG.format(
            type=USER_ERROR,
            url=url,
            reason=f"Layout API returned HTTP {error.code}{detail}",
            solution="Sign into Videoland in Firefox again (or clear an outdated TOKEN in the config file)",
        )) from error


def _program_id_from_url(url):
    match = re.search(r"-p_(\d+)", urlparse(url).path)
    return match.group(1) if match else None


# ================================ auth =====================================

_AUTH_LOCK = threading.Lock()
_AUTH_CACHE = {"token": None, "uid": None, "exp": 0}


def _auth_error(url, reason, solution):
    return CustomException(ERR_MSG.format(type=USER_ERROR, url=url, reason=reason, solution=solution))


def _token_from_firefox():
    """Run the web app's login chain with the Videoland session stored in Firefox."""
    solution = "Sign into https://v2.videoland.com in Firefox (or set TOKEN in the config file)"
    try:
        jar = browser_cookie3.firefox(domain_name="videoland.com")
    except browser_cookie3.BrowserCookieError as error:
        raise _auth_error(GIGYA_ACCOUNT_INFO, f"Could not read Firefox cookies: {error}", solution) from error

    cookies = {c.name: c.value for c in jar}
    glt_name = next((name for name in cookies if name.startswith("glt_")), None)
    if glt_name is None:
        raise _auth_error(GIGYA_ACCOUNT_INFO, "No Videoland login found in Firefox cookies", solution)

    headers = {
        "User-Agent": USER_AGENT,
        "Origin": "https://v2.videoland.com",
        "Referer": "https://v2.videoland.com/",
    }
    try:
        account = requests.post(GIGYA_ACCOUNT_INFO, cookies=jar, headers=headers, timeout=REQUEST_TIMEOUT, data={
            "include": "profile,data",
            "lang": "nl",
            "APIKey": glt_name[len("glt_"):],
            "sdk": "js_latest",
            "login_token": cookies[glt_name],
            "authMode": "cookie",
            "pageURL": "https://v2.videoland.com/",
            "format": "json",
        }).json()
    except (requests.RequestException, ValueError) as error:
        raise _auth_error(GIGYA_ACCOUNT_INFO, f"Login request failed: {error}", solution) from error
    if account.get("errorCode") != 0 or "UID" not in account:
        raise _auth_error(
            GIGYA_ACCOUNT_INFO,
            f"Videoland login was rejected ({account.get('errorCode')}: {account.get('errorMessage')})",
            "Log out and back into https://v2.videoland.com in Firefox",
        )

    jwt_url = GET_JWT_URL.format(platform=PLATFORM)
    jwt_headers = {
        **headers,
        "X-Customer-Name": CUSTOMER,
        "X-Client-Release": CLIENT_RELEASE,
        "x-auth-device-name": "Apple Macintosh - Firefox",
        "x-auth-device-id": cookies.get("deviceId", ""),
        "X-Auth-gigya-uid": account["UID"],
        "X-Auth-gigya-signature": account["UIDSignature"],
        "X-Auth-gigya-signature-timestamp": str(account["signatureTimestamp"]),
    }

    def mint(extra_headers):
        try:
            response = requests.get(jwt_url, timeout=REQUEST_TIMEOUT, headers={**jwt_headers, **extra_headers})
            response.raise_for_status()
            return response.json()["token"]
        except (requests.RequestException, ValueError, KeyError) as error:
            raise _auth_error(jwt_url, f"Could not obtain a Videoland token: {error}", solution) from error

    # Without a profile the layout API answers with a "choose your profile" lock instead of
    # content, so pick the first adult profile and mint a profile-bound token.
    token = mint({})
    profiles_url = PROFILES_URL.format(platform=PLATFORM, uid=account["UID"])
    try:
        response = requests.get(profiles_url, timeout=REQUEST_TIMEOUT, headers={
            **headers, "Authorization": f"Bearer {token}",
        })
        response.raise_for_status()
        profiles = response.json()
    except (requests.RequestException, ValueError) as error:
        raise _auth_error(profiles_url, f"Could not list Videoland profiles: {error}", solution) from error
    profile = next((p for p in profiles if not p.get("is_kid")), profiles[0] if profiles else None)
    if not profile or not profile.get("uid"):
        raise _auth_error(profiles_url, "The Videoland account has no profiles", "Create a profile on v2.videoland.com")
    return mint({"X-Auth-profile-id": profile["uid"]})


def _get_auth():
    """
    Return {"token", "uid"} for a logged-in session. A TOKEN in the config file wins while it is
    still valid; otherwise a fresh one is made from the Firefox session (and cached until expiry).
    """
    with _AUTH_LOCK:
        now = time.time()
        if _AUTH_CACHE["token"] and _AUTH_CACHE["exp"] - now > TOKEN_MARGIN:
            return dict(_AUTH_CACHE)

        token = v2_videoland_com.get_credentials()["TOKEN"]
        payload = _jwt_payload(token) if token else {}
        if not token or payload.get("exp", 0) - now <= TOKEN_MARGIN:
            token = _token_from_firefox()
            payload = _jwt_payload(token)

        _AUTH_CACHE.update(token=token, uid=payload.get("accountid"), exp=payload.get("exp", now + 1800))
        return dict(_AUTH_CACHE)


def _get_dt_token(auth, clip_id):
    """DRMtoday auth token for one clip; a DT_TOKEN in the config file wins."""
    configured = v2_videoland_com.get_credentials()["DT_TOKEN"]
    if configured:
        return configured
    url = UPFRONT_TOKEN_URL.format(customer=CUSTOMER, platform=PLATFORM, uid=auth["uid"], clip_id=clip_id)
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT, headers={
            "Authorization": f"Bearer {auth['token']}",
            "User-Agent": USER_AGENT,
            "Origin": "https://v2.videoland.com",
            "Referer": "https://v2.videoland.com/",
        })
        response.raise_for_status()
        return response.json()["token"]
    except (requests.RequestException, ValueError, KeyError) as error:
        raise _auth_error(
            url, f"Could not obtain a DRM token: {error}",
            "Check that the account has an active subscription for this content",
        ) from error


def _episodes_from_layout(layout, program_id=None):
    """
    Collect episodes from a layout response. Every episode card carries
    action.target.value_layout = {type: "video", id: "clip_N", seo: "aflevering-2",
    parent: {id: "5841", seo: "schiphol-diamantroof"}} and an extraTitle like "2. D-Day".
    """
    episodes = {}

    def add(item_content, target):
        if not target or target.get("type") != "video":
            return
        parent = target.get("parent") or {}
        if program_id and str(parent.get("id")) != str(program_id):
            return
        clip = re.search(r"\d+", str(target.get("id", "")))
        if not clip or not parent.get("seo") or not target.get("seo"):
            return
        extra = (item_content.get("extraTitle") or "").strip()
        match = re.match(r"^\s*(\d+)\.\s*(.+)$", extra)
        clip_num = int(clip.group(0))
        entry = {
            "clip": clip_num,
            "number": int(match.group(1)) if match else None,
            "title": (match.group(2) if match else extra or target["seo"]).strip(),
            "url": f"https://v2.videoland.com/{parent['seo']}-p_{parent['id']}/{target['seo']}-c_{clip_num}",
        }
        # The same clip can appear unnumbered (hero / "continue watching") and numbered
        # (episode list); the numbered card wins.
        if clip_num not in episodes or (episodes[clip_num]["number"] is None and entry["number"] is not None):
            episodes[clip_num] = entry

    for block in layout.get("blocks", []) or []:
        for item in ((block or {}).get("content") or {}).get("items", []) or []:
            content = (item or {}).get("itemContent") or {}
            action = content.get("action") or {}
            add(content, (action.get("target") or {}).get("value_layout"))

    # The clip currently open in the player has no "layout" action; take it from entity/seo.
    entity = layout.get("entity") or {}
    seo = layout.get("seo") or {}
    parent = layout.get("parent") or {}
    if entity.get("type") == "video" and (not program_id or str(parent.get("id")) == str(program_id)):
        clip = re.search(r"\d+", str(entity.get("id", "")))
        code = (entity.get("metadata") or {}).get("code")
        if clip and code and parent.get("seo"):
            clip_num = int(clip.group(0))
            episodes.setdefault(clip_num, {
                "clip": clip_num,
                "number": (seo.get("video") or {}).get("episode"),
                "title": (entity.get("metadata") or {}).get("title") or code,
                "url": f"https://v2.videoland.com/{parent['seo']}-p_{parent['id']}/{code}-c_{clip_num}",
            })

    return sorted(episodes.values(), key=lambda e: (e["number"] is None, e["number"] or 0, e["clip"]))


def _pick_asset(layout):
    """
    Find the best DASH asset in the layout response.

    Preference: software DRM (licenses fine with an L3 CDM; max ~540p on
    Videoland). The 1080p packages use hardware DRM with a different KID
    and normally require an L1 CDM, so swap the preference to "hardware"
    if you have such a device.
    """
    best = None
    for block in layout.get("blocks", []):
        for item in (block.get("content") or {}).get("items", []):
            if item.get("itemType") != "video":
                continue
            for asset in ((item.get("itemContent") or {}).get("video") or {}).get("assets", []):
                if asset.get("format") != "dashcenc":
                    continue
                if asset.get("drm", {}).get("type") == "software":
                    return asset
                if best is None:
                    best = asset
    if best is None:
        raise CustomException(ERR_MSG.format(
            type=USER_ERROR,
            url=layout.get("_url", LAYOUT_API),
            reason="No DASH (dashcenc) asset found in the layout response",
            solution="Verify the TOKEN belongs to a logged-in, subscribed account",
        ))
    return best


def _fetch_mpd(mpd_url):
    """
    Download the DASH manifest. The sr.vod... URL 302-redirects to an
    edge CDN host; we return the FINAL url so that the segment URLs
    (which are absolute-path relative) resolve against the right host.
    """
    request = Request(mpd_url, headers={
        "Accept": "*/*",
        "User-Agent": USER_AGENT,
        "Origin": "https://v2.videoland.com",
        "Referer": "https://v2.videoland.com/",
    })
    with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        final_url = response.geturl()
        mpd_text = response.read().decode("utf-8", errors="replace")
    return final_url, mpd_text


def _extract_widevine_pssh(mpd_text):
    """Pull the Widevine cenc:pssh box (base64) out of the MPD."""
    match = re.search(
        r'<ContentProtection\s+schemeIdUri="urn:uuid:EDEF8BA9-79D6-4ACE-A3C8-27DCD51D21ED"[^>]*>\s*'
        r'<cenc:pssh>([^<]+)</cenc:pssh>',
        mpd_text,
    )
    if not match:
        raise CustomException(ERR_MSG.format(
            type=USER_ERROR,
            url="mpd",
            reason="No Widevine PSSH found in the DASH manifest",
            solution="Check that the asset is actually DRM protected",
        ))
    return match.group(1).strip()


def _list_show(show_url):
    """
    List a show's episodes. Returns (show folder name, {season_index: [episode, ...]}) with each
    season's episodes sorted by number; an episode is {"clip", "number", "title", "url"}.
    """
    page = _index_videoland_page(show_url)
    show_title = get_valid_filename(page["title"] or urlparse(show_url).path.strip("/").split("/")[0])
    program_id = _program_id_from_url(show_url)
    token = _get_auth()["token"]

    # 1) The program layout; 2) fallback: layout of any clip of this program found on the
    #    public page (e.g. the trailer), whose "next videos" block lists the episodes.
    episodes = []
    try:
        episodes = _episodes_from_layout(
            _fetch_layout(token, program_id, show_url, entity_type="program"), program_id
        )
    except CustomException:
        pass
    if not [e for e in episodes if e["number"] is not None]:
        for link in page["links"]:
            match = re.search(rf"-p_{program_id}/.*-c_(\d+)", urlparse(link["url"]).path)
            if not match:
                continue
            episodes = _episodes_from_layout(
                _fetch_layout(token, f"clip_{match.group(1)}", link["url"]), program_id
            )
            if [e for e in episodes if e["number"] is not None]:
                break

    numbered = [e for e in episodes if e["number"] is not None]
    if not numbered:
        raise CustomException(ERR_MSG.format(
            type=USER_ERROR,
            url=show_url,
            reason="No episodes found in the Videoland layout API response",
            solution="Use direct episode URLs (...-c_NNNN)",
        ))

    # Season numbers come from the public page sections ("Seizoen 1"), matched on number + title.
    season_of = {}
    for public in page["episodes"]:
        match = re.search(r"\d+", public["section"])
        season_of[(public["number"], public["title"].casefold())] = int(match.group(0)) if match else 1

    # Extras blocks (promos, trailers) number their own items from 1 as well; when the public
    # page lists the episodes, keep only the API episodes that match one of them.
    matched = [e for e in numbered if (e["number"], e["title"].casefold()) in season_of]
    if matched:
        numbered = matched

    seasons = {}
    seen = set()
    for episode in numbered:
        season_index = season_of.get((episode["number"], episode["title"].casefold()), 1)
        if (season_index, episode["number"]) in seen:
            continue
        seen.add((season_index, episode["number"]))
        seasons.setdefault(season_index, []).append(episode)
    for season_episodes in seasons.values():
        season_episodes.sort(key=lambda e: e["number"])
    return show_title, seasons


# ================================ service ==================================

class v2_videoland_com(BaseService):
    DEMO_URLS = [
        "https://v2.videoland.com/schiphol-diamantroof-p_5841",
        "https://v2.videoland.com/schiphol-diamantroof-p_5841/aflevering-1-c_102367",
    ]

    @staticmethod
    def test_service():
        main_service.run_service(v2_videoland_com)

    @staticmethod
    def credentials_needed():
        # Normally nothing to fill in: tokens are made from the Videoland login in Firefox.
        # TOKEN / DT_TOKEN are optional manual overrides (Bearer token of a
        # layout.videoland.bedrock.tech request / x-dt-auth-token of the drmtoday request).
        return {"FIREFOX_COOKIES": True, "TOKEN": "", "DT_TOKEN": ""}

    @staticmethod
    def get_credentials():
        class_name = v2_videoland_com.__name__.replace("_", ".")
        credentials = builtins.CONFIG.get("SERVICE_CREDENTIALS", {}).get(class_name, {}) or {}
        return {k: (credentials.get(k) or "").strip() for k in ["TOKEN", "DT_TOKEN"]}

    @staticmethod
    def initialize_service():
        return v2_videoland_com

    @staticmethod
    def index_page(url, timeout=REQUEST_TIMEOUT):
        return _index_videoland_page(url, timeout)

    @staticmethod
    def get_collection_elements(collection_url):
        _validate_url(collection_url)
        output_path = join(
            str(builtins.CONFIG["DOWNLOAD_COMMANDS"]["OUTPUT_MEDIA_PATH"]),
            v2_videoland_com.__name__
        )

        # Single episode URL: same <Show>/Season_N/Episode_N_<Title> location as when the whole
        # show is downloaded (like npo_nl); falls back to <Title-slug> if the show can't be listed.
        if re.search(r"-c_\d+", urlparse(collection_url).path):
            clip_id = _clip_id_from_url(collection_url)
            parsed = urlparse(collection_url)
            show_url = f"{parsed.scheme}://{parsed.netloc}/{parsed.path.strip('/').split('/')[0]}"
            try:
                show_title, seasons = _list_show(show_url) if "-p_" in show_url else (None, {})
            except CustomException:
                show_title, seasons = None, {}
            for season_index, episodes in seasons.items():
                for episode in episodes:
                    if f"clip_{episode['clip']}" == clip_id:
                        return [BaseElement(
                            url=collection_url,
                            collection=join(output_path, show_title, f"Season_{season_index}"),
                            element=get_valid_filename(f"Episode_{episode['number']}_{episode['title']}"),
                        )]
            last_segment = parsed.path.strip("/").rsplit("/", 1)[-1]
            return [BaseElement(
                url=collection_url,
                collection=join(output_path, show_title) if show_title else output_path,
                element=get_valid_filename(last_segment.split("-c_")[0]),
            )]

        collection_title, seasons = _list_show(collection_url)
        collection_path = join(output_path, collection_title)

        collection = []
        for season_index in sorted(seasons.keys()):
            check = check_range(True, season_index, None)
            if check is True:
                continue
            elif check is False:
                return collection

            for episode in seasons[season_index]:
                episode_index = episode["number"]
                check = check_range(False, season_index, episode_index)
                if check is True:
                    continue
                elif check is False:
                    return collection

                collection.append(BaseElement(
                    url=episode["url"],
                    collection=join(collection_path, f"Season_{season_index}"),
                    element=get_valid_filename(f"Episode_{episode_index}_{episode['title']}"),
                ))
        return collection

    @staticmethod
    def get_video_data(source_element):
        url = source_element.url
        _validate_url(url)

        auth = _get_auth()
        clip_id = _clip_id_from_url(url)
        layout = _fetch_layout(auth["token"], clip_id, url)
        layout["_url"] = url
        asset = _pick_asset(layout)

        mpd_url, mpd_text = _fetch_mpd(asset["path"])
        pssh = _extract_widevine_pssh(mpd_text)

        return mpd_url, pssh, {
            "license_url": LICENSE_URL,
            "dt_token": _get_dt_token(auth, clip_id),
        }

    @staticmethod
    def get_keys(challenge, additional):
        dt_token = additional.get("dt_token")
        if not dt_token:
            raise CustomException(ERR_MSG.format(
                type=USER_ERROR,
                url=LICENSE_URL,
                reason="Missing DRMtoday auth token (DT_TOKEN)",
                solution="Set SERVICE_CREDENTIALS -> v2.videoland.com -> DT_TOKEN in the config file "
                         "(x-dt-auth-token header of the license-proxy-widevine request)",
            ))

        request = Request(additional["license_url"], data=challenge, method="POST", headers={
            "Content-Type": "application/octet-stream",
            "Accept": "*/*",
            "User-Agent": USER_AGENT,
            "x-dt-auth-token": dt_token,
            "Origin": "https://v2.videoland.com",
            "Referer": "https://v2.videoland.com/",
        })
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                body = response.read()
        except HTTPError as error:
            raise CustomException(ERR_MSG.format(
                type=USER_ERROR,
                url=LICENSE_URL,
                reason=f"License request failed with HTTP {error.code}",
                solution="Clear an outdated DT_TOKEN in the config file, or check the account subscription",
            )) from error

        # DRMtoday answers with JSON {"status":"OK","license":"<base64>"}; fall back to raw bytes.
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return body

        if "license" not in payload:
            raise CustomException(ERR_MSG.format(
                type=USER_ERROR,
                url=LICENSE_URL,
                reason=f"DRMtoday returned status {payload.get('status')!r}",
                solution="Clear an outdated DT_TOKEN in the config file, or check the account subscription",
            ))
        return payload["license"]
