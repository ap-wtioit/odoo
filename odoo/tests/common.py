# -*- coding: utf-8 -*-
"""
The module :mod:`odoo.tests.common` provides unittest test cases and a few
helpers and classes to write tests.

"""
import base64
import importlib
import inspect
import itertools
import json
import logging
import os
import platform
import re
import requests
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import requests
import threading
import time
import werkzeug.urls
from datetime import datetime
from decorator import decorator
from lxml import etree

from odoo.tools import pycompat
from odoo.tools.misc import find_in_path
from odoo.tools.safe_eval import safe_eval

try:
    from itertools import zip_longest as izip_longest
except ImportError:
    from itertools import izip_longest

try:
    import websocket
except ImportError:
    # chrome headless tests will be skipped
    websocket = None

try:
    from xmlrpc import client as xmlrpclib
except ImportError:
    # pylint: disable=bad-python3-import
    import xmlrpclib

import odoo
from odoo import api
from odoo.service import security


_logger = logging.getLogger(__name__)

# The odoo library is supposed already configured.
ADDONS_PATH = odoo.tools.config['addons_path']
HOST = '127.0.0.1'
PORT = odoo.tools.config['http_port']
# Useless constant, tests are aware of the content of demo data
ADMIN_USER_ID = odoo.SUPERUSER_ID


def get_db_name():
    db = odoo.tools.config['db_name']
    # If the database name is not provided on the command-line,
    # use the one on the thread (which means if it is provided on
    # the command-line, this will break when installing another
    # database from XML-RPC).
    if not db and hasattr(threading.current_thread(), 'dbname'):
        return threading.current_thread().dbname
    return db


# For backwards-compatibility - get_db_name() should be used instead
DB = get_db_name()


def at_install(flag):
    """ Sets the at-install state of a test, the flag is a boolean specifying
    whether the test should (``True``) or should not (``False``) run during
    module installation.

    By default, tests are run right after installing the module, before
    starting the installation of the next module.
    """
    def decorator(obj):
        obj.at_install = flag
        return obj
    return decorator

def post_install(flag):
    """ Sets the post-install state of a test. The flag is a boolean
    specifying whether the test should or should not run after a set of
    module installations.

    By default, tests are *not* run after installation of all modules in the
    current installation set.
    """
    def decorator(obj):
        obj.post_install = flag
        return obj
    return decorator

class TreeCase(unittest.TestCase):
    def __init__(self, methodName='runTest'):
        super(TreeCase, self).__init__(methodName)
        self.addTypeEqualityFunc(etree._Element, self.assertTreesEqual)

    def assertTreesEqual(self, n1, n2, msg=None):
        self.assertEqual(n1.tag, n2.tag, msg)
        # Because lxml.attrib is an ordereddict for which order is important
        # to equality, even though *we* don't care
        self.assertEqual(dict(n1.attrib), dict(n2.attrib), msg)

        self.assertEqual((n1.text or u'').strip(), (n2.text or u'').strip(), msg)
        self.assertEqual((n1.tail or u'').strip(), (n2.tail or u'').strip(), msg)

        for c1, c2 in izip_longest(n1, n2):
            self.assertEqual(c1, c2, msg)

