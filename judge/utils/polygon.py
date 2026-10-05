"""Import Codeforces Polygon packages (the "full" package zip).

Used by the problem import page (judge.views.problem.ProblemImportPolygon)
and by the ``import_polygon_package`` management command. Parsing is split
from the database writes so the mapping can be tested without a database.

Package layout (paths relative to problem.xml):
    problem.xml                                 metadata, tests, groups, assets
    tests/01, tests/01.a, ...                   see <input/answer-path-pattern>
    files/check.cpp                             checker source
    files/interactor.cpp                        interactor (interactive problems)
    files/grader.cpp, files/*.h                 grader + header (resources with an
                                                <asset name="solution"/>)
    statements/<language>/problem-properties.json   statement sections in TeX
"""
import hashlib
import json
import math
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from django.conf import settings
from django.core.files import File
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from lxml import etree as ET

from judge.models import Language, Problem, ProblemData, ProblemGroup, ProblemTestCase, ProblemTranslation, \
    problem_data_storage
from judge.utils.problem_data import ProblemDataCompiler
from judge.views.widgets import django_uploader

__all__ = ['PolygonImportError', 'PolygonPackage', 'import_polygon_package', 'tex_to_markdown']

# Polygon standard checkers that behave exactly like a built-in judge checker.
# Everything else runs the package's own testlib source.
NATIVE_CHECKERS = {
    # The judge's "standard" checker compares tokens and keeps line structure, like lcmp.
    'std::lcmp.cpp': ('standard', None),
    # rcmpN: absolute or relative error 10^-N, as the judge's "floats" checker.
    'std::rcmp4.cpp': ('floats', {'precision': 4}),
    'std::rcmp6.cpp': ('floats', {'precision': 6}),
    'std::rcmp9.cpp': ('floats', {'precision': 9}),
}

# Polygon statement language -> site language code.
SITE_LANGUAGES = {'vietnamese': 'vi', 'english': 'en'}
PREFERRED_LANGUAGES = ['vietnamese', 'english']

SECTION_TITLES = {
    'english': {
        'input': 'Input Specification', 'output': 'Output Specification', 'interaction': 'Interaction',
        'scoring': 'Scoring', 'sample_input': 'Sample Input {0}', 'sample_output': 'Sample Output {0}',
        'notes': 'Notes',
    },
    'vietnamese': {
        'input': 'Dữ liệu vào', 'output': 'Kết quả', 'interaction': 'Tương tác', 'scoring': 'Chấm điểm',
        'sample_input': 'Dữ liệu vào mẫu {0}', 'sample_output': 'Kết quả mẫu {0}', 'notes': 'Ghi chú',
    },
}

PANDOC_FILTER = """
local List = require 'pandoc.List'

function normalize_quote(text)
    -- These four quotes are disallowed characters.
    -- See DMOJ_PROBLEM_STATEMENT_DISALLOWED_CHARACTERS
    text = text:gsub('\\u{2018}', "'") -- left single quote
    text = text:gsub('\\u{2019}', "'") -- right single quote
    text = text:gsub('\\u{201C}', '"') -- left double quote
    text = text:gsub('\\u{201D}', '"') -- right double quote
    return text
end

function Math(m)
    -- Fix math delimiters
    local delimiter = m.mathtype == 'InlineMath' and '~' or '$$'
    return pandoc.RawInline('markdown', delimiter .. m.text .. delimiter)
end

function Image(el)
    -- And line breaks before the image
    return {pandoc.RawInline('markdown', '\\n\\n'), el}
end

function Code(el)
    -- Normalize quotes
    el.text = normalize_quote(el.text)
    return el
end

function CodeBlock(el)
    -- Normalize quotes
    el.text = normalize_quote(el.text)
    return el
end

function Str(el)
    -- en dash and em dash would still show up correctly if we don't escape
    -- them, but they would be hardly noticeable while editing.
    el.text = el.text:gsub('\\u{2013}', '&ndash;')
    el.text = el.text:gsub('\\u{2014}', '&mdash;')

    -- Normalize quotes
    el.text = normalize_quote(el.text)

    return el
end

function Div(el)
    -- Currently only used for <center>
    -- FIXME: What about other classes?
    local res = List:new{}
    table.insert(res, pandoc.RawBlock('markdown', '<' .. el.classes[1] .. '>'))
    for _, block in ipairs(el.content) do
        table.insert(res, block)
    end
    table.insert(res, pandoc.RawBlock('markdown', '</' .. el.classes[1] .. '>'))
    return res
end
"""


