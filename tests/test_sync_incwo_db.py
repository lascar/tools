"""Garde-fous du plan et ordre des sauvegardes/tests avant écriture locale."""
import contextlib
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sync_incwo_db as sync


def ddl(kind='varchar(20)', extra=' DEFAULT NULL'):
    return ('CREATE TABLE `samples` (\n'
            '  `id` int NOT NULL AUTO_INCREMENT,\n'
            '  `label` %s%s,\n'
            '  PRIMARY KEY (`id`),\n'
            '  KEY `by_label` (`label`(10))\n'
            ') ENGINE=InnoDB DEFAULT CHARSET=utf8mb3' % (kind, extra))


def metadata(fields, table='samples'):
    return {'api_details': {table: {'table_metadata': {'table_name': table, 'fields': {
        key: {'sql_type': value, 'sql_field_name': key} for key, value in fields.items()}}}}}


class PlanTests(unittest.TestCase):
    def test_manual_followup_explains_numeric_checks_without_claiming_they_ran(self):
        plan = {'manual': [
            {'table': 'amounts', 'column': 'amount', 'reason': 'Conversion non automatique : decimal(20,0) -> decimal(20,6)'},
            {'table': 'samples', 'column': 'id', 'reason': 'Conversion non automatique : bigint -> int'},
        ]}
        text = '\n'.join(sync.manual_followup(plan))
        self.assertIn('amounts.amount', text)
        self.assertIn('14 chiffres avant la virgule', text)
        self.assertIn('-2147483648 à 2147483647', text)
        self.assertIn('documenter l’écart accepté', text)
        self.assertIn('n’a été exécuté par cette comparaison', text)

    def test_manual_followup_distinguishes_missing_table_cases_and_sources(self):
        plan = {'manual': [
            {'table': 'task_dependencies', 'api_fields_count': 0, 'review_sources': ['db/setup/1.tables.sql']},
            {'table': 'inventories_items', 'api_fields_count': 0},
            {'table': 'bak_20261005_products_names', 'api_fields_count': 0},
        ]}
        text = '\n'.join(sync.manual_followup(plan))
        self.assertIn('0 champ(s) exposé(s)', text)
        self.assertIn('db/setup/1.tables.sql', text)
        self.assertIn('des DROP et ne doit pas être exécuté en entier', text)
        self.assertIn('ne définit pas la table initiale', text)
        self.assertIn('classer cet écart comme accepté', text)

    def plan(self, fields, schema=None, encodings=None):
        return sync.build_plan(schema or {'samples': ddl()}, metadata(fields), Path('/nonexistent'), encodings or {'samples.label': 3})

    def test_integer_display_widths_are_ignored_but_signedness_is_not(self):
        self.assertEqual(self.plan({'id': 'int(11)'})['operations'], [])
        result = self.plan({'id': 'int unsigned'})
        self.assertEqual(len(result['manual']), 1)

    def test_existing_legacy_double_zerofill_and_binary_types_are_noops(self):
        for kind in ['double(20,6)', 'decimal(20,6) unsigned zerofill', 'binary(1)']:
            with self.subTest(kind=kind):
                result = self.plan({'label': kind}, {'samples': ddl(kind)})
                self.assertFalse(result['operations'])
                self.assertFalse(result['manual'])

    def test_widening_preserves_collation_default_comment_and_nullability(self):
        schema = {'samples': ddl(extra=" CHARACTER SET utf8mb3 COLLATE utf8mb3_bin NOT NULL DEFAULT 'a' COMMENT 'garder'")}
        op = self.plan({'label': 'varchar(50)'}, schema)['operations'][0]
        self.assertIn("varchar(50) CHARACTER SET utf8mb3 COLLATE utf8mb3_bin NOT NULL DEFAULT 'a' COMMENT 'garder'", op['sql'])

    def test_varchar_to_text_checks_worst_case_bytes(self):
        result = self.plan({'label': 'text'}, {'samples': ddl('varchar(30000)')})
        self.assertFalse(result['operations'])
        self.assertEqual(len(result['manual']), 1)

    def test_varchar_to_text_refuses_unknown_encoding(self):
        result = sync.build_plan({'samples': ddl()}, metadata({'label': 'text'}), Path('/nonexistent'), {})
        self.assertFalse(result['operations'])

    def test_varchar_to_text_keeps_non_null_default_for_manual_review(self):
        result = self.plan({'label': 'text'}, {'samples': ddl(extra=" NOT NULL DEFAULT 'défaut'")})
        self.assertFalse(result['operations'])
        self.assertIn('défaut', result['manual'][0]['reason'])

    def test_api_accepts_legacy_latin1_without_replacing_characters(self):
        response = MagicMock()
        response.geturl.return_value = sync.URL
        response.read.return_value = '{"description":"français été"}'.encode('iso-8859-1')
        response.__enter__.return_value = response
        with patch.object(sync, 'urlopen', return_value=response):
            self.assertEqual(sync.fetch_json(sync.URL, 30), {'description': 'français été'})

    def test_unsafe_numeric_temporal_and_narrowing_changes_are_manual(self):
        for old, new in [('bigint', 'int'), ('decimal(20,0)', 'decimal(20,6)'), ('date', 'timestamp'),
                         ('int', 'decimal(3,2)'), ('varchar(50)', 'varchar(20)'), ('char(20)', 'varchar(50)')]:
            with self.subTest(old=old, new=new):
                result = self.plan({'label': new}, {'samples': ddl(old)})
                self.assertFalse(result['operations'])
                self.assertEqual(len(result['manual']), 1)

    def test_missing_columns_are_nullable_and_virtual_fields_are_ignored(self):
        result = self.plan({'extra': 'decimal(20,6)', 'virtual': None})
        self.assertEqual(result['operations'][0]['sql'], 'ALTER TABLE `samples` ADD COLUMN `extra` decimal(20,6) NULL DEFAULT NULL;')
        self.assertEqual(result['untyped'], ['samples.virtual'])

    def test_existing_type_is_noop_and_local_columns_are_preserved(self):
        result = self.plan({'label': 'varchar(20)'})
        self.assertFalse(result['operations'])
        self.assertFalse(result['manual'])

    def test_injected_type_never_becomes_active_sql(self):
        result = self.plan({'extra': 'int; DROP TABLE samples'})
        self.assertFalse(result['operations'])
        self.assertEqual(len(result['manual']), 1)

    def test_injected_identifiers_are_rejected(self):
        for name in ['foo`', 'db.samples', 'x; DROP TABLE samples', '../other', '', 'a' * 65]:
            with self.subTest(name=name), self.assertRaises(sync.SyncError):
                self.plan({name: 'int'})

    def test_numeric_prefix_column_names_are_safely_quoted(self):
        op = self.plan({'404_errors': 'int'})['operations'][0]
        self.assertIn('ADD COLUMN `404_errors` int', op['sql'])

    def test_wrong_table_response_is_not_silently_accepted(self):
        with self.assertRaises(sync.SyncError):
            sync.api_fields('samples', {'table_metadata': {'table_name': 'other', 'fields': {}}})

    def test_missing_table_without_known_ddl_is_manual(self):
        result = sync.build_plan({'samples': ddl()}, metadata({'id': 'int'}, 'unknown'), Path('/nonexistent'), {})
        self.assertFalse(result['operations'])
        self.assertEqual(len(result['manual']), 1)

    def test_known_create_is_exactly_checked_and_dml_is_excluded(self):
        table = 'wli_prospects'
        text = "CREATE TABLE IF NOT EXISTS wli_prospects (\n  id INT NOT NULL AUTO_INCREMENT,\n  PRIMARY KEY (id)\n) ENGINE=InnoDB DEFAULT CHARSET=utf8;\nUPDATE users SET id=0;"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / sync.SOURCES[table]
            source.parent.mkdir(parents=True)
            source.write_text(text)
            result = sync.known_create(root, table, {'id': 'int'})
            self.assertIn('DEFAULT CHARSET=utf8mb3;', result)
            self.assertNotIn('UPDATE', result)
            self.assertIsNone(sync.known_create(root, table, {'id': 'bigint'}))
            self.assertIsNone(sync.known_create(root, table, {'id': 'int', 'virtual': None}))
            source.write_text(text.replace('PRIMARY KEY (id)', 'PRIMARY KEY (id); DELETE FROM users'))
            self.assertIsNone(sync.known_create(root, table, {'id': 'int'}))

    def test_schema_verification_detects_lost_attributes_and_indexes(self):
        before = {'samples': ddl()}
        ops = self.plan({'label': 'varchar(50)'})['operations']
        after = {'samples': ddl('varchar(50)')}
        sync.verify_schema(before, after, ops)
        for bad in [ddl('varchar(50)').replace('KEY `by_label` (`label`(10))', 'KEY `other` (`id`)'),
                    ddl('varchar(50)', ' NOT NULL'), ddl('varchar(50)').replace('InnoDB', 'MyISAM')]:
            with self.subTest(bad=bad), self.assertRaises(sync.SyncError):
                sync.verify_schema(before, {'samples': bad}, ops)

    def test_schema_verification_accepts_text_default_normalization(self):
        ops = self.plan({'label': 'text'})['operations']
        sync.verify_schema({'samples': ddl()}, {'samples': ddl('text', '')}, ops)

    def test_cached_metadata_cannot_be_applied(self):
        with self.assertRaisesRegex(sync.SyncError, 'collecte fraîche'):
            sync.main(['--apply', '--metadata-file', '/does/not/exist'])


