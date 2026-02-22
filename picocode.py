#!/usr/bin/env python3
"""picocode - minimal and feature complete openai-compatible coding assistant derived from nanocode"""

import atexit, concurrent.futures, glob as globlib, html as html_lib, json, os, re, ssl, subprocess, sys, threading, time, urllib.parse, urllib.request, urllib.error

# Try to import readline (may not be available on all systems)
try:
    import readline

    _has_readline = True
except ImportError:
    _has_readline = False

# Load .env file if present
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

OPENAI_KEY = os.environ.get("OPENAI_API_KEY")
API_URL = os.environ.get("OPENAI_API_URL", "https://api.openai.com/v1/chat/completions")
MODEL = os.environ.get("MODEL", "gpt-4o")
RPM_LIMIT = int(os.environ.get("RPM_LIMIT", "40"))

# ANSI colors
RESET, BOLD, DIM, ITALIC, STRIKETHROUGH, UNDERLINE = (
    "\033[0m",
    "\033[1m",
    "\033[2m",
    "\033[3m",
    "\033[9m",
    "\033[4m",
)
BLUE, CYAN, GREEN, YELLOW, RED, MAGENTA = (
    "\033[34m",
    "\033[36m",
    "\033[32m",
    "\033[33m",
    "\033[31m",
    "\033[35m",
)

_print_lock = threading.Lock()
_in_subagent = threading.local()
_subagent_display: dict[int, dict[str, str]] = {}
_subagent_display_lines = 0
_next_subagent_id = 0
_SPIN = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_spin_frame = 0
_throbber_stop = threading.Event()
_throbber_thread = None


def _next_sid():
    global _next_subagent_id
    sid = _next_subagent_id
    _next_subagent_id += 1
    return sid


def _redraw_subagents():
    """Redraw subagent status block in-place. Must hold _print_lock."""
    global _subagent_display_lines
    if _subagent_display_lines > 0:
        sys.stdout.write(f"\033[{_subagent_display_lines}A\033[J")
    sys.stdout.write("\n")
    lines = 1
    for info in _subagent_display.values():
        done = info["action"].startswith("✓")
        prefix = f"{GREEN}✓{RESET}" if done else f"{CYAN}{_SPIN[_spin_frame]}{RESET}"
        sys.stdout.write(f"  {prefix} {MAGENTA}{info['name']}{RESET}\n")
        sys.stdout.write(f"    {DIM}↳ {info['action']}{RESET}\n")
        lines += 2
    sys.stdout.flush()
    _subagent_display_lines = lines


def _subagent_throbber_loop():
    global _spin_frame
    while not _throbber_stop.wait(0.08):
        _spin_frame = (_spin_frame + 1) % len(_SPIN)
        with _print_lock:
            if _subagent_display:
                _redraw_subagents()


def _start_subagent_throbber():
    global _throbber_thread
    if _throbber_thread and _throbber_thread.is_alive():
        return
    _throbber_stop.clear()
    _throbber_thread = threading.Thread(target=_subagent_throbber_loop, daemon=True)
    _throbber_thread.start()


def _stop_subagent_throbber():
    _throbber_stop.set()
    if _throbber_thread:
        _throbber_thread.join()


def _finalize_subagent_display():
    """Freeze subagent display as static output and reset tracking."""
    global _subagent_display_lines
    _stop_subagent_throbber()
    with _print_lock:
        _subagent_display.clear()
        _subagent_display_lines = 0


# --- Rate limiter ---

_rpm_timestamps: list[float] = []
_rpm_lock = threading.Lock()


def _rpm_wait():
    """Block until we're under RPM_LIMIT requests in the last 60s."""
    while True:
        now = time.monotonic()
        with _rpm_lock:
            _rpm_timestamps[:] = [t for t in _rpm_timestamps if now - t < 60]
            if len(_rpm_timestamps) < RPM_LIMIT:
                _rpm_timestamps.append(now)
                return
            wait = 60 - (now - _rpm_timestamps[0])
        is_sub = getattr(_in_subagent, "active", False)
        if not is_sub:
            with _print_lock:
                print(
                    f"  {DIM}⏳ rate limit ({RPM_LIMIT} rpm), waiting {wait:.1f}s...{RESET}",
                    flush=True,
                )
        time.sleep(wait)


