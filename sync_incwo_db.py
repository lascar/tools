#!/usr/bin/env python3
"""Compare les métadonnées Incwo et applique uniquement des changements conservateurs."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
import datetime
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlencode
from urllib.request import Request, urlopen

URL = 'https://www.incwo.com/webservice/incwo_get_metadata'
SOURCES = {
    'user_passkeys': 'db/daily_migrate/20261006_create_user_passkeys.sql',
    'user_passkey_challenges': 'db/daily_migrate/20261006_create_user_passkeys.sql',
    'user_passkey_rate_limits': 'db/daily_migrate/20261006_create_user_passkeys.sql',
    'wli_prospects': 'app/ai/augmentcode/we-love-incwo/migration.sql',
    'wli_trial_resets': 'app/ai/augmentcode/we-love-incwo/migration.sql',
}
REVIEW_SOURCES = {
    'booking_reservation_details': ['db/daily_migrate/20261016_create_booking_v2.sql',
                                    'db/daily_migrate/20261018_add_booking_audit_columns.sql'],
    'scheduled_run_gates': ['app/ai/augmentcode/2026-10-25-scheduled-run-gates.sql'],
    'task_dependencies': ['db/setup/1.tables.sql'],
    'inventories_items': ['db/daily_migrate/20260812_add_inventory_advanced_features.sql'],
}
TYPE = re.compile(r'(?:tinyint|smallint|mediumint|int|integer|bigint)(?:\(\d+\))?(?: unsigned)?(?: zerofill)?|'
                  r'decimal\(\d+,\d+\)(?: unsigned)?(?: zerofill)?|(?:var)?(?:char|binary)\(\d+\)|'
                  r'(?:tiny|medium|long)?(?:text|blob)|date|datetime|timestamp|time|'
                  r'(?:float|double)(?:\(\d+,\d+\))?(?: unsigned)?', re.I)


class SyncError(RuntimeError):
    pass


def ident(name):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,64}', name):
        raise SyncError('Identifiant SQL non pris en charge : %r' % name)
    return '`' + name + '`'


def sql_type(value):
    if not isinstance(value, str) or not TYPE.fullmatch(value.strip()):
        raise SyncError('Type SQL non pris en charge : %r' % value)
    value = value.strip().lower().replace('integer', 'int')
    value = re.sub(r'\b(tinyint|smallint|mediumint|int|bigint)\(\d+\)', r'\1', value)
    return value


def canonical(line):
    # MySQL omet DEFAULT NULL dans SHOW CREATE pour les colonnes TEXT.
    if re.match(r'`[^`]+` (?:tiny|medium|long)?text\b', line):
        definition, separator, comment = line.partition(' COMMENT ')
        definition = re.sub(r' DEFAULT NULL(?= |$)', '', definition, count=1)
        return definition + separator + comment
    return line


def columns(ddl):
    return {m[1]: m[0].strip().rstrip(',') for m in re.finditer(r'^  `([^`]+)` .+$', ddl, re.M)}


def local_type(definition):
    m = re.match(r'`[^`]+`\s+(' + TYPE.pattern + r')(?=\s|$)', definition, re.I)
    if not m:
        raise SyncError('Définition de colonne non prise en charge : ' + definition)
    return m[1], sql_type(m[1])


def widening(old, new):
    capacities = {'tinytext': 255, 'text': 65535, 'mediumtext': 16777215, 'longtext': 4294967295}
    a, b = re.fullmatch(r'varchar\((\d+)\)', old), re.fullmatch(r'varchar\((\d+)\)', new)
    if a and b:
        return int(b[1]) > int(a[1])
    if old in capacities and new in capacities:
        return capacities[new] > capacities[old]
    # Un VARCHAR compte des caractères ; TEXT compte des octets. Le contrôle
    # final utilise le jeu de caractères réel de la colonne, pas celui du client.
    return bool(a and new in capacities)


def api_fields(table, response):
    if not isinstance(response, dict):
        raise SyncError('Réponse API non structurée pour ' + table)
    meta = response.get('table_metadata', {})
    if not isinstance(meta, dict) or meta.get('table_name') != table or not isinstance(meta.get('fields'), dict):
        raise SyncError('Réponse de métadonnées invalide pour ' + table)
    fields = {}
    for key, item in meta['fields'].items():
        if not isinstance(item, dict):
            raise SyncError('Champ API invalide dans ' + table)
        name = item.get('sql_field_name') or key
        ident(name)
        if name in fields:
            raise SyncError('Champ API dupliqué dans ' + table + ' : ' + name)
        fields[name] = item.get('sql_type')
    return fields


def fetch_json(url, timeout):
    req = Request(url, headers={'User-Agent': 'incwo-local-schema-sync/1.0'})
    with urlopen(req, timeout=timeout) as response:
        if not response.geturl().startswith(URL):
            raise SyncError('Redirection inattendue de l’API Incwo')
        data = response.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024:
            raise SyncError('Réponse API trop volumineuse')
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        # Certaines réponses legacy de cette API sont encore en ISO-8859-1.
        # Aucun remplacement de caractère : un JSON invalide reste bloquant.
        text = data.decode('iso-8859-1')
    return json.loads(text)


def collect_metadata(timeout):
    summary = fetch_json(URL, timeout)
    tables = summary.get('tables_summary')
    if not isinstance(tables, dict) or not tables:
        raise SyncError('Inventaire API absent ou vide')
    names = sorted(tables)
    for table in names:
        ident(table)
    def fetch(table):
        response = fetch_json(URL + '?' + urlencode({'oz': table}), timeout)
        api_fields(table, response)
        return table, response
    # Quatre lectures au maximum ; aucune erreur n'est assimilée à une table vide.
    with ThreadPoolExecutor(max_workers=4) as pool:
        details = dict(pool.map(fetch, names))
    return {'source_url': URL, 'summary': summary, 'api_details': details}


def known_create(root, table, fields):
    """Extrait un CREATE connu, jamais le fichier de migration complet et ses DML."""
    path = SOURCES.get(table)
    if not path or not (root / path).is_file() or not fields or any(not t for t in fields.values()):
        return None
    text = (root / path).read_text()
    pattern = (r'^CREATE TABLE IF NOT EXISTS ' + re.escape(table) +
               r'\s*\((.*?)\) ENGINE=InnoDB DEFAULT CHARSET=utf8;')
    matches = list(re.finditer(pattern, text, re.M | re.S))
    if len(matches) != 1:
        return None
    match = matches[0]
    # Le corps provient d'un DDL connu et borné. Une modification inattendue de
    # cette forme demande une revue, au lieu d'étendre l'exécution de la migration.
    body = match[1]
    if re.search(r'[;#]|--|/\*|\b(?:SELECT|REFERENCES|TRIGGER|AS|DROP|UPDATE|INSERT|DELETE)\b', body, re.I):
        return None
    definitions = {}
    for line in body.splitlines():
        m = re.match(r'^  ([a-z][a-z0-9_]*)\s+(' + TYPE.pattern + r')(?=\s|,|$)', line, re.I)
        if m:
            definitions[m[1]] = sql_type(m[2])
    try:
        wanted = {name: sql_type(t) for name, t in fields.items()}
    except SyncError:
        return None
    if definitions != wanted:
        return None
    return match[0].replace('DEFAULT CHARSET=utf8;', 'DEFAULT CHARSET=utf8mb3;')


def build_plan(schema, metadata, root, encodings):
    plan, manual, untyped = [], [], []
    details = metadata.get('api_details')
    if not isinstance(details, dict) or not details:
        raise SyncError('Détails API absents')
    for table, response in sorted(details.items()):
        ident(table)
        fields = api_fields(table, response)
        if table not in schema:
            create = known_create(root, table, fields)
            if create:
                plan.append({'table': table, 'kind': 'create', 'sql': create, 'source': SOURCES[table]})
            else:
                manual.append({'table': table, 'reason': 'Table absente : DDL fiable indisponible ou champs API incomplets',
                               'api_fields_count': len(fields),
                               'review_sources': [p for p in REVIEW_SOURCES.get(table, []) if (root / p).is_file()]})
            continue
        existing = columns(schema[table])
        for col, target in sorted(fields.items()):
            if not target:
                untyped.append(table + '.' + col)
                continue
            try:
                target = sql_type(target)
            except SyncError:
                manual.append({'table': table, 'column': col, 'reason': 'Type API non pris en charge'})
                continue
            if col not in existing:
                # Les métadonnées ne prouvent ni NOT NULL, ni une valeur par défaut.
                plan.append({'table': table, 'column': col, 'kind': 'add', 'target': target,
                             'sql': 'ALTER TABLE %s ADD COLUMN %s %s NULL DEFAULT NULL;' % (ident(table), ident(col), target)})
                continue
            try:
                original_type, old = local_type(existing[col])
            except SyncError:
                manual.append({'table': table, 'column': col, 'reason': 'Type local non pris en charge'})
                continue
            if old == target:
                continue
            reason = None
            if not widening(old, target):
                reason = 'Conversion non automatique : %s -> %s' % (old, target)
            elif 'GENERATED' in existing[col].upper():
                reason = 'Colonne calculée : revue nécessaire'
            elif old.startswith('varchar(') and target.endswith('text'):
                capacity = {'tinytext': 255, 'text': 65535, 'mediumtext': 16777215, 'longtext': 4294967295}[target]
                maxlen = encodings.get(table + '.' + col)
                if not maxlen or int(old[8:-1]) * maxlen > capacity:
                    reason = 'Capacité texte insuffisante ou encodage inconnu'
                elif re.search(r'\bDEFAULT\s+(?!NULL\b)', existing[col], re.I):
                    reason = 'Valeur par défaut non NULL : conversion en TEXT à revoir'
            if reason:
                manual.append({'table': table, 'column': col, 'reason': reason})
                continue
            # Remplacer le type seul préserve NULL, DEFAULT, COLLATE, COMMENT,
            # visibilité et autres attributs SQL de la définition actuelle.
            new = existing[col].replace(original_type, target, 1)
            plan.append({'table': table, 'column': col, 'kind': 'widen', 'definition': new,
                         'sql': 'ALTER TABLE %s MODIFY COLUMN %s;' % (ident(table), new)})
    return {'operations': plan, 'manual': manual, 'untyped': untyped,
            'local_only_tables': sorted(set(schema) - set(details))}


def command(args, timeout=300, **kwargs):
    result = subprocess.run([str(a) for a in args], timeout=timeout, check=False, **kwargs)
    if result.returncode:
        stderr = result.stderr.decode(errors='replace') if isinstance(result.stderr, bytes) else result.stderr
        raise SyncError('Échec de %s (code %s) : %s' % (args[0], result.returncode, stderr or 'voir le journal'))
    return result


class Database:
    def __init__(self, container, database, timeout=600, tcp=False):
        self.container, self.database, self.timeout, self.tcp = container, database, timeout, tcp
        ident(database)

    def cli(self, binary='mysql'):
        # Aucun secret dans les arguments côté hôte ou les rapports.
        script = 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec ' + binary + ' -uroot "$@"'
        args = ['docker', 'exec', '-i', self.container, 'sh', '-c', script, binary]
        if self.tcp:
            args.append('--host=127.0.0.1')
        else:
            args.extend(['--host=localhost', '--protocol=SOCKET'])
        return args

    def sql(self, query):
        args = self.cli() + ['--connect-timeout=5', '--batch', '--raw', '--skip-column-names', '--binary-mode', '--default-character-set=utf8mb4']
        result = command(args, timeout=self.timeout, input=query.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.stderr:
            raise SyncError('Diagnostic MySQL inattendu : ' + result.stderr.decode(errors='replace'))
        return result.stdout.decode()

    def schema(self):
        names = self.sql("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA='%s' AND TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME;" % self.database).splitlines()
        for table in names:
            ident(table)
        if not names:
            raise SyncError('Base absente ou sans table : ' + self.database)
        output = self.sql('\n'.join('SHOW CREATE TABLE %s.%s;' % (ident(self.database), ident(t)) for t in names))
        # SHOW CREATE ne contient pas de tabulation brute dans les commentaires
        # MySQL, mais peut contenir des retours à la ligne entre les colonnes.
        starts = list(re.finditer(r'^([A-Za-z0-9_]+)\tCREATE TABLE ', output, re.M))
        result = {}
        for index, match in enumerate(starts):
            end = starts[index + 1].start() if index + 1 < len(starts) else len(output)
            result[match[1]] = output[match.start():end].split('\t', 1)[1].rstrip('\n')
        if set(result) != set(names):
            raise SyncError('Inventaire des définitions SQL incomplet')
        return result

    def encodings(self):
        query = ("SELECT c.TABLE_NAME,c.COLUMN_NAME,s.MAXLEN FROM information_schema.COLUMNS c "
                 "JOIN information_schema.CHARACTER_SETS s ON c.CHARACTER_SET_NAME=s.CHARACTER_SET_NAME "
                 "WHERE c.TABLE_SCHEMA='%s';" % self.database)
        return {t + '.' + c: int(n) for t, c, n in (row.split('\t') for row in self.sql(query).splitlines())}

    def apply(self, operations):
        # STRICT_ALL_TABLES transforme en erreurs les troncatures et conversions
        # invalides. Aucune option --force : arrêt au premier échec.
        setup = "USE %s; SET SESSION sql_mode='STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION'; SET SESSION lock_wait_timeout=15;\n" % ident(self.database)
        warnings = []
        allowed = [
            "Warning\t1287\t'utf8mb3' is deprecated and will be removed in a future release. Please use utf8mb4 instead",
            "Warning\t3719\t'utf8' is currently an alias for the character set UTF8MB3, but will be an alias for UTF8MB4 in a future release. Please consider using UTF8MB4 in order to be unambiguous.",
        ]
        for op in operations:
            output = self.sql(setup + op['sql'] + '\nSHOW WARNINGS;')
            for warning in output.splitlines():
                collation_notice = re.fullmatch(r"Warning\t3778\t'utf8(?:mb3)?_[a-z0-9_]+' is a collation of the deprecated character set UTF8MB3\. Please consider using UTF8MB4 with an appropriate collation instead\.", warning)
                if warning not in allowed and not collation_notice:
                    raise SyncError('Avertissement SQL bloquant : ' + warning)
                warnings.append({'table': op['table'], 'warning': warning})
        return warnings

    def fingerprints(self, schema, tables):
        result = {}
        for table in sorted(tables):
            ddl = schema[table]
            fields = list(columns(ddl))
            pk = re.search(r'PRIMARY KEY \(([^)]+)\)', ddl)
            if not pk or not re.fullmatch(r'`\w+`(?:,`\w+`)*', pk[1]):
                raise SyncError('Clé primaire simple requise pour vérifier toutes les valeurs : ' + table)
            parts = ["IF(%s IS NULL,'N',CONCAT('V',HEX(CAST(%s AS BINARY))))" % (ident(c), ident(c)) for c in fields]
            query = "SET time_zone='+00:00'; SELECT SHA2(CONCAT_WS('|',%s),256) FROM %s.%s ORDER BY %s;" % (','.join(parts), ident(self.database), ident(table), pk[1])
            raw = self.sql(query).encode()
            if any(not re.fullmatch(rb'[0-9a-f]{64}', line) for line in raw.splitlines()):
                raise SyncError('Empreinte de ligne invalide dans ' + table)
            result[table] = {'rows': raw.count(b'\n'), 'sha256': hashlib.sha256(raw).hexdigest()}
        return result

    def backup(self, path):
        temporary = path.with_suffix('.partial')
        args = self.cli('mysqldump') + ['--single-transaction', '--quick', '--hex-blob', '--set-gtid-purged=OFF',
                                     '--no-tablespaces', '--routines', '--events', '--triggers', '--default-character-set=utf8mb4', self.database]
        # Un fichier temporaire sur disque évite de bloquer sur un pipe plein et
        # permet un vrai timeout du sous-processus, y compris pendant un gros dump.
        with tempfile.TemporaryFile() as raw:
            result = command(args, timeout=self.timeout, stdout=raw, stderr=subprocess.PIPE)
            if result.stderr:
                raise SyncError('Diagnostic de sauvegarde : ' + result.stderr.decode(errors='replace'))
            raw.seek(0)
            tail = b''
            with gzip.open(temporary, 'wb') as target:
                while True:
                    block = raw.read(1024 * 1024)
                    if not block:
                        break
                    target.write(block)
                    tail = (tail + block)[-4096:]
            if b'Dump completed on' not in tail:
                raise SyncError('Sauvegarde incomplète : ' + str(temporary))
        os.replace(temporary, path)
        # Lire jusqu'au bout contrôle aussi le CRC du fichier gzip.
        with gzip.open(path, 'rb') as stream:
            while stream.read(1024 * 1024):
                pass
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        return {'path': str(path), 'bytes': path.stat().st_size, 'sha256': digest}

    def restore(self, path):
        with gzip.open(path, 'rb') as source, tempfile.TemporaryFile() as raw:
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                raw.write(block)
            raw.seek(0)
            command(self.cli() + ['--binary-mode', self.database], timeout=self.timeout,
                    stdin=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def success_backups(self, directory):
        """Exporte les deux dumps datés de la fiche good_to_know/mysql.html."""
        stamp = datetime.datetime.now().strftime('%Y-%m-%d')
        directory = Path(directory)
        stem = 'dump_' + self.database
        if any((directory / (name + stamp + '.sql')).exists() for name in (stem + '_', stem + '_struct_')):
            stamp += datetime.datetime.now().strftime('_%H%M%S-%f')
        paths = {'data': directory / (stem + '_' + stamp + '.sql'),
                 'structure': directory / (stem + '_struct_' + stamp + '.sql')}
        temporary, published, result = {}, [], {}
        try:
            for kind, path in paths.items():
                fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.partial', dir=directory)
                temporary[kind] = Path(tmp)
                options = ['--no-data'] if kind == 'structure' else ['--single-transaction', '--quick']
                args = self.cli('mysqldump') + options + ['--hex-blob', '--set-gtid-purged=OFF', '--no-tablespaces',
                                                        '--routines', '--events', '--triggers', '--default-character-set=utf8mb4', self.database]
                with os.fdopen(fd, 'w+b') as stream:
                    process = command(args, timeout=self.timeout, stdout=stream, stderr=subprocess.PIPE)
                    if process.stderr:
                        raise SyncError('Diagnostic de sauvegarde après synchronisation : ' + process.stderr.decode(errors='replace'))
                    stream.flush()
                    stream.seek(max(0, stream.tell() - 4096))
                    if b'Dump completed on' not in stream.read():
                        raise SyncError('Dump après synchronisation incomplet : ' + str(path))
                    stream.seek(0)
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                    os.fsync(stream.fileno())
                result[kind] = {'path': str(path), 'bytes': temporary[kind].stat().st_size, 'sha256': digest}
            # Les deux dumps doivent être complets avant leur publication. Un
            # lien dur refuse un fichier existant, même s'il apparaît entre-temps.
            for kind, path in paths.items():
                os.link(temporary[kind], path)
                published.append(path)
            return result
        except BaseException:
            for path in published:
                path.unlink()
            raise
        finally:
            for path in temporary.values():
                path.unlink(missing_ok=True)


def verify_schema(before, after, operations):
    expected = {t: dict(columns(ddl)) for t, ddl in before.items()}
    for op in operations:
        if op['kind'] == 'widen':
            expected[op['table']][op['column']] = op['definition']
        elif op['kind'] == 'add':
            expected[op['table']][op['column']] = '%s %s DEFAULT NULL' % (ident(op['column']), op['target'])
    strip = lambda ddl: re.sub(r'^  `[^`]+` .*\n?', '', ddl, flags=re.M)
    for table, ddl in before.items():
        current = after.get(table)
        if current is None:
            raise SyncError('Table disparue : ' + table)
        actual = {c: canonical(v) for c, v in columns(current).items()}
        if actual != {c: canonical(v) for c, v in expected[table].items()}:
            raise SyncError('Attribut de colonne inattendu : ' + table)
        if strip(ddl) != strip(current):
            raise SyncError('Index, contrainte ou option modifié : ' + table)
    creates = {op['table'] for op in operations if op['kind'] == 'create'}
    if set(after) != set(before) | creates:
        raise SyncError('Inventaire final des tables inattendu')


def docker_inspect(container):
    return json.loads(command(['docker', 'inspect', container], stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout)[0]


@contextlib.contextmanager
def isolated_mysql(live, image):
    name = 'incwo-schema-test-' + uuid.uuid4().hex[:12]
    started = False
    try:
        # Image déjà présente, aucun port ni réseau. Les données restent dans le
        # conteneur jetable ; aucun volume de la base réelle n'est monté.
        started = True
        command(['docker', 'run', '--pull=never', '--detach', '--rm', '--name', name, '--network=none',
                 '-e', 'MYSQL_ALLOW_EMPTY_PASSWORD=yes', image, '--sql-mode=NO_ENGINE_SUBSTITUTION'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        clone = Database(name, live.database, live.timeout, tcp=True)
        for attempt in range(90):
            try:
                clone.sql('SELECT 1;')
                break
            except SyncError:
                if attempt == 89:
                    raise SyncError('La copie MySQL ne démarre pas')
                time.sleep(1)
        # Le serveur temporaire d'initialisation n'écoute pas en TCP : on attend
        # ainsi le serveur final, et pas un serveur qui va redémarrer à l'import.
        clone.sql('CREATE DATABASE %s CHARACTER SET utf8mb3;' % ident(live.database))
        yield clone
    finally:
        if started:
            exists = subprocess.run(['docker', 'inspect', name], timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if exists.returncode == 0:
                command(['docker', 'stop', '--time=10', name], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            elif b'No such object' not in exists.stderr:
                raise SyncError('Impossible de vérifier le nettoyage du conteneur de test : ' + exists.stderr.decode())


@contextlib.contextmanager
def quiescent(live):
    from _wt_docker import app_mounts
    paused, changed = [], False
    original = live.sql('SELECT @@GLOBAL.read_only;').strip()
    if original != '0':
        raise SyncError('Le serveur est déjà en lecture seule ; aucune modification')
    try:
        for service, root, cid, running in app_mounts(running_only=True):
            info = docker_inspect(cid)
            if info['State'].get('Paused'):
                continue
            paused.append(cid)
            command(['docker', 'pause', cid], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        changed = True
        live.sql('SET GLOBAL read_only=ON;')
        if live.sql('SELECT COUNT(*) FROM information_schema.INNODB_TRX;').strip() != '0':
            raise SyncError('Transactions actives : synchronisation annulée')
        # read_only bloque les clients ordinaires. Des clients privilégiés encore
        # connectés pourraient le contourner ; on refuse dans ce cas.
        privileged = live.sql("SELECT COUNT(*) FROM information_schema.PROCESSLIST p WHERE p.ID<>CONNECTION_ID() "
                              "AND p.USER NOT IN ('event_scheduler','system user') AND "
                              "(EXISTS(SELECT 1 FROM mysql.user u WHERE u.User=p.USER AND u.Super_priv='Y') OR "
                              "EXISTS(SELECT 1 FROM mysql.global_grants g WHERE g.USER=p.USER AND g.PRIV='CONNECTION_ADMIN')); ")
        if privileged.strip() != '0':
            raise SyncError('Autre client MySQL privilégié connecté : synchronisation annulée')
        events = live.sql("SELECT COUNT(*) FROM information_schema.EVENTS WHERE STATUS='ENABLED';")
        if events.strip() != '0':
            raise SyncError('Événements MySQL actifs : synchronisation annulée')
        yield
    finally:
        # Une erreur de nettoyage reste visible, mais ne doit pas empêcher de
        # reprendre les autres services, même après Ctrl-C.
        errors = []
        if changed:
            try:
                live.sql('SET GLOBAL read_only=OFF;')
            except Exception as error:
                errors.append(str(error))
        for cid in paused:
            try:
                if docker_inspect(cid)['State'].get('Paused'):
                    command(['docker', 'unpause', cid], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except Exception as error:
                errors.append(str(error))
        if errors:
            raise SyncError('Nettoyage incomplet : ' + '; '.join(errors))


def execute(live, schema, plan, output, report, save):
    operations = plan['operations']
    tables = {op['table'] for op in operations if op['kind'] != 'create'}
    info = docker_inspect(live.container)
    version = live.sql('SELECT VERSION();').strip()
    if not re.fullmatch(r'8\.0\.\d+', version):
        raise SyncError('Application automatique limitée à un MySQL Docker 8.0 local')
    engines = live.sql("SELECT DISTINCT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA='%s' AND TABLE_TYPE='BASE TABLE';" % live.database).splitlines()
    if engines != ['InnoDB']:
        raise SyncError('Sauvegarde cohérente automatique limitée aux tables InnoDB')
    print('Sauvegarde complète et restauration sur un MySQL isolé…', flush=True)
    backup = output / 'avant_test.sql.gz'
    report['test_backup'] = live.backup(backup)
    save()
    with isolated_mysql(live, info['Image']) as clone:
        if clone.sql('SELECT VERSION();').strip() != version:
            raise SyncError('La version du MySQL isolé diffère de la version locale')
        clone.restore(backup)
        before = clone.schema()
        if before != schema:
            raise SyncError('La structure a changé pendant la sauvegarde ; relance le script')
        fingerprints = clone.fingerprints(before, tables)
        report['test_warnings'] = clone.apply(operations)
        after = clone.schema()
        verify_schema(before, after, operations)
        if clone.fingerprints(before, tables) != fingerprints:
            raise SyncError('Le test a modifié des valeurs existantes ; base locale non modifiée')
        report['test'] = {'restored_backup': True, 'data_unchanged': True, 'fingerprints': fingerprints}
        tested_creates = {op['table']: after[op['table']] for op in operations if op['kind'] == 'create'}
        save()
    print('Test validé. Pause des clients locaux, sauvegarde finale puis application…', flush=True)
    with quiescent(live):
        if live.schema() != schema:
            raise SyncError('La structure a changé depuis le test ; base locale non modifiée')
        report['final_backup'] = live.backup(output / 'avant_application.sql.gz')
        fingerprints = live.fingerprints(schema, tables)
        report['before'] = fingerprints
        report['application_started'] = True
        save()
        report['application_warnings'] = live.apply(operations)
        after = live.schema()
        verify_schema(schema, after, operations)
        for table, ddl in tested_creates.items():
            if after.get(table) != ddl:
                raise SyncError('La table créée diffère de celle testée : ' + table)
        report['after'] = live.fingerprints(schema, tables)
        if report['after'] != fingerprints:
            raise SyncError('Les valeurs diffèrent après application ; conserve la sauvegarde et les rapports')
        report['data_unchanged'] = True
        report['schema_after'] = after
        save()


def parser():
    p = argparse.ArgumentParser(prog='sync_incwo_db.sh', description=__doc__)
    p.add_argument('--apply', action='store_true', help='Sauvegarder, tester sur une copie, puis appliquer les changements conservateurs')
    p.add_argument('--container', help='Conteneur MySQL local (sinon service efsql1-lan du profil des outils)')
    p.add_argument('--database', default='facile_building', help='Base locale cible (défaut : facile_building)')
    p.add_argument('--service', default='efsql1-lan', help='Service Compose MySQL')
    p.add_argument('--output', type=Path, help='Nouveau dossier privé de rapports et sauvegardes')
    p.add_argument('--metadata-file', type=Path, help='Métadonnées archivées pour comparaison seulement ; interdit avec --apply')
    p.add_argument('--http-timeout', type=int, default=30, help='Timeout de chaque requête API, en secondes')
    p.add_argument('--command-timeout', type=int, default=900, help='Timeout de chaque commande Docker/MySQL, en secondes')
    return p


def publish_text(path, content):
    """Remplace un rapport avec un fichier privé complet, jamais partiel."""
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def manual_followup(plan):
    """Décrit les contrôles à effectuer ; aucune requête ni conversion exécutée."""
    profiles = [
        ('decimal(20,0) -> decimal(20,6)', 'Montants à six décimales',
         'La précision totale reste 20 : on passe de 20 à 14 chiffres avant la virgule. La nouvelle valeur doit rester strictement comprise entre -10^14 et 10^14.',
         'Mesurer MIN/MAX, compter les valeurs hors de ces bornes et vérifier l’unité des montants. Contrôler les totaux avant/après sur une copie.',
         'Si toutes les valeurs tiennent, préparer la conversion en préservant les attributs SQL. Sinon, choisir une précision totale supérieure avec validation métier ; ne pas tronquer les montants. Cette modification ne reconstitue pas des décimales éventuellement perdues auparavant.'),
        ('decimal(20,0) -> decimal(20,4)', 'Valeurs et scores à quatre décimales',
         'La partie entière passe de 20 à 16 chiffres. Les valeurs doivent rester strictement comprises entre -10^16 et 10^16.',
         'Mesurer MIN/MAX, compter les dépassements, confirmer l’unité des valeurs et comparer les agrégats sur une copie.',
         'Convertir seulement si les valeurs tiennent. Si elles ne tiennent pas, conserver le type et faire valider une précision plus grande.'),
        ('int -> decimal(3,2)', 'Échelle du score de confiance',
         'DECIMAL(3,2) ne contient qu’un chiffre avant la virgule : son intervalle va de -9,99 à 9,99. Ce n’est pas une simple augmentation de précision.',
         'Relever MIN/MAX et les valeurs distinctes. Déterminer si le score est un ratio entre 0 et 1, un pourcentage entre 0 et 100, ou une autre unité.',
         'Si le score actuel est un pourcentage, ne pas convertir directement : une éventuelle division par 100 est une transformation de données à concevoir et valider séparément. Sinon vérifier que chaque valeur entre dans le nouveau type.'),
        ('date -> timestamp', 'Date transformée en date et heure',
         'TIMESTAMP a une plage plus limitée que DATE et applique des conversions de fuseau horaire. Il faut aussi choisir l’heure associée à chaque date.',
         'Relever les dates minimales/maximales, les NULL et dates nulles, puis vérifier les bornes TIMESTAMP de MySQL 8.0, sql_mode et le fuseau des connexions. Définir la signification de minuit.',
         'Tester la conversion et la lecture avec le fuseau de l’application, sans ajout implicite de CURRENT_TIMESTAMP ni ON UPDATE. Ne pas convertir tant que la signification temporelle n’est pas définie.'),
        ('bigint -> varchar(300)', 'Identifiant numérique transformé en texte',
         'La capacité de 300 caractères suffit pour écrire un BIGINT en texte, mais les tris, comparaisons et relations peuvent changer de comportement.',
         'Déterminer si la valeur est une clé interne ou une référence de prestataire. Recenser les jointures, index et lectures applicatives, puis tester leur comportement avec une chaîne.',
         'Confirmer le modèle attendu avant de changer le type. Préserver exactement les valeurs et NULL ; ne pas inventer de nouvelles références ou modifier les données de paiement.'),
        ('int -> bigint', 'Élargissement des identifiants',
         'Cet élargissement augmente la capacité ; il ne réduit pas les valeurs représentables. Le blocage automatique vient des relations à maintenir cohérentes, pas d’une réduction de capacité.',
         'Inventorier clés étrangères explicites et références applicatives, types signés/non signés, index et AUTO_INCREMENT. Les relations sans contrainte SQL doivent aussi être examinées.',
         'Préparer ensemble les clés et leurs références. En particulier, coordonner import_cells.id et import_cells_logs.import_cell_id lorsqu’elles figurent dans le plan. Tester les jointures et les valeurs sur une copie avant application.'),
        ('int -> int unsigned', 'Identifiants rendus non signés',
         'Un entier non signé n’accepte plus de valeur négative. Le signe des colonnes liées doit rester cohérent.',
         'Compter les valeurs négatives, examiner MIN/MAX et la prochaine valeur AUTO_INCREMENT ; vérifier le signe des références et les contraintes.',
         'Convertir seulement si aucune valeur négative n’a à être conservée et si les références sont compatibles. Sinon laisser la définition actuelle et clarifier le besoin.'),
        ('varchar(64) -> char(64)', 'Empreinte SHA-256 à longueur fixe',
         'CHAR et VARCHAR ne traitent pas les espaces terminaux de la même manière. La longueur seule ne suffit pas à prouver que le contenu est une empreinte.',
         'Compter les valeurs non NULL de longueur différente de 64, les espaces terminaux et les valeurs non hexadécimales. Vérifier la collation et les index.',
         'Si toutes les valeurs sont bien des empreintes de 64 caractères, tester CHAR en conservant NULL, collation et index. Sinon garder VARCHAR jusqu’à clarification ; ne pas nettoyer les valeurs automatiquement.'),
        ('bigint -> int', 'Réduction des entiers locaux',
         'Le type local est plus large que celui annoncé par l’API. Le réduire peut exclure des valeurs ; conserver BIGINT peut être une décision acceptable si cette largeur est volontaire.',
         'Pour chaque colonne, mesurer MIN/MAX et compter les valeurs hors de -2147483648 à 2147483647. Pour les clés, vérifier aussi AUTO_INCREMENT, références et contraintes.',
         'Décider d’abord si l’alignement exact est nécessaire. Si non, documenter l’écart accepté. Si oui, ne réduire qu’après contrôle de toutes les valeurs et des relations, puis test sur copie. Un compteur tel que addon_module_counts.count doit aussi être contrôlé, même s’il n’est pas une clé.'),
    ]
    groups = {}
    missing = []
    for item in plan['manual']:
        if 'column' in item:
            key = item['reason'].removeprefix('Conversion non automatique : ')
            groups.setdefault(key, []).append(item)
        else:
            missing.append(item)
    lines = ['', '## Ce qui reste à faire, en détail', '',
             'Les contrôles ci-dessous sont à réaliser. Aucun relevé MIN/MAX, audit de relations ou test de ces conversions manuelles n’a été exécuté par cette comparaison.', '',
             'L’API expose des métadonnées applicatives, pas le DDL complet de production. Un écart de type ou un nom de table ne suffit donc pas, à lui seul, à autoriser une modification.', '']
    covered = set()
    for key, title, risk, checks, action in profiles:
        items = groups.get(key, [])
        if not items:
            continue
        covered.add(key)
        lines += ['### %s — %s colonne(s)' % (title, len(items)), '', '**Écart :** `%s`.' % key, '',
                  '**Colonnes concernées :**', '']
        lines += ['- `%s.%s`' % (item['table'], item['column']) for item in items]
        lines += ['', '**Pourquoi vérifier :** ' + risk, '', '**Contrôles à faire :** ' + checks, '',
                  '**Action attendue :** ' + action, '']
    for key in sorted(set(groups) - covered):
        lines += ['### Autre écart à qualifier', '', key, '']
        lines += ['- `%s.%s`' % (item['table'], item['column']) for item in groups[key]]
        lines += ['', 'Récupérer la définition SQL complète, vérifier les valeurs et préciser la transformation avant de proposer une modification.', '']
    if missing:
        lines += ['### Tables absentes localement : vérification une par une', '',
                  'Un nombre de champs API égal à zéro ne prouve pas qu’une table vide doit être créée. Il peut aussi signaler un modèle inaccessible, ancien ou non présent sur le serveur répondant.', '']
        for item in missing:
            table = item['table']
            lines += ['#### `%s`' % table, '']
            if item.get('api_fields_count') is not None:
                lines += ['**Constat API :** %s champ(s) exposé(s).' % item['api_fields_count'], '']
            if table.startswith('bak_'):
                action = 'Le nom évoque une sauvegarde ponctuelle : confirmer son usage avec l’environnement source. Si elle n’est pas nécessaire au fonctionnement local, classer cet écart comme accepté plutôt que recréer une sauvegarde vide. Sinon obtenir son SHOW CREATE TABLE et définir séparément si ses données sont nécessaires.'
            elif table == 'booking_reservation_details':
                action = 'Comparer le CREATE de la migration V2 avec le schéma actuel attendu et ses évolutions d’audit. Vérifier les tables liées, notamment timesheet_lines et booking_pages, ainsi que l’unicité du lien timesheet_line_id. Préparer uniquement la création de cette table après validation, sans rejouer toute la migration ni modifier les réservations existantes.'
            elif table == 'scheduled_run_gates':
                action = 'Vérifier que les traitements coordonnés par cette table doivent fonctionner sur cette base. La définition candidate utilise gate_key comme clé primaire, sans colonne id. Comparer avec le DDL de l’environnement concerné puis tester le CREATE isolément ; ne pas lancer toute la chaîne de migrations.'
            elif table == 'task_dependencies':
                action = 'Extraire uniquement le CREATE depuis le fichier de setup, qui contient aussi des DROP et ne doit pas être exécuté en entier. Vérifier le type de tasks.id, les deux clés étrangères vers tasks, la contrainte unique et les suppressions en cascade. Créer sur une copie, tester ces contraintes, puis seulement préparer une création locale.'
            elif table == 'inventories_items':
                action = 'La migration trouvée ajoute des colonnes mais ne définit pas la table initiale. Obtenir le CREATE complet et confirmer le nom exact utilisé par l’inventaire ; vérifier les relations aux inventaires, produits et stocks. Ne pas exécuter les ALTER sur une table absente et ne pas fabriquer un CREATE à partir de ces trois ajouts.'
            else:
                action = 'Identifier le composant qui utilise cette table et l’environnement où elle est réellement attendue. Obtenir un SHOW CREATE TABLE ou une migration complète avec index et contraintes, puis recouper ses champs avec une source accessible avant toute création.'
            if item.get('review_sources'):
                lines += ['**Sources locales à examiner :**', '']
                lines += ['- `%s`' % source for source in item['review_sources']]
                lines += ['']
            lines += ['**Ce qu’il faut faire :** ' + action, '']
    lines += ['### Validation avant de solder un écart', '',
              'Pour chaque groupe, consigner soit une correction validée, soit une différence volontairement acceptée, soit une demande d’information au serveur source. Après une correction : sauvegarde, essai sur copie isolée, comparaison des valeurs existantes et vérification des index, contraintes et références. Un simple compteur à zéro ne prouve pas une égalité complète avec la production.', '',
              '### Références MySQL 8.0', '',
              '- [Capacité des entiers](https://dev.mysql.com/doc/refman/8.0/en/integer-types.html)',
              '- [Précision et échelle des décimaux](https://dev.mysql.com/doc/refman/8.0/en/fixed-point-types.html)',
              '- [Types DATE et TIMESTAMP](https://dev.mysql.com/doc/refman/8.0/en/datetime.html)', '']
    return lines


def markdown_summary(report):
    def code(value):
        value = str(value).replace('\n', ' ').replace('\r', ' ')
        delimiter = '`' * (max([len(s) for s in re.findall(r'`+', value)] or [0]) + 1)
        return delimiter + value + delimiter
    statuses = {'applied': 'Changements automatiques appliqués et vérifiés.',
                'no_change': 'Aucun changement automatique applicable.',
                'comparison_only': 'Comparaison uniquement : changements proposés non appliqués.',
                'failed': 'Échec du traitement.'}
    lines = ['# Comparaison et sauvegarde de la base locale', '',
             statuses.get(report.get('status'), 'Traitement en cours.'), '',
             '- Base : ' + code(report['database']),
             '- Structure : ' + ('application demandée' if report['apply'] else 'comparaison seule, aucune modification effectuée'),
             '- Début : ' + code(report['started_at']),
             '- Fin : ' + code(report.get('finished_at', 'en cours')), '']
    plan = report.get('plan')
    if plan is not None:
        ops = plan['operations']
        if plan['manual']:
            column_diffs = sum('column' in item for item in plan['manual'])
            table_diffs = len(plan['manual']) - column_diffs
            lines += ['**La structure reste différente de celle décrite par l’API** : %s différence(s) de colonnes et %s table(s) non créée(s).' %
                      (column_diffs, table_diffs), '',
                      'Le script les conserve pour éviter une conversion risquée ou une création de table sans définition vérifiée.', '']
        if not report['apply'] and ops:
            lines += ['Pour appliquer les %s changements automatiques proposés : `doc/tools/sync_incwo_db.sh --apply`.' % len(ops), '']
        lines += ['## Bilan', '', '| Élément | Nombre |', '| --- | ---: |']
        for label, count in [('Colonnes à ajouter', sum(o['kind'] == 'add' for o in ops)),
                             ('Tables à créer', sum(o['kind'] == 'create' for o in ops)),
                             ('Élargissements', sum(o['kind'] == 'widen' for o in ops)),
                             ('Écarts manuels', len(plan['manual'])),
                             ('Champs sans type SQL', len(plan['untyped'])),
                             ('Tables locales non exposées', len(plan['local_only_tables']))]:
            lines.append('| %s | %s |' % (label, count))
        if ops:
            lines += ['', '## Changements du plan', '']
            labels = {'add': 'Ajouter la colonne', 'create': 'Créer la table', 'widen': 'Élargir la colonne'}
            for op in ops[:20]:
                name = op['table'] + ('.' + op['column'] if 'column' in op else '')
                lines.append('- %s %s.' % (labels[op['kind']], code(name)))
            if len(ops) > 20:
                lines += ['', '%s autres changements sont détaillés dans le rapport JSON.' % (len(ops) - 20)]
        if plan['manual']:
            lines += ['', '## Différences de colonnes non corrigées', '',
                      'Ces conversions ne sont pas exécutées automatiquement. Il faut vérifier les valeurs et les colonnes liées avant de les appliquer.', '',
                      '| Colonne | Changement demandé par l’API |', '| --- | --- |']
            for item in plan['manual']:
                if 'column' in item:
                    reason = item['reason'].removeprefix('Conversion non automatique : ')
                    lines.append('| %s | %s |' % (code(item['table'] + '.' + item['column']), code(reason)))
            lines += ['', 'Par exemple, passer de `bigint` à `int` réduit la capacité du nombre ; ajouter des décimales à précision totale constante réduit la partie entière. Les identifiants liés doivent aussi rester cohérents.']
            missing = [item['table'] for item in plan['manual'] if 'column' not in item]
            if missing:
                lines += ['', '## Tables non créées', '',
                          'Il manque une définition complète et vérifiée pour créer ces tables. Le seul nom fourni par l’API ne suffit pas.', '']
                lines += ['- ' + code(table) for table in missing]
        if plan['untyped']:
            lines += ['', '%s champs de l’API n’ont pas de type SQL. Ils ne sont pas considérés comme des colonnes manquantes prouvées.' % len(plan['untyped'])]
        if plan['local_only_tables']:
            lines += ['', '%s tables locales ne sont pas exposées par l’API. Elles sont conservées.' % len(plan['local_only_tables'])]
        if plan['manual']:
            lines += manual_followup(plan)
    else:
        lines += ['Comparaison incomplète : aucun bilan fiable disponible.', '']
    if report.get('data_unchanged'):
        lines += ['', 'Valeurs préexistantes vérifiées identiques avant/après application.']
    if report.get('error'):
        lines += ['', '## Erreur', '', code(report['error'])]
        if report.get('synchronization_success'):
            lines += ['', 'La synchronisation a été validée ; le traitement final a échoué.']
        elif report.get('comparison_success'):
            lines += ['', 'La comparaison est terminée ; la sauvegarde finale a échoué.']
        elif report.get('application_started'):
            lines += ['', 'Une application a commencé : vérifier la structure réelle avant de reprendre.']
    if report.get('post_success_backup'):
        lines += ['', '## Sauvegardes finales', '']
        for label, key in [('Données et structure', 'data'), ('Structure seule', 'structure')]:
            lines.append('- %s : %s.' % (label, code(report['post_success_backup'][key]['path'])))
    else:
        lines += ['', '## Sauvegardes finales', '', 'Aucun dump final n’a été créé pour cette exécution.']
    lines += ['', '## Rapports détaillés', '']
    if report.get('sql_report'):
        lines += ['- SQL : ' + code(report['sql_report'])]
    lines += ['- JSON et archives : ' + code(report['details_directory']), '']
    return '\n'.join(lines)


def main(argv=None):
    args = parser().parse_args(argv)
    if args.apply and args.metadata_file:
        raise SyncError('--apply exige une collecte fraîche ; retire --metadata-file')
    if not (1 <= args.http_timeout <= 300 and 1 <= args.command_timeout <= 7200):
        raise SyncError('Timeout hors limites')
    from _wt_common import MAIN
    from _wt_docker import base
    os.umask(0o077)
    ident(args.database)
    output = (args.output or MAIN / 'doc' / ('sync_incwo_db_' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f'))).resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {'source_url': URL, 'database': args.database, 'apply': args.apply, 'details_directory': str(output),
              'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    def save():
        temporary = output / 'rapport.json.tmp'
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        os.replace(temporary, output / 'rapport.json')
    # Le verrou est partagé entre tous les ateliers et porte sur la base cible.
    lock_path = MAIN / 'doc' / ('.sync_incwo_db_' + args.database + '.lock')
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError('Une synchronisation de cette base est déjà en cours')
        try:
            container = args.container
            if not container:
                ids = command(base() + ['ps', '-q', args.service], stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode().split()
                if len(ids) != 1:
                    raise SyncError('Conteneur MySQL absent ou ambigu ; précise --container')
                container = ids[0]
            live = Database(container, args.database, args.command_timeout)
            schema = live.schema()
            print('Collecte des métadonnées Incwo (4 requêtes simultanées au maximum)…', flush=True)
            metadata = json.loads(args.metadata_file.read_text()) if args.metadata_file else collect_metadata(args.http_timeout)
            (output / 'metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n')
            plan = build_plan(schema, metadata, MAIN, live.encodings())
            report.update({'container': container, 'schema_before': schema, 'plan': plan})
            sql = 'USE %s;\n' % ident(args.database) + '\n'.join(op['sql'] for op in plan['operations']) + '\n'
            sql = '-- Ne pas appliquer directement : utiliser sync_incwo_db.sh --apply pour les contrôles.\n' + sql
            sql_report = MAIN / 'doc' / ('rapport_sync_db_' + datetime.datetime.now().strftime('%Y-%m-%d') + '.sql')
            # Conserver une copie par exécution et remplacer atomiquement le
            # rapport du jour pour ne jamais exposer un fichier partiellement écrit.
            (output / 'rapport.sql').write_text(sql)
            publish_text(sql_report, sql)
            report['sql_report'] = str(sql_report)
            save()
            print('Rapport SQL : ' + str(sql_report), flush=True)
            counts = {kind: sum(op['kind'] == kind for op in plan['operations']) for kind in ['add', 'create', 'widen']}
            print('%s colonne(s) à ajouter, %s table(s) à créer, %s élargissement(s) ; %s écart(s) manuel(s), %s champ(s) sans type SQL.' %
                  (counts['add'], counts['create'], counts['widen'], len(plan['manual']), len(plan['untyped'])), flush=True)
            if args.apply:
                if plan['operations']:
                    execute(live, schema, plan, output, report, save)
                report['synchronization_success'] = True
            else:
                report['comparison_success'] = True
                print('Comparaison terminée. Aucune modification de structure.', flush=True)
            save()
            print('Sauvegarde des données et de la structure dans doc/…', flush=True)
            report['post_success_backup'] = live.success_backups(MAIN / 'doc')
            report['status'] = ('applied' if args.apply else 'comparison_only') if plan['operations'] else 'no_change'
            save()
            for backup in report['post_success_backup'].values():
                print('Sauvegarde créée : ' + backup['path'], flush=True)
        except BaseException as error:
            report['status'] = 'failed'
            report['error'] = str(error) or type(error).__name__
            raise
        finally:
            report['finished_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            summary_path = (Path(report['sql_report']).with_suffix('.md') if report.get('sql_report') else
                            MAIN / 'doc' / ('rapport_sync_db_' + datetime.datetime.now().strftime('%Y-%m-%d') + '.md'))
            report['markdown_report'] = str(summary_path)
            save()
            summary = markdown_summary(report)
            (output / 'rapport.md').write_text(summary)
            publish_text(summary_path, summary)
            print('Résumé Markdown : ' + str(summary_path), flush=True)
            print('Rapport et sauvegardes : ' + str(output), flush=True)


def interrupted(signum, frame):
    raise KeyboardInterrupt('Interruption demandée')


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, interrupted)
    try:
        main()
    except KeyboardInterrupt:
        print('Interrompu ; consulte le rapport pour savoir si une application a commencé.', file=sys.stderr)
        sys.exit(130)
    except (SyncError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print('ERREUR : ' + str(error), file=sys.stderr)
        sys.exit(1)
