"""Provides a base class from which all xml based remote models should
inherit.

Example usage:
 prj = RemoteProject.find('project_name')
 # add a new maintainer
 prj.add_person(userid='foobar', role='maintainer')
 # remove the first repository
 del prj.repository[0]
 # send changes back to the server
 prj.store()
"""

import logging
from tempfile import NamedTemporaryFile
import os
from cStringIO import StringIO

from lxml import etree, objectify

from osc.core import Osc

__all__ = ['RemoteModel', 'RemoteProject', 'RemotePackage', 'Request']

def _get_http_method(request_obj, method):
    """Get the requested http method from the http object (internal)"""
    meth = getattr(request_obj, method.lower(), None)
    if meth is None:
        msg = "http request object doesn't support method: %s" % method
        raise ValueError(msg)
    return meth

class ElementFactory(object):
    """Adds a new element called "tag" to the provided "element"

    Note: it tries to add the new element next to existing elements
    (if they exists). Otherwise it's simply appended to "element".

    """

    def __init__(self, element, tag):
        """Constructs a new ElementFactory object.

        "element" is the element to which we will add a new element
        which is called "tag".

        """
        self._element = element
        self._tag = tag

    def _add_data(self, data, attribs):
        data_elm = objectify.DataElement(data)
        target_elm = self._element.makeelement(self._tag, **attribs)
        existing = self._element.findall(self._tag)
        if existing:
            # add it next to the latest element
            existing[-1].addnext(target_elm)
        else:
            self._element.append(target_elm)
        # this is everything but thread-safe (OTOH it's highly unlikely
        # that the same model is shared (and modified) between threads)
        getattr(self._element, self._tag).__setitem__(len(existing), data_elm)
        return data_elm

    def _add_tree(self, attribs):
        elm = self._element.makeelement(self._tag, **attribs)
        existing = self._element.findall(self._tag)
        if existing:
            # add it next to the latest element
            existing[-1].addnext(elm)
        else:
            self._element.append(elm)
        return elm

    def __call__(self, *args, **kwargs):
        """Add the new element"""
        if not args:
            return self._add_tree(kwargs)
        return self._add_data(args[0], kwargs)


class OscElement(objectify.ObjectifiedElement):
    """Base class for all osc elements.

    This class overrides __getattr__ in order to return our special
    method object if name matches the pattern: add_tagname.
    In this case an instance is returned which adds a new element
    "tagname" to _this_ element.

    """
    def __getattr__(self, name):
        data = name.split('_', 1)
        if len(data) == 1 or not data[0] == 'add':
            return super(OscElement, self).__getattr__(name)
        factory = ElementFactory(self, data[1])
        return factory

class OscElementClassLookup(etree.PythonElementClassLookup):
    """A data element should be represented by a StringElement"""

    def __init__(self):
        fallback = objectify.ObjectifyElementClassLookup(tree_class=OscElement)
        super(OscElementClassLookup, self).__init__(fallback=fallback)

    def lookup(self, doc, root):
        # use StringElement if we have text and no children
        if root.text and not root:
            return objectify.StringElement
        return None


class RemoteModel(object):
    """Base class for all remote models"""

    def __init__(self, tag='', xml_data='', schema='', store_schema='',
                 **kwargs):
        """Creates a new remote model object.

        Keyword arguments:
        tag -- root tag for this model (default: '')
        xml_data -- xml string which represents this model (default: '')
        schema -- path to schema for this model (default: '')
        store_schema -- path to the schema file which is used to validate the
                        response after storing the xml (default: '')
        kwargs -- attributes for the root tag

        Note: if tag _and_ xml_data is specified, tag is ignored

        """
        super(RemoteModel, self).__init__()
        self._schema = schema
        self._store_schema = store_schema
        self._logger = logging.getLogger(__name__)