def _rpm_backoff(retry_after=None):
    """Called when we get a 429 — freeze the window so all threads back off."""
    wait = retry_after or 10.0
    with _rpm_lock:
        # fill the window so every thread hitting _rpm_wait blocks too
        now = time.monotonic()
        _rpm_timestamps[:] = [now] * RPM_LIMIT
    is_sub = getattr(_in_subagent, "active", False)
    if not is_sub:
        with _print_lock:
            print(
                f"  {DIM}⏳ 429 from API, backing off {wait:.1f}s...{RESET}", flush=True
            )
    time.sleep(wait)


# --- Tool implementations ---


def read(args):
    lines = open(args["path"]).readlines()
    offset = args.get("offset", 0)
    limit = args.get("limit", len(lines))
    selected = lines[offset : offset + limit]
    return "".join(f"{offset + idx + 1:4}| {line}" for idx, line in enumerate(selected))


def write(args):
    with open(args["path"], "w") as f:
        f.write(args["content"])
    return "ok"


def edit(args):
    text = open(args["path"]).read()
    old, new = args["old"], args["new"]
    if old not in text:
        return "error: old_string not found"
    count = text.count(old)
    if not args.get("all") and count > 1:
        return f"error: old_string appears {count} times, must be unique (use all=true)"
    replacement = (
        text.replace(old, new) if args.get("all") else text.replace(old, new, 1)
    )
    with open(args["path"], "w") as f:
        f.write(replacement)
    return "ok"


def glob(args):
    pattern = (args.get("path", ".") + "/" + args["pat"]).replace("//", "/")
    files = globlib.glob(pattern, recursive=True)
    files = sorted(
        files,
        key=lambda f: os.path.getmtime(f) if os.path.isfile(f) else 0,
        reverse=True,
    )
    return "\n".join(files) or "none"


def grep(args):
    pattern = re.compile(args["pat"])
    hits = []
    for filepath in globlib.glob(args.get("path", ".") + "/**", recursive=True):
        try:
            for line_num, line in enumerate(open(filepath), 1):
                if pattern.search(line):
                    hits.append(f"{filepath}:{line_num}:{line.rstrip()}")
        except Exception:
            pass
    return "\n".join(hits[:50]) or "none"


def search(args):
    query = args.get("query", "")
    if not query:
        return "[]"

    url = f"https://lite.duckduckgo.com/lite/?q={urllib.parse.quote(query)}"
    req_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    req = urllib.request.Request(url, headers=req_headers)

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            html_text = response.read().decode("utf-8")
    except Exception:
        return "[]"

    # Parse results - look for result-link pattern
    results = []
    result_pattern = re.compile(
        r"<a rel=\"nofollow\" href=\"([^\"]+)\" class=.result-link.>([^<]+)</a>"
    )

    for match in result_pattern.finditer(html_text):
        # Extract URL from redirect
        redirect_url = match.group(1)
        parsed_qs = urllib.parse.parse_qs(urllib.parse.urlparse(redirect_url).query)
        actual_url = parsed_qs.get("uddg", [redirect_url])[0]

        title = html_lib.unescape(match.group(2).strip())

        # Extract source from URL
        parsed_url = urllib.parse.urlparse(actual_url)
        source = parsed_url.netloc

        results.append(
            {"url": actual_url, "title": title, "description": "", "source": source}
        )

        if len(results) >= 10:
            break

    # Extract descriptions from result-snippet cells
    snippet_pattern = re.compile(r"<td class='result-snippet'>(.*?)</td>", re.DOTALL)
    snippets = snippet_pattern.findall(html_text)

    for i, r in enumerate(results):
        if i < len(snippets):
            desc = html_lib.unescape(snippets[i])
            desc = re.sub(r"<b>([^<]+)</b>", r"\1", desc)  # remove bold tags
            r["description"] = desc.strip()[:200]

    return json.dumps(results)


