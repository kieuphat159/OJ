import shutil

from django.core.files.base import ContentFile
from django.test import TestCase

from judge.models import ProblemData, problem_data_storage
from judge.models.tests.util import create_problem


class ProblemDataZipfileTestCase(TestCase):
    def setUp(self):
        self.problem = create_problem(code='zipreplace')
        self.data = ProblemData.objects.create(problem=self.problem)
        self.data.zipfile.save('old.zip', ContentFile(b'old'))

    def tearDown(self):
        shutil.rmtree(problem_data_storage.path(self.problem.code), ignore_errors=True)

    def replace(self, name, content):
        # What ModelForm.save() does with an uploaded file.
        data = ProblemData.objects.get(pk=self.data.pk)
        data.zipfile = ContentFile(content, name=name)
        data.save()
        return ProblemData.objects.get(pk=self.data.pk)

    def test_replace_with_another_name(self):
        data = self.replace('new.zip', b'new')
        self.assertEqual(data.zipfile.name, 'zipreplace/new.zip')
        self.assertEqual(data.zipfile.read(), b'new')
        self.assertFalse(problem_data_storage.exists('zipreplace/old.zip'))

    def test_replace_with_same_name(self):
        data = self.replace('old.zip', b'new')
        self.assertEqual(data.zipfile.name, 'zipreplace/old.zip')
        self.assertEqual(data.zipfile.read(), b'new')

    def test_clear(self):
        data = ProblemData.objects.get(pk=self.data.pk)
        data.zipfile = None
        data.save()
        self.assertFalse(ProblemData.objects.get(pk=self.data.pk).zipfile)
        self.assertFalse(problem_data_storage.exists('zipreplace/old.zip'))

    def test_saving_again_keeps_file(self):
        data = ProblemData.objects.get(pk=self.data.pk)
        data.save()
        data.save()
        self.assertTrue(problem_data_storage.exists('zipreplace/old.zip'))
