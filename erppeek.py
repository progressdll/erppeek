#!/usr/bin/env python
# -*- coding: utf-8 -*-
""" erppeek.py -- OpenERP command line tool

Author: Florent Xicluna
(derived from a script by Alan Bell)
"""
from __future__ import with_statement

import functools
import optparse
import os.path
from pprint import pprint
import re
import sys
import warnings
try:                    # Python 3
    import configparser
    from xmlrpc.client import Fault, ServerProxy
    basestring = str
    int_types = int
except ImportError:     # Python 2
    import ConfigParser as configparser
    from xmlrpclib import Fault, ServerProxy
    int_types = int, long

try:
    # first, try importing directly
    from ast import literal_eval
except ImportError:
    import _ast

    # Port of Python 2.6's ast.literal_eval for use under Python 2.5
    SAFE_CONSTANTS = {'None': None, 'True': True, 'False': False}

    def _convert(node):
        if isinstance(node, _ast.Str):
            return node.s
        elif isinstance(node, _ast.Num):
            return node.n
        elif isinstance(node, _ast.Tuple):
            return tuple(map(_convert, node.elts))
        elif isinstance(node, _ast.List):
            return list(map(_convert, node.elts))
        elif isinstance(node, _ast.Dict):
            return dict((_convert(k), _convert(v)) for k, v
                        in zip(node.keys, node.values))
        elif isinstance(node, _ast.Name):
            if node.id in SAFE_CONSTANTS:
                return SAFE_CONSTANTS[node.id]
        raise ValueError('malformed or disallowed expression')

    def literal_eval(node_or_string):
        if isinstance(node_or_string, basestring):
            node_or_string = compile(node_or_string,
                                     '<unknown>', 'eval', _ast.PyCF_ONLY_AST)
        if isinstance(node_or_string, _ast.Expression):
            node_or_string = node_or_string.body
        return _convert(node_or_string)


__version__ = '0.9.1'
__all__ = ['Client', 'Model', 'Record', 'RecordList', 'Service', 'read_config']

CONF_FILE = 'erppeek.ini'
DEFAULT_URL = 'http://localhost:8069'
DEFAULT_DB = 'openerp'
DEFAULT_USER = 'admin'
DEFAULT_PASSWORD = 'admin'

USAGE = """\
Usage (main commands):
    search(obj, domain)
    search(obj, domain, offset=0, limit=None, order=None)
                                    # Return a list of IDs
    count(obj, domain)              # Count the matching objects

    read(obj, ids, fields=None)
    read(obj, domain, fields=None)
    read(obj, domain, fields=None, offset=0, limit=None, order=None)
                                    # Return values for the fields

    models(name)                    # List models matching pattern
    model(name)                     # Return a Model instance
    keys(obj)                       # List field names of the model
    fields(obj, names=None)         # Return details for the fields
    field(obj, name)                # Return details for the field
    access(obj, mode='read')        # Check access on the model

    do(obj, method, *params)        # Generic 'object.execute'
    wizard(name)                    # Return the 'id' of a new wizard
    wizard(name_or_id, datas=None, action='init')
                                    # Generic 'wizard.execute'
    exec_workflow(obj, signal, id)  # Trigger workflow signal

    client                          # Client object, connected
    client.login(user)              # Login with another user
    client.connect(env)             # Connect to another env.
    client.modules(name)            # List modules matching pattern
    client.upgrade(module1, module2, ...)
                                    # Upgrade the modules
"""

STABLE_STATES = ('uninstallable', 'uninstalled', 'installed')
DOMAIN_OPERATORS = frozenset('!|&')
# Supported operators are
#   =, !=, >, >=, <, <=, like, ilike, in,
#   not like, not ilike, not in, child_of
# Not supported operators are
#  - redundant operators: '<>', '=like', '=ilike'
#  - future operator(s) (6.1): '=?'
_term_re = re.compile(
    '(\S+)\s*'
    '(=|!=|>|>=|<|<=|like|ilike|in|not like|not ilike|not in|child_of)'
    '\s*(.*)')
_fields_re = re.compile(r'(?:[^%]|^)%\(([^)]+)\)')