class FakeDatabase:
    def __init__(self, events, name, schema, corrupt=False, fail_backup=False):
        self.events, self.name, self.current = events, name, copy.deepcopy(schema)
        self.container, self.database, self.timeout = name, 'fixture_db', 60
        self.changed, self.corrupt, self.fail_backup = False, corrupt, fail_backup

    def sql(self, query):
        if 'VERSION()' in query:
            return '8.0.28\n'
        if 'DISTINCT ENGINE' in query:
            return 'InnoDB\n'
        raise AssertionError(query)

    def schema(self):
        return self.current

    def backup(self, path):
        self.events.append((self.name, 'backup'))
        if self.fail_backup:
            raise sync.SyncError('Échec de sauvegarde simulé')
        return {'path': str(path)}

    def restore(self, path):
        self.events.append((self.name, 'restore'))

    def fingerprints(self, schema, tables):
        return {'samples': {'rows': 2, 'sha256': 'bad' if self.corrupt and self.changed else 'good'}}

    def apply(self, operations):
        self.events.append((self.name, 'apply'))
        self.current = {'samples': ddl('varchar(50)')}
        self.changed = True


class ExecutionTests(unittest.TestCase):
    def execute(self, *, corrupt=False, fail_backup=False, drift=False):
        events, before = [], {'samples': ddl()}
        live = FakeDatabase(events, 'live', before, fail_backup=fail_backup)
        clone = FakeDatabase(events, 'clone', before, corrupt=corrupt)
        if drift:
            live.current = {'samples': ddl('varchar(30)')}
        plan = sync.build_plan(before, metadata({'label': 'varchar(50)'}), Path('/nonexistent'), {'samples.label': 3})
        report = {}
        @contextlib.contextmanager
        def isolated(*args):
            yield clone
        @contextlib.contextmanager
        def paused(*args):
            events.append(('live', 'pause'))
            try:
                yield
            finally:
                events.append(('live', 'resume'))
        info = {'Image': 'immutable-image', 'Config': {'Cmd': ['mysqld']}}
        with tempfile.TemporaryDirectory() as folder, patch.object(sync, 'docker_inspect', return_value=info), \
                patch.object(sync, 'isolated_mysql', isolated), patch.object(sync, 'quiescent', paused):
            try:
                sync.execute(live, before, plan, Path(folder), report, lambda: None)
            except sync.SyncError:
                return events, report, True
        return events, report, False

    def test_backup_and_isolated_validation_precede_live_writes(self):
        events, report, failed = self.execute()
        self.assertFalse(failed)
        self.assertTrue(report['data_unchanged'])
        self.assertEqual(events, [('live', 'backup'), ('clone', 'restore'), ('clone', 'apply'),
                                  ('live', 'pause'), ('live', 'backup'), ('live', 'apply'), ('live', 'resume')])

    def test_corrupt_test_data_prevents_live_application(self):
        events, report, failed = self.execute(corrupt=True)
        self.assertTrue(failed)
        self.assertNotIn(('live', 'apply'), events)
        self.assertNotIn(('live', 'pause'), events)

    def test_failed_backup_prevents_any_application(self):
        events, report, failed = self.execute(fail_backup=True)
        self.assertTrue(failed)
        self.assertEqual(events, [('live', 'backup')])

    def test_schema_drift_aborts_and_resumes_services(self):
        events, report, failed = self.execute(drift=True)
        self.assertTrue(failed)
        self.assertNotIn(('live', 'apply'), events)
        self.assertEqual(events[-1], ('live', 'resume'))


