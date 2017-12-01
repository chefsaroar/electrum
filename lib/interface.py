#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2011 thomasv@gitorious
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import os
import re
import ssl
import sys
import threading
import time
import traceback
import asyncio

import requests

from .util import print_error

ca_path = requests.certs.where()

from . import util
from . import x509
from . import pem
from . import aio

class Interface(util.PrintError):
    """The Interface class handles a socket connected to a single remote
    electrum server.  It's exposed API is:

    - Member functions close(), fileno(), get_responses(), has_timed_out(),
      ping_required(), queue_request(), send_request()
    - Member variable server.
    """

    def __init__(self, server, hostname_port, loop):
        self.server = server
        self.host, _, _ = server.rsplit(':', 2)

        print("constructing socketpipe")
        self.pipe = aio.SocketPipe(hostname_port, loop)
        # Dump network messages.  Set at runtime from the console.
        self.debug = False
        self.unsent_requests = asyncio.PriorityQueue(loop=loop)
        self.unanswered_requests = {}
        # Set last ping to zero to ensure immediate ping
        self.last_request = time.time()
        self.last_ping = 0
        self.closed_remotely = False

    def diagnostic_name(self):
        return self.host

    async def close(self):
        await self.pipe.close()

    async def queue_request(self, *args):  # method, params, _id
        '''Queue a request, later to be send with send_requests when the
        socket is available for writing.
        '''
        self.request_time = time.time()
        await self.unsent_requests.put((self.request_time, args))

    def num_requests(self):
        '''Keep unanswered requests below 100'''
        n = 100 - len(self.unanswered_requests)
        return min(n, self.unsent_requests.qsize())

    async def send_request(self):
        '''Sends queued requests.  Returns False on failure.'''
        make_dict = lambda m, p, i: {'method': m, 'params': p, 'id': i}
        n = self.num_requests()
        prio, request = await self.unsent_requests.get()
        print("got queue item", request)
        try:
            await self.pipe.send_all([make_dict(*request)])
        except Exception as e:
            self.print_error("socket error:", e)
            await self.unsent_requests.put((prio, r))
            sys.exit(1)
            #return False
        if self.debug:
            self.print_error("-->", request)
        self.unanswered_requests[request[2]] = request
        return True

    def ping_required(self):
        '''Maintains time since last ping.  Returns True if a ping should
        be sent.
        '''
        now = time.time()
        if now - self.last_ping > 60:
            self.last_ping = now
            return True
        return False

    def has_timed_out(self):
        '''Returns True if the interface has timed out.'''
        if (self.unanswered_requests and time.time() - self.request_time > 10
            and self.pipe.idle_time() > 10):
            self.print_error("timeout", len(self.unanswered_requests))
            return True

        return False

    async def get_responses(self):
        '''Call if there is data available on the socket.  Returns a list of
        (request, response) pairs.  Notifications are singleton
        unsolicited responses presumably as a result of prior
        subscriptions, so request is None and there is no 'id' member.
        Otherwise it is a response, which has an 'id' member and a
        corresponding request.  If the connection was closed remotely
        or the remote server is misbehaving, a (None, None) will appear.
        '''
        responses = []
        while True:
            response = await self.pipe.get()
            print("pipe get returned")
            if not type(response) is dict:
                responses.append((None, None))
                if response is None:
                    self.closed_remotely = True
                    self.print_error("connection closed remotely")
                break
            if self.debug:
                self.print_error("<--", response)
            wire_id = response.get('id', None)
            if wire_id is None:  # Notification
                responses.append((None, response))
            else:
                request = self.unanswered_requests.pop(wire_id, None)
                if request:
                    responses.append((request, response))
                else:
                    self.print_error("unknown wire ID", wire_id)
                    responses.append((None, None)) # Signal
                    break

        return responses


def check_cert(host, cert):
    try:
        b = pem.dePem(cert, 'CERTIFICATE')
        x = x509.X509(b)
    except:
        traceback.print_exc(file=sys.stdout)
        return

    try:
        x.check_date()
        expired = False
    except:
        expired = True

    m = "host: %s\n"%host
    m += "has_expired: %s\n"% expired
    util.print_msg(m)


# Used by tests
def _match_hostname(name, val):
    if val == name:
        return True

    return val.startswith('*.') and name.endswith(val[1:])


def test_certificates():
    from .simple_config import SimpleConfig
    config = SimpleConfig()
    mydir = os.path.join(config.path, "certs")
    certs = os.listdir(mydir)
    for c in certs:
        p = os.path.join(mydir,c)
        with open(p) as f:
            cert = f.read()
        check_cert(c, cert)

if __name__ == "__main__":
    test_certificates()