# Published object methods
_methods = {
    'db': ['create', 'drop', 'dump', 'restore', 'rename', 'list', 'list_lang',
           'change_admin_password', 'server_version', 'migrate_databases'],
    'common': ['about', 'login', 'timezone_get', 'get_server_environment',
               'login_message', 'check_connectivity'],
    'object': ['execute', 'exec_workflow'],
    'wizard': ['execute', 'create'],
    'report': ['report', 'report_get'],
}
_methods_6_1 = {
    'db': ['create_database', 'db_exist'],
    'common': ['get_stats', 'list_http_services', 'version',
               'authenticate', 'get_os_time', 'get_sqlcount'],
    'object': ['execute_kw'],
    'wizard': [],
    'report': ['render_report'],
}
# Hidden methods:
#  - (not in 6.1) 'common': ['logout', 'ir_get', 'ir_set', 'ir_del']
#  - (not in 6.1) 'object': ['obj_list']
#  - 'db': ['get_progress']
#  - 'common': ['get_available_updates', 'get_migration_scripts', 'set_loglevel']


def mixedcase(s, _cache={}):
    """Convert to MixedCase.

    >>> mixedcase('res.company')
    'ResCompany'
    """
    try:
        return _cache[s]
    except KeyError:
        _cache[s] = s = ''.join([w.capitalize() for w in s.split('.')])
    return s


def lowercase(s, _sub=re.compile('[A-Z]').sub,
              _repl=(lambda m: '.' + m.group(0).lower()), _cache={}):
    """Convert to lowercase with dots.

    >>> lowercase('ResCompany')
    'res.company'
    """
    try:
        return _cache[s]
    except KeyError:
        _cache[s] = s = _sub(_repl, s).lstrip('.')
        return s


def read_config(section=None):
    """Read the environment settings from the configuration file.

    The config file ``erppeek.ini`` contains a `section` for each environment.
    Each section provides parameters for the connection: ``host``, ``port``,
    ``database``, ``user`` and (optional) ``password``.  Default values are
    read from the ``[DEFAULT]`` section.  If the ``password`` is not in the
    configuration file, it is requested on login.
    Return a tuple ``(server, db, user, password or None)``.
    Without argument, it returns the list of configured environments.
    """
    p = configparser.SafeConfigParser()
    with open(Client._config_file) as f:
        p.readfp(f)
    if section is None:
        return p.sections()
    server = 'http://%s:%s' % (p.get(section, 'host'), p.get(section, 'port'))
    db = p.get(section, 'database')
    user = p.get(section, 'username')
    if p.has_option(section, 'password'):
        password = p.get(section, 'password')
    else:
        password = None
    return (server, db, user, password)


def issearchdomain(arg):
    """Check if the argument is a search domain.

    Examples:
      - ``[('name', '=', 'mushroom'), ('state', '!=', 'draft')]``
      - ``['name = mushroom', 'state != draft']``
      - ``[]``
      - ``'state != draft'``
      - ``('state', '!=', 'draft')``
    """
    return isinstance(arg, (list, tuple, basestring)) and not (arg and (
        # Not a list of ids: [1, 2, 3]
        isinstance(arg[0], int_types) or
        # Not a list of ids as str: ['1', '2', '3']
        (isinstance(arg[0], basestring) and arg[0].isdigit())))


def searchargs(params, kwargs=None, context=None):
    """Compute the 'search' parameters."""
    if not params:
        return ([],)
    domain = params[0]
    if isinstance(domain, (basestring, tuple)):
        domain = [domain]
        warnings.warn('Domain should be a list: %s' % domain)
    elif not isinstance(domain, list):
        return params
    for idx, term in enumerate(domain):
        if isinstance(term, basestring) and term not in DOMAIN_OPERATORS:
            m = _term_re.match(term.strip())
            if not m:
                raise ValueError("Cannot parse term %r" % term)
            (field, operator, value) = m.groups()
            try:
                value = literal_eval(value)
            except Exception:
                # Interpret the value as a string
                pass
            domain[idx] = (field, operator, value)
    if (kwargs or context) and len(params) == 1:
        params = (domain,
                  kwargs.pop('offset', 0),
                  kwargs.pop('limit', None),
                  kwargs.pop('order', None),
                  context)
    else:
        params = (domain,) + params[1:]
    return params


class Service(ServerProxy):
    """A wrapper around XML-RPC endpoints.

    The connected endpoints are exposed on the Client instance.
    The `server` argument is the URL of the server (scheme+host+port).
    The `endpoint` argument is the last part of the URL
    (examples: ``"object"``, ``"db"``).  The `methods` is the list of methods
    which should be exposed on this endpoint.  Use ``dir(...)`` on the
    instance to list them.
    """
    def __init__(self, server, endpoint, methods):
        uri = server + '/xmlrpc/' + endpoint
        ServerProxy.__init__(self, uri, allow_none=True)
        self._methods = sorted(methods)

    def __repr__(self):
        rname = '%s%s' % (self._ServerProxy__host, self._ServerProxy__handler)
        return '<Service %s>' % rname
    __str__ = __repr__

    def __dir__(self):
        return self._methods

    def __getattr__(self, name):
        if name in self._methods:
            wrapper = lambda s, *args: s._ServerProxy__request(name, args)
            wrapper.__name__ = name
            return wrapper.__get__(self, type(self))
        raise AttributeError("'Service' object has no attribute %r" % name)