def fetch(args):
    url = args.get("url", "")
    if not url:
        return "error: url required"

    # Ensure URL has scheme
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    req_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=15, context=ctx) as response:
            content = response.read().decode("utf-8", errors="ignore")
    except Exception as e:
        return f"error: failed to fetch: {e}"

    # If HTML, strip tags and extract text
    if "<html" in content.lower() or "<!doctype" in content.lower():
        # Remove script and style elements
        content = re.sub(
            r"<script[^>]*>.*?</script>", "", content, flags=re.DOTALL | re.IGNORECASE
        )
        content = re.sub(
            r"<style[^>]*>.*?</style>", "", content, flags=re.DOTALL | re.IGNORECASE
        )
        # Remove HTML tags
        content = re.sub(r"<[^>]+>", " ", content)
        # Decode HTML entities
        content = html_lib.unescape(content)
        # Clean whitespace
        content = re.sub(r"\s+", " ", content).strip()

    # Limit output size
    max_chars = args.get("limit", 8000)
    if len(content) > max_chars:
        content = content[:max_chars] + "\n\n... (truncated)"

    return content


def bash(args):
    proc = subprocess.Popen(
        args["cmd"],
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output_lines = []
    try:
        while True:
            line = proc.stdout.readline()  # type: ignore[union-attr]
            if not line and proc.poll() is not None:
                break
            if line:
                if not getattr(_in_subagent, "active", False):
                    print(f"  {DIM}│ {line.rstrip()}{RESET}", flush=True)
                output_lines.append(line)
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        output_lines.append("\n(timed out after 30s)")
    return "".join(output_lines).strip() or "(empty)"


def subagent(args):
    return _run_subagent(
        args["task"], args["system_prompt"], name=args.get("name", "Subagent")
    )


# --- Tool definitions: (description, schema, function) ---

TOOLS = {
    "read": (
        "Read file with line numbers (file path, not directory)",
        {"path": "string", "offset": "number?", "limit": "number?"},
        read,
    ),
    "write": (
        "Write content to file",
        {"path": "string", "content": "string"},
        write,
    ),
    "edit": (
        "Replace old with new in file (old must be unique unless all=true)",
        {"path": "string", "old": "string", "new": "string", "all": "boolean?"},
        edit,
    ),
    "glob": (
        "Find files by pattern, sorted by mtime",
        {"pat": "string", "path": "string?"},
        glob,
    ),
    "grep": (
        "Search files for regex pattern",
        {"pat": "string", "path": "string?"},
        grep,
    ),
    "bash": (
        "Run shell command",
        {"cmd": "string"},
        bash,
    ),
    "search": (
        "Search the web using DuckDuckGo Lite",
        {"query": "string"},
        search,
    ),
    "fetch": (
        "Fetch webpage content as plain text",
        {"url": "string", "limit": "number?"},
        fetch,
    ),
    "subagent": (
        "Spawn an autonomous subagent with a custom system prompt and full tool access. "
        "Use for complex tasks, parallel workloads, or when specialized behavior is needed. "
        "Craft a detailed system_prompt tailored to the subtask (include cwd, constraints, persona). "
        "Multiple subagents run concurrently when called together. "
        "Give each subagent a short, distinctive name (2-4 words).",
        {"name": "string", "task": "string", "system_prompt": "string"},
        subagent,
    ),
}


def run_tool(name, args):
    try:
        return TOOLS[name][2](args)
    except Exception as err:
        return f"error: {err}"


def make_schema(exclude=()):
    result = []
    for name, (description, params, _fn) in TOOLS.items():
        if name in exclude:
            continue
        properties = {}
        required = []
        for param_name, param_type in params.items():
            is_optional = param_type.endswith("?")
            base_type = param_type.rstrip("?")
            properties[param_name] = {
                "type": "integer" if base_type == "number" else base_type
            }
            if not is_optional:
                required.append(param_name)
        result.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            }
        )
    return result


CONTEXT_LIMIT = 200000
RESERVED_TOKENS = 8192
MAX_INPUT_TOKENS = CONTEXT_LIMIT - RESERVED_TOKENS