class PolygonImportError(Exception):
    pass


def tex_to_markdown(tex):
    """Converts a Polygon TeX statement section to Markdown with pandoc."""
    if not tex or not tex.strip():
        return ''
    if not shutil.which('pandoc'):
        raise PolygonImportError(_('pandoc is not installed on the server.'))
    # Polygon's \t{...} is monospace text; in LaTeX (and pandoc) it is the tie accent.
    tex = re.sub(r'\\t(?=\s*\{)', r'\\texttt', tex)
    with tempfile.TemporaryDirectory() as tmp_dir:
        with open(os.path.join(tmp_dir, 'temp.tex'), 'w', encoding='utf-8') as f:
            f.write(tex)
        with open(os.path.join(tmp_dir, 'filter.lua'), 'w', encoding='utf-8') as f:
            f.write(PANDOC_FILTER)
        subprocess.run(['pandoc', '--lua-filter=filter.lua', '-t', 'gfm', '-o', 'temp.md', 'temp.tex'],
                       cwd=tmp_dir, check=True, timeout=60)
        with open(os.path.join(tmp_dir, 'temp.md'), encoding='utf-8') as f:
            markdown = f.read()
    # A "|" inside ~math~ in a table row would end the cell.
    return re.sub(r'(?m)^\|.*\|[ \t]*$',
                  lambda row: re.sub(r'~[^~\n]*~', lambda m: re.sub(r'(?<!\\)\|', r'\\|', m.group(0)), row.group(0)),
                  markdown)


def format_pattern(pattern, index):
    """Polygon path patterns use printf syntax, e.g. "tests/%02d"."""
    return re.sub(r'%(0?)(\d*)d', lambda m: str(index).rjust(int(m.group(2) or 0), '0' if m.group(1) else ' '),
                  pattern, count=1)


def judge_language(source_type):
    """Polygon source types look like "cpp.g++17" or "cpp.gcc14-64-msys2-g++23"."""
    return 'CPP20' if re.search(r'(g\+\+|c\+\+)(20|23|2a|2b)', source_type or '') else 'CPP17'


def _number(value):
    try:
        return float(value) if value not in (None, '') else None
    except ValueError:
        return None


@dataclass
class Case:
    type: str  # C = case, S = batch start (holds the points), E = batch end
    input_file: str = ''
    output_file: str = ''
    points: Optional[int] = None


@dataclass
class TestData:
    zip_path: str
    cases: List[Case]
    partial: bool
    checker: str
    checker_args: Optional[dict] = None
    custom_checker: Optional[str] = None  # path inside the package
    grader: str = 'standard'
    grader_args: Dict[str, str] = field(default_factory=dict)
    custom_grader: Optional[str] = None
    custom_header: Optional[str] = None


