"""Sadoc's voice.

Sadoc is the sailor of the west sea from the First Age - the one who traded in
the shadow of the mountains while Nori, the Dwarf of Númenóre, built the boats
that carried him. He brings news of things that have drifted in from far away.

The voice is used in three places, and each one gets a different amount of it:

* **Email** - the identity is explicit. Sadoc signs his name, the subject line
  says who is writing, and the wording is his.
* **GitHub comments** - a light touch: a sign-off on a reply.
* **Pull request descriptions** - a single closing line, no name and no mention
  of automation, so the pull request still reads like one a person wrote.
  It can be turned off entirely.

Nothing here ever names the model, the harness, or the fact that the work was
automated - that stays in the local database.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Persona:
    name: str = "Sadoc"
    tagline: str = "news from the western shore"
    subject_prefix: str = "[Sadoc]"
    # Pull request descriptions get a closing line with no name in it.
    pr_flourish: bool = True
    pr_flourishes: list[str] = field(
        default_factory=lambda: [
            "The tide turned; these versions hold.",
            "Sail on the newer water.",
            "Checked against the stars, and steady.",
            "Fair winds; the holds are sound.",
            "Out of the shadow, and into better weather.",
        ]
    )
    # GitHub comments get a sign-off.
    comment_signoff: str = "\n\n— Sadoc"

    def subject(self, text: str) -> str:
        return f"{self.subject_prefix} {text}"

    def signature(self) -> str:
        """The footer of an email."""
        return self.tagline

    def flourish(self, seed: int = 0) -> str:
        """A closing line for a pull request description."""
        if not self.pr_flourish or not self.pr_flourishes:
            return ""
        return self.pr_flourishes[seed % len(self.pr_flourishes)]

    def signoff(self) -> str:
        return self.comment_signoff


DEFAULT = Persona()


def load(config: dict | None) -> Persona:
    """Build a persona from the configuration."""
    if not config:
        return Persona()
    persona = Persona()
    for field_name in ("name", "tagline", "subject_prefix", "comment_signoff"):
        if config.get(field_name):
            setattr(persona, field_name, config[field_name])
    if "pr_flourish" in config:
        persona.pr_flourish = bool(config["pr_flourish"])
    if config.get("pr_flourishes"):
        persona.pr_flourishes = list(config["pr_flourishes"])
    return persona