class Client(object):
    """Connection to an OpenERP instance.

    This is the top level object.
    The `server` is the URL of the instance, like ``http://localhost:8069``.
    The `db` is the name of the database and the `user` should exist in the
    table ``res.users``.  If the `password` is not provided, it will be
    asked on login.
    """
    _config_file = os.path.join(os.path.curdir, CONF_FILE)

    def __init__(self, server, db, user, password=None):
        self._server = server
        self._db = db
        self._environment = None
        self.user = None
        major_version = None
        self._execute = None

        def get_proxy(name):
            if major_version in ('5.0', None):
                methods = _methods[name]
            else:
                # Only for OpenERP >= 6
                methods = _methods[name] + _methods_6_1[name]
            return Service(server, name, methods)
        self.server_version = ver = get_proxy('db').server_version()
        self.major_version = major_version = '.'.join(ver.split('.', 2)[:2])
        # Create the XML-RPC proxies
        self.db = get_proxy('db')
        self.common = get_proxy('common')
        self._object = get_proxy('object')
        self._wizard = get_proxy('wizard')
        self._report = get_proxy('report')
        # Try to login
        self._login(user, password)
        self._models = {}

    @classmethod
    def from_config(cls, environment):
        """Create a connection to a defined environment.

        Read the settings from the section ``[environment]`` in the
        ``erppeek.ini`` file and return a connected :class:`Client`.
        See :func:`read_config` for details of the configuration file format.
        """
        client = cls(*read_config(environment))
        client._environment = environment
        return client

    def __repr__(self):
        return "<Client '%s#%s'>" % (self._server, self._db)

    def login(self, user, password=None):
        """Switch `user`.

        If the `password` is not provided, it will be asked.
        """
        (uid, password) = self._auth(user, password)
        if uid is False:
            print('Error: Invalid username or password')
            return
        self.user = user

        # Authenticated endpoints
        def authenticated(method):
            return functools.partial(method, self._db, uid, password)
        self._execute = authenticated(self._object.execute)
        self._exec_workflow = authenticated(self._object.exec_workflow)
        self._wizard_execute = authenticated(self._wizard.execute)
        self._wizard_create = authenticated(self._wizard.create)
        self.report = authenticated(self._report.report)
        self.report_get = authenticated(self._report.report_get)
        if self.major_version != '5.0':
            # Only for OpenERP >= 6
            self.execute_kw = authenticated(self._object.execute_kw)
            self.render_report = authenticated(self._report.render_report)
        return uid

    # Needed for interactive use
    _login = login
    _login.cache = {}

    def _check_valid(self, uid, password):
        execute = self._object.execute
        try:
            execute(self._db, uid, password, 'res.users', 'fields_get_keys')
            return True
        except Fault:
            return False

    def _auth(self, user, password):
        cache_key = (self._server, self._db, user)
        if password:
            # If password is explicit, call the 'login' method
            uid = None
        else:
            # Read from cache
            uid, password = self._login.cache.get(cache_key) or (None, None)
            # Read from table 'res.users'
            if not uid and self.access('res.users', 'write'):
                obj = self.read('res.users', [('login', '=', user)], 'id password')
                if obj:
                    uid = obj[0]['id']
                    password = obj[0]['password']
                else:
                    # Invalid user
                    uid = False
            # Ask for password
            if not password and uid is not False:
                from getpass import getpass
                password = getpass('Password for %r: ' % user)
        if uid:
            # Check if password changed
            if not self._check_valid(uid, password):
                if cache_key in self._login.cache:
                    del self._login.cache[cache_key]
                uid = False
        elif uid is None:
            # Do a standard 'login'
            uid = self.common.login(self._db, user, password)
        if uid:
            # Update the cache
            self._login.cache[cache_key] = (uid, password)
        return (uid, password)

    @classmethod
    def _set_interactive(cls, write=False):
        g = globals()
        # Don't call multiple times
        del Client._set_interactive
        global_names = ['wizard', 'exec_workflow', 'read', 'search', 'count',
                        'model', 'models', 'keys', 'fields', 'field', 'access']
        if write:
            global_names.extend(['write', 'create', 'copy', 'unlink'])

        def connect(self, env=None):
            if env:
                client = self.from_config(env)
            else:
                client = self
                env = self._environment or self._db
            g['client'] = client
            # Tweak prompt
            sys.ps1 = '%s >>> ' % env
            sys.ps2 = '%s ... ' % env
            # Logged in?
            if client.user:
                g['do'] = client.execute
                for name in global_names:
                    g[name] = getattr(client, name)
                print('Logged in as %r' % (client.user,))
            else:
                g['do'] = None
                g.update(dict.fromkeys(global_names))

        def login(self, user):
            if self._login(user):
                # If successful, register the new globals()
                self.connect()

        # Set hooks to recreate the globals()
        cls.login = login
        cls.connect = connect

    def execute(self, obj, method, *params, **kwargs):
        """Wrapper around ``object.execute`` RPC method.

        Argument `method` is the name of an ``osv.osv`` method or
        a method available on this `obj`.
        Method `params` are allowed.  If needed, keyword
        arguments are collected in `kwargs`.
        """
        assert isinstance(obj, basestring) and isinstance(method, basestring)
        context = kwargs.pop('context', None)
        if method in ('read', 'name_get'):
            assert params
            if issearchdomain(params[0]):
                # Combine search+read
                search_params = searchargs(params[:1], kwargs, context)
                ids = self._execute(obj, 'search', *search_params)
            else:
                ids = params[0]
            if len(params) > 1:
                params = (ids,) + params[1:]
            elif method == 'read':
                params = (ids, kwargs.pop('fields', None))
            else:
                params = (ids,)
        elif method == 'search':
            # Accept keyword arguments for the search method
            params = searchargs(params, kwargs, context)
            context = None
        elif method == 'search_count':
            params = searchargs(params)
        if context:
            params = params + (context,)
        # Ignore extra keyword arguments
        for item in kwargs.items():
            print('Ignoring: %s = %r' % item)
        # print('DBG: _execute(%r, %r, *%r)' % (obj, method, params))
        return self._execute(obj, method, *params)

    def exec_workflow(self, obj, signal, obj_id):
        """Wrapper around ``object.exec_workflow`` RPC method.

        Argument `obj` is the name of the model.  The `signal`
        is sent to the object identified by its integer ``id`` `obj_id`.
        """
        assert isinstance(obj, basestring) and isinstance(signal, basestring)
        return self._exec_workflow(obj, signal, obj_id)

    def wizard(self, name, datas=None, action='init', context=None):
        """Wrapper around ``wizard.create`` and ``wizard.execute``
        RPC methods.

        If only `name` is provided, a new wizard is created and its ``id`` is
        returned.  If `action` is not ``"init"``, then the action is executed.
        In this case the `name` is either an ``id`` or a string.
        If the `name` is a string, the wizard is created before the execution.
        The optional `datas` argument provides data for the action.
        The optional `context` argument is passed to the RPC method.
        """
        if isinstance(name, int_types):
            wiz_id = name
        else:
            wiz_id = self._wizard_create(name)
        if datas is None:
            if action == 'init' and name != wiz_id:
                return wiz_id
            datas = {}
        return self._wizard_execute(wiz_id, datas, action, context)

    def _upgrade(self, modules, button):
        # First, update the list of modules
        updated, added = self.execute('ir.module.module', 'update_list')
        if added:
            print('%s module(s) added to the list' % added)
        # Click upgrade/install/uninstall button
        ids = self.search('ir.module.module', [('name', 'in', modules)])
        if not ids:
            print('%s module(s) updated' % updated)
            return
        self.execute('ir.module.module', button, ids)
        mods = self.read('ir.module.module',
                         [('state', 'not in', STABLE_STATES)], 'name state')
        if not mods:
            return
        print('%s module(s) selected' % len(ids))
        print('%s module(s) to update:' % len(mods))
        for mod in mods:
            print('  %(state)s\t%(name)s' % mod)

        self._models.clear()
        if self.major_version == '5.0':
            # Wizard "Apply Scheduled Upgrades"
            rv = self.wizard('module.upgrade', action='start')
            if 'config' not in [state[0] for state in rv.get('state', ())]:
                # Something bad happened
                return rv
        else:
            self.execute('base.module.upgrade', 'upgrade_module', [])

    def upgrade(self, *modules):
        """Press the button ``Upgrade``."""
        return self._upgrade(modules, button='button_upgrade')

    def install(self, *modules):
        """Press the button ``Install``."""
        return self._upgrade(modules, button='button_install')

    def uninstall(self, *modules):
        """Press the button ``Uninstall``."""
        return self._upgrade(modules, button='button_uninstall')

    def search(self, obj, *params, **kwargs):
        """Filter the records in the `domain`, return the ``ids``."""
        return self.execute(obj, 'search', *params, **kwargs)

    def count(self, obj, domain=None):
        """Count the records in the `domain`."""
        return self.execute(obj, 'search_count', domain or [])

    def read(self, obj, *params, **kwargs):
        """Wrapper for ``client.execute(obj, 'read', [...], ('a', 'b'))``.

        The first argument `obj` is the model name (example: ``"res.partner"``)

        The second argument, `domain`, accepts:
         - ``[('name', '=', 'mushroom'), ('state', '!=', 'draft')]``
         - ``['name = mushroom', 'state != draft']``
         - ``[]``
         - a list of ids ``[1, 2, 3]`` or a single id ``42``

        The third argument, `fields`, accepts:
         - ``('street', 'city')``
         - ``'street city'``
         - ``'%(street)s %(city)s'``
        """
        fmt = None
        if len(params) > 1 and isinstance(params[1], basestring):
            fmt = ('%(' in params[1]) and params[1]
            if fmt:
                fields = _fields_re.findall(fmt)
            else:
                # transform: "zip city" --> ("zip", "city")
                fields = params[1].split()
                if len(fields) == 1:
                    fmt = ()    # marker
            params = (params[0], fields) + params[2:]
        res = self.execute(obj, 'read', *params, **kwargs)
        if not res:
            return res
        if fmt:
            if isinstance(res, list):
                return [fmt % d for d in res]
            return fmt % res
        if fmt == ():
            if isinstance(res, list):
                return [d[fields[0]] for d in res]
            return res[fields[0]]
        return res

    def _model(self, name):
        try:
            return self._models[name]
        except KeyError:
            # m = Model(self, name)
            m = object.__new__(Model)
        m._init(self, name)
        self._models[name] = m
        return m

    def models(self, name=''):
        """Return a dictionary of models.

        The argument `name` is a pattern to filter the models returned.
        If omitted, all models are returned.
        Keys are camel case names of the models.
        Values are instances of :class:`Model`.

        The return value can be used to declare the models in the global
        namespace:

        >>> globals().update(client.models('res.'))
        """
        domain = [('model', 'like', name)]
        models = self.execute('ir.model', 'read', domain, ('model',))
        names = [m['model'] for m in models]
        return dict([(mixedcase(name), self._model(name)) for name in names])

    def model(self, name):
        """Return a :class:Model instance.

        The argument `name` is the name of the model.
        """
        try:
            return self._models[name]
        except KeyError:
            models = self.models(name)
        if name in self._models:
            return self._models[name]
        if models:
            errmsg = 'Model not found.  These models exist:'
        else:
            errmsg = 'Model not found: %s' % (name,)
        raise RuntimeError('\n * '.join([errmsg] + [str(m) for m in models.values()]))

    def modules(self, name='', installed=None):
        """Return a dictionary of modules.

        The optional argument `name` is a pattern to filter the modules.
        If the boolean argument `installed` is :const:`True`, the modules
        which are "Not Installed" or "Not Installable" are omitted.  If
        the argument is :const:`False`, only these modules are returned.
        If argument `installed` is omitted, all modules are returned.
        The return value is a dictionary where module names are grouped in
        lists according to their ``state``.
        """
        domain = [('name', 'like', name)]
        if installed is not None:
            op = installed and 'not in' or 'in'
            domain.append(('state', op, ['uninstalled', 'uninstallable']))
        mods = self.read('ir.module.module', domain, 'name state')
        if mods:
            res = {}
            for mod in mods:
                if mod['state'] in res:
                    res[mod['state']].append(mod['name'])
                else:
                    res[mod['state']] = [mod['name']]
            return res

    def keys(self, obj):
        """Wrapper for :meth:`Model.keys` method."""
        return self.model(obj).keys()

    def fields(self, obj, names=None):
        """Wrapper for :meth:`Model.fields` method."""
        return self.model(obj).fields(names=names)

    def field(self, obj, name):
        """Wrapper for :meth:`Model.field` method."""
        return self.model(obj).field(name)

    def access(self, obj, mode='read'):
        """Wrapper for :meth:`Model.access` method."""
        return self.model(obj).access(mode=mode)

    def __getattr__(self, method):
        if method.startswith('__'):
            raise AttributeError("'Client' object has no attribute %r" % method)
        if not method.islower():
            rv = self.model(lowercase(method))
            self.__dict__[method] = rv
            return rv

        # miscellaneous object methods
        def wrapper(self, obj, *params, **kwargs):
            """Wrapper for client.execute(obj, %r, *params, **kwargs)."""
            return self.execute(obj, method, *params, **kwargs)
        wrapper.__name__ = method
        wrapper.__doc__ %= method
        return wrapper.__get__(self, type(self))