def estimate_tokens(text):
    """Rough token estimation: ~4 chars per token"""
    return len(text) // 4


def trim_messages(messages, max_tokens=MAX_INPUT_TOKENS):
    """Trim old messages to stay within context limit, keeping system prompt."""
    if not messages:
        return messages

    # Calculate current total
    total = sum(estimate_tokens(json.dumps(m)) for m in messages)

    if total <= max_tokens:
        return messages

    # Keep system prompt
    system_prompt = (
        messages[0]["content"] if messages[0].get("role") == "system" else ""
    )
    system_msg = [{"role": "system", "content": system_prompt}] if system_prompt else []

    # Keep recent messages (working backwards)
    recent = []
    for msg in reversed(messages[1:]):
        total += estimate_tokens(json.dumps(msg))
        if total > max_tokens:
            break
        recent.insert(0, msg)

    return system_msg + recent


class Throbber:
    """Animated spinner with cycling whimsical status messages."""

    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    MUSINGS = [
        "Thinking...",
        "Pondering...",
        "Consulting...",
        "Rummaging...",
        "Manifolding...",
        "Perturbing...",
        "Decoding...",
        "Reticulating...",
        "Descending...",
        "Communing...",
        "Unfolding...",
        "Attending...",
        "Vibing...",
        "Fermenting...",
        "Hallucinating...",
        "Spelunking...",
        "Confabulating...",
        "Extrapolating...",
    ]

    def __init__(self):
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self):
        import random

        i = 0
        msg_idx = 0
        last_switch = time.monotonic()
        msgs = self.MUSINGS[:]
        random.shuffle(msgs)
        reveal = len(msgs[0])  # fully revealed initially
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_switch > 3.0:
                msg_idx = (msg_idx + 1) % len(msgs)
                last_switch = now
                reveal = 0  # start typewriter for new message
            frame = self.FRAMES[i % len(self.FRAMES)]
            msg = msgs[msg_idx]
            if reveal < len(msg):
                reveal = min(reveal + 1, len(msg))
                shown = msg[:reveal]
            else:
                shown = msg
            sys.stdout.write(f"\r\033[2K{CYAN}{frame}{RESET} {DIM}{shown}{RESET}")
            sys.stdout.flush()
            i += 1
            self._stop.wait(0.04 if reveal < len(msg) else 0.08)
        sys.stdout.write("\r\033[2K")
        sys.stdout.flush()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join()


def _parse_retry_after(e):
    """Extract retry delay from a 429 response."""
    retry_after = None
    try:
        retry_after = float(e.headers.get("Retry-After", ""))
    except (ValueError, TypeError, AttributeError):
        pass
    return retry_after


def call_api_stream(messages, tools):
    """Stream API response, printing text tokens live. Returns assembled message dict."""
    payload = {
        "model": MODEL,
        "max_tokens": 8192,
        "messages": messages,
        "tools": tools,
        "stream": True,
    }
    for attempt in range(5):
        _rpm_wait()
        request = urllib.request.Request(
            API_URL,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {OPENAI_KEY}",
            },
        )
        throbber = Throbber()
        throbber.start()
        try:
            response = urllib.request.urlopen(request)
            break
        except urllib.error.HTTPError as e:
            throbber.stop()
            if e.code == 429:
                _rpm_backoff(_parse_retry_after(e))
                continue
            error_body = e.read().decode()
            raise Exception(f"API error {e.code}: {error_body}")
    else:
        raise Exception("API error 429: rate limited after 5 retries")

    text_parts = []
    tool_calls_by_idx = {}
    started_text = False
    first_token = True
    renderer = MarkdownRenderer()

    for raw_line in response:
        line = raw_line.decode("utf-8", errors="ignore").strip()
        if not line.startswith("data: "):
            continue
        data = line[6:]
        if data == "[DONE]":
            break
        chunk = json.loads(data)
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta", {})

        # stop throbber on first meaningful delta
        if first_token and (delta.get("content") or delta.get("tool_calls")):
            throbber.stop()
            first_token = False

        # stream text content
        if delta.get("content"):
            if not started_text:
                sys.stdout.write(f"\n{CYAN}⏺{RESET} ")
                sys.stdout.flush()
                started_text = True
            renderer.feed(delta["content"])
            text_parts.append(delta["content"])

        # accumulate tool calls
        for tc in delta.get("tool_calls") or []:
            idx = tc["index"]
            if idx not in tool_calls_by_idx:
                tool_calls_by_idx[idx] = {
                    "id": tc.get("id", ""),
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                }
            entry = tool_calls_by_idx[idx]
            if tc.get("id"):
                entry["id"] = tc["id"]
            fn = tc.get("function", {})
            if fn.get("name"):
                entry["function"]["name"] += fn["name"]
            if fn.get("arguments"):
                entry["function"]["arguments"] += fn["arguments"]

    if first_token:
        throbber.stop()
    if started_text:
        renderer.flush()

    # assemble final message
    message = {}
    if text_parts:
        message["content"] = "".join(text_parts)
    if tool_calls_by_idx:
        message["tool_calls"] = [
            tool_calls_by_idx[i] for i in sorted(tool_calls_by_idx)
        ]
    return message