class PolygonPackage:
    def __init__(self, package):
        """`package` is a path or a file object of the package zip."""
        try:
            self.zip = zipfile.ZipFile(package)
        except zipfile.BadZipFile:
            raise PolygonImportError(_('The file is not a zip archive.'))
        xml_paths = sorted((n for n in self.zip.namelist() if posixpath.basename(n) == 'problem.xml'), key=len)
        if not xml_paths:
            raise PolygonImportError(_('This zip has no problem.xml. Upload the package downloaded from Polygon '
                                       '(Packages tab).'))
        self.root = posixpath.dirname(xml_paths[0])
        try:
            self.xml = ET.fromstring(self.zip.read(xml_paths[0]))
        except ET.XMLSyntaxError:
            raise PolygonImportError(_('problem.xml could not be read.'))
        if self.xml.tag != 'problem':
            raise PolygonImportError(_('problem.xml could not be read.'))
        self.warnings = []
        self._parse()

    def add_warning(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    def path(self, name):
        return posixpath.join(self.root, name) if self.root else name

    def has(self, name):
        try:
            self.zip.getinfo(self.path(name))
        except KeyError:
            return False
        return True

    def read(self, name):
        return self.zip.read(self.path(name))

    def _parse(self):
        problem = self.xml
        self.short_name = problem.get('short-name', '')
        self.names = {n.get('language'): n.get('value', '') for n in problem.findall('names/name')}

        judging = problem.find('judging')
        self.input_file = judging.get('input-file', '') if judging is not None else ''
        self.output_file = judging.get('output-file', '') if judging is not None else ''

        testsets = problem.findall('judging/testset')
        self.testset = next((t for t in testsets if t.get('name') == 'tests'), testsets[0] if testsets else None)
        self.other_testsets = [t.get('name') for t in testsets if t is not self.testset]

        checker = problem.find('assets/checker')
        self.checker = checker
        self.interactor = problem.find('assets/interactor')

        # Graders are resources compiled together with the contestant's
        # solution: <file for-types="cpp.*" ...><assets><asset name="solution"/>.
        resources = problem.findall('files/resources/file')
        self.graders = [f.get('path') for f in resources if f.find('assets/asset[@name="solution"]') is not None]
        self.headers = [f.get('path') for f in resources
                        if re.search(r'\.h(pp)?$', f.get('path', '')) and
                        posixpath.basename(f.get('path')) != 'testlib.h']

    # Limits use the site's units: seconds and KiB (Polygon: ms and bytes).
    @property
    def time_limit(self):
        value = _number(self.testset.findtext('time-limit')) if self.testset is not None else None
        return value / 1000 if value is not None else None

    @property
    def memory_limit(self):
        value = _number(self.testset.findtext('memory-limit')) if self.testset is not None else None
        if value is None:
            return None
        limit = int(value) // 1024
        if hasattr(settings, 'DMOJ_PROBLEM_MIN_MEMORY_LIMIT'):
            limit = max(limit, settings.DMOJ_PROBLEM_MIN_MEMORY_LIMIT)
        if hasattr(settings, 'DMOJ_PROBLEM_MAX_MEMORY_LIMIT'):
            limit = min(limit, settings.DMOJ_PROBLEM_MAX_MEMORY_LIMIT)
        return limit

    def tests(self):
        return [{
            'index': i,
            'points': _number(t.get('points')),
            'group': t.get('group', ''),
            'sample': t.get('sample') == 'true',
        } for i, t in enumerate(self.testset.findall('tests/test'), start=1)]

    def groups(self):
        return {g.get('name'): {
            'points': _number(g.get('points')),
            'policy': g.get('points-policy') or 'complete-group',
            'dependencies': [d.get('group') for d in g.findall('dependencies/dependency')],
        } for g in self.testset.findall('groups/group')}

    def is_scored(self):
        return (any((t['points'] or 0) > 0 for t in self.tests()) or
                any((g['points'] or 0) > 0 for g in self.groups().values()))

    def code(self):
        return re.sub(r'[^a-z0-9_]', '_', self.short_name.lower())[:32]

    # ------------------------------------------------------------------
    # Test data

    def test_data(self, tmp_dir):
        """Repacks the tests into tmp_dir/<code>.zip and maps the judging setup."""
        if self.testset is None or not self.testset.findall('tests/test'):
            raise PolygonImportError(_('The package has no tests.'))
        if self.other_testsets:
            self.add_warning(_("Testsets '{0}' were ignored; only '{1}' was imported.").format(
                ', '.join(self.other_testsets), self.testset.get('name')))

        tests = self.tests()
        width = max(2, len(str(len(tests))))
        names = {t['index']: ('%0*d.inp' % (width, t['index']), '%0*d.out' % (width, t['index'])) for t in tests}
        input_pattern = self.testset.findtext('input-path-pattern')
        answer_pattern = self.testset.findtext('answer-path-pattern')

        # Polygon's "standard" packages only contain manual tests; the "full"
        # package also has the generated ones.
        missing = [t['index'] for t in tests
                   if not self.has(format_pattern(input_pattern, t['index'])) or
                   not self.has(format_pattern(answer_pattern, t['index']))]
        if missing:
            shown = ', '.join(map(str, missing[:10])) + (', ...' if len(missing) > 10 else '')
            raise PolygonImportError(_("Test files are missing from the package (tests {0}). Download the 'full' "
                                       'package, which contains generated tests.').format(shown))

        zip_path = os.path.join(tmp_dir, (self.code() or 'tests') + '.zip')
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as out:
            for t in tests:
                out.writestr(names[t['index']][0], self.read(format_pattern(input_pattern, t['index'])))
                out.writestr(names[t['index']][1], self.read(format_pattern(answer_pattern, t['index'])))

        data = TestData(zip_path=zip_path, cases=self._cases(tests, names), partial=self.is_scored(),
                        checker='standard')
        self._map_checker(data)
        self._map_grader(data)
        return data

    def _cases(self, tests, names):
        groups = self.groups()

        if not self.is_scored():
            # ICPC-style package: one point per test.
            if groups:
                self.add_warning(
                    _('Groups have no points, so each test was imported as a separate case worth 1 point.'))
            return [Case('C', *names[t['index']], points=1) for t in tests]

        def points(value):
            rounded = math.floor((value or 0) + 0.5)
            if value and rounded != value:
                self.add_warning(_('Points must be integers here: {0} was rounded to {1}.').format(value, rounded))
            return rounded

        cases = []
        done = set()
        for position, t in enumerate(tests):
            group = groups.get(t['group']) if t['group'] else None
            if group is None or group['policy'] != 'complete-group':
                cases.append(Case('C', *names[t['index']], points=points(t['points'])))
                continue
            if t['group'] in done:
                continue
            done.add(t['group'])

            members = [u for u in tests if u['group'] == t['group']]
            if members != tests[position:position + len(members)]:
                self.add_warning(_("Tests of group '{0}' are not contiguous in Polygon; they were moved together "
                                   'into one batch.').format(t['group']))
            if group['dependencies']:
                self.add_warning(_("Group '{0}' depends on {1}. Dependencies between subtasks are not supported "
                                   'here and were dropped.').format(t['group'], ', '.join(group['dependencies'])))

            total = group['points'] if group['points'] is not None else sum(u['points'] or 0 for u in members)
            cases.append(Case('S', points=points(total)))
            cases.extend(Case('C', *names[u['index']]) for u in members)
            cases.append(Case('E'))
        return cases

    def _map_checker(self, data):
        checker = self.checker
        if checker is None:
            self.add_warning(_('The package has no checker; the standard checker is used.'))
            return

        # An interactive problem's checker reads the interactor's output, which
        # the judge only does for a custom checker, so keep Polygon's checker.
        native = NATIVE_CHECKERS.get(checker.get('name')) if self.interactor is None else None
        if native:
            data.checker, data.checker_args = native
            return

        if checker.get('type') != 'testlib':
            self.add_warning(
                _("The checker type is '{0}', not testlib; check it carefully.").format(checker.get('type')))
        source = checker.find('source')
        path = source.get('path') if source is not None else ''
        if not path.lower().endswith('.cpp') or not self.has(path):
            self.add_warning(_("Checker source '{0}' is not a .cpp file in the package; the standard checker "
                               'is used.').format(path))
            return
        data.checker = 'bridged'
        data.custom_checker = path
        data.checker_args = {'files': posixpath.basename(path), 'lang': judge_language(source.get('type')),
                             'type': 'testlib'}

    def _map_grader(self, data):
        if self.interactor is not None:
            source = self.interactor.find('source')
            path = source.get('path') if source is not None else ''
            if path.lower().endswith('.cpp') and self.has(path):
                data.grader = 'interactive'
                data.custom_grader = path
            else:
                self.add_warning(_("Interactor source '{0}' is not a .cpp file in the package; set up the "
                                   'interactor yourself.').format(path))
        elif self.graders:
            graders = [p for p in self.graders if p.lower().endswith('.cpp') and self.has(p)]
            headers = [p for p in self.headers if self.has(p)]
            if graders and headers:
                data.grader = 'signature'
                data.custom_grader, data.custom_header = graders[0], headers[0]
                if len(self.graders) > 1 or len(self.headers) > 1:
                    self.add_warning(_('The package has several grader files; only {0} and {1} were used.').format(
                        posixpath.basename(graders[0]), posixpath.basename(headers[0])))
            else:
                self.add_warning(_('The package has a grader but no header file; set up the grader yourself.'))

        def is_std(name):
            return name in ('', 'stdin', 'stdout')

        if is_std(self.input_file) and is_std(self.output_file):
            return
        files = (self.input_file or 'stdin', self.output_file or 'stdout')
        if data.grader != 'standard':
            self.add_warning(_('File input/output ({0}, {1}) cannot be combined with this grader and was '
                               'ignored.').format(*files))
        elif is_std(self.input_file) or is_std(self.output_file):
            self.add_warning(_('Only one of input/output uses a file ({0}, {1}); the judge needs both, so standard '
                               'IO was kept.').format(*files))
        else:
            data.grader_args = {'io_method': 'file', 'io_input_file': self.input_file,
                                'io_output_file': self.output_file}

    # ------------------------------------------------------------------
    # Statement

    def statement_languages(self):
        languages = [s.get('language') for s in self.xml.findall('statements/statement')
                     if s.get('type') == 'application/x-tex']
        prefix = self.path('statements/')
        for name in self.zip.namelist():
            m = re.match(re.escape(prefix) + r'([^/.][^/]*)/problem-properties\.json$', name)
            if m and m.group(1) not in languages:
                languages.append(m.group(1))
        return [lang for lang in languages if self.has('statements/%s/problem-properties.json' % lang)]

    def statement(self, language, upload_image):
        """Returns (name, markdown, has_tutorial) for one statement language."""
        folder = 'statements/%s/' % language
        props = json.loads(self.read(folder + 'problem-properties.json').decode('utf-8'))
        titles = SECTION_TITLES.get(language, SECTION_TITLES['english'])

        parts = [tex_to_markdown(props.get('legend'))]
        for key in ('input', 'output', 'interaction', 'scoring'):
            body = props.get(key)
            if body and body.strip():
                parts.append('## %s\n\n%s' % (titles[key], tex_to_markdown(body)))
        for i, sample in enumerate(props.get('sampleTests') or [], start=1):
            parts.append('## %s\n\n```\n%s\n```' % (titles['sample_input'].format(i), sample['input'].strip()))
            parts.append('## %s\n\n```\n%s\n```' % (titles['sample_output'].format(i), sample['output'].strip()))
        if props.get('notes') and props['notes'].strip():
            parts.append('## %s\n\n%s' % (titles['notes'], tex_to_markdown(props['notes'])))
        markdown = '\n\n'.join(p.strip() for p in parts if p and p.strip()) + '\n'

        # Images: ![image](file.png) and <img src="file.png">, relative to the statement folder.
        images = set(re.findall(r'!\[image\]\((.+?)\)', markdown))
        images |= set(m.group(2) for m in re.finditer(r'<\s*img[^>]+src\s*=\s*(["\'])(.*?)\1', markdown))
        for image in images:
            path = posixpath.normpath(folder + image)
            if not self.has(path):
                self.add_warning(
                    _("Image '{0}' is referenced by the statement but missing from the package.").format(image))
                continue
            markdown = markdown.replace('](%s)' % image, '](%s)' % upload_image(path))
            markdown = re.sub(r'(src\s*=\s*["\'])%s(["\'])' % re.escape(image),
                              lambda m: m.group(1) + upload_image(path) + m.group(2), markdown)

        name = props.get('name') or self.names.get(language, '')
        return name, markdown, bool((props.get('tutorial') or '').strip())


def _site_language(language):
    code = SITE_LANGUAGES.get(language)
    return code if code in dict(settings.LANGUAGES) else None


@transaction.atomic
def import_polygon_package(package, code=None, *, name=None, points=0.0, group=None, types=(),
                           author=None, statement_language=None):
    """Creates a problem, its statement(s) and test data from a Polygon package.

    Returns (problem, warnings). Raises PolygonImportError when the package
    cannot be imported; nothing is written in that case.
    """
    pkg = PolygonPackage(package)
    code = code or pkg.code()
    Problem._meta.get_field('code').run_validators(code)
    if Problem.objects.filter(code=code).exists():
        raise PolygonImportError(_('A problem with code {0} already exists.').format(code))

    image_cache = {}

    def upload_image(path):
        data = pkg.read(path)
        digest = hashlib.sha1(data).hexdigest()
        if digest not in image_cache:
            image = ContentFile(data, name=posixpath.basename(path))
            image_cache[digest] = json.loads(django_uploader(image))['link']
        return image_cache[digest]

    # Statement: the chosen (or preferred) language is the main description,
    # the others become translations when the site has that language.
    languages = pkg.statement_languages()
    main = statement_language if statement_language in languages else \
        next((lang for lang in PREFERRED_LANGUAGES if lang in languages), languages[0] if languages else None)
    description, translations = '', []
    if main is None:
        pkg.add_warning(_('The package has no statement; write the description yourself.'))
    else:
        main_name, description, tutorial = pkg.statement(main, upload_image)
        name = name or main_name
        if tutorial:
            pkg.add_warning(_('The package contains a tutorial; it was not imported (add it as an editorial).'))
        for language in languages:
            if language == main:
                continue
            site_language = _site_language(language)
            if site_language is None:
                pkg.add_warning(_("The statement in '{0}' was not imported: the site has no such language.").format(
                    language))
                continue
            translations.append((site_language,) + pkg.statement(language, upload_image)[:2])
    if not name:
        name = pkg.names.get(main) or next(iter(pkg.names.values()), '') or code

    with tempfile.TemporaryDirectory() as tmp_dir:
        data = pkg.test_data(tmp_dir)
        try:
            problem = _create_problem(pkg, data, code, name, description, translations, points, group, types,
                                      author)
        except Exception:
            # The transaction undoes the rows, but not the files already
            # written to the problem data folder.
            shutil.rmtree(problem_data_storage.path(code), ignore_errors=True)
            raise

    return problem, pkg.warnings


def _create_problem(pkg, data, code, name, description, translations, points, group, types, author):
    group = group or ProblemGroup.objects.order_by('id').first()  # Uncategorized
    problem = Problem(
        code=code,
        name=name[:100],
        description=description,
        time_limit=pkg.time_limit or 1.0,
        memory_limit=pkg.memory_limit or 262144,
        points=points,
        partial=data.partial,
        group=group,
        date=timezone.now(),
    )
    problem.save()
    problem.allowed_languages.set(Language.objects.filter(include_in_problem=True))
    problem.types.set(types)
    if author is not None:
        problem.authors.add(author)

    for language, translated_name, translated_description in translations:
        ProblemTranslation(problem=problem, language=language, name=(translated_name or name)[:100],
                           description=translated_description).save()

    problem_data = ProblemData(problem=problem, checker=data.checker, grader=data.grader,
                               checker_args=json.dumps(data.checker_args) if data.checker_args else '',
                               grader_args=json.dumps(data.grader_args) if data.grader_args else '')
    with open(data.zip_path, 'rb') as f:
        problem_data.zipfile.save(os.path.basename(data.zip_path), File(f), save=False)
    for field_name, path in (('custom_checker', data.custom_checker), ('custom_grader', data.custom_grader),
                             ('custom_header', data.custom_header)):
        if path:
            getattr(problem_data, field_name).save(posixpath.basename(path), ContentFile(pkg.read(path)),
                                                   save=False)
    problem_data.save()

    for order, case in enumerate(data.cases, start=1):
        ProblemTestCase(dataset=problem, order=order, type=case.type, input_file=case.input_file,
                        output_file=case.output_file, points=case.points, is_pretest=False).save()

    ProblemDataCompiler.generate(problem, problem_data, problem.cases.order_by('order'),
                                 zipfile.ZipFile(data.zip_path).namelist())
    problem_data.refresh_from_db()
    if problem_data.feedback:
        raise PolygonImportError(_('The test data could not be compiled: {0}').format(problem_data.feedback))
    return problem
