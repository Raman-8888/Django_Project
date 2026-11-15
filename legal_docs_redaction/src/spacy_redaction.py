"""
spacy_redaction.py — in-place PII redaction that preserves ALL DOCX formatting.

Strategy
--------
Instead of rebuilding a blank Document() from scratch (which destroys every
style, font, margin, header, footer and table structure), we:

  1. Open the *original* Document object.
  2. Walk every paragraph — both in the main body and inside every table cell.
  3. For each paragraph we:
       a. Concatenate all Run texts to get the paragraph's full text.
       b. Run spaCy NER on that text to find PII spans.
       c. Split the text into (normal | redacted) segments.
       d. Remove the existing <w:r> run elements from the paragraph XML.
       e. Re-insert new runs with the replacement text, copying the original
          run's <w:rPr> (font, bold, italic, size, colour …) so formatting
          is preserved at the character level.
  4. Save the modified Document to a new file — the original is untouched.

This keeps intact:
  • Paragraph styles (Heading 1, Body Text, Normal, …)
  • Per-run character formatting (bold, italic, underline, font, size, colour)
  • Tables and their cell/row formatting
  • Page layout: margins, headers, footers, page numbers, columns
  • Images and other embedded objects (they live outside <w:r> and are untouched)
"""

import copy
import glob
import os
import warnings
from pathlib import Path

import pandas as pd
import spacy
from dateutil import parser
from docx import Document
from name_matching.name_matcher import NameMatcher
from sentinel_datetime import sentinel
from tqdm import tqdm

from utils import split_text_into_chunks, sanitize_str

warnings.simplefilter(action='ignore', category=FutureWarning)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _copy_run_format(target_run, source_run):
    """
    Copy the <w:rPr> element (all run-level formatting) from source to target.
    This preserves bold, italic, underline, font name, font size, colour, etc.
    """
    src_rpr = source_run._r.find(
        '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}rPr'
    )
    if src_rpr is not None:
        # Remove any rPr the new run already has, then deep-copy the source one
        tgt_rpr = target_run._r.find(
            '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}rPr'
        )
        if tgt_rpr is not None:
            target_run._r.remove(tgt_rpr)
        target_run._r.insert(0, copy.deepcopy(src_rpr))


