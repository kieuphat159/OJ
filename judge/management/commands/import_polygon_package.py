from django.contrib.sites.models import Site
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse
from django.utils import translation

from judge.utils.polygon import PolygonImportError, import_polygon_package


class Command(BaseCommand):
    help = 'import Codeforces Polygon full package'

    def add_arguments(self, parser):
        parser.add_argument('package', help='path to package in zip format')
        parser.add_argument('code', nargs='?', help='problem code (default: the package short name)')
        parser.add_argument('--language', help='Polygon statement language used as the main statement, '
                                               'e.g. vietnamese (default: vietnamese, then english)')
        parser.add_argument('--points', type=float, default=0.0, help='problem points (default: 0)')

    def handle(self, *args, **options):
        # Force using English
        translation.activate('en')

        try:
            with open(options['package'], 'rb') as package:
                problem, warnings = import_polygon_package(package, options['code'], points=options['points'],
                                                           statement_language=options['language'])
        except (PolygonImportError, ValidationError) as e:
            raise CommandError(e)

        for warning in warnings:
            self.stdout.write(self.style.WARNING(warning))
        problem_url = 'https://' + Site.objects.first().domain + reverse('problem_detail', args=[problem.code])
        self.stdout.write(f'Imported successfully. View problem at {problem_url}')