class Model(object):
    """The class for OpenERP models."""

    def __new__(cls, client, name):
        return client.model(name)

    def _init(self, client, name):
        self.client = client
        self._name = name
        self._execute = functools.partial(client.execute, name)
        self.search = functools.partial(client.search, name)
        self.count = functools.partial(client.count, name)
        self.read = functools.partial(client.read, name)

    def __repr__(self):
        return "<Model '%s'>" % (self._name,)

    def _get_keys(self):
        obj_keys = self._execute('fields_get_keys')
        obj_keys.sort()
        return obj_keys

    def _get_fields(self):
        return self._execute('fields_get')

    def keys(self):
        """Return the keys of the model."""
        return self._keys

    def fields(self, names=None):
        """Return a dictionary of the fields of the model.

        Optional argument `names` is a sequence of field names or
        a space separated string of these names.
        If omitted, all fields are returned.
        """
        if names is None:
            return self._fields
        if isinstance(names, basestring):
            names = names.split()
        return dict([(k, v) for (k, v) in self._fields.items() if k in names])

    def field(self, name):
        """Return the field properties for field `name`."""
        return self._fields[name]

    def access(self, mode="read"):
        """Check if the user has access to this model.

        Optional argument `mode` is the access mode to check.  Valid values
        are ``read``, ``write``, ``create`` and ``unlink``. If omitted,
        the ``read`` mode is checked.  Return a boolean.
        """
        try:
            self.client._execute('ir.model.access', 'check', self._name, mode)
            return True
        except (TypeError, Fault):
            return False

    def browse(self, ids, context=None):
        """Return a :class:`Record` or a :class:`RecordList`.

        The argument `ids` accepts a single integer ``id``, a list of ids
        or a search domain.
        If it is a single integer, the return value is a :class:`Record`.
        Otherwise, the return value is a :class:`RecordList`.
        """
        if isinstance(ids, int_types):
            return Record(self, ids, context=context)
        if issearchdomain(ids):
            ids = self._execute('search', ids, context=context)
        return RecordList(self, ids, context=context)
    # alias
    get = browse

    def create(self, values, context=None):
        """Create a :class:`Record`.

        The argument `values` is a dictionary of values which are used to
        create the record.  The newly created :class:`Record` is returned.
        """
        new_id = self._execute('create', values, context=context)
        return Record(self, new_id, context=context)

    def __getattr__(self, attr):
        if attr in ('_keys', '_fields'):
            self.__dict__[attr] = rv = getattr(self, '_get' + attr)()
            return rv
        if attr.startswith('__'):
            raise AttributeError("'Model' object has no attribute %r" % attr)

        def wrapper(self, *params, **kwargs):
            """Wrapper for client.execute(%r, %r, *params, **kwargs)."""
            return self._execute(attr, *params, **kwargs)
        wrapper.__name__ = attr
        wrapper.__doc__ %= (self._name, attr)
        self.__dict__[attr] = mobj = wrapper.__get__(self, type(self))
        return mobj


