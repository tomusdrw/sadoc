"""Sadoc's voice.

Sadoc Burrows is the Trailfinder of the Harfoots in *The Rings of Power*. He
keeps the Star-book - the trails and the signs handed down by the Trailfinders
before him - reads the stars, and leads the caravan east from one seasonal
haven to the next. He is the first to notice that something is wrong in the
sky, long before anyone else thinks to look up.

That is rather the job of a dependency update agent.

Nori, the curious young Harfoot he looks after, is the companion piece to this
one.

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
    tagline: str = "reading the signs, and the stars"
    subject_prefix: str = "[Sadoc]"
    # Pull request descriptions get a closing line with no name in it.
    pr_flourish: bool = True
    pr_flourishes: list[str] = field(
        default_factory=lambda: [
            "The stars look clear; these versions hold.",
            "Another season on the road.",
            "The trail is sound and the holds are steady.",
            "A fair wind out of the Southlands.",
            "Read the signs, and take the newer road.",
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
