"""Keep full evidence outside the model context, addressed by opaque IDs."""
from pathlib import Path
import json
import re
import uuid
from .config import default_output_dir


def folder():
    return Path(default_output_dir()) / "evidence_store"


def save(card):
    evidence_id = uuid.uuid4().hex
    target = folder()
    target.mkdir(parents=True, exist_ok=True)
    path = target / (evidence_id + ".json")
    path.write_text(json.dumps(card, ensure_ascii=False), encoding="utf8")
    result = {key: value for key, value in card.items() if key != "html"}
    result["evidence_id"] = evidence_id
    result["evidence_url"] = "/api/evidence?id=" + evidence_id
    result["html_included"] = False
    return result


def read(evidence_id):
    if not isinstance(evidence_id, str) or not re.fullmatch(r"[a-f0-9]{32}", evidence_id):
        return None
    path = folder() / (evidence_id + ".json")
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf8"))