class RecordList(object):
    """A sequence of OpenERP :class:`Record`."""

    def __init__(self, res_model, ids, context=None):
        self._model = res_model
        self._ids = ids
        self._context = context

    def __repr__(self):
        if len(self._ids) > 16:
            ids = 'length=%d' % len(self._ids)
        else:
            ids = self._ids
        return "<RecordList '%s,%s'>" % (self._model._name, ids)

    def __dir__(self):
        return ['__getitem__', 'read', 'write', 'unlink',
                '_context', '_ids', '_model']

    def __getitem__(self, key):
        if isinstance(key, slice):
            return RecordList(
                self._model, self._ids[key], context=self._context)
        return Record(self._model, self._ids[key], context=self._context)

    def __getattr__(self, attr):
        if attr.startswith('__'):
            errmsg = "'RecordList' object has ""no attribute %r" % attr
            raise AttributeError(errmsg)
        model_name = self._model._name
        context = self._context
        execute = self._model.client.execute

        def wrapper(self, *params, **kwargs):
            """Wrapper for client.execute(%r, %r, [...], *params, **kwargs)."""
            if context:
                kwargs.setdefault('context', context)
            return execute(model_name, attr, self._ids, *params, **kwargs)
        wrapper.__name__ = attr
        wrapper.__doc__ %= (model_name, attr)
        self.__dict__[attr] = mobj = wrapper.__get__(self, type(self))
        return mobj