class SuccessBackupTests(unittest.TestCase):
    def test_two_plain_dumps_match_documented_names_and_options(self):
        calls = []
        def dump(args, **kwargs):
            calls.append(args)
            kwargs['stdout'].write(b'-- SQL fixture\n-- Dump completed on today\n')
            return type('Result', (), {'stderr': b''})()
        with tempfile.TemporaryDirectory() as folder, patch.object(sync, 'command', side_effect=dump):
            result = sync.Database('unused', 'facile_building').success_backups(folder)
            self.assertRegex(Path(result['data']['path']).name, r'^dump_facile_building_\d{4}-\d{2}-\d{2}\.sql$')
            self.assertRegex(Path(result['structure']['path']).name, r'^dump_facile_building_struct_\d{4}-\d{2}-\d{2}\.sql$')
            self.assertIn('--single-transaction', calls[0])
            self.assertIn('--no-data', calls[1])
            for item in result.values():
                path = Path(item['path'])
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertIn(b'Dump completed on', path.read_bytes())
                self.assertEqual(len(item['sha256']), 64)

    def test_second_run_keeps_previous_backups(self):
        def dump(args, **kwargs):
            kwargs['stdout'].write(b'-- Dump completed on today\n')
            return type('Result', (), {'stderr': b''})()
        with tempfile.TemporaryDirectory() as folder, patch.object(sync, 'command', side_effect=dump):
            db = sync.Database('unused', 'facile_building')
            first = db.success_backups(folder)
            second = db.success_backups(folder)
            self.assertNotEqual(first['data']['path'], second['data']['path'])
            self.assertEqual(len(list(Path(folder).glob('*.sql'))), 4)

    def test_failed_structure_dump_publishes_neither_dump(self):
        count = 0
        def dump(args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise sync.SyncError('Échec structure simulé')
            kwargs['stdout'].write(b'-- Dump completed on today\n')
            return type('Result', (), {'stderr': b''})()
        with tempfile.TemporaryDirectory() as folder, patch.object(sync, 'command', side_effect=dump):
            with self.assertRaises(sync.SyncError):
                sync.Database('unused', 'facile_building').success_backups(folder)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def run_main(self, *, apply=True, operations=True, sync_error=False, backup_error=False):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'doc').mkdir()
            output = root / 'report'
            live = MagicMock()
            live.schema.return_value = {'samples': ddl()}
            live.encodings.return_value = {'samples.label': 3}
            live.success_backups.return_value = {'data': {'path': 'data.sql'}, 'structure': {'path': 'struct.sql'}}
            events = []
            live.success_backups.side_effect = (sync.SyncError('Sauvegarde finale en échec') if backup_error else
                                               lambda *args: events.append('backup') or live.success_backups.return_value)
            def execute(*args):
                events.append('execute')
                if sync_error:
                    raise sync.SyncError('Synchronisation en échec')
            with patch('_wt_common.MAIN', root), patch.object(sync, 'Database', return_value=live), \
                    patch.object(sync, 'collect_metadata', return_value=metadata({'label': 'varchar(50)' if operations else 'varchar(20)'})), \
                    patch.object(sync, 'execute', side_effect=execute):
                args = ['--container', 'fixture', '--output', str(output)] + (['--apply'] if apply else [])
                try:
                    sync.main(args)
                except sync.SyncError:
                    pass
            report = __import__('json').loads((output / 'rapport.json').read_text())
            sql_report = root / 'doc' / ('rapport_sync_db_' + sync.datetime.datetime.now().strftime('%Y-%m-%d') + '.sql')
            self.assertEqual(report['sql_report'], str(sql_report))
            self.assertEqual(sql_report.read_bytes(), (output / 'rapport.sql').read_bytes())
            self.assertIn(b'USE `facile_building`;', sql_report.read_bytes())
            md_report = sql_report.with_suffix('.md')
            self.assertEqual(report['markdown_report'], str(md_report))
            self.assertEqual(md_report.read_bytes(), (output / 'rapport.md').read_bytes())
            summary = md_report.read_text()
            self.assertIn('| Élargissements | %s |' % (1 if operations else 0), summary)
            if sync_error or backup_error:
                self.assertIn('Échec du traitement.', summary)
                self.assertIn('## Erreur', summary)
            elif apply and operations:
                self.assertIn('Changements automatiques appliqués et vérifiés.', summary)
            elif not operations:
                self.assertIn('Aucun changement automatique applicable.', summary)
            else:
                self.assertIn('changements proposés non appliqués', summary)
            if 'post_success_backup' in report:
                self.assertIn('## Sauvegardes finales', summary)
                self.assertIn('data.sql', summary)
            else:
                self.assertIn('Aucun dump final n’a été créé pour cette exécution.', summary)
            if backup_error:
                expected = ('La synchronisation a été validée ; le traitement final a échoué.' if apply else
                            'La comparaison est terminée ; la sauvegarde finale a échoué.')
                self.assertIn(expected, summary)
            return events, report, live.success_backups.call_count

    def test_main_backs_up_only_after_successful_application(self):
        events, report, count = self.run_main()
        self.assertEqual(events, ['execute', 'backup'])
        self.assertEqual(report['status'], 'applied')
        self.assertIn('post_success_backup', report)

    def test_main_backs_up_successful_noop_apply(self):
        events, report, count = self.run_main(operations=False)
        self.assertEqual(events, ['backup'])
        self.assertEqual(report['status'], 'no_change')

    def test_main_does_not_back_up_failed_sync(self):
        events, report, count = self.run_main(sync_error=True)
        self.assertEqual(count, 0)

    def test_main_backs_up_successful_comparison(self):
        events, report, count = self.run_main(apply=False)
        self.assertEqual(events, ['backup'])
        self.assertTrue(report['comparison_success'])
        self.assertEqual(report['status'], 'comparison_only')

    def test_main_reports_backup_failure_after_comparison(self):
        events, report, count = self.run_main(apply=False, backup_error=True)
        self.assertTrue(report['comparison_success'])
        self.assertEqual(report['status'], 'failed')

    def test_main_reports_post_backup_failure_after_valid_sync(self):
        events, report, count = self.run_main(backup_error=True)
        self.assertTrue(report['synchronization_success'])
        self.assertEqual(report['status'], 'failed')
        self.assertNotIn('post_success_backup', report)


