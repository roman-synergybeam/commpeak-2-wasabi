"""Recorded announcements, learned by repetition.

A carrier's voicemail greeting, a "the number you have dialled is busy", the
organisation's own phone menu: each is the same sentence, word for word, in
hundreds of unrelated calls. Measured on the first 30,000 transcripts, the top
of the list was InterMagnum's menu greeting (414 calls), Claro's voicemail
(184), "después del tono grabe su mensaje" (177) and a Brazilian "caixa
postal" prompt (127). A conversation never repeats like that.

``transcribe_phrases`` counts, per organisation, how many recordings each
normalised sentence of five or more words has appeared in. Past a threshold it
is treated as a recording, not a person, and a call with too little speech
besides it is skipped. Per organisation, under the same row-level security as
everything else: one company's call content is not evidence about another's.
"""

from __future__ import annotations

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE transcribe_phrases (
            brand_id     BIGINT      NOT NULL REFERENCES brands (id),
            phrase_hash  CHAR(16)    NOT NULL,
            sample       TEXT        NOT NULL,
            calls        INTEGER     NOT NULL DEFAULT 0,
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (brand_id, phrase_hash)
        )
        """
    )
    op.execute("ALTER TABLE transcribe_phrases ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE transcribe_phrases FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY transcribe_phrases_brand_isolation ON transcribe_phrases
            USING (
                brand_id = NULLIF(current_setting('c2w.brand_id', true), '')::bigint
            )
            WITH CHECK (
                brand_id = NULLIF(current_setting('c2w.brand_id', true), '')::bigint
            )
        """
    )
    op.execute(
        "CREATE POLICY transcribe_phrases_platform ON transcribe_phrases "
        "TO c2w_platform USING (true)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS transcribe_phrases")