class Record(object):
    """A class for all OpenERP records.

    It maps any OpenERP object.
    The fields can be accessed through attributes.  The changes are immediately
    saved in the database.
    The ``many2one``, ``one2many`` and ``many2many`` attributes are followed
    when the record is read.  Howeverm, when writing on these special fields,
    use the appropriate syntax described in the official OpenERP documentation.
    The attributes are evaluated lazily, and they are cached in the record.
    The cache is invalidated if the :meth:`~Record.write` or the
    :meth:`~Record.unlink` method is called.
    """
    def __init__(self, res_model, res_id, context=None):
        self.__dict__.update({
            'client': res_model.client,
            '_model_name': res_model._name,
            '_model': res_model,
            '_context': context,
            'id': res_id,
        })

    def __repr__(self):
        return "<Record '%s,%d'>" % (self._model_name, self.id)

    def __str__(self):
        return self._name

    def _get_name(self):
        try:
            (id_name,) = self._model._execute('name_get', [self.id])
            name = '[%d] %s' % (self.id, id_name[1])
        except Exception:
            name = '[%d] -' % (self.id,)
        return name

    @property
    def _keys(self):
        return self._model._keys

    @property
    def _fields(self):
        return self._model._fields

    def _clear_cache(self):
        for key in self._model._keys:
            if key != 'id' and key in self.__dict__:
                delattr(self, key)

    def read(self, fields=None, context=None):
        """Read the `fields` of the :class:`Record`.

        The argument `fields`, accepts different type of values:
         - ``('street', 'city')``
         - ``'street city'``
         - ``'%(street)s %(city)s'``

        If omitted, all fields are read.

        Return a dictionary of values.
        """
        if context is None and self._context:
            context = self._context
        rv = self.client.read(self._model_name, self.id,
                              fields, context=context)
        self.__dict__.update(rv)
        return rv

    def write(self, values, context=None):
        """Write the `values` in the :class:`Record`."""
        if context is None and self._context:
            context = self._context
        rv = self.client.execute(self._model_name, 'write', [self.id],
                                 values, context=context)
        self._clear_cache()
        return rv

    def unlink(self, context=None):
        """Delete the current :class:`Record` from the database."""
        if context is None and self._context:
            context = self._context
        rv = self.client.execute(self._model_name, 'unlink', [self.id],
                                 context=context)
        self._clear_cache()
        return rv

    def copy(self, default=None, context=None):
        """Copy a record and return the new :class:`Record`.

        The optional argument `default` is a mapping which overrides some
        values of the new record.
        """
        if context is None and self._context:
            context = self._context
        new_id = self.client.copy(self._model_name, 'copy', self.id,
                                  default=default, context=context)
        return Record(self._model, new_id)

    def __dir__(self):
        return ['client', 'read', 'write', 'copy', 'unlink',
                '_context', 'id', '_model', '_model_name',
                '_name', '_keys', '_fields'] + self._model._keys

    def __getitem__(self, attr):
        try:
            return self.__dict__[attr]
        except KeyError:
            if attr not in self._model._keys:
                raise
        value = self.client.read(self._model_name, self.id, attr)
        rv = self._update({attr: value})
        return rv[attr]

    def _update(self, values):
        new_values = {}
        for key, value in values.items():
            field = self._model._fields[key]
            field_type = field['type']
            if not value:
                pass
            elif field_type == 'many2one':
                value = self.client.model(field['relation']).browse(
                    value[0], context=self._context)
            elif field_type in ('one2many', 'many2many'):
                value = self.client.model(field['relation']).browse(
                    value, context=self._context)
            new_values[key] = value
        self.__dict__.update(new_values)
        return new_values

    def __getattr__(self, attr):
        if attr in self._model._keys:
            return self[attr]
        if attr == '_name':
            self.__dict__['_name'] = name = self._get_name()
            return name
        if attr.startswith('__'):
            raise AttributeError("'Record' object has no attribute %r" % attr)
        context = self._context

        def wrapper(self, *params, **kwargs):
            """Wrapper for client.execute(%r, %r, %d, *params, **kwargs)."""
            if context:
                kwargs.setdefault('context', context)
            return self.client.execute(
                self._model_name, attr, self.id, *params, **kwargs)
        wrapper.__name__ = attr
        wrapper.__doc__ %= (self._model_name, attr, self.id)
        self.__dict__[attr] = mobj = wrapper.__get__(self, type(self))
        return mobj

    def __setattr__(self, attr, value):
        if attr in self._model._keys:
            assert attr != 'id'
            self.write({attr: value})
            if attr in self.__dict__:
                delattr(self, attr)
        else:
            object.__setattr__(self, attr, value)


