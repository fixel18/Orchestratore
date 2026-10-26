"""Catalogo deterministico: nessuna lettura della home o esecuzione CLI."""

CATALOGO = {
    "codex": [{"id": "gpt-6.1-sol", "nome": "Codex", "predefinito": "medium",
               "livelli": ["low", "medium", "high", "xhigh", "max", "ultra"]}],
    "claude": [{"id": "claude-opus-5-5", "nome": "Claude", "predefinito": "medium",
                "livelli": ["low", "medium", "high", "xhigh", "max"]}],
    "antigravity": [],
    "limite_percentuale_supportato": {"codex": True, "claude": True, "antigravity": True},
}