@unittest.skipUnless(os.environ.get('INCWO_SYNC_TEST_MYSQL_CONTAINER'), 'MySQL isolé requis explicitement')
class MySQLTests(unittest.TestCase):
    def test_success_dumps_can_restore_data_and_keep_schema_only_separate(self):
        db = sync.Database(os.environ['INCWO_SYNC_TEST_MYSQL_CONTAINER'], 'incwo_backup_fixture', tcp=True)
        db.sql("CREATE DATABASE incwo_backup_fixture; CREATE DATABASE incwo_backup_restore; "
               "CREATE TABLE incwo_backup_fixture.samples (id INT PRIMARY KEY, value TEXT) ENGINE=InnoDB; "
               "INSERT INTO incwo_backup_fixture.samples VALUES (1,'été'),(2,NULL);")
        before = db.schema()
        expected = db.fingerprints(before, {'samples'})
        with tempfile.TemporaryDirectory() as folder:
            backups = db.success_backups(folder)
            data = Path(backups['data']['path'])
            structure = Path(backups['structure']['path'])
            self.assertIn(b'INSERT INTO', data.read_bytes())
            self.assertNotIn(b'INSERT INTO', structure.read_bytes())
            restored = sync.Database(db.container, 'incwo_backup_restore', tcp=True)
            with data.open('rb') as stream:
                sync.command(restored.cli() + ['--binary-mode', restored.database], stdin=stream,
                             stdout=sync.subprocess.PIPE, stderr=sync.subprocess.PIPE)
            self.assertEqual(restored.fingerprints(before, {'samples'}), expected)

    def test_real_alter_preserves_values_defaults_collations_indexes_and_trigger(self):
        # Le nom de base est réservé à ce test et ne peut pas viser facile_building.
        db = sync.Database(os.environ['INCWO_SYNC_TEST_MYSQL_CONTAINER'], 'incwo_sync_fixture', tcp=True)
        db.sql('CREATE DATABASE `incwo_sync_fixture` CHARACTER SET utf8mb3;')
        db.sql("USE `incwo_sync_fixture`; SET sql_mode=''; CREATE TABLE samples (id INT PRIMARY KEY, "
               "label VARCHAR(20) CHARACTER SET utf8mb3 COLLATE utf8mb3_bin NOT NULL DEFAULT 'défaut' COMMENT 'à garder', "
               "body VARCHAR(20) CHARACTER SET utf8mb3 COLLATE utf8mb3_bin DEFAULT NULL COMMENT 'texte', "
               "touched TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP, "
               "KEY by_label (label(10))) ENGINE=InnoDB; "
               "INSERT INTO samples VALUES (1,'été  ','été  ', '2020-01-01 10:00:00'),(2,'漢字',NULL, '0000-00-00 00:00:00'); "
               "CREATE TRIGGER fixture_guard BEFORE UPDATE ON samples FOR EACH ROW SET NEW.label='modifié';")
        before = db.schema()
        fields = {'label': 'varchar(50)', 'body': 'text', 'extra': 'decimal(20,6)'}
        plan = sync.build_plan(before, metadata(fields), Path('/nonexistent'), db.encodings())
        fingerprint = db.fingerprints(before, {'samples'})
        db.apply(plan['operations'])
        try:
            sync.verify_schema(before, db.schema(), plan['operations'])
        except sync.SyncError as error:
            self.fail('%s\nAvant : %r\nAprès : %r\nPlan : %r' % (error, before, db.schema(), plan))
        self.assertEqual(db.fingerprints(before, {'samples'}), fingerprint)
        self.assertEqual(db.sql('SELECT COUNT(*) FROM incwo_sync_fixture.samples WHERE extra IS NULL;').strip(), '2')
        self.assertEqual(db.sql("SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA='incwo_sync_fixture';").strip(), '1')
        # Une seconde comparaison doit être vide, sans réappliquer les ADD.
        second = sync.build_plan(db.schema(), metadata(fields), Path('/nonexistent'), db.encodings())
        self.assertEqual(second['operations'], [])


if __name__ == '__main__':
    unittest.main()
