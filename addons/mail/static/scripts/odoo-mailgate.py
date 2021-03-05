#!/usr/bin/env python2
# Part of Odoo. See LICENSE file for full copyright and licensing details.
#
# odoo-mailgate
#
# This program will read an email from stdin and forward it to odoo. Configure
# a pipe alias in your mail server to use it, postfix uses a syntax that looks
# like:
#
# email@address: "|/home/odoo/src/odoo-mail.py"
#
# while exim uses a syntax that looks like:
#
# *: |/home/odoo/src/odoo-mail.py
#
# Note python2 was chosen on purpose for backward compatibility with old mail
# servers.
#
# Dev Note exit codes should comply with https://www.unix.com/man-page/freebsd/3/sysexits/
# see http://www.postfix.org/aliases.5.html, output may end up in bounce mails
#
try:
    import optparse
    import sys
    import traceback
    import xmlrpclib
    import socket


    class OptionParser(optparse.OptionParser):
        # option parser that has postfix compatible return code
        # according to https://docs.python.org/2.7/library/optparse.html
        # "If optparse's default error-handling behaviour does not suit your needs, you'll need to subclass OptionParser
        # and override its exit() and/or error() methods."
        def exit(self, status=0, msg=None):
            if msg:
                sys.stderr.write(msg)
            sys.stderr.write(' optparse status: %s' % status)
            sys.exit(64)  # EX_USAGE


    def xmlrpcfault_details(e):
        # reformat xmlrpc faults to print a readable traceback
        err = "xmlrpclib.Fault: %s\n%s" % (e.faultCode, e.faultString)
        sys.stderr.write(err)


    def main():
        op = OptionParser(usage='usage: %prog [options]', version='%prog v1.3')
        op.add_option("-d", "--database", dest="database",
                      help="Odoo database name (default: %default)", default='odoo')
        op.add_option("-u", "--userid", dest="userid",
                      help="Odoo user id to connect with (default: %default)", default=1, type=int)
        op.add_option("-p", "--password", dest="password",
                      help="Odoo user password (default: %default)", default='admin')
        op.add_option("--host", dest="host", help="Odoo host (default: %default)", default='localhost')
        op.add_option("--port", dest="port", help="Odoo port (default: %default)", default=8069, type=int)
        op.add_option("--proto", dest="protocol",
                      help="Protocol to use (default: %default), http or https", default='http')
        op.add_option("--debug", dest="debug", action="store_true",
                      help="Enable debug (may lead to stack traces in bounce mails)", default=False)
        op.add_option("--retry-status", dest="retry", action="store_true",
                      help="Send temporary failure status code on connection errors.", default=False)
        (o, args) = op.parse_args()
        if o.protocol not in ['http', 'https']:
            op.print_help()
            sys.exit(64)  # EX_USAGE

        try:
            msg = sys.stdin.read()
            models = xmlrpclib.ServerProxy('%s://%s:%s/xmlrpc/2/object' % (o.protocol, o.host, o.port), allow_none=True)
            models.execute_kw(o.database, o.userid, o.password, 'mail.thread', 'message_process', [False, xmlrpclib.Binary(msg)], {})
        except xmlrpclib.Fault as e:
            if e.faultString == 'Access denied' and e.faultCode == 3:
                # odoo user doesn't exist or password is wrong (no stacktrace sent from odoo)
                xmlrpcfault_details(e)
                sys.exit(77)  # EX_NOPERM
            elif 'database' in e.faultString and 'does not exist' in e.faultString and e.faultCode == 1:
                # database was not found on the odoo server
                if o.debug:
                    xmlrpcfault_details(e)
                else:
                    sys.stderr.write('database does not exist: %s' % o.database)
                sys.exit(78)  # EX_CONFIG
            elif 'No possible route' in e.faultString and e.faultCode == 1:
                # email alias was not configured in odoo
                if o.debug:
                    xmlrpcfault_details(e)
                else:
                    sys.stderr.write('alias does not exist in odoo')
                sys.exit(67)  # EX_NOUSER
            else:
                if o.debug:
                    xmlrpcfault_details(e)
                else:
                    sys.stderr.write('xmlrpclib.Fault')
                sys.exit(70)  # EX_SOFTWARE
        except socket.gaierror as e:
            if o.debug:
                traceback.print_exc(None, sys.stderr)
            # unkown host / host addr cannot be resolved
            sys.stderr.write('connection error: %s: %s (%s)' % (e.__class__.__name__, e, o.host))
            if o.retry:
                sys.exit(75)  # EX_TEMPFAIL
            else:
                sys.exit(68)  # EX_NOHOST
        except socket.error as e:
            if o.debug:
                traceback.print_exc(None, sys.stderr)
            # [Errno 111] Connection refused
            sys.stderr.write('connection error: %s: %s (%s)' % (e.__class__.__name__, e, o.host))
            if o.retry:
                sys.exit(75)  # EX_TEMPFAIL
            else:
                sys.exit(69)  # EX_UNAVAILABLE
        except Exception as e:
            if o.debug:
                traceback.print_exc(None, sys.stderr)
            else:
                raise e

    if __name__ == '__main__':
        main()
except Exception as e:
    # handle all unhandled exceptions to prevent postfix from send a bounce mail that includes the invoked command with
    # args which may include the password for the odoo user.
    # make sure no exceptions are raised in this part.
    try:
        traceback.print_exc(None, sys.stderr)
    except Exception as e:
        # our error handling failed we do not try to do anything more to not provoke any more exceptions
        pass
    finally:
        if 'sys' in locals():
            sys.exit(70)  # EX_SOFTWARE
        exit(70)  # EX_SOFTWARE
