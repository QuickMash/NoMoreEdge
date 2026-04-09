import ctypes
import os
import subprocess
import sys
import webbrowser
import urllib.parse

# Defaults for browser URLs; CLI flags override these
DEFAULT_BROWSER_HOME_TOKEN = "__DEFAULT_BROWSER_HOME__"
DEFAULT_HOME_URL = DEFAULT_BROWSER_HOME_TOKEN
DEFAULT_SEARCH_URL = "https://www.google.com/search?q={query}"
WEATHER_HOME_URL = "https://weather.com"
WEATHER_LOCATION_URL = "https://weather.com/search/enhancedlocalsearch?where={location}"

# Parse command-line flags to override defaults
def parse_cli_overrides(raw_args: list[str]) -> tuple[dict[str, str], list[str]]:
    overrides = {
        "home_url": DEFAULT_HOME_URL,
        "search_url": DEFAULT_SEARCH_URL,
        "weather_home_url": WEATHER_HOME_URL,
        "weather_location_url": WEATHER_LOCATION_URL,
    }

    value_flags = {
        "--home-url": "home_url",
        "--search-url": "search_url",
        "--weather-home-url": "weather_home_url",
        "--weather-location-url": "weather_location_url",
    }

    force_flags = {
        "--force-default-search": ("search_url", "https://www.google.com/search?q={query}"),
        "--force-default-home": ("home_url", DEFAULT_BROWSER_HOME_TOKEN),
        "--force-default-weather-home": ("weather_home_url", "https://weather.com"),
        "--force-default-weather-location": (
            "weather_location_url",
            "https://weather.com/search/enhancedlocalsearch?where={location}",
        ),
    }

    remaining = []
    i = 0
    while i < len(raw_args):
        arg = raw_args[i]

        if arg in force_flags:
            key, value = force_flags[arg]
            overrides[key] = value
            i += 1
            continue

        if arg in value_flags:
            key = value_flags[arg]
            if i + 1 < len(raw_args):
                overrides[key] = raw_args[i + 1]
                i += 2
                continue

        remaining.append(arg)
        i += 1

    return overrides, remaining

# Hide the Windows console window
def hide_console_window() -> None:
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass

# Decode URL-encoded strings safely
def safe_unquote(value: str, max_rounds: int = 3) -> str:
    text = value
    for _ in range(max_rounds):
        updated = urllib.parse.unquote(text)
        if updated == text:
            break
        text = updated
    return text

# Remove Edge-specific prefixes from launcher URLs
def strip_edge_prefix(raw_value: str) -> str:
    value = raw_value.strip()
    lowered = value.lower()
    prefixes = (
        "microsoft-edge:",
        "msedge:",
        "edge:",
        "microsoftedge:",
        "mse:",
        "microsoft-edge-http:",
        "microsoft-edge-https:",
        "edge-http:",
        "edge-https:",
    )
    for prefix in prefixes:
        if lowered.startswith(prefix):
            value = value[len(prefix):]
            break
    return safe_unquote(value)

# Clean up input from Edge launcher or shell
def sanitize_proxy_input(raw_value: str) -> str:
    value = raw_value.strip()
    while value.startswith("--"):
        value = value[2:].lstrip()
    return strip_edge_prefix(value)

# Check if string is a full URL
def looks_like_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    return bool(parsed.scheme and parsed.netloc)

# Check if string looks like a domain
def looks_like_domain(value: str) -> bool:
    if " " in value:
        return False
    candidate = value.split("/", 1)[0]
    return "." in candidate and not candidate.startswith(".")

# Normalize domain or Edge-prefixed input into a full URL
def normalize_target(value: str) -> str:
    target = strip_edge_prefix(value)
    if not target:
        return ""
    if not looks_like_url(target) and (target.startswith("www.") or looks_like_domain(target)):
        return f"https://{target}"
    return target

# Handle weather-specific searches
def weather_redirect(query: str, weather_home_url: str, weather_location_url: str) -> str:
    clean_query = query.strip()
    lower = clean_query.lower()
    if not clean_query or lower in {"weather", "w"}:
        return weather_home_url
    location = clean_query
    for prefix in ("weather", "w"):
        if lower.startswith(prefix):
            location = clean_query[len(prefix):]
            break
    location = location.strip(" :,-")
    if not location:
        return weather_home_url
    encoded = urllib.parse.quote(location)
    return weather_location_url.format(location=encoded)

# Format a Google search URL
def google_search_url(query: str, search_url: str) -> str:
    encoded = urllib.parse.quote_plus(query)
    return search_url.format(query=encoded)

# Detect Google webhp URLs
def is_google_webhp_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    return host.endswith("google.com") and (path.startswith("/webhp") or path == "/")