class BaseCase(TreeCase):
    """
    Subclass of TestCase for common OpenERP-specific code.

    This class is abstract and expects self.registry, self.cr and self.uid to be
    initialized by subclasses.
    """

    longMessage = True      # more verbose error message by default: https://www.odoo.com/r/Vmh

    def cursor(self):
        return self.registry.cursor()

    def ref(self, xid):
        """ Returns database ID for the provided :term:`external identifier`,
        shortcut for ``get_object_reference``

        :param xid: fully-qualified :term:`external identifier`, in the form
                    :samp:`{module}.{identifier}`
        :raise: ValueError if not found
        :returns: registered id
        """
        return self.browse_ref(xid).id

    def browse_ref(self, xid):
        """ Returns a record object for the provided
        :term:`external identifier`

        :param xid: fully-qualified :term:`external identifier`, in the form
                    :samp:`{module}.{identifier}`
        :raise: ValueError if not found
        :returns: :class:`~odoo.models.BaseModel`
        """
        assert "." in xid, "this method requires a fully qualified parameter, in the following form: 'module.identifier'"
        return self.env.ref(xid)

    @contextmanager
    def _assertRaises(self, exception):
        """ Context manager that clears the environment upon failure. """
        with super(BaseCase, self).assertRaises(exception) as cm:
            with self.env.clear_upon_failure():
                yield cm

    def assertRaises(self, exception, func=None, *args, **kwargs):
        if func:
            with self._assertRaises(exception):
                func(*args, **kwargs)
        else:
            return self._assertRaises(exception)

    @contextmanager
    def assertQueryCount(self, default=0, **counters):
        """ Context manager that counts queries. It may be invoked either with
            one value, or with a set of named arguments like ``login=value``::

                with self.assertQueryCount(42):
                    ...

                with self.assertQueryCount(admin=3, demo=5):
                    ...

            The second form is convenient when used with :func:`users`.
        """
        if self.warm:
            # mock random in order to avoid random bus gc
            with self.subTest(), patch('random.random', lambda: 1):
                login = self.env.user.login
                expected = counters.get(login, default)
                count0 = self.cr.sql_log_count
                yield
                count = self.cr.sql_log_count - count0
                if count != expected:
                    # add some info on caller to allow semi-automatic update of query count
                    frame, filename, linenum, funcname, lines, index = inspect.stack()[2]
                    if "/odoo/addons/" in filename:
                        filename = filename.rsplit("/odoo/addons/", 1)[1]
                    if count > expected:
                        msg = "Query count more than expected for user %s: %d > %d in %s at %s:%s"
                        self.fail(msg % (login, count, expected, funcname, filename, linenum))
                    else:
                        logger = logging.getLogger(type(self).__module__)
                        msg = "Query count less than expected for user %s: %d < %d in %s at %s:%s"
                        logger.info(msg, login, count, expected, funcname, filename, linenum)
        else:
            yield

    def assertRecordValues(self, records, expected_values):
        ''' Compare a recordset with a list of dictionaries representing the expected results.
        This method performs a comparison element by element based on their index.
        Then, the order of the expected values is extremely important.

        Note that:
          - Comparison between falsy values is supported: False match with None.
          - Comparison between monetary field is also treated according the currency's rounding.
          - Comparison between x2many field is done by ids. Then, empty expected ids must be [].
          - Comparison between many2one field id done by id. Empty comparison can be done using any falsy value.

        :param records:               The records to compare.
        :param expected_values:       List of dicts expected to be exactly matched in records
        '''

        def _compare_candidate(record, candidate):
            ''' Return True if the candidate matches the given record '''
            for field_name in candidate.keys():
                record_value = record[field_name]
                candidate_value = candidate[field_name]
                field_type = record._fields[field_name].type
                if field_type == 'monetary':
                    # Compare monetary field.
                    currency_field_name = record._fields[field_name]._related_currency_field
                    record_currency = record[currency_field_name]
                    if record_currency.compare_amounts(candidate_value, record_value)\
                            if record_currency else candidate_value != record_value:
                        return False
                elif field_type in ('one2many', 'many2many'):
                    # Compare x2many relational fields.
                    # Empty comparison must be an empty list to be True.
                    if set(record_value.ids) != set(candidate_value):
                        return False
                elif field_type == 'many2one':
                    # Compare many2one relational fields.
                    # Every falsy value is allowed to compare with an empty record.
                    if (record_value or candidate_value) and record_value.id != candidate_value:
                        return False
                elif (candidate_value or record_value) and record_value != candidate_value:
                    # Compare others fields if not both interpreted as falsy values.
                    return False
            return True

        def _format_message(records, expected_values):
            ''' Return a formatted representation of records/expected_values. '''
            all_records_values = records.read(list(expected_values[0].keys()), load=False)
            msg1 = '\n'.join(pprint.pformat(dic) for dic in all_records_values)
            msg2 = '\n'.join(pprint.pformat(dic) for dic in expected_values)
            return 'Current values:\n\n%s\n\nExpected values:\n\n%s' % (msg1, msg2)

        # if the length or both things to compare is different, we can already tell they're not equal
        if len(records) != len(expected_values):
            msg = 'Wrong number of records to compare: %d != %d.\n\n' % (len(records), len(expected_values))
            self.fail(msg + _format_message(records, expected_values))

        for index, record in enumerate(records):
            if not _compare_candidate(record, expected_values[index]):
                msg = 'Record doesn\'t match expected values at index %d.\n\n' % index
                self.fail(msg + _format_message(records, expected_values))

    def shortDescription(self):
        doc = self._testMethodDoc
        return doc and ' '.join(l.strip() for l in doc.splitlines() if not l.isspace()) or None

    if not pycompat.PY2:
        # turns out this thing may not be quite as useful as we thought...
        def assertItemsEqual(self, a, b, msg=None):
            self.assertCountEqual(a, b, msg=None)