MAX_SUBAGENT_DEPTH = 3


def call_api_sync(messages, tools):
    """Non-streaming API call for subagents."""
    payload = {"model": MODEL, "max_tokens": 8192, "messages": messages, "tools": tools}
    for attempt in range(5):
        _rpm_wait()
        req = urllib.request.Request(
            API_URL,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {OPENAI_KEY}",
            },
        )
        try:
            resp = urllib.request.urlopen(req)
            return json.loads(resp.read().decode())["choices"][0]["message"]
        except urllib.error.HTTPError as e:
            if e.code == 429:
                _rpm_backoff(_parse_retry_after(e))
                continue
            error_body = e.read().decode()
            raise Exception(f"API error {e.code}: {error_body}")
    raise Exception("API error 429: rate limited after 5 retries")


def _run_subagent(task, system_prompt, depth=0, name="Subagent"):
    """Run a full agentic loop with a custom system prompt."""
    sid = _next_sid()
    _in_subagent.active = True

    _start_subagent_throbber()
    with _print_lock:
        _subagent_display[sid] = {"name": name, "action": "spawning..."}
        _redraw_subagents()

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]
    exclude = ["subagent"] if depth >= MAX_SUBAGENT_DEPTH else []
    tools = make_schema(exclude=exclude)
    text_content = ""

    try:
        for _ in range(30):
            with _print_lock:
                _subagent_display[sid]["action"] = "thinking..."
                _redraw_subagents()

            messages = trim_messages(messages)
            message = call_api_sync(messages, tools)
            text_content = message.get("content") or ""
            tool_calls = message.get("tool_calls") or []

            assistant_msg = {"role": "assistant"}
            if text_content:
                assistant_msg["content"] = text_content
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls  # type: ignore[assignment]
            messages.append(assistant_msg)

            if not tool_calls:
                break

            for call in tool_calls:
                fn_name = call["function"]["name"]
                fn_args = call["function"]["arguments"]
                fn_args = fn_args if isinstance(fn_args, dict) else json.loads(fn_args)
                arg_preview = str(list(fn_args.values())[0])[:50] if fn_args else ""

                with _print_lock:
                    _subagent_display[sid]["action"] = f"{fn_name}({arg_preview})"
                    _redraw_subagents()

                if fn_name == "subagent":
                    result = _run_subagent(
                        fn_args["task"], fn_args["system_prompt"], depth + 1
                    )
                else:
                    result = run_tool(fn_name, fn_args)

                messages.append(
                    {
                        "tool_call_id": call["id"],
                        "role": "tool",
                        "name": fn_name,
                        "content": result,
                    }
                )
    finally:
        _in_subagent.active = False

    summary = (text_content[:60] + "...") if len(text_content) > 60 else text_content
    with _print_lock:
        _subagent_display[sid]["action"] = f"✓ {summary or 'done'}"
        _redraw_subagents()

    return text_content or "(no response)"


