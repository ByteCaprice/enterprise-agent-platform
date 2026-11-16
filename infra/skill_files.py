"""Import one explicit Markdown Skill into an ordinary versioned text capability."""

from pathlib import Path

import yaml

from domain.capability import CapabilitySpec, CapabilityVersionDraft, SkillDefinition
from domain.capability_bundle import BundleCapability, CapabilityBundle

MAX_SKILL_FILE_BYTES = 128_000


class SkillImportError(ValueError):
    pass


def load_skill_bundle(path: str | Path, *, key: str | None = None, version: str = "1") -> CapabilityBundle:
    """Read only SKILL.md (or the supplied .md file), never scripts or sibling assets.

    Frontmatter name/description become capability metadata; its other fields do
    not install code, change tools, or confer permissions. The body is guidance.
    """
    try:
        source = Path(path)
        if source.is_dir():
            source /= "SKILL.md"
        if source.suffix.lower() != ".md":
            raise ValueError
        with source.open("rb") as stream:
            content = stream.read(MAX_SKILL_FILE_BYTES + 1)
        if len(content) > MAX_SKILL_FILE_BYTES:
            raise ValueError
        lines = content.decode("utf-8-sig").splitlines(keepends=True)
        metadata = {}
        if lines and lines[0].strip() == "---":
            end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
            metadata = yaml.safe_load("".join(lines[1:end]))
            if metadata is None:
                metadata = {}
            lines = lines[end + 1:]
        if not isinstance(metadata, dict):
            raise ValueError
        name = metadata.get("name", key)
        description = metadata.get("description", "")
        if not isinstance(name, str) or not isinstance(description, str):
            raise ValueError
        spec = CapabilitySpec(key=key if key is not None else name.strip(), kind="skill",
                              name=name.strip(), description=description.strip())
        definition = SkillDefinition(instructions="".join(lines).strip())
        item = BundleCapability(spec, CapabilityVersionDraft(version=version, definition=definition.model_dump()))
        return CapabilityBundle(spec.key, version, spec.name, (item,), description=spec.description)
    except (OSError, UnicodeError, ValueError, TypeError, StopIteration, yaml.YAMLError):
        # YAML/parser errors can contain private instructions and filesystem paths.
        raise SkillImportError("Invalid Skill Markdown or metadata") from None
