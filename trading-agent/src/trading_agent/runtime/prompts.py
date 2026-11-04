import hashlib
from pathlib import Path

ROLES = ("parent", "1h", "4h", "6h", "12h", "review")


def load_prompts(root: Path | None = None, *, mode="structural"):
    if root is None:
        packaged = Path(__file__).resolve().parents[1] / "resources" / "prompts"
        root = packaged if packaged.exists() else Path(__file__).resolve().parents[3] / "prompts"
    if mode != "structural":
        raise ValueError("旧预测流程已归档；仅支持 structural")
    root = root / "structural"
    return {role: (root / f"{role}.md").read_text(encoding="utf-8") for role in ROLES}


def prompt_hashes(prompts):
    if set(prompts) != set(ROLES):
        raise ValueError("All six versioned prompts are required")
    return {role: hashlib.sha256(text.encode()).hexdigest() for role, text in prompts.items()}