class TransactionCase(BaseCase):
    """ TestCase in which each test method is run in its own transaction,
    and with its own cursor. The transaction is rolled back and the cursor
    is closed after each test.
    """

    def setUp(self):
        self.registry = odoo.registry(get_db_name())
        #: current transaction's cursor
        self.cr = self.cursor()
        self.uid = odoo.SUPERUSER_ID
        #: :class:`~odoo.api.Environment` for the current test case
        self.env = api.Environment(self.cr, self.uid, {})

        @self.addCleanup
        def reset():
            # rollback and close the cursor, and reset the environments
            self.registry.clear_caches()
            self.registry.reset_changes()
            self.env.reset()
            self.cr.rollback()
            self.cr.close()

        self.patch(type(self.env['res.partner']), '_get_gravatar_image', lambda *a: False)

    def patch(self, obj, key, val):
        """ Do the patch ``setattr(obj, key, val)``, and prepare cleanup. """
        old = getattr(obj, key)
        setattr(obj, key, val)
        self.addCleanup(setattr, obj, key, old)

    def patch_order(self, model, order):
        """ Patch the order of the given model (name), and prepare cleanup. """
        self.patch(type(self.env[model]), '_order', order)


class SingleTransactionCase(BaseCase):
    """ TestCase in which all test methods are run in the same transaction,
    the transaction is started with the first test method and rolled back at
    the end of the last.
    """

    @classmethod
    def setUpClass(cls):
        cls.registry = odoo.registry(get_db_name())
        cls.cr = cls.registry.cursor()
        cls.uid = odoo.SUPERUSER_ID
        cls.env = api.Environment(cls.cr, cls.uid, {})

    @classmethod
    def tearDownClass(cls):
        # rollback and close the cursor, and reset the environments
        cls.registry.clear_caches()
        cls.env.reset()
        cls.cr.rollback()
        cls.cr.close()


savepoint_seq = itertools.count()
class SavepointCase(SingleTransactionCase):
    """ Similar to :class:`SingleTransactionCase` in that all test methods
    are run in a single transaction *but* each test case is run inside a
    rollbacked savepoint (sub-transaction).

    Useful for test cases containing fast tests but with significant database
    setup common to all cases (complex in-db test data): :meth:`~.setUpClass`
    can be used to generate db test data once, then all test cases use the
    same data without influencing one another but without having to recreate
    the test data either.
    """
    def setUp(self):
        self._savepoint_id = next(savepoint_seq)
        self.cr.execute('SAVEPOINT test_%d' % self._savepoint_id)
    def tearDown(self):
        self.cr.execute('ROLLBACK TO SAVEPOINT test_%d' % self._savepoint_id)
        self.env.clear()
        self.registry.clear_caches()