def separator():
    return f"{DIM}{'─' * min(os.get_terminal_size().columns, 80)}{RESET}"


def _format_inline(text):
    """Apply inline markdown formatting: code, bold, italic, strikethrough, links."""
    # inline code first (protect contents from further formatting)
    parts = re.split(r"(`[^`]+`)", text)
    result = []
    for part in parts:
        if part.startswith("`") and part.endswith("`"):
            result.append(f"{YELLOW}{part[1:-1]}{RESET}")
        else:
            p = part
            p = re.sub(r"\*\*(.+?)\*\*", f"{BOLD}\\1{RESET}", p)
            p = re.sub(r"__(.+?)__", f"{BOLD}\\1{RESET}", p)
            p = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", f"{ITALIC}\\1{RESET}", p)
            p = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", f"{ITALIC}\\1{RESET}", p)
            p = re.sub(r"~~(.+?)~~", f"{STRIKETHROUGH}\\1{RESET}", p)
            p = re.sub(
                r"\[([^\]]+)\]\(([^)]+)\)",
                f"{UNDERLINE}\\1{RESET} {DIM}(\\2){RESET}",
                p,
            )
            result.append(p)
    return "".join(result)


class MarkdownRenderer:
    """Line-buffered streaming GFM renderer for terminal output."""

    def __init__(self):
        self.in_code_block = False
        self.buffer = ""

    def feed(self, text):
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self._render_line(line)

    def flush(self):
        if self.buffer:
            if self.in_code_block:
                sys.stdout.write(f"  {DIM}│{RESET} {GREEN}{self.buffer}{RESET}")
            else:
                sys.stdout.write(_format_inline(self.buffer))
            self.buffer = ""
            sys.stdout.flush()

    def _render_line(self, line):
        stripped = line.strip()

        # code fence
        if stripped.startswith("```"):
            if not self.in_code_block:
                self.in_code_block = True
                lang = stripped[3:].strip()
                label = f" {lang} " if lang else "─"
                sys.stdout.write(
                    f"  {DIM}┌─{label}{'─' * max(0, 38 - len(label))}┐{RESET}\n"
                )
            else:
                self.in_code_block = False
                sys.stdout.write(f"  {DIM}└{'─' * 40}┘{RESET}\n")
            sys.stdout.flush()
            return

        if self.in_code_block:
            sys.stdout.write(f"  {DIM}│{RESET} {GREEN}{line}{RESET}\n")
            sys.stdout.flush()
            return

        # headers
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            text = _format_inline(m.group(2))
            sys.stdout.write(
                f"{BOLD}{CYAN}{text}{RESET}\n"
                if len(m.group(1)) <= 2
                else f"{BOLD}{text}{RESET}\n"
            )
            sys.stdout.flush()
            return

        # horizontal rule
        if re.match(r"^(-{3,}|\*{3,}|_{3,})\s*$", stripped):
            cols = min(os.get_terminal_size().columns, 60)
            sys.stdout.write(f"  {DIM}{'─' * cols}{RESET}\n")
            sys.stdout.flush()
            return

        # blockquote
        if stripped.startswith("> "):
            sys.stdout.write(
                f"  {DIM}▌{RESET} {DIM}{_format_inline(stripped[2:])}{RESET}\n"
            )
            sys.stdout.flush()
            return

        # task list
        task = re.match(r"^(\s*)([-*+])\s+\[([ xX])\]\s+(.*)", line)
        if task:
            check = f"{GREEN}✓{RESET}" if task.group(3) in "xX" else f"{DIM}○{RESET}"
            sys.stdout.write(
                f"{task.group(1)}  {check} {_format_inline(task.group(4))}\n"
            )
            sys.stdout.flush()
            return

        # unordered list
        ul = re.match(r"^(\s*)([-*+])\s+(.*)", line)
        if ul:
            sys.stdout.write(f"{ul.group(1)}  • {_format_inline(ul.group(3))}\n")
            sys.stdout.flush()
            return

        # ordered list
        ol = re.match(r"^(\s*)(\d+)[.)]\s+(.*)", line)
        if ol:
            sys.stdout.write(
                f"{ol.group(1)}  {DIM}{ol.group(2)}.{RESET} {_format_inline(ol.group(3))}\n"
            )
            sys.stdout.flush()
            return

        # regular line
        sys.stdout.write(_format_inline(line) + "\n")
        sys.stdout.flush()


