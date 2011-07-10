import os
import unittest
import tempfile
import shutil

from osc.wc.project import Project
from osc.wc.util import WCInconsistentError
from test.osctest import OscTest
from test.httptest import GET

def suite():
    return unittest.makeSuite(TestProject)

class TestProject(OscTest):
    def __init__(self, *args, **kwargs):
        kwargs['fixtures_dir'] = 'wc/test_project_fixtures'
        super(TestProject, self).__init__(*args, **kwargs)

    def test1(self):
        """init a project dir"""
        tmpdir = None
        try:
            tmpdir = tempfile.mkdtemp()
            prj = Project.init(tmpdir, 'openSUSE:Tools',
                               'https://api.opensuse.org')
            prj_fname = os.path.join(tmpdir, '.osc', '_project')
            self.assertTrue(os.path.exists(prj_fname))
            self.assertEqual(open(prj_fname, 'r').read(), 'openSUSE:Tools\n')
            pkgs_fname = os.path.join(tmpdir, '.osc', '_packages')
            self.assertTrue(os.path.exists(pkgs_fname))
            self.assertEqual(open(pkgs_fname, 'r').read(), '<packages/>\n')
            apiurl_fname = os.path.join(tmpdir, '.osc', '_apiurl')
            self.assertTrue(os.path.exists(apiurl_fname))
            self.assertEqual(open(apiurl_fname, 'r').read(),
                            'https://api.opensuse.org\n')
            data_dir = os.path.join(tmpdir, '.osc', 'data')
            self.assertTrue(os.path.exists(data_dir))
            self.assertEqual(prj.name, 'openSUSE:Tools')
            self.assertEqual(prj.apiurl, 'https://api.opensuse.org')
            self.assertTrue(len(prj.packages()) == 0)
        finally:
            if tmpdir is not None:
                shutil.rmtree(tmpdir)

    def test2(self):
        """init existing wc"""
        path = self.fixture_file('prj1')
        self.assertRaises(ValueError, Project.init, path,
                          'foo', 'http://localhost')

    def test3(self):
        """read project"""
        path = self.fixture_file('prj1')
        prj = Project(path)
        self.assertEqual(prj.name, 'prj1')
        self.assertEqual(prj.apiurl, 'http://localhost')
        self.assertTrue(len(prj.packages()) == 0)

    def test4(self):
        """read invalid project (missing storefiles)"""
        path = self.fixture_file('inv1')
        self.assertRaises(WCInconsistentError, Project, path)

    def test5(self):
        """read invalid project (corrupt xml)"""
        path = self.fixture_file('inv2')
        self.assertRaises(WCInconsistentError, Project, path)

    def test6(self):
        """test _status"""
        path = self.fixture_file('prj2')
        prj = Project(path)
        self.assertEqual(prj._status('foo'), ' ')
        self.assertEqual(prj._status('bar'), 'A')
        self.assertEqual(prj._status('abc'), 'D')
        self.assertEqual(prj._status('xxx'), '!')
        # del is not ! because it's also marked for deletion
        self.assertEqual(prj._status('del'), 'D')
        self.assertEqual(prj._status('asdf'), '?')

    @GET('http://localhost/source/prj2', file='prj2_list1.xml')
    def test7(self):
        """test _calculate_updateinfo"""
        path = self.fixture_file('prj2')
        prj = Project(path)
        uinfo = prj._calculate_updateinfo()
        self.assertEqual(uinfo.candidates, ['foo', 'abc'])
        self.assertEqual(uinfo.added, ['osc'])
        self.assertEqual(uinfo.deleted, ['del'])
        self.assertEqual(uinfo.conflicted, ['xxx'])

    @GET('http://localhost/source/prj2', file='prj2_list2.xml')
    def test8(self):
        """test _calculate_updateinfo 2"""
        path = self.fixture_file('prj2')
        prj = Project(path)
        uinfo = prj._calculate_updateinfo()
        self.assertEqual(uinfo.candidates, ['foo'])
        self.assertEqual(uinfo.added, ['osc'])
        self.assertEqual(uinfo.deleted, ['abc', 'xxx', 'del'])
        # local state: A
        self.assertEqual(uinfo.conflicted, ['bar'])

    @GET('http://localhost/source/prj2', text='<directory count="0"/>')
    def test9(self):
        """test _calculate_updateinfo 3 (empty package list)"""
        path = self.fixture_file('prj2')
        prj = Project(path)
        self.assertEqual(prj._status('bar'), 'A')
        uinfo = prj._calculate_updateinfo()
        self.assertEqual(uinfo.candidates, [])
        self.assertEqual(uinfo.added, [])
        self.assertEqual(uinfo.deleted, ['foo', 'abc', 'xxx', 'del'])

#    def test10(self):
#        """test add_package"""
#        path = self.fixture_file('project')
#        prj = Project(path)
#        prj.add_package('

if __name__ == '__main__':
    unittest.main()