class ChromeBrowser():
    """ Helper object to control a Chrome headless process. """

    def __init__(self, logger):
        self._logger = logger
        if websocket is None:
            self._logger.warning("websocket-client module is not installed")
            raise unittest.SkipTest("websocket-client module is not installed")
        self.devtools_port = PORT + 2
        self.ws_url = ''  # WebSocketUrl
        self.ws = None  # websocket
        self.request_id = 0
        self.user_data_dir = tempfile.mkdtemp(suffix='_chrome_odoo')
        self.chrome_process = None
        self.screencast_frames = []
        self._chrome_start()
        self._find_websocket()
        self._logger.info('Websocket url found: %s', self.ws_url)
        self._open_websocket()
        self._logger.info('Enable chrome headless console log notification')
        self._websocket_send('Runtime.enable')
        self._logger.info('Chrome headless enable page notifications')
        self._websocket_send('Page.enable')

    def stop(self):
        if self.chrome_process is not None:
            self._logger.info("Closing chrome headless with pid %s", self.chrome_process.pid)
            self._websocket_send('Browser.close')
            if self.chrome_process.poll() is None:
                self._logger.info("Terminating chrome headless with pid %s", self.chrome_process.pid)
                self.chrome_process.terminate()
                self.chrome_process.wait()
        if self.user_data_dir and os.path.isdir(self.user_data_dir) and self.user_data_dir != '/':
            self._logger.info('Removing chrome user profile "%s"', self.user_data_dir)
            shutil.rmtree(self.user_data_dir)

    @property
    def executable(self):
        system = platform.system()
        if system == 'Linux':
            for bin_ in ['google-chrome', 'chromium']:
                try:
                    return find_in_path(bin_)
                except IOError:
                    continue

        elif system == 'Darwin':
            bins = [
                '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                '/Applications/Chromium.app/Contents/MacOS/Chromium',
            ]
            for bin_ in bins:
                if os.path.exists(bin_):
                    return bin_

        elif system == 'Windows':
            # TODO: handle windows platform: https://stackoverflow.com/a/40674915
            pass

        raise unittest.SkipTest("Chrome executable not found")

    def _chrome_start(self):
        if self.chrome_process is not None:
            return
        switches = {
            '--headless': '',
            '--enable-logging': 'stderr',
            '--no-default-browser-check': '',
            '--no-first-run': '',
            '--disable-extensions': '',
            '--user-data-dir': self.user_data_dir,
            '--disable-translate': '',
            '--window-size': '1366x768',
            '--remote-debugging-port': str(self.devtools_port)
        }
        cmd = [self.executable]
        cmd += ['%s=%s' % (k, v) if v else k for k, v in switches.items()]
        url = 'about:blank'
        cmd.append(url)
        self._logger.info('chrome_run executing %s', ' '.join(cmd))
        try:
            self.chrome_process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            raise unittest.SkipTest("%s not found" % cmd[0])
        self._logger.info('Chrome pid: %s', self.chrome_process.pid)

    def _find_websocket(self):
        version = self._json_command('version')
        self._logger.info('Browser version: %s', version['Browser'])
        try:
            infos = self._json_command('')[0]  # Infos about the first tab
        except IndexError:
            self._logger.warning('No tab found in Chrome')
            self.stop()
            raise unittest.SkipTest('No tab found in Chrome')
        self.ws_url = infos['webSocketDebuggerUrl']
        self._logger.info('Chrome headless temporary user profile dir: %s', self.user_data_dir)

    def _json_command(self, command, timeout=3):
        """
        Inspect dev tools with get
        Available commands:
            '' : return list of tabs with their id
            list (or json/): list tabs
            new : open a new tab
            activate/ + an id: activate a tab
            close/ + and id: close a tab
            version : get chrome and dev tools version
            protocol : get the full protocol
        """
        self._logger.info("Issuing json command %s", command)
        command = os.path.join('json', command).strip('/')
        while timeout > 0:
            try:
                url = werkzeug.urls.url_join('http://127.0.0.1:%s/' % self.devtools_port, command)
                self._logger.info('Url : %s', url)
                r = requests.get(url, timeout=3)
                if r.ok:
                    return r.json()
                return {'status_code': r.status_code}
            except requests.ConnectionError:
                time.sleep(0.1)
                timeout -= 0.1
            except requests.exceptions.ReadTimeout:
                break
        self._logger.error('Could not connect to chrome debugger')
        raise unittest.SkipTest("Cannot connect to chrome headless")

    def _open_websocket(self):
        self.ws = websocket.create_connection(self.ws_url)
        if self.ws.getstatus() != 101:
            raise unittest.SkipTest("Cannot connect to chrome dev tools")
        self.ws.settimeout(0.01)

    def _websocket_send(self, method, params=None):
        """
        send chrome devtools protocol commands through websocket
        """
        sent_id = self.request_id
        payload = {
            'method': method,
            'id':  sent_id,
        }
        if params:
            payload.update({'params': params})
        self.ws.send(json.dumps(payload))
        self.request_id += 1
        return sent_id

    def _websocket_wait_id(self, awaited_id, timeout=10):
        """
        blocking wait for a certain id in a response
        warning other messages are discarded
        """
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                res = json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException:
                res = None
            if res and res.get('id') == awaited_id:
                return res
        self._logger.info('timeout exceeded while waiting for id : %d', awaited_id)
        return {}

    def _websocket_wait_event(self, method, params=None, timeout=10):
        """
        blocking wait for a particular event method and eventually a dict of params
        """
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                res = json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException:
                res = None
            if res and res.get('method', '') == method:
                if params:
                    if set(params).issubset(set(res.get('params', {}))):
                        return res
                else:
                    return res
            elif res:
                self._logger.debug('chrome devtools protocol event: %s', res)
        self._logger.info('timeout exceeded while waiting for : %s', method)

    def _get_shotname(self, prefix, ext):
        """ return a unique filename for screenshot or screencast"""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        base_file = os.path.splitext(odoo.tools.config['logfile'])[0]
        name = '%s_%s_%s.%s' % (base_file, prefix, timestamp, ext)
        return name

    def take_screenshot(self, prefix='failed'):
        if not odoo.tools.config['logfile']:
            self._logger.info('Screenshot disabled !')
            return None
        ss_id = self._websocket_send('Page.captureScreenshot')
        self._logger.info('Asked for screenshot (id: %s)', ss_id)
        res = self._websocket_wait_id(ss_id)
        base_png = res.get('result', {}).get('data')
        decoded = base64.decodebytes(bytes(base_png.encode('utf-8')))
        outfile = self._get_shotname(prefix, 'png')
        with open(outfile, 'wb') as f:
            f.write(decoded)
        self._logger.info('Screenshot in: %s', outfile)

    def _save_screencast(self, prefix='failed'):
        # could be encododed with something like that
        #  ffmpeg -framerate 3 -i frame_%05d.png  output.mp4
        if not odoo.tools.config['logfile']:
            self._logger.info('Screencast disabled !')
            return None
        sdir = tempfile.mkdtemp(suffix='_chrome_odoo_screencast')
        nb = 0
        for frame in self.screencast_frames:
            outfile = os.path.join(sdir, 'frame_%05d.png' % nb)
            with open(outfile, 'wb') as f:
                f.write(base64.decodebytes(bytes(frame.get('data').encode('utf-8'))))
                nb += 1
        framerate = int(nb / (self.screencast_frames[nb-1].get('metadata').get('timestamp') - self.screencast_frames[0].get('metadata').get('timestamp')))
        outfile = self._get_shotname(prefix, 'mp4')
        r = subprocess.run(['ffmpeg', '-framerate', str(framerate), '-i', '%s/frame_%%05d.png' % sdir, outfile])
        shutil.rmtree(sdir)
        if r.returncode == 0:
            self._logger.info('Screencast in: %s', outfile)

    def start_screencast(self):
        self._websocket_send('Page.startScreencast', {'params': {'everyNthFrame': 5, 'maxWidth': 683, 'maxHeight': 384}})

    def set_cookie(self, name, value, path, domain):
        params = {'name': name, 'value': value, 'path': path, 'domain': domain}
        self._websocket_send('Network.setCookie', params=params)

    def _wait_ready(self, ready_code, timeout=60):
        self._logger.info('Evaluate ready code "%s"', ready_code)
        awaited_result = {'result': {'type': 'boolean', 'value': True}}
        ready_id = self._websocket_send('Runtime.evaluate', params={'expression': ready_code})
        last_bad_res = ''
        start_time = time.time()
        tdiff = time.time() - start_time
        has_exceeded = False
        while tdiff < timeout:
            try:
                res = json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException:
                res = None
            if res and res.get('id') == ready_id:
                if res.get('result') == awaited_result:
                    return True
                else:
                    last_bad_res = res
                    ready_id = self._websocket_send('Runtime.evaluate', params={'expression': ready_code})
            tdiff = time.time() - start_time
            if tdiff >= 2 and not has_exceeded:
                has_exceeded = True
                self._logger.warning('The ready code takes too much time : %s', tdiff)

        self.take_screenshot(prefix='failed_ready')
        self._logger.info('Ready code last try result: %s', last_bad_res or res)
        return False

    def _wait_code_ok(self, code, timeout):
        self._logger.info('Evaluate test code "%s"', code)
        code_id = self._websocket_send('Runtime.evaluate', params={'expression': code})
        start_time = time.time()
        logged_error = False
        while time.time() - start_time < timeout:
            try:
                res = json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException:
                res = None
            if res and res.get('id', -1) == code_id:
                self._logger.info('Code start result: %s', res)
                if res.get('result', {}).get('result').get('subtype', '') == 'error':
                    self._logger.error("Running code returned an error")
                    return False
            elif res and res.get('method') == 'Runtime.consoleAPICalled' and res.get('params', {}).get('type') in ('log', 'error'):
                logs = res.get('params', {}).get('args')
                log_type = res.get('params', {}).get('type')
                content = " ".join([str(log.get('value', '')) for log in logs])
                if log_type == 'error':
                    self._logger.error(content)
                    logged_error = True
                else:
                    self._logger.info('console log: %s', content)
                for log in logs:
                    if log.get('type', '') == 'string' and log.get('value', '').lower() == 'ok':
                        # it is possible that some tests returns ok while an error was shown in logs.
                        # since runbot should always be red in this case, better explicitly fail.
                        if logged_error:
                            return False
                        return True
                    elif log.get('type', '') == 'string' and log.get('value', '').lower().startswith('error'):
                        self.take_screenshot()
                        self._save_screencast()
                        return False
            elif res and res.get('method') == 'Page.screencastFrame':
                self.screencast_frames.append(res.get('params'))
            elif res:
                self._logger.debug('chrome devtools protocol event: %s', res)
        self._logger.error('Script timeout exceeded : %s', (time.time() - start_time))
        self.take_screenshot()
        return False

    def navigate_to(self, url, wait_stop=False):
        self._logger.info('Navigating to: "%s"', url)
        nav_id = self._websocket_send('Page.navigate', params={'url': url})
        nav_result = self._websocket_wait_id(nav_id)
        self._logger.info("Navigation result: %s", nav_result)
        frame_id = nav_result.get('result', {}).get('frameId', '')
        if wait_stop and frame_id:
            self._logger.info('Waiting for frame "%s" to stop loading', frame_id)
            self._websocket_wait_event('Page.frameStoppedLoading', params={'frameId': frame_id})

    def clear(self):
        self._websocket_send('Page.stopScreencast')
        self.screencast_frames = []
        self._websocket_send('Page.stopLoading')
        self._logger.info('Deleting cookies and clearing local storage')
        dc_id = self._websocket_send('Network.clearBrowserCookies')
        self._websocket_wait_id(dc_id)
        cl_id = self._websocket_send('Runtime.evaluate', params={'expression': 'localStorage.clear()'})
        self._websocket_wait_id(cl_id)
        self.navigate_to('about:blank', wait_stop=True)