#        if tag and xml_data:
#            raise ValueError("Either specificy tag or xml_data but not both")
        if xml_data:
            self._read_xml_data(xml_data)
        elif tag:
            self._xml = self._get_parser().makeelement(tag, **kwargs)
        else:
            raise ValueError("Either tag or xml_data is required")

    def _read_xml_data(self, xml_data):
        parser = self._get_parser()
        self._xml = objectify.fromstring(xml_data, parser=parser)

    def _get_parser(self):
        """Returns a parser object which is configured with OscElement as the
        default tree_class.

        """
        parser = objectify.makeparser()
        lookup = OscElementClassLookup()
        parser.set_element_class_lookup(lookup)
        return parser

    def __getattr__(self, name):
        return getattr(self._xml, name)

    def __setattr__(self, name, value):
        if name.startswith('_'):
            self.__dict__[name] = value
            return value
        return setattr(self._xml, name, value)

    def __delattr__(self, name):
        if name.startswith('_'):
            return delattr(self.__dict__, name)
        return delattr(self._xml, name)

    def tostring(self):
        """Returns object as xml string"""
        objectify.deannotate(self._xml)
        etree.cleanup_namespaces(self._xml)
        return etree.tostring(self._xml, pretty_print=True)

    # XXX: should we introduce a custom exception if validation fails?
    def validate(self):
        """Validates xml against a schema.

        If validation is disabled, False is returned.
        If validation succeeds True is returned.
        If validation fails an exception is raised (etree.DocumentInvalid)

        """
        if not self._schema:
            return False
        self._logger.debug("validate modle against schema: %s", self._schema)
        if self._schema.endswith('.rng'):
            schema = etree.RelaxNG(file=self._schema)
        elif self._schema.endswith('.xsd'):
            schema = etree.XMLSchema(file=self._schema)
        else:
            raise ValueError('unsupported schema file')
        schema.assertValid(self._xml)
        return True

    def store(self, path, method='PUT', **kwargs):
        """Store the xml to the server.

        Keyword arguments:
        path -- the url path (default: '')
        method -- the http method (default: 'PUT')
        kwargs -- parameters for the http request (like query parameters,
                  post data etc.)

        """
        self.validate()
        request = Osc.get_osc().get_reqobj()
        http_method = _get_http_method(request, method)
        if not 'data' in kwargs:
            kwargs['data'] = self.tostring()
        if not 'schema' in kwargs:
            kwargs['schema'] = self._store_schema
        return http_method(path, **kwargs)

    @classmethod
    def find(cls, path, method='GET', **kwargs):
        """Get the remote model from the server.

        Keyword arguments:
        path -- the url path (default: '')
        method -- the http method (default: 'GET')
        kwargs -- parameters for the http request (like query parameters,
                  schema etc.)

        """
        request = Osc.get_osc().get_reqobj()
        http_method = _get_http_method(request, method)
        xml_data = http_method(path, **kwargs).read()
        return cls(xml_data=xml_data)

class RemoteProject(RemoteModel):
    PATH = '/source/%(project)s/_meta'
    SCHEMA = ''
    # used to validate the response after the xml is stored
    PUT_RESPONSE_SCHEMA = ''

    def __init__(self, name='', **kwargs):
        store_schema = RemoteProject.PUT_RESPONSE_SCHEMA
        super(RemoteProject, self).__init__(tag='project', name=name,
                                            schema=RemoteProject.SCHEMA,
                                            store_schema=store_schema,
                                            **kwargs)

    @classmethod
    def find(cls, project, **kwargs):
        path = RemoteProject.PATH % {'project': project}
        if not 'schema' in kwargs:
            kwargs['schema'] = RemoteProject.SCHEMA
        return super(RemoteProject, cls).find(path, **kwargs)

    def store(self, **kwargs):
        path = RemoteProject.PATH % {'project': self.get('name')}
        return super(RemoteProject, self).store(path, method='PUT', **kwargs)


class RemotePackage(RemoteModel):
    PATH = '/source/%(project)s/%(package)s/_meta'
    SCHEMA = ''
    # used to validate the response after the xml is stored
    PUT_RESPONSE_SCHEMA = ''

    def __init__(self, project='', name='', **kwargs):
        # project is not required
        if project:
            kwargs['project'] = project
        store_schema = RemotePackage.PUT_RESPONSE_SCHEMA
        super(RemotePackage, self).__init__(tag='package', name=name,
                                            schema=RemotePackage.SCHEMA,
                                            store_schema=store_schema,
                                            **kwargs)

    @classmethod
    def find(cls, project, package, **kwargs):
        path = RemotePackage.PATH % {'project': project, 'package': package}
        if not 'schema' in kwargs:
            kwargs['schema'] = RemotePackage.SCHEMA
        return super(RemotePackage, cls).find(path, **kwargs)

    def store(self, **kwargs):
        path = RemotePackage.PATH % {'project': self.get('project'),
                                     'package': self.get('name')}
        return super(RemotePackage, self).store(path, method='PUT', **kwargs)


class Request(RemoteModel):
    GET_PATH = '/request/%(reqid)s'
    SCHEMA = ''

    def __init__(self, **kwargs):
        super(Request, self).__init__(tag='request', schema=Request.SCHEMA,
                                      store_schema=Request.SCHEMA, **kwargs)

    @classmethod
    def find(cls, reqid, **kwargs):
        path = Request.GET_PATH % {'reqid': reqid}
        if not 'schema' in kwargs:
            kwargs['schema'] = Request.SCHEMA
        return super(Request, cls).find(path, **kwargs)

    def store(self, **kwargs):
        path = '/request'
        f = super(Request, self).store(path, method='POST',
                                       cmd='create', **kwargs)
        self._read_xml_data(f.read())


