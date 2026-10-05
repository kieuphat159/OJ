import io
import os
import shutil
import tempfile
import zipfile

import yaml
from django.test import SimpleTestCase, TestCase
from django.utils import translation

from judge.models import Problem, ProblemGroup, problem_data_storage
from judge.utils.polygon import PolygonImportError, PolygonPackage, format_pattern, import_polygon_package, \
    judge_language

CHECKER = '<checker name="std::lcmp.cpp" type="testlib"><source path="files/check.cpp" type="cpp.g++17"/></checker>'


def make_package(tests=3, test_attrs=None, groups='', checker=CHECKER, assets='', resources='', judging_attrs='',
                 extra_files=None, skip_files=(), root=''):
    test_attrs = test_attrs or [''] * tests
    xml = f'''<?xml version="1.0" encoding="utf-8"?>
<problem revision="1" short-name="a-plus-b">
  <names><name language="english" value="A Plus B"/></names>
  <judging input-file="" output-file="" {judging_attrs}>
    <testset name="tests">
      <time-limit>1500</time-limit>
      <memory-limit>268435456</memory-limit>
      <test-count>{tests}</test-count>
      <input-path-pattern>tests/%02d</input-path-pattern>
      <answer-path-pattern>tests/%02d.a</answer-path-pattern>
      <tests>{''.join(f'<test method="manual" {a}/>' for a in test_attrs)}</tests>
      {groups}
    </testset>
  </judging>
  <files><resources>{resources}</resources></files>
  <assets>{checker}{assets}</assets>
</problem>'''
    files = {'problem.xml': xml, 'files/check.cpp': '// checker'}
    for i in range(1, tests + 1):
        files['tests/%02d' % i] = '%d %d\n' % (i, i)
        files['tests/%02d.a' % i] = '%d\n' % (2 * i)
    files.update(extra_files or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as z:
        for name, content in files.items():
            if name not in skip_files:
                z.writestr(root + name, content)
    buffer.seek(0)
    return buffer


class PolygonPackageTestCase(SimpleTestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        # Warnings are asserted in English.
        translation.activate('en')

    def tearDown(self):
        translation.deactivate()
        shutil.rmtree(self.tmp_dir)

    def load(self, **kwargs):
        package = PolygonPackage(make_package(**kwargs))
        return package, package.test_data(self.tmp_dir)

    def test_helpers(self):
        self.assertEqual(format_pattern('tests/%02d', 7), 'tests/07')
        self.assertEqual(format_pattern('tests/%d.in', 12), 'tests/12.in')
        self.assertEqual(judge_language('cpp.g++17'), 'CPP17')
        self.assertEqual(judge_language('cpp.gcc14-64-msys2-g++23'), 'CPP20')

    def test_metadata(self):
        package = PolygonPackage(make_package())
        self.assertEqual(package.code(), 'a_plus_b')
        self.assertEqual(package.time_limit, 1.5)
        self.assertEqual(package.memory_limit, 262144)

    def test_unscored_tests(self):
        package, data = self.load()
        self.assertFalse(data.partial)
        self.assertEqual([(c.type, c.input_file, c.output_file, c.points) for c in data.cases],
                         [('C', '01.inp', '01.out', 1), ('C', '02.inp', '02.out', 1), ('C', '03.inp', '03.out', 1)])
        self.assertEqual(sorted(zipfile.ZipFile(data.zip_path).namelist()),
                         ['01.inp', '01.out', '02.inp', '02.out', '03.inp', '03.out'])
        self.assertEqual(zipfile.ZipFile(data.zip_path).read('02.out'), b'4\n')

    def test_groups(self):
        groups = '''<groups>
            <group name="samples" points="0" points-policy="complete-group"/>
            <group name="each" points="4" points-policy="each-test"/>
            <group name="full" points="50.0" points-policy="complete-group">
              <dependencies><dependency group="samples"/></dependencies>
            </group></groups>'''
        attrs = ['group="samples" points="0" sample="true"', 'group="full" points="50"', 'group="each" points="1.5"',
                 'group="full" points="0"']
        package, data = self.load(tests=4, test_attrs=attrs, groups=groups)
        self.assertTrue(data.partial)
        self.assertEqual([(c.type, c.input_file, c.points) for c in data.cases], [
            ('S', '', 0), ('C', '01.inp', None), ('E', '', None),
            ('S', '', 50), ('C', '02.inp', None), ('C', '04.inp', None), ('E', '', None),
            ('C', '03.inp', 2),
        ])
        warnings = ' '.join(package.warnings)
        self.assertIn("'full' are not contiguous", warnings)
        self.assertIn("'full' depends on samples", warnings)
        self.assertIn('1.5 was rounded to 2', warnings)

    def test_checkers(self):
        self.assertEqual(self.load()[1].checker, 'standard')

        data = self.load(checker=CHECKER.replace('lcmp', 'rcmp6'))[1]
        self.assertEqual((data.checker, data.checker_args), ('floats', {'precision': 6}))

        data = self.load(checker=CHECKER.replace('lcmp', 'wcmp'))[1]
        self.assertEqual((data.checker, data.custom_checker), ('bridged', 'files/check.cpp'))
        self.assertEqual(data.checker_args, {'files': 'check.cpp', 'lang': 'CPP17', 'type': 'testlib'})

        data = self.load(checker=CHECKER.replace(' name="std::lcmp.cpp"', '').replace('g++17', 'g++20'))[1]
        self.assertEqual((data.checker, data.checker_args['lang']), ('bridged', 'CPP20'))

    def test_interactive(self):
        package, data = self.load(
            assets='<interactor><source path="files/interactor.cpp" type="cpp.g++17"/></interactor>',
            extra_files={'files/interactor.cpp': '// interactor'})
        self.assertEqual((data.grader, data.custom_grader), ('interactive', 'files/interactor.cpp'))
        # The checker reads the interactor's output, so it must stay a custom checker.
        self.assertEqual(data.checker, 'bridged')

    def test_signature_grader(self):
        resources = ('<file for-types="cpp.*" path="files/grader.cpp" type="cpp.g++17">'
                     '<assets><asset name="solution"/></assets></file>'
                     '<file path="files/testlib.h" type="h.g++"/><file path="files/sum.h" type="h.g++"/>')
        package, data = self.load(resources=resources,
                                  extra_files={'files/grader.cpp': '', 'files/sum.h': '', 'files/testlib.h': ''})
        self.assertEqual((data.grader, data.custom_grader, data.custom_header),
                         ('signature', 'files/grader.cpp', 'files/sum.h'))

    def test_file_io(self):
        package = PolygonPackage(make_package())
        package.input_file, package.output_file = 'sum.inp', 'sum.out'
        data = package.test_data(self.tmp_dir)
        self.assertEqual(data.grader_args, {'io_method': 'file', 'io_input_file': 'sum.inp',
                                            'io_output_file': 'sum.out'})

        package = PolygonPackage(make_package())
        package.input_file, package.output_file = 'sum.inp', ''
        self.assertEqual(package.test_data(self.tmp_dir).grader_args, {})
        self.assertTrue(package.warnings)

    def test_nested_root(self):
        package, data = self.load(root='a-plus-b-7$linux/')
        self.assertEqual(package.root, 'a-plus-b-7$linux')
        self.assertEqual(len(data.cases), 3)

    def test_errors(self):
        with self.assertRaisesRegex(PolygonImportError, r'tests 2'):
            self.load(skip_files=('tests/02.a',))
        with self.assertRaisesRegex(PolygonImportError, 'problem.xml'):
            PolygonPackage(make_package(skip_files=('problem.xml',)))
        with self.assertRaisesRegex(PolygonImportError, 'not a zip'):
            PolygonPackage(io.BytesIO(b'not a zip'))


class ImportPolygonPackageTestCase(TestCase):
    def setUp(self):
        translation.activate('en')

    def tearDown(self):
        translation.deactivate()
        shutil.rmtree(problem_data_storage.path('a_plus_b'), ignore_errors=True)

    def test_import(self):
        groups = '<groups><group name="all" points="100" points-policy="complete-group"/></groups>'
        group = ProblemGroup.objects.create(name='test', full_name='Test')
        problem, warnings = import_polygon_package(
            make_package(tests=2, test_attrs=['group="all" points="100"', 'group="all"'], groups=groups),
            points=5, group=group)

        problem = Problem.objects.get(code='a_plus_b')
        self.assertEqual((problem.name, problem.time_limit, problem.memory_limit, problem.points, problem.partial),
                         ('A Plus B', 1.5, 262144, 5, True))
        self.assertEqual(problem.data_files.checker, 'standard')
        self.assertEqual(problem.data_files.feedback, '')
        self.assertEqual(list(problem.cases.order_by('order').values_list('type', flat=True)), ['S', 'C', 'C', 'E'])

        with open(os.path.join(problem_data_storage.path('a_plus_b'), 'init.yml')) as f:
            init = yaml.safe_load(f)
        self.assertEqual(init['archive'], 'a_plus_b.zip')
        self.assertEqual(init['test_cases'], [{'points': 100, 'batched': [{'in': '01.inp', 'out': '01.out'},
                                                                          {'in': '02.inp', 'out': '02.out'}]}])
        self.assertIn('The package has no statement; write the description yourself.', warnings)

    def test_failed_import_writes_nothing(self):
        with self.assertRaises(PolygonImportError):
            import_polygon_package(make_package(skip_files=('tests/01',)))
        self.assertFalse(Problem.objects.filter(code='a_plus_b').exists())
        self.assertFalse(os.path.exists(problem_data_storage.path('a_plus_b')))
