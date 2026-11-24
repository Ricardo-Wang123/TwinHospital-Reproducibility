"""Index the streamed native capture in private scratch space for per-run audits."""

from contextlib import closing, contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from .trace_evidence import parent_versions, prepare_capture


class LegacyMutationFormat(ValueError):
    """An older capture uses the ordinary JSON reader."""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def mutation_header(stream):
    # The capture writer emits the small object header followed by one event per line.
    header = stream.readline(65536)
    if not header.endswith(',\n') or stream.readline(32) != '"events": [\n':
        raise LegacyMutationFormat('Legacy native mutation export.')
    result = json.loads(header[:-2] + '}')
    if not isinstance(result, dict) or 'events' in result:
        raise ValueError('Malformed streamed native mutation header.')
    return result


def mutation_rows(stream):
    seen_row = need_row = False
    for line in stream:
        value = line.strip()
        if not value and not seen_row:
            continue
        if value == ']}':
            if need_row or any(trailing.strip() for trailing in stream):
                raise ValueError('Malformed streamed native mutation trailer.')
            return
        if seen_row and not need_row:
            raise ValueError('Missing streamed native mutation delimiter.')
        need_row = value.endswith(',')
        row = json.loads(value.removesuffix(','))
        if not isinstance(row, dict):
            raise ValueError('A native mutation must be a JSON object.')
        seen_row = True
        yield row
    raise ValueError('Incomplete streamed native mutation export.')


def json_lines(path):
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError('A native journal entry must be a JSON object.')
                yield row


def decoded_rows(rows):
    return [json.loads(body) for (body,) in rows]


class Rows:
    def __init__(self, connection, table, count):
        self.connection, self.table, self.count = connection, table, count

    def __len__(self):
        return self.count

    def __iter__(self):
        for (body,) in self.connection.execute(f'SELECT body FROM {self.table} ORDER BY seq'):
            yield json.loads(body)


class Lookup:
    def __init__(self, connection, query, *, parameters=None, decode=decoded_rows):
        self.connection, self.query, self.parameters, self.decode = connection, query, parameters, decode

    def get(self, key, default=None):
        values = key if isinstance(key, tuple) else (key,)
        if self.parameters is not None:
            values = self.parameters(key)
        return self.decode(self.connection.execute(self.query, values)) or default


def lookups(connection):
    return {
        'events_by_request': Lookup(connection, 'SELECT body FROM events WHERE request_key=? ORDER BY seq',
                                    parameters=lambda key: (canonical(key),)),
        'native_by_run': Lookup(connection, 'SELECT j.body FROM intents i JOIN journal j ON j.seq=i.last_seq '
                                'WHERE i.run_id IS ? ORDER BY i.first_seq',
                                decode=lambda rows: {entry.get('request_id'): entry for entry in decoded_rows(rows)}),
        'native_by_action': Lookup(connection, 'SELECT j.body FROM intents i JOIN journal j ON j.seq=i.last_seq '
                                   'WHERE i.run_id IS ? AND i.action_id IS ? ORDER BY i.first_seq'),
        'gateway_by_run': Lookup(connection, 'SELECT body FROM journal WHERE run_id IS ? AND kind IN '
                                 "('gateway_action_intent','gateway_action_outcome') ORDER BY seq"),
        'write_journal_by_run': Lookup(connection, 'SELECT j.body FROM intents i CROSS JOIN journal j '
                                       'ON j.request_key=i.request_key WHERE i.run_id IS ? AND j.kind IN '
                                       "('business_request_intent','business_request_outcome') ORDER BY j.seq"),
        'parents': Lookup(connection, 'SELECT patient_id,encounter_id FROM parents WHERE table_name IS ? AND row_id=?',
                          decode=set),
    }