class RemoteFile(object):
    """Provides basic methods to read and to store a remote file.

    Note: it isn't possible to seek around the file, once the data is
    read it isn't possible to read it again. If you need to seeking and
    more advanced file support use RWRemoteFile.

    """

    def __init__(self, path, stream_bufsize=8192, method='GET', **kwargs):
        """Constructs a new RemoteFile object.

        path is the remote path which is used for the http request.

        Keyword arguments:
        stream_bufsize -- read bytes which are returned when iterating over
                          this object (default: 8192)
        method -- the http method which is used for the request (default: GET)
        kwargs -- optional arguments for the http request (like query
                  parameters)

        """
        self.path = path
        self.stream_bufsize = stream_bufsize
        self.method = 'GET'
        self.kwargs = kwargs
        self._fobj = None

    def _read(self):
        request = Osc.get_osc().get_reqobj()
        http_method = _get_http_method(request, self.method)
        self._fobj = http_method(self.path, **self.kwargs)

    def read(self, size=-1):
        """Reads size bytes.

        If the size argument is omitted or negative read everything.

        """
        if self._fobj is None:
            self._read()
        return self._fobj.read(size)

    def close(self):
        if self._fobj is not None:
            self._fobj.close()

    def _write_to(self, fobj, size):
        for data in self.__iter__(size):
            fobj.write(data)

    def write_to(self, dest, size=-1):
        """Write file to dest.

        If dest is a file-like object (that is it has a write(buf) method)
        it's write method will be called. If dest is a filename the data will
        be written to it (existing files will be overwritten, if the file
        doesn't exist it will be created).

        Keyword arguments:
        size -- write only size bytes (default: -1 (means write everything))

        """
        if hasattr(dest, 'write'):
            self._write_to(dest, size)
            return
        dirname = os.path.dirname(dest)
        if os.path.exists(dest) and not os.access(dest, os.W_OK):
            raise ValueError("invalid dest filename: %s is not writable" %
                             dest)
        elif not os.path.exists(dirname):
            # or should we check that it is really a dir?
            raise ValueError("invalid dest filename: dir %s does not exist" %
                             dir)
        elif not os.access(dirname, os.W_OK):
            raise ValueError("invalid dest filename: dir %s is not writable" %
                             dir)
        tmp_filename = ''
        try:
            tmp = NamedTemporaryFile(dir=dirname)
            tmp_filename = tmp.file
            self._write_to(tmp, size)
            tmp.close()
            os.rename(tmp_filename, dest)
            tmp_filename = ''
        finally:
            if tmp_filename and os.path.isfile(tmp_filename):
                os.unlink(tmp_filename)

    def __iter__(self, size=-1):
        """Iterates over the file"""
        # this rsize handling is needed by write_to
        rsize = self.stream_bufsize
        if size >= 0 and size < rsize:
            rsize = size
        data = self.read(rsize)
        while data:
            yield data
            size -= len(data)
            if size == 0:
                break
            data = self.read(rsize)

class RWRemoteFile(RemoteFile):
    """Provides more advanced methods for reading and writing a remote file.

    It's possible to read, write and seek the file. Additionally convenience
    methods like readline, readlines are also provided.
    Note: if you write data to this file the data will be stored in memory
    until the file is closed (at this point all data is written to the server).
    This class is mainly used for small text files.

    """

    def __init__(self, path, append=False, wb_method='PUT', wb_path='',
                 schema='', **kwargs):
        """Constructs a new RWRemoteFile object.

        path is the remote path which is used for the http request
        (to get the file, if needed).

        Keyword arguments:
        append -- append data to the existing file instead of overwriting it
                   (default: False)
        wb_method -- write back method for the http request (default: PUT)
        wb_path -- path which is used to store the file back (default: path)
        schema -- filename to xml schema which is used to validate the repsonse
                  after the writeback
        kwargs -- see class RemoteFile 

        """
        super(RWRemoteFile, self).__init__(path, **kwargs)
        self.append = append
        self.wb_method = wb_method
        self.wb_path = wb_path
        self._schema = schema
        self._sio = None

    def __getattr__(self, name):
        if self._sio is None:
            self._init_sio(name != 'write')
        return getattr(self._sio, name)

    def _init_sio(self, read_required, size=-1):
        data = ''
        if read_required or self.append:
            data = super(RWRemoteFile, self).read(size)
        self._sio = StringIO()
        self._sio.write(data)
        self._sio.seek(0, os.SEEK_SET)

    def read(self, size=-1):
        if self._sio is None:
            self._init_sio(True, size)
        pos = self._sio.tell()
        # TODO: does this make sense?
        buf = buffer(self._sio.getvalue())
        tmp = StringIO(buf)
        tmp.seek(pos, os.SEEK_SET)
        return tmp.read(size)

    def close(self, **kwargs):
        """Close this file and write data back to server.

        The writeback only happens if at least one time the write method
        was invoced.

        Keyword arguments:
        kwargs -- optional parameters for the writeback http request (like
                  query parameters)

        """
        request = Osc.get_osc().get_reqobj()
        http_method = _get_http_method(request, self.wb_method)
        data = self._sio.getvalue()
        if not 'schema' in kwargs:
            kwargs['schema'] = self._schema
        http_method(self.path, data=data, **kwargs)