def _interact(use_pprint=True, usage=USAGE):
    import code
    try:
        import builtins
        _exec = getattr(builtins, 'exec')
    except ImportError:
        def _exec(code, g):
            exec('exec code in g')
        import __builtin__ as builtins
    # Do not run twice
    del globals()['_interact']

    def excepthook(exc_type, exc, tb, _original_hook=sys.excepthook):
        # Print readable 'Fault' errors
        if ((issubclass(exc_type, Fault) and
             isinstance(exc.faultCode, basestring))):
            etype, _, msg = exc.faultCode.partition('--')
            if etype.strip() != 'warning':
                msg = exc.faultCode
                if not msg.startswith('FATAL:'):
                    msg += '\n' + exc.faultString
            print('%s: %s' % (exc_type.__name__, msg.strip()))
        else:
            _original_hook(exc_type, exc, tb)

    if use_pprint:
        def displayhook(value, _printer=pprint, _builtins=builtins):
            # Pretty-format the output
            if value is None:
                return
            _printer(value)
            _builtins._ = value
        sys.displayhook = displayhook

    class Usage(object):
        def __call__(self):
            print(usage)
        __repr__ = lambda s: usage
    builtins.usage = Usage()

    try:
        __import__('readline')
    except ImportError:
        pass

    class Console(code.InteractiveConsole):
        def runcode(self, code):
            try:
                _exec(code, globals())
            except SystemExit:
                raise
            except:
                # Work around http://bugs.python.org/issue12643
                excepthook(*sys.exc_info())

    warnings.simplefilter('always', UserWarning)
    # Key UP to avoid an empty line
    Console().interact('\033[A')