def populate(connection, metadata, mutation_fields, events, journal):
    # The index is rebuildable. Source hashes are rechecked by the audit driver.
    connection.executescript("""
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        CREATE TABLE events(seq INTEGER PRIMARY KEY, request_key TEXT, body TEXT NOT NULL);
        CREATE INDEX events_request ON events(request_key,seq);
        CREATE TABLE journal(seq INTEGER PRIMARY KEY, kind TEXT, request_key TEXT, run_id TEXT, body TEXT NOT NULL);
        CREATE INDEX journal_request ON journal(request_key,seq);
        CREATE INDEX journal_run ON journal(run_id,seq);
        CREATE TABLE intents(request_key TEXT PRIMARY KEY, first_seq INTEGER, last_seq INTEGER, run_id TEXT, action_id TEXT);
        CREATE INDEX intents_run ON intents(run_id,first_seq);
        CREATE INDEX intents_action ON intents(run_id,action_id,first_seq);
        CREATE TABLE parents(table_name TEXT, row_id TEXT, patient_id TEXT, encounter_id TEXT);
        CREATE INDEX parents_row ON parents(table_name,row_id);
    """)

    def store_event(seq, row, body):
        connection.execute('INSERT INTO events VALUES(?,?,?)', (seq, canonical(row.get('request_window_id')), body))
        connection.executemany('INSERT INTO parents VALUES(?,?,?,?)',
                               ((*key, *version) for key, version in parent_versions(row)))

    def store_journal(seq, row, body):
        kind = row.get('event')
        request_key = canonical(row.get('request_id'))
        scope = row.get('scope') or {}
        run_id, action_id = scope.get('run_id'), scope.get('action_request_id')
        # Scope identifiers in the trace are strings. Avoid SQLite TEXT coercion for malformed values.
        run_id = run_id if isinstance(run_id, str) else None
        action_id = action_id if isinstance(action_id, str) else None
        connection.execute('INSERT INTO journal VALUES(?,?,?,?,?)', (seq, kind, request_key, run_id, body))
        if kind == 'business_request_intent':
            connection.execute('INSERT INTO intents VALUES(?,?,?,?,?) ON CONFLICT(request_key) DO UPDATE SET '
                               'last_seq=excluded.last_seq,run_id=excluded.run_id,action_id=excluded.action_id',
                               (request_key, seq, seq, run_id, action_id))

    fingerprint = sha256()

    def emit(value):
        fingerprint.update(value.encode('utf-8'))

    def array(rows, store):
        count = 0
        emit('[')
        for count, row in enumerate(rows, 1):
            if count > 1:
                emit(',')
            body = canonical(row)
            emit(body)
            store(count, row, body)
        emit(']')
        return count

    # Match the canonical JSON digest of the historical in-memory representation.
    with connection:
        emit('{"journal":')
        journal_count = array(journal, store_journal)
        emit(',"metadata":')
        emit(canonical(metadata))
        emit(',"mutations":{')
        for i, key in enumerate(sorted((*mutation_fields, 'events'))):
            if i:
                emit(',')
            emit(canonical(key) + ':')
            if key == 'events':
                mutation_count = array(events, store_event)
            else:
                emit(canonical(mutation_fields[key]))
        emit('}}')
    return mutation_count, journal_count, fingerprint.hexdigest()


@contextmanager
def indexed_capture(meta_path, mutations_path, journal_path, spec, *, workspace):
    """Yield a capture and lookup index whose scratch files disappear after the audit."""
    with mutations_path.open(encoding='utf-8') as stream:
        mutation_fields = mutation_header(stream)
        metadata = json.loads(meta_path.read_text())
        workspace = Path(workspace)
        workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        with TemporaryDirectory(prefix='.capture-index-', dir=workspace) as scratch:
            with closing(sqlite3.connect(Path(scratch) / 'index.sqlite')) as connection:
                events, journal, fingerprint = populate(connection, metadata, mutation_fields,
                                                        mutation_rows(stream), json_lines(journal_path))
                capture = {'metadata': metadata, 'mutations': {**mutation_fields, 'events': Rows(connection, 'events', events)},
                           'journal': Rows(connection, 'journal', journal)}
                prepared = prepare_capture(capture, spec, disk_indexes=lookups(connection), capture_sha256=fingerprint)
                yield capture, prepared