class HttpCase(TransactionCase):
    """ Transactional HTTP TestCase with url_open and Chrome headless helpers.
    """
    registry_test_mode = True
    browser = None

    def __init__(self, methodName='runTest'):
        super(HttpCase, self).__init__(methodName)
        # v8 api with correct xmlrpc exception handling.
        self.xmlrpc_url = url_8 = 'http://%s:%d/xmlrpc/2/' % (HOST, PORT)
        self.xmlrpc_common = xmlrpclib.ServerProxy(url_8 + 'common')
        self.xmlrpc_db = xmlrpclib.ServerProxy(url_8 + 'db')
        self.xmlrpc_object = xmlrpclib.ServerProxy(url_8 + 'object')

    @classmethod
    def start_browser(cls, logger):
        # start browser on demand
        if cls.browser is None:
            cls.browser = ChromeBrowser(logger)

    @classmethod
    def tearDownClass(cls):
        if cls.browser:
            cls.browser.stop()
            cls.browser = None
        super(HttpCase, cls).tearDownClass()

    def setUp(self):
        super(HttpCase, self).setUp()

        if self.registry_test_mode:
            self.registry.enter_test_mode()
            self.addCleanup(self.registry.leave_test_mode)
        # setup a magic session_id that will be rollbacked
        self.session = odoo.http.root.session_store.new()
        self.session_id = self.session.sid
        self.session.db = get_db_name()
        odoo.http.root.session_store.save(self.session)
        # setup an url opener helper
        self.opener = requests.Session()
        self.opener.cookies['session_id'] = self.session_id

    def url_open(self, url, data=None, timeout=10):
        if url.startswith('/'):
            url = "http://%s:%s%s" % (HOST, PORT, url)
        if data:
            return self.opener.post(url, data=data, timeout=timeout)
        return self.opener.get(url, timeout=timeout)

    def _wait_remaining_requests(self):
        t0 = int(time.time())
        for thread in threading.enumerate():
            if thread.name.startswith('odoo.service.http.request.'):
                join_retry_count = 10
                while thread.isAlive():
                    # Need a busyloop here as thread.join() masks signals
                    # and would prevent the forced shutdown.
                    thread.join(0.05)
                    join_retry_count -= 1
                    if join_retry_count < 0:
                        self._logger.warning("Stop waiting for thread %s handling request for url %s",
                                        thread.name, getattr(thread, 'url', '<UNKNOWN>'))
                        break
                    time.sleep(0.5)
                    t1 = int(time.time())
                    if t0 != t1:
                        self._logger.info('remaining requests')
                        odoo.tools.misc.dumpstacks()
                        t0 = t1

    def authenticate(self, user, password):
        # stay non-authenticated
        if user is None:
            return

        db = get_db_name()
        uid = self.registry['res.users'].authenticate(db, user, password, None)
        env = api.Environment(self.cr, uid, {})

        # self.session.authenticate(db, user, password, uid=uid)
        # OpenERPSession.authenticate accesses the current request, which we
        # don't have, so reimplement it manually...
        session = self.session

        session.db = db
        session.uid = uid
        session.login = user
        session.session_token = uid and security.compute_session_token(session, env)
        session.context = env['res.users'].context_get() or {}
        session.context['uid'] = uid
        session._fix_lang(session.context)

        odoo.http.root.session_store.save(session)
        if self.browser:
            self._logger.info('Setting session cookie in browser')
            self.browser.set_cookie('session_id', self.session_id, '/', '127.0.0.1')

    def browser_js(self, url_path, code, ready='', login=None, timeout=60, **kw):
        """ Test js code running in the browser
        - optionnally log as 'login'
        - load page given by url_path
        - wait for ready object to be available
        - eval(code) inside the page

        To signal success test do:
        console.log('ok')

        To signal failure do:
        console.log('error')

        If neither are done before timeout test fails.
        """
        if not hasattr(self, '_logger'):
            self._logger = logging.getLogger(__name__)
        self.start_browser(self._logger)

        try:
            self.authenticate(login, login)
            url = "http://%s:%s%s" % (HOST, PORT, url_path or '/')
            self._logger.info('Open "%s" in browser', url)

            self._logger.info('Starting screen cast')
            self.browser.start_screencast()
            self.browser.navigate_to(url)

            # Needed because tests like test01.js (qunit tests) are passing a ready
            # code = ""
            ready = ready or "document.readyState === 'complete'"
            self.assertTrue(self.browser._wait_ready(ready), 'The ready "%s" code was always falsy' % ready)
            if code:
                message = 'The test code "%s" failed' % code
            else:
                message = "Some js test failed"
            self.assertTrue(self.browser._wait_code_ok(code, timeout), message)
        finally:
            # clear browser to make it stop sending requests, in case we call
            # the method several times in a test method
            self.browser.clear()
            self._wait_remaining_requests()

    phantom_js = browser_js

