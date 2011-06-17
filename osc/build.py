"""This modules provides classes and methods for build related stuff.

To access the remote build data use the class BuildResult.
"""

from lxml import etree, objectify

from osc.remote import RemoteFile, RWRemoteFile
from osc.core import Osc

def _get_parser():
    """Returns a parser object which uses OscElementClassLookup as
    the lookup class.

    """
    parser = objectify.makeparser()
    lookup = OscElementClassLookup()
    parser.set_element_class_lookup(lookup)
    return parser

class OscElementClassLookup(etree.PythonElementClassLookup):
    """A data element should be represented by a StringElement"""

    def __init__(self):
        fallback = objectify.ObjectifyElementClassLookup()
        super(OscElementClassLookup, self).__init__(fallback=fallback)

    def lookup(self, doc, root):
        if root.tag == 'status':
            return Status
        elif root.tag == 'binarylist':
            return BinaryList
        elif root.tag == 'binary':
            return Binary
        return None

class Status(objectify.ObjectifiedElement):
    """Represents a status tag"""

    def __getattr__(self, name):
        try:
            return super(Status, self).__getattr__(name)
        except AttributeError:
            if name == 'details':
                return ''
            raise

class BinaryList(objectify.ObjectifiedElement):
    """Represents a binarylist + some additional data"""
    SCHEMA = ''

    @staticmethod
    def create(project, repository, arch, package='_repository', **kwargs):
        """Creates a new BinaryList object.

        project, repository and arch parameters are required.

        Keyword arguments:
        package -- specify an optional package (default: '_repository')
        kwargs -- optional parameters for the http request (like query
                  parameters)

        """
        path = '/build/%s/%s/%s/%s' % (project, repository, arch, package)
        request = Osc.get_osc().get_reqobj()
        if not 'schema' in kwargs:
            kwargs['schema'] = BinaryList.SCHEMA
        f = request.get(path, **kwargs)
        parser = _get_parser()
        bl = objectify.fromstring(f.read(), parser=parser)
        bl.set('project', project)
        bl.set('package', package)
        bl.set('repository', repository)
        bl.set('arch', arch)
        return bl

class Binary(objectify.ObjectifiedElement):
    """Represents a binary tag + some additional data"""

    def file(self, **kwargs):
        """Returns a RemoteFile object.
        
        This can be used to read/save the binary file.

        Keyword arguments:
        **kwargs -- optional parameters for the http request

        """
        path = '/build/%(project)s/%(repository)s/%(arch)s/%(package)s/' \
               '%(fname)s'
        parent = self.getparent()
        data = {'project': parent.get('project'),
                'package': parent.get('package'),
                'repository': parent.get('repository'),
                'arch': parent.get('arch'), 'fname': self.get('filename')}
        path = path % data
        return RemoteFile(path, **kwargs)

class BuildResult(object):
    """Provides methods to access the remote build result"""

    def __init__(self, project, package='', repository='', arch=''):
        """Constructs a new object.

        project is the project for which the build result should be
        retrieved.

        Keyword arguments:
        package -- limit results to this package (default: '')
        repository -- limit results to this repository (default: '')
        arch -- limit results to this arch (default: '')

        Note: some commands require a package or repository or arch
        parameter. If those weren't specified here it's possible to
        specify them when the specific method is invoked (if they're
        not present a ValueError is raised).

        """
        self.project = project
        self.package = package
        self.repository = repository
        self.arch = arch

    def result(self, **kwargs):
        """Get the build result.

        Keyword arguments:
        package -- limit results to package (default: '')
        repository -- limit results repository
        arch -- limit results to arch
        kwargs -- optional arguments for the http request
        Note: package, repository and arch may override the
        current package, repository and arch instance attributes.

        """
        package = kwargs.pop('package', self.package)
        repository = kwargs.pop('repository', self.repository)
        arch = kwargs.pop('arch', self.arch)
        request = Osc.get_osc().get_reqobj()
        path = "/build/%s/_result" % self.project
        f = request.get(path, package=package, repository=repository,
                        arch=arch)
        parser = _get_parser()
        results = objectify.fromstring(f.read(), parser=parser)
        return results

    def _prepare_kwargs(self, kwargs, *required):
        for i in required:
            if not i in kwargs and getattr(self, i, ''):
                kwargs[i] = getattr(self, i)
            else:
                raise ValueError("missing parameter: %s" % i)

    def binarylist(self, **kwargs):
        """Get the binarylist.

        Keyword arguments:
        kwargs -- optional arguments for the http request

        """
        return BinaryList.create(self.project, self.repository, self.arch,
                                 self.package or '_repository', **kwargs)

    def log(self, **kwargs):
        """Get the buildlog.

        If repository, arch or package weren't specified during the __init__
        call a ValueError is raised.

        Keyword arguments:
        **kwargs -- optional parameters for the http request

        """
        if not (self.repository and self.arch and self.package):
            raise ValueError("repository, arch, package are mandatory for log")
        request = Osc.get_osc().get_reqobj()
        path = '/build/%s/%s/%s/%s/_log' % (self.project, self.repository,
                                            self.arch, self.package)
        return RWRemoteFile(path, **kwargs)
        