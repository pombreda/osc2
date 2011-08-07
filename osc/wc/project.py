"""Class to manage a project working copy."""

import os
import fcntl
import shutil

from lxml import objectify, etree

from osc.wc.base import (WorkingCopy, UpdateStateMixin, CommitStateMixin,
                         PendingTransactionError, FileConflictError)
from osc.wc.package import Package
from osc.wc.util import (wc_read_project, wc_read_apiurl, wc_read_packages,
                         wc_init, wc_write_apiurl, wc_write_project,
                         wc_write_packages, missing_storepaths, wc_lock,
                         WCInconsistentError, wc_is_project, wc_is_package,
                         wc_pkg_data_mkdir, XMLTransactionState, _storedir,
                         _STORE, wc_pkg_data_filename)
from osc.source import Project as SourceProject
from osc.remote import RemotePackage


class PackageUpdateInfo(object):
    """Contains information about an update.

    It provides the following information:
    - added packages (packages which exist on the server
      but not in the local working copy)
    - deleted packages (packages which don't exist on the
      server but are still present in the local wc)
    - conflicted packages (local state '?' and a package with the
      same name exists on the server; local state '!' and package
      still exists on the server)
    - candidate packages (packages which exist on the server
      and in the local wc)

    """

    def __init__(self, candidates, added, deleted, conflicted):
        super(PackageUpdateInfo, self).__init__()
        self.candidates = candidates
        self.added = added
        self.deleted = deleted
        self.conflicted = conflicted


class PackageCommitInfo(object):
    """Contains information about a commit.

    It provides the following information:
    - unchanged packages (packages which didn't change)
    - added packages (packages with state 'A')
    - deleted packages (package with state 'D')
    - modified packages (packages which were modified)
    - conflicted packages (packages with conflicts)

    """
    def __init__(self, unchanged, added, deleted, modified, conflicted):
        self.unchanged = unchanged
        self.added = added
        self.deleted = deleted
        self.modified = modified
        self.conflicted = conflicted


class ProjectUpdateState(XMLTransactionState, UpdateStateMixin):

    def __init__(self, path, uinfo=None, xml_data=None, **states):
        initial_state = UpdateStateMixin.STATE_PREPARE
        super(ProjectUpdateState, self).__init__(path, 'update', initial_state,
                                                 uinfo, xml_data, **states)

    def _listnames(self):
        # it would be sufficient to store only added and deleted
        return ('candidates', 'added', 'deleted', 'conflicted')

    def processed(self, package, new_state=None):
        # directly set state back to STATE_PREPARE
        self.state = UpdateStateMixin.STATE_PREPARE
        super(ProjectUpdateState, self).processed(package, new_state)

    @property
    def info(self):
        """Return the ProjectUpdateInfo object."""
        lists = self._lists()
        return PackageUpdateInfo(**lists)

    @staticmethod
    def rollback(path):
        ustate = ProjectUpdateState.read_state(path)
        if ustate.name != 'update':
            raise ValueError("no update transaction")
        if ustate.state == UpdateStateMixin.STATE_PREPARE:
            ustate.cleanup()
            return True
        return False


class ProjectCommitState(XMLTransactionState, CommitStateMixin):

    def __init__(self, path, cinfo=None, xml_data=None, **states):
        initial_state = CommitStateMixin.STATE_TRANSFER
        super(ProjectCommitState, self).__init__(path, 'commit', initial_state,
                                                 cinfo, xml_data, **states)

    def _listnames(self):
        return ('unchanged', 'added', 'deleted', 'modified', 'conflicted')

    def processed(self, package, new_state=None):
        # set back to STATE_TRANSFER
        self.state = CommitStateMixin.STATE_TRANSFER
        super(ProjectCommitState, self).processed(package, new_state)

    @property
    def info(self):
        """Return the ProjectCommitInfo object."""
        lists = self._lists()
        return PackageCommitInfo(**lists)

    @staticmethod
    def rollback(path):
        cstate = ProjectCommitState.read_state(path)
        if cstate.name != 'commit':
            raise ValueError("no commit transaction")
        if ustate.state == CommitStateMixin.STATE_TRANSFER:
            ustate.cleanup()
            return True
        return False


class Project(WorkingCopy):
    """Represents a project working copy."""

    PACKAGES_SCHEMA = ''

    def __init__(self, path, **kwargs):
        """Constructs a new project object.

        path is the path to the working copy.
        Raises a ValueError exception if path is
        no valid project working copy.
        Raises a WCInconsistentError if the wc's
        metadata is corrupt.

        Keyword arguments:
        kwargs -- see class WorkingCopy for the details

        """
        (meta, xml_data) = self.wc_check(path)
        if meta or xml_data:
            raise WCInconsistentError(path, meta, xml_data)
        self.apiurl = wc_read_apiurl(path)
        self.name = wc_read_project(path)
        with wc_lock(path) as lock:
            self._packages = wc_read_packages(path)
        super(Project, self).__init__(path, ProjectUpdateState,
                                      ProjectCommitState, **kwargs)

    def packages(self, obj=False):
        """Return list of all package names"""
        pkgs = []
        for entry in self._packages:
            pkgs.append(entry.get('name'))
        return pkgs

    def _status(self, pkg):
        """Return the status of package pkg.

        pkg might be an arbitrary str (it doesn't have to
        be an "existing package")

        Status can be one of the following:
        ' ' -- unchanged
        'A' -- pkg will be added to the project
        'D' -- pkg is marked for deletion
        '!' -- pkg is missing (e.g. removed by non-osc command)
        '?' -- pkg is not tracked

        """
        pkg_dir = os.path.join(self.path, pkg)
        exists = os.path.exists(pkg_dir)
        entry = self._packages.find(pkg)
        if entry is None:
            return '?'
        st = entry.get('state')
        if not exists and st != 'D':
            return '!'
        return st

    def has_conflicts(self):
        return []

    def _calculate_updateinfo(self, *packages):
        added = []
        deleted = []
        candidates = []
        conflicted = []
        sprj = SourceProject(self.name)
        remote_pkgs = [pkg.name for pkg in sprj.list()]
        local_pkgs = self.packages()
        for package in remote_pkgs:
            if package in local_pkgs:
                candidates.append(package)
            else:
                added.append(package)
        for package in local_pkgs:
            st = self._status(package)
            pkg = self.package(package)
            if pkg is not None and not pkg.is_updateable():
                conflicted.append(package)
            elif st != 'A' and not package in remote_pkgs:
                deleted.append(package)
        # check for conflicts
        for package in candidates[:]:
            pkg = self.package(package)
            if (self._status(package) in ('A', '!')
                or not pkg.is_updateable()):
                conflicted.append(package)
                candidates.remove(package)
        for package in added[:]:
            path = os.path.join(self.path, package)
            st = self._status(package)
            if st == '?' and os.path.exists(path):
                conflicted.append(package)
                added.remove(package)
        if packages:
            # only consider specified packages
            candidates = [p for p in candidates if p in packages]
            added = [p for p in added if p in packages]
            deleted = [p for p in deleted if p in packages]
            conflicted = [p for p in conflicted if p in packages]
        return PackageUpdateInfo(candidates, added, deleted, conflicted)

    def _clear_uinfo(self, ustate):
        self._clear_info(ustate, 'candidates', 'added', 'deleted',
                         'conflicted')

    def _clear_cinfo(self, cstate):
        self._clear_info(cstate, 'unchanged', 'added', 'deleted',
                         'modified', 'conflicted')

    def _clear_info(self, state, *listnames):
        # do not start any new transaction
        info = state.info
        entry = ''
        for listname in listnames:
            l = getattr(info, listname)
            if l:
                entry = l[0]
                break
        state.clear_info(entry)

    def update(self, *packages):
        """Update project working copy.

        If *packages are specified only the specified
        packages will be updated. Otherwise all packages
        will be updated.

        """
        with wc_lock(self.path) as lock:
            ustate = ProjectUpdateState.read_state(self.path)
            if not self.is_updateable(rollback=True):
                raise PendingTransactionError('commit')
            if (ustate is not None
                and ustate.state == UpdateStateMixin.STATE_UPDATING):
                self._clear_uinfo(ustate)
                self._update(ustate)
            else:
                uinfo = self._calculate_updateinfo(*packages)
                conflicts = uinfo.conflicted
                if conflicts:
                    # a package might be in conflicts because
                    # its is_updateable method returned False
                    raise FileConflictError(conflicts)
                if not self._transaction_begin('prj_update', uinfo):
                    return
                states = dict([(p, self._status(p)) for p in self.packages()])
                ustate = ProjectUpdateState(self.path, uinfo=uinfo, **states)
                self._update(ustate)
                self.notifier.finished('prj_update', aborted=False)

    def _update(self, ustate):
        self._perform_adds(ustate)
        self._perform_deletes(ustate)
        self._perform_candidates(ustate)
        self._packages.merge(ustate.entrystates)

    def _perform_adds(self, ustate):
        uinfo = ustate.info
        tl = self.notifier.listener
        for package in uinfo.added:
            tmp_dir = os.path.join(ustate.location, package)
            storedir = wc_pkg_data_filename(self.path, package)
            if ustate.state == UpdateStateMixin.STATE_PREPARE:
                os.mkdir(storedir)
                pkg = Package.init(tmp_dir, self.name, package,
                                   self.apiurl, storedir,
                                   transaction_listener=tl)
                pkg.update()
                ustate.state = UpdateStateMixin.STATE_UPDATING
            # fixup symlink
            new_dir = os.path.join(self.path, package)
            path = os.path.relpath(storedir, new_dir)
            old_storelink = _storedir(tmp_dir)
            if os.path.isdir(tmp_dir):
                if os.path.exists(old_storelink):
                    os.unlink(old_storelink)
                os.symlink(path, old_storelink)
                os.rename(tmp_dir, new_dir)
            ustate.processed(package, ' ')

    def _perform_deletes(self, ustate):
        uinfo = ustate.info
        global _STORE
        for package in uinfo.deleted:
            # a delete is always possible
            ustate.state = UpdateStateMixin.STATE_UPDATING
            # XXX: None is not a good idea
            self.notifier.begin('update', None)
            self._remove_wc_dir(package, notify=True)
            ustate.processed(package, None)
            self.notifier.finished('update', aborted=False)

    def _perform_candidates(self, ustate):
        uinfo = ustate.info
        tl = self.notifier.listener
        for package in uinfo.candidates:
            pkg = self.package(package, transaction_listener=tl)
            # pkg should never ever be None at this point
            if pkg is None:
                msg = "package \"%s\" is an invalid candidate." % package
                raise ValueError(msg)
            pkg.update()
            ustate.processed(package, ' ')

    def _remove_wc_dir(self, package, notify=False):
        pkg = self.package(package)
        if pkg is not None:
            for filename in pkg.files():
                pkg.remove(filename)
                if notify:
                    self.notifier.processed(filename, None)
            store = os.path.join(pkg.path, _STORE)
            if os.path.exists(store) and os.path.islink(store):
                os.unlink(store)
            filenames = [f for f in os.listdir(pkg.path)]
            if not filenames:
                os.rmdir(pkg.path)
        store = wc_pkg_data_filename(self.path, package)
        if os.path.exists(store):
            shutil.rmtree(store)

    def _calculate_commitinfo(self, *packages):
        unchanged = []
        added = []
        deleted = []
        modified = []
        conflicted = []
        if not packages:
            packages = self.packages()
        for package in packages:
            st = self._status(package)
            pkg = self.package(package)
            if st == 'A':
                added.append(package)
            elif st == 'D':
                deleted.append(package)
            elif pkg is None:
                conflicted.append(package)
            else:
                commitable = pkg.is_commitable()
                mod = pkg.is_modified()
                if mod and commitable:
                    modified.append(package)
                elif not commitable:
                    conflicted.append(package)
                else:
                    unchanged.append(package)
        return PackageCommitInfo(unchanged, added, deleted,
                                 modified, conflicted)

    def commit(self, *packages):
        """Commit project working copy.

        If *packages are specified only the specified
        packages will be commited. Otherwise all packages
        will be updated.

        """
        with wc_lock(self.path) as lock:
            cstate = ProjectCommitState.read_state(self.path)
            if not self.is_commitable(rollback=True):
                raise PendingTransactionError('commit')
            if (cstate is not None
                and cstate.state == CommitStateMixin.STATE_COMMITTING):
                self._clear_cinfo(cstate)
                self._commit(cstate)
            else:
                cinfo = self._calculate_commitinfo(*packages)
                conflicts = cinfo.conflicted
                if conflicts:
                    # a package might be in conflicts because
                    # its is_commitable method returned False
                    raise FileConflictError(conflicts)
                if not self._transaction_begin('prj_commit', cinfo):
                    return
                states = dict([(p, self._status(p)) for p in self.packages()])
                cstate = ProjectCommitState(self.path, cinfo=cinfo, **states)
                self._commit(cstate)
                self.notifier.finished('prj_commit', aborted=False)

    def _commit(self, cstate):
        self._commit_adds(cstate)
        self._commit_deletes(cstate)
        self._commit_modified(cstate)
        self._packages.merge(cstate.entrystates)

    def _commit_adds(self, cstate):
        cinfo = cstate.info
        tl = self.notifier.listener
        for package in cinfo.added:
            if cstate.state == CommitStateMixin.STATE_TRANSFER:
                # check if package was created in the meantime
                if not RemotePackage.exists(self.name, package):
                    pkg = RemotePackage(self.name, package)
                    pkg.store()
                pkg = self.package(package, transaction_listener=tl)
                pkg.commit()
                cstate.state = CommitStateMixin.STATE_COMMITTING
            cstate.processed(package, ' ')

    def _commit_deletes(self, cstate):
        cinfo = cstate.info
        for package in cinfo.deleted:
            if cstate.state == CommitStateMixin.STATE_TRANSFER:
                RemotePackage.delete(self.name, package)
                cstate.state = CommitStateMixin.STATE_COMMITTING
            self._remove_wc_dir(package, notify=True)
            cstate.processed(package, None)

    def _commit_modified(self, cstate):
        cinfo = cstate.info
        tl = self.notifier.listener
        for package in cinfo.modified:
            if cstate.state == CommitStateMixin.STATE_TRANSFER:
                pkg = self.package(package, transaction_listener=tl)
                pkg.commit()
                cstate.state = CommitStateMixin.STATE_COMMITTING
            cstate.processed(package, ' ')

    def add(self, pkg):
        """Add a new package to the project.

        pkg is the name of the directory which will be added.
        A ValueError is raised if pkg is already tracked or if
        pkg is already an osc working copy.
        Also if prj/pkg does not exist or is no directory
        a ValueError is raised.

        """
        super(Project, self).add(pkg)
        with wc_lock(self.path) as lock:
            if self._status(pkg) != '?':
                raise ValueError("package \"%s\" is already tracked" % pkg)
            pkg_path = os.path.join(self.path, pkg)
            if not os.path.isdir(pkg_path):
                raise ValueError("path \"%s\" is no dir" % pkg_path)
            elif wc_is_project(pkg_path) or wc_is_package(pkg_path):
                msg = ("path \"%s\" is already an initialized"
                       "working copy" % pkg_path)
                raise ValueError(msg)
            storedir = wc_pkg_data_mkdir(self.path, pkg)
            Package.init(pkg_path, pkg, self.name, self.apiurl,
                         ext_storedir=storedir)
            # TODO: add files
            self._packages.add(pkg, state='A')
            self._packages.write()

    def remove(self, pkg):
        """Mark a package for deletion.

        pkg is the name of the package to be deleted.
        A ValueError is raised if pkg is not under version control.
        If pkg has state 'A' it is directly removed.

        """
        super(Project, self).remove(pkg)
        with wc_lock(self.path) as lock:
            st = self._status(pkg)
            if st == '?':
                msg = "package \"%s\" is not under version control" % pkg
                raise ValueError(msg)
            elif st == 'A':
                # remove files
                self._packages.remove(pkg)
            else:
                self._packages.set(pkg, 'D')
                # XXX: pkg has a file conflict, pkg totally broken, remove
                #      files
            self._packages.write()

    def package(self, package, *args, **kwargs):
        """Return a Package object for package package.

        None is returned if package is missing (has state '!')
        or if package is untracked.

        *args and **kwargs are additional arguments for the
        Package's __init__ method.

        """
        path = os.path.join(self.path, package)
        st = self._status(package)
        if st in ('!', '?') or st == 'D' and not wc_is_package(path):
            return None
        return Package(path, *args, **kwargs)

    @classmethod
    def wc_check(cls, path):
        """Check path is a consistent project working copy.

        A 2-tuple (missing, xml_data) is returned:
        - missing is a tuple which contains all missing storefiles
        - xml_data is a str which contains the invalid packages xml str
          (if the xml is valid xml_data is the empty str (''))

        """
        meta = missing_storepaths(path, '_project', '_apiurl',
                                  '_packages')
        dirs = missing_storepaths(path, 'data', dirs=True)
        missing = meta + dirs
        if '_packages' in missing:
            return (missing, '')
        # check if _packages file is a valid xml
        try:
            data = wc_read_packages(path)
        except ValueError as e:
            return (missing, wc_read_packages(path, raw=True))
        return (missing, '')

    @staticmethod
    def init(path, project, apiurl):
        """Initializes a directory as a project working copy.

        path is a path to a directory, project is the name
        of the project and apiurl is the apiurl.

        """
        wc_init(path)
        wc_write_project(path, project)
        wc_write_apiurl(path, apiurl)
        wc_write_packages(path, '<packages/>')
        return Project(path)