def users(*logins):
    """ Decorate a method to execute it once for each given user. """
    @decorator
    def wrapper(func, *args, **kwargs):
        self = args[0]
        old_uid = self.uid
        try:
            # retrieve users
            user_id = {
                user.login: user.id
                for user in self.env['res.users'].search([('login', 'in', list(logins))])
            }
            for login in logins:
                with self.subTest(login=login):
                    # switch user and execute func
                    self.uid = user_id[login]
                    func(*args, **kwargs)
        finally:
            self.uid = old_uid

    return wrapper


@decorator
def warmup(func, *args, **kwargs):
    """ Decorate a test method to run it twice: once for a warming up phase, and
        a second time for real.  The test attribute ``warm`` is set to ``False``
        during warm up, and ``True`` once the test is warmed up.  Note that the
        effects of the warmup phase are rolled back thanks to a savepoint.
    """
    self = args[0]
    # run once to warm up the caches
    self.warm = False
    self.cr.execute('SAVEPOINT test_warmup')
    func(*args, **kwargs)
    self.cr.execute('ROLLBACK TO SAVEPOINT test_warmup')
    self.env.cache.invalidate()
    # run once for real
    self.warm = True
    func(*args, **kwargs)


def can_import(module):
    """ Checks if <module> can be imported, returns ``True`` if it can be,
    ``False`` otherwise.

    To use with ``unittest.skipUnless`` for tests conditional on *optional*
    dependencies, which may or may be present but must still be tested if
    possible.
    """
    try:
        importlib.import_module(module)
    except ImportError:
        return False
    else:
        return True
