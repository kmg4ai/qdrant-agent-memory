"""Kto zapisuje do Qdrant — wspolne dla obu sciezek.

Dwa pola, bo to dwie rozne informacje i mieszanie ich gubi jedna z nich:

  agent — KTO wywolal: `claude`, `opencode`, `hermes`, `cron`, `manual`
  via   — JAK: `store` (agent zapisal wprost) albo `ingest` (skrypt wciagnal pliki)

Wczesniej ingest ustawial `agent="ingest"`, co mowilo tylko "zrobil to skrypt"
i gubilo autora — a ingest NIE chodzi z timera ani crona, wiec autorem jest
zawsze ktos konkretny i jest on poznawalny ze srodowiska.

QDRANT_AGENT nadpisuje autora: wrapper, ktory wie lepiej niz srodowisko,
powinien moc to powiedziec wprost.
"""
import os

_AGENT_MARKERS = (
    ("CLAUDE_CODE_SESSION_ID", "claude"),
    ("CLAUDE_CODE_ENTRYPOINT", "claude"),
    ("CLAUDE_CODE_EXECPATH", "claude"),
    ("OPENCODE_CONFIG", "opencode"),
    ("OPENCODE_BIN", "opencode"),
    ("OPENCODE", "opencode"),
    ("HERMES_HOME", "hermes"),
    ("HERMES", "hermes"),
    ("CURSOR_TRACE_ID", "cursor"),
    ("CODEX_HOME", "codex"),
    ("AIDER_MODEL", "aider"),
    ("CLINE_DIR", "cline"),
    ("WINDSURF_HOME", "windsurf"),
    ("GEMINI_CLI_HOME", "gemini"),
    ("GOOSE_HOME", "goose"),
    ("CRUSH_HOME", "crush"),
    ("AMP_HOME", "amp"),
)


def detect_agent() -> str:
    """Kto wywoluje: QDRANT_AGENT, potem markery srodowiskowe, potem `manual`.

    Nazwy sa krotkie, ale NIE inicjaly (`claude`, nie `CC` — to tez kompilator C).
    Zgadzaja sie z katalogami w `agents/`, wiec mapa agent→integracja jest jedna.

    Miejsca to nie oszczedza: `CC` vs `ClaudeCode` to ~8 B na wpis, ~27 KB na
    calej kolekcji — pol procenta tego, co zajmuja same wektory.
    """
    override = (os.environ.get("QDRANT_AGENT") or "").strip()
    if override:
        return override
    for var, name in _AGENT_MARKERS:
        if os.environ.get(var):
            return name
    return "manual"