def _get_run_at(pos, run_spans):
    """Return the Run object whose span contains character position *pos*."""
    for start, end, run in run_spans:
        if start <= pos < end:
            return run
    # Fallback: last run (covers trailing text)
    return run_spans[-1][2]


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class DataRedaction:

    def __init__(self, input_dir_path, redacted_output_path):
        self.entity_types_to_mask   = ["DATE", "MONEY", "PERCENT", "QUANTITY", "TIME"]
        self.entity_type_to_replace = ["ORG", "PERSON", "PRODUCT"]
        self.nlp = spacy.load("en_core_web_trf")
        self.input_dir_path = input_dir_path
        self.redacted_output_path = redacted_output_path
        os.makedirs(self.redacted_output_path, exist_ok=True)

        self.number_of_matches = 10
        self.matcher = NameMatcher(
            top_n=self.number_of_matches * 2,
            number_of_matches=self.number_of_matches,
            lowercase=True,
            punctuations=True,
            remove_ascii=True,
            legal_suffixes=True,
            common_words=False,
            verbose=False,
        )
        self.matcher.set_distance_metrics(
            ['discounted_levenshtein', 'SSK', 'fuzzy_wuzzy_token_sort']
        )
        self.matching_threshold = 80
        # Extra entities injected by the Orchestrator (from LLM detector + verifier)
        # Format: [{"text": "...", "label": "PHONE", ...}, ...]
        self.extra_entities = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def redact_all_files_in_folder(self):
        pattern = os.path.join(self.input_dir_path, '**', '*.docx')
        for file_path in tqdm(glob.glob(pattern, recursive=True), desc='Data Redaction'):
            self.redact_one_file(file_path)

    def redact_one_file(self, file_path):
        """
        Redact PII from *file_path* (a .docx) and write two output files:
          - <base>_redacted.docx   — the redacted document (formatting intact)
          - <base>_redaction_map.csv — mapping of original → redacted text

        If self.extra_entities is set (by the Orchestrator), those LLM-detected
        entities are injected into the redaction alongside spaCy's findings.
        """
        doc = Document(file_path)

        # --- Global pass: collect all text to build a cross-document
        #     entity-grouping map (so "John Smith" and "J. Smith" get the
        #     same anonymisation label throughout the whole document).
        all_text = self._extract_full_document_text(doc)
        all_entities = self._ner(all_text)
        try:
            entity_groups = self._group_similar_names(all_entities)
        except Exception:
            entity_groups = {}
        last_group_id = (max(entity_groups.values()) + 1) if entity_groups else 0

        # --- In-place redaction pass ---
        redaction_map = []

        # Body paragraphs
        last_group_id = self._redact_paragraph_list(
            doc.paragraphs, entity_groups, last_group_id, redaction_map
        )

        # Table cell paragraphs
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    last_group_id = self._redact_paragraph_list(
                        cell.paragraphs, entity_groups, last_group_id, redaction_map
                    )

        # --- Persist outputs ---
        output_dir = self._get_and_create_relative_output_dir(file_path)
        base = os.path.splitext(os.path.basename(file_path))[0]

        output_doc_path = os.path.join(output_dir, base + '_redacted.docx')
        doc.save(output_doc_path)

        csv_path = os.path.join(output_dir, base + '_redaction_map.csv')
        pd.DataFrame(redaction_map).to_csv(csv_path, index=False)

    # ------------------------------------------------------------------
    # Private — paragraph-level redaction
    # ------------------------------------------------------------------

    def _redact_paragraph_list(self, paragraphs, entity_groups, last_group_id, redaction_map):
        for para in paragraphs:
            last_group_id = self._redact_paragraph(
                para, entity_groups, last_group_id, redaction_map
            )
        return last_group_id

    def _redact_paragraph(self, para, entity_groups, last_group_id, redaction_map):
        """
        Redact PII inside a single paragraph while keeping every Run's
        character formatting (bold, font, size, colour, …) intact.
        """
        runs = para.runs
        if not runs:
            return last_group_id

        # Build the paragraph's full text and a span map: (start, end, run)
        full_text = ""
        run_spans = []
        for run in runs:
            s = len(full_text)
            full_text += run.text
            run_spans.append((s, s + len(run.text), run))

        if not full_text.strip():
            return last_group_id

        # NER on this paragraph's text
        nlp_doc = self.nlp(full_text)
        entities = sorted(
            [
                e for e in nlp_doc.ents
                if (
                    e.label_ in self.entity_types_to_mask
                    or e.label_ in self.entity_type_to_replace
                )
                and self._validate_entity_text(e)
            ],
            key=lambda e: e.start_char,
        )

        # Merge extra entities (from LLM detector) into the spaCy entity list
        extra = self._get_extra_entities_in(full_text)
        all_entities = self._merge_entities(entities, extra, full_text)

        if not all_entities:
            return last_group_id

        # Build ordered list of text segments: (new_text, source_char_pos)
        # source_char_pos is used to look up which original Run to copy format from.
        segments = []          # list of (new_text, src_pos_for_formatting)
        redacted_text_map = {} # cache: entity key -> replacement string
        prev_end = 0

        for ent in all_entities:
            # Support both spaCy span objects and plain dicts (from LLM detector)
            if isinstance(ent, dict):
                ent_text  = ent["text"]
                ent_label = ent["label"]
                ent_start = ent.get("start", full_text.find(ent_text))
                ent_end   = ent_start + len(ent_text) if ent_start >= 0 else -1
            else:
                ent_text  = ent.text
                ent_label = ent.label_
                ent_start = ent.start_char
                ent_end   = ent.end_char

            if ent_start < prev_end or ent_start < 0:
                continue   # skip overlapping or unlocated entities
            # Non-PII text between previous entity and this one
            if ent_start > prev_end:
                segments.append((
                    sanitize_str(full_text[prev_end:ent_start]),
                    prev_end,
                ))

            # Replacement text for this entity
            cache_key = ent_text.lower().strip() + '\x00' + ent_label
            if cache_key in redacted_text_map:
                repl = redacted_text_map[cache_key]
            else:
                # Build a simple namespace for _redact_entity compatibility
                class _Ent:
                    def __init__(self, text, label_):
                        self.text = text
                        self.label_ = label_
                repl, last_group_id = self._redact_entity(_Ent(ent_text, ent_label), entity_groups, last_group_id)
                redacted_text_map[cache_key] = repl

            segments.append((repl, ent_start))

            redaction_map.append({
                "original_text":  ent_text,
                "original_span":  (ent_start, ent_end),
                "entity_type":    ent_label,
                "redacted_text":  repl,
            })
            prev_end = ent_end

        # Trailing non-PII text
        if prev_end < len(full_text):
            segments.append((sanitize_str(full_text[prev_end:]), prev_end))

        # --- Rebuild the paragraph's runs in-place ---
        # 1. Remove all existing <w:r> elements from the paragraph XML
        p_elem = para._p
        for run in list(para.runs):
            p_elem.remove(run._r)

        # 2. Re-add runs: one per segment, inheriting source-run's formatting
        for new_text, src_pos in segments:
            if not new_text:
                continue
            source_run = _get_run_at(src_pos, run_spans)
            new_run = para.add_run(new_text)
            _copy_run_format(new_run, source_run)

        return last_group_id

    # ------------------------------------------------------------------
    # Private — entity detection & grouping
    # ------------------------------------------------------------------

    def _get_extra_entities_in(self, text: str) -> list:
        """Return extra_entities whose text appears in this paragraph."""
        found = []
        for e in getattr(self, 'extra_entities', []):
            if e.get("text") and e["text"] in text:
                found.append(e)
        return found

    def _merge_entities(self, spacy_ents, extra: list, full_text: str) -> list:
        """Merge spaCy entity spans and dict-style LLM entities into one sorted list."""
        merged = list(spacy_ents)
        covered = {e.text.lower().strip() for e in spacy_ents}
        for e in extra:
            txt = e.get("text", "")
            if txt.lower().strip() not in covered:
                idx = full_text.find(txt)
                if idx >= 0:
                    e = {**e, "start": idx, "end": idx + len(txt)}
                merged.append(e)
                covered.add(txt.lower().strip())

        def sort_key(e):
            return e.start_char if hasattr(e, "start_char") else e.get("start", 99999)

        return sorted(merged, key=sort_key)

    def _extract_full_document_text(self, doc):
        """Collect all readable text from body paragraphs and table cells."""
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    parts.extend(p.text for p in cell.paragraphs)
        return '\n'.join(parts)

    def _ner(self, text):
        """Run spaCy on (possibly chunked) text; return sorted entity list."""
        chunks = split_text_into_chunks(text)
        docs = [self.nlp(chunk) for chunk in chunks]
        combined = spacy.tokens.Doc.from_docs(docs)
        return sorted(combined.ents, key=lambda e: e.start_char)

    def _redact_entity(self, ent, grouped_entities, last_group_id):
        if ent.label_ in self.entity_type_to_replace:
            key = ent.text.lower().strip() + '_' + ent.label_
            group_id = grouped_entities.get(key)
            if group_id is not None:
                redacted_text = ent.label_ + "_" + str(group_id)
            else:
                redacted_text = ent.label_ + "_" + str(last_group_id)
                last_group_id += 1
        else:
            redacted_text = "XXXXX"
        return redacted_text, last_group_id

    def _group_similar_names(self, entities):
        """Group near-duplicate entity names (e.g. "John Smith" ≈ "J. Smith")."""
        group_id = 0
        grouped_entities = {}

        for i in range(len(entities)):
            entity_text      = entities[i].text.lower().strip()
            entity_text_type = entity_text + '_' + entities[i].label_

            if grouped_entities.get(entity_text_type) or (
                entities[i].label_ not in self.entity_type_to_replace
                and entities[i].label_ not in self.entity_types_to_mask
            ):
                continue

            similar = [
                j for j in entities[i + 1:]
                if j.label_ == entities[i].label_
                and entity_text != j.text.lower().strip()
                and j.label_ in self.entity_type_to_replace
            ]
            if not similar:
                continue

            master = pd.DataFrame({"name": [n.text for n in similar], "ent": similar})
            master.drop_duplicates(subset=['name'], inplace=True)
            to_match = pd.DataFrame({"name": [entities[i].text], "ent": [entities[i]]})

            self.matcher.load_and_process_master_data('name', master)
            result = self.matcher.match_names(
                to_be_matched=to_match, column_matching='name'
            ).iloc[0]

            grouped_entities[entity_text_type] = group_id
            for j in range(self.number_of_matches):
                score_key = 'score_' + str(j)
                name_key  = 'match_name_' + str(j)
                if result[score_key] >= self.matching_threshold:
                    mk = result[name_key].lower().strip() + '_' + entities[i].label_
                    grouped_entities[mk] = group_id
            group_id += 1

        return grouped_entities

    def _validate_entity_text(self, entity):
        if entity.label_ == 'DATE':
            try:
                parsed = parser.parse(entity.text, default=sentinel())
                return parsed.has_year and parsed.has_month and parsed.has_day
            except Exception:
                return False
        return True

    def _get_and_create_relative_output_dir(self, input_path):
        relative = Path(input_path).parent.relative_to(self.input_dir_path)
        out      = Path(self.redacted_output_path).joinpath(relative)
        out.mkdir(parents=True, exist_ok=True)
        return str(out)