# Redirect webhp URLs to proper search URL
def handle_google_webhp_url(url: str, search_url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    query_values = urllib.parse.parse_qs(parsed.query)
    q = (
        query_values.get("q", [""])[0]
        or query_values.get("query", [""])[0]
        or query_values.get("p", [""])[0]
    )
    return google_search_url(q or "", search_url)

# Extract search query from raw payload
def extract_query_payload(target: str, search_url: str) -> str | None:
    text = target.strip()
    if not text:
        return None
    if text.startswith("?"):
        text = text[1:]
    if "=" not in text:
        return None
    parsed = urllib.parse.parse_qs(text, keep_blank_values=False)
    if not parsed:
        return None
    for key in ("query", "q", "p", "search", "searchTerm", "text", "term"):
        value = parsed.get(key, [""])[0].strip()
        if value:
            return google_search_url(value, search_url)
    for key in ("url", "u", "target", "redirect", "redirect_url"):
        value = parsed.get(key, [""])[0].strip()
        if not value:
            continue
        decoded = safe_unquote(value)
        if is_google_webhp_url(decoded):
            return handle_google_webhp_url(decoded, search_url)
        if looks_like_url(decoded):
            return decoded
        if looks_like_domain(decoded):
            return f"https://{decoded}"
    return None

# Extract URLs from redirect wrappers
def extract_embedded_url_or_query(target: str, search_url: str) -> str | None:
    parsed = urllib.parse.urlparse(target)
    qs = urllib.parse.parse_qs(parsed.query)
    for key in ("q", "query", "p", "text"):
        candidate = qs.get(key, [""])[0].strip()
        if candidate:
            return google_search_url(candidate, search_url)
    for key in ("url", "u", "uri", "target", "dest", "destination", "redirect", "redirect_url"):
        candidate = qs.get(key, [""])[0].strip()
        if not candidate:
            continue
        decoded = safe_unquote(candidate)
        if is_google_webhp_url(decoded):
            return handle_google_webhp_url(decoded, search_url)
        if looks_like_url(decoded):
            return decoded
        if looks_like_domain(decoded):
            return f"https://{decoded}"
    return None

# Extract query from Bing URL
def extract_bing_query(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if "bing.com" not in parsed.netloc.lower():
        return None
    query_values = urllib.parse.parse_qs(parsed.query)
    return query_values.get("q", [None])[0]

# Extract search intent from URL
def extract_search_query_from_url(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    query_values = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
    for key in ("q", "query", "p", "search", "searchterm", "text", "term"):
        value = query_values.get(key, [""])[0].strip()
        if value:
            return value
    return None

# Remove tracking parameters from URL
def strip_all_url_tracking(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", "")
    )

# Finalize destination URL
def finalize_destination(candidate: str, search_url: str) -> str:
    cleaned = sanitize_proxy_input(candidate)
    if not cleaned:
        return google_search_url("", search_url)
    if looks_like_domain(cleaned) and not looks_like_url(cleaned):
        cleaned = f"https://{cleaned}"
    if looks_like_url(cleaned):
        extracted_query = extract_search_query_from_url(cleaned)
        if extracted_query:
            return google_search_url(extracted_query, search_url)
        if is_google_webhp_url(cleaned):
            return handle_google_webhp_url(cleaned, search_url)
        bing_query = extract_bing_query(cleaned)
        if bing_query:
            return google_search_url(bing_query, search_url)
        extracted = extract_embedded_url_or_query(cleaned, search_url)
        if extracted and extracted != cleaned:
            return finalize_destination(extracted, search_url)
        return strip_all_url_tracking(cleaned)
    return google_search_url(cleaned, search_url)

# Transform launcher input into final URL
def transform_target(value: str, settings: dict[str, str]) -> str:
    home_url = settings["home_url"]
    search_url = settings["search_url"]
    cleaned = sanitize_proxy_input(value)
    if not cleaned:
        return home_url
    payload_result = extract_query_payload(cleaned, search_url)
    if payload_result:
        return finalize_destination(payload_result, search_url)
    target = normalize_target(cleaned)
    return finalize_destination(target, search_url)

# Open URL with default browser
def launch_default_browser(url: str) -> None:
    if os.name == "nt":
        try:
            os.startfile(url)
            return
        except Exception:
            pass
    try:
        webbrowser.open_new_tab(url)
        return
    except Exception:
        pass
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        subprocess.Popen(
            ["cmd", "/c", "start", "", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
        )

# Launch browser home page
def launch_default_browser_home() -> None:
    if os.name == "nt":
        try:
            creationflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
            subprocess.Popen(
                ["cmd", "/c", "start", ""],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
                close_fds=True,
            )
            return
        except Exception:
            pass
    launch_default_browser("about:blank")

# Launch resolved destination URL
def launch_destination(destination: str) -> None:
    if destination == DEFAULT_BROWSER_HOME_TOKEN:
        launch_default_browser_home()
        return
    launch_default_browser(destination)

# Main entry point
def main() -> None:
    hide_console_window()
    settings, positional = parse_cli_overrides(sys.argv[1:])
    if not positional:
        launch_destination(settings["home_url"])
        return
    raw_input = " ".join(positional).strip()
    if not raw_input:
        launch_destination(settings["home_url"])
        return
    try:
        target = transform_target(raw_input, settings)
    except Exception:
        target = google_search_url(raw_input, settings["search_url"])
    launch_destination(target or settings["home_url"])

if __name__ == "__main__":
    main()
