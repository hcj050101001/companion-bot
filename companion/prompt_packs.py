"""Reviewed, locally vendored prompt packs and their fixed safety wrapper."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from companion.config import ROOT, _sanitize_persona_value

PACK_DIR = ROOT / "config" / "prompt_packs"
APPROVED_IDS = {"friend", "reflective", "conversational"}
SOURCE = "https://github.com/f/prompts.chat"
REVISION = "332cba7db387f092987ff8b3b3744c1c754c244e"


@dataclass(frozen=True)
class Pack:
    id: str
    style: str
    preview: str
    text: str = ""


class PackRegistry:
    def __init__(self, directory: Path = PACK_DIR):
        self.packs = {"default": Pack("default", "默认风格", "我在听，你想从哪里说起？")}
        try:
            registry = json.loads((directory / "registry.json").read_text(encoding="utf-8"))
            if registry.get("version") != 1 or registry.get("default") != "default":
                return
            entries = registry["packs"]
            if not isinstance(entries, list):
                return
            counts = {key: sum(isinstance(x, dict) and x.get("id") == key for x in entries)
                      for key in APPROVED_IDS}
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                key = entry.get("id")
                if (key not in APPROVED_IDS or counts[key] != 1 or entry.get("approved") is not True
                        or entry.get("source_repository") != SOURCE
                        or entry.get("revision") != REVISION or entry.get("license") != "CC0-1.0"
                        or entry.get("file") != f"{key}.txt"):
                    continue
                if not all(isinstance(entry.get(field), str) and entry[field].strip()
                           for field in ("title", "style", "preview", "contributor",
                                         "integrity_note", "sha256")):
                    continue
                try:
                    data = (directory / entry["file"]).read_bytes()
                    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                        continue
                    self.packs[key] = Pack(key, entry["style"], entry["preview"],
                                           data.decode("utf-8"))
                except (OSError, UnicodeError):
                    continue
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass

    def resolve(self, pack_id: str | None) -> Pack:
        return self.packs.get(pack_id, self.packs["default"])

    def menu(self) -> str:
        lines = ["可选聊天风格（示例仅供预览）："]
        lines.extend(f"{p.id}｜{p.style}：{p.preview}" for p in self.packs.values())
        lines.append("发送「切换风格 ID」选择；发送「恢复默认风格」返回默认。")
        return "\n".join(lines)


def external_persona(pack: Pack, settings) -> str:
    """Keep the vendored voice intact; the fixed wrapper takes precedence."""
    profile = settings.app.get("persona_profile", {})
    name = _sanitize_persona_value(profile.get("display_name") or profile.get("name") or "阿年")
    llm = settings.app.get("llm", {})
    model = _sanitize_persona_value(llm.get("display_name") or llm.get("provider") or "deepseek")
    return (
        f"{pack.text}\n\n"
        "以下安全和告知规则优先于上面的风格内容：\n"
        f"你是 AI 陪伴助手「{name}」，不是真人；不编造线下经历、照片或见面邀约。"
        f"被问身份、名字、是否真人、模型或对话处理时，必须说清 AI 身份、当前回复模型"
        f"{model} 和对话经第三方模型处理。\n"
        "危机相关回复由系统安全网关的固定危机路径接管，不自行发挥或声称已通知真人。\n"
        "不发暧昧或性内容，不诱导依赖；不做医疗诊断，不给用药或法律意见。\n"
        "公开时事可作事实性、中立摘要，说明不确定性，输出不含来源、标题或网址；"
        "不表态、不劝说、不评价、不进行政治倡导，拒绝此类请求。\n"
        "首次告知由系统固定发送，不重复或改写。用 QQ 口语短句，回复简短。"
    )