def _exec_tool_call(call):
    """Execute a single tool call with thread-safe output. Returns tool result message."""
    tool_name = call["function"]["name"]
    args_data = call["function"]["arguments"]
    tool_args = args_data if isinstance(args_data, dict) else json.loads(args_data)

    if tool_name == "subagent":
        result = run_tool(tool_name, tool_args)
        return {
            "tool_call_id": call["id"],
            "role": "tool",
            "name": tool_name,
            "content": result,
        }

    arg_preview = str(list(tool_args.values())[0])[:50] if tool_args else ""
    with _print_lock:
        print(f"\n{GREEN}⏺ {tool_name.capitalize()}{RESET}({DIM}{arg_preview}{RESET})")

    result = run_tool(tool_name, tool_args)

    result_lines = result.split("\n")
    preview = result_lines[0][:60]
    if len(result_lines) > 1:
        preview += f" ... +{len(result_lines) - 1} lines"
    elif len(result_lines[0]) > 60:
        preview += "..."
    with _print_lock:
        print(f"  {DIM}⎿  {preview}{RESET}")

    return {
        "tool_call_id": call["id"],
        "role": "tool",
        "name": tool_name,
        "content": result,
    }


def main():
    # Prompt history
    history_file = os.path.join(os.path.expanduser("~"), ".picocode_history")
    _history: list[str] = []

    if _has_readline:
        try:
            readline.read_history_file(history_file)  # type: ignore[union-attr]
        except FileNotFoundError:
            pass
        readline.set_history_length(1000)  # type: ignore[union-attr]
        atexit.register(readline.write_history_file, history_file)  # type: ignore[union-attr]
    else:
        # Simple file-based history for systems without readline
        if os.path.exists(history_file):
            try:
                with open(history_file) as f:
                    _history = [line.rstrip("\n") for line in f if line.strip()]
            except Exception:
                pass

        def _save_history():
            try:
                with open(history_file, "w") as f:
                    for line in _history[-1000:]:
                        f.write(line + "\n")
            except Exception:
                pass

        atexit.register(_save_history)

    print(f"{BOLD}picocode{RESET} | {DIM}{MODEL} (OpenAI) | {os.getcwd()}{RESET}\n")
    messages = [
        {"role": "system", "content": f"Concise coding assistant. cwd: {os.getcwd()}"}
    ]

    while True:
        try:
            print(separator())
            user_input = input(f"{BOLD}{BLUE}❯{RESET} ").strip()
            print(separator())
            if not user_input:
                continue
            if user_input in ("/q", "exit"):
                break
            if user_input == "/c":
                messages = [
                    {
                        "role": "system",
                        "content": f"Concise coding assistant. cwd: {os.getcwd()}",
                    }
                ]
                print(f"{GREEN}⏺ Cleared conversation{RESET}")
                continue

            messages.append({"role": "user", "content": user_input})

            # agentic loop: keep calling API until no more tool calls
            while True:
                messages = trim_messages(messages)
                tools = make_schema()
                message = call_api_stream(messages, tools)
                text_content = message.get("content") or ""
                tool_calls = message.get("tool_calls") or []

                # Execute tool calls in parallel
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    tool_results = list(pool.map(_exec_tool_call, tool_calls))
                _finalize_subagent_display()

                # Add assistant message with tool calls
                assistant_msg = {"role": "assistant"}
                if text_content:
                    assistant_msg["content"] = text_content
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls  # type: ignore[assignment]
                messages.append(assistant_msg)

                if not tool_results:
                    break
                messages.extend(tool_results)

            print()

        except (KeyboardInterrupt, EOFError):
            break
        except Exception as err:
            print(f"{RED}⏺ Error: {err}{RESET}")


if __name__ == "__main__":
    main()