def main():
    parser = optparse.OptionParser(
        usage='%prog [options] [id [id ...]]', version=__version__,
        description='Inspect data on OpenERP objects')
    parser.add_option(
        '-l', '--list', action='store_true', dest='list_env',
        help='list sections of the configuration')
    parser.add_option(
        '--env',
        help='read connection settings from the given section')
    parser.add_option(
        '-c', '--config', default=CONF_FILE,
        help='specify alternate config file (default %r)' % CONF_FILE)
    parser.add_option(
        '--server', default=DEFAULT_URL,
        help='full URL to the XML-RPC server (default %s)' % DEFAULT_URL)
    parser.add_option('-d', '--db', default=DEFAULT_DB, help='database')
    parser.add_option('-u', '--user', default=DEFAULT_USER, help='username')
    parser.add_option(
        '-p', '--password', default=DEFAULT_PASSWORD,
        help='password (yes this will be in your shell history and '
             'ps from other users)')
    parser.add_option(
        '-m', '--model',
        help='the type of object to find')
    parser.add_option(
        '-s', '--search', action='append',
        help='search condition (multiple allowed); alternatively, pass '
             'multiple IDs as positional parameters after the options')
    parser.add_option(
        '-f', '--fields', action='append',
        help='restrict the output to certain fields (multiple allowed)')
    parser.add_option(
        '-i', '--interact', action='store_true',
        help='use interactively')
    parser.add_option(
        '--write', action='store_true',
        help='enable "write", "create", "copy" and "unlink" helpers')

    (args, ids) = parser.parse_args()

    Client._config_file = os.path.join(os.path.curdir, args.config)
    if args.list_env:
        print('Available settings:  ' + ' '.join(read_config()))
        return

    if (args.interact or not args.model):
        Client._set_interactive(write=args.write)
        print(USAGE)

    if args.env:
        client = Client.from_config(args.env)
    else:
        client = Client(args.server, args.db, args.user, args.password)

    if args.model:
        if args.search:
            (searchquery,) = searchargs((args.search,))
            ids = client.execute(args.model, 'search', searchquery)
        if ids is None:
            data = None
        elif args.fields:
            data = client.execute(args.model, 'read', ids, args.fields)
        else:
            data = client.execute(args.model, 'read', ids)
        pprint(data)

    if hasattr(client, 'connect'):
        # Set the globals()
        client.connect()
        # Enter interactive mode
        _interact()

if __name__ == '__main__':
    main()
