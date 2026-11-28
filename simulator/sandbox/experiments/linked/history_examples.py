"""Read the same frozen local MIMIC case index in every assisted condition."""

import json
from pathlib import Path
import re
import sqlite3

from .contracts import digest


FILENAME = 'history.private.sqlite'
TERMS = re.compile(r'\b(?:copd|chronic obstructive pulmonary disease|dyspn(?:ea|oea)|shortness of breath|'
                   r'wheez(?:e|ing)|heart failure|pneumonia|hypoxi[ac]|cough|dysuria|urinary tract infection|'
                   r'pyelonephritis|hyperglyc(?:emia|aemia)|diabetes|hypoglyc(?:emia|aemia)|edema)\b', re.I)


class HistoricalExamples:
    def __init__(self, path):
        self.uri = Path(path).resolve().as_uri() + '?mode=ro&immutable=1'
        with sqlite3.connect(self.uri, uri=True) as db:
            self.metadata = dict(db.execute('SELECT key, value FROM metadata'))
        self.excluded = frozenset(json.loads(self.metadata.pop('excluded_subjects')))
        if len(self.excluded) != 100:
            raise ValueError('The historical index must exclude all formal patients')

    def retrieve(self, visible_parts):
        query = list(dict.fromkeys(term.lower() for term in TERMS.findall(json.dumps(visible_parts, ensure_ascii=False))))[:4]
        if not query:
            return None
        expression = ' OR '.join('"' + term + '"' for term in query)
        with sqlite3.connect(self.uri, uri=True) as db:
            rows = db.execute('SELECT note_id, subject_id, history FROM examples WHERE examples MATCH ? '
                              'ORDER BY bm25(examples), note_id LIMIT 2', (expression,)).fetchall()
        if any(subject in self.excluded for _, subject, _ in rows):
            raise ValueError('The historical case index contains a formal patient')
        return {'version': self.metadata['version'], 'terms': query,
                'usage': 'Other-patient examples for orientation only. Current patient statements, findings and decisions '
                         'must be supported by the current patient data.',
                'other_patients': [{'example_id': digest([self.metadata['version'], note_id])[:24],
                                    'excerpt': 'Prior admission history. ' + history} for note_id, _, history in rows]}
