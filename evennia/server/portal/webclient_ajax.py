"""
AJAX/COMET fallback webclient

The AJAX/COMET web client consists of two components running on
twisted and django. They are both a part of the Evennia website url
tree (so the testing website might be located on
http://localhost:8000/, whereas the webclient can be found on
http://localhost:8000/webclient.)

/webclient - this url is handled through django's template
             system and serves the html page for the client
             itself along with its javascript chat program.
/webclientdata - this url is called by the ajax chat using
                 POST requests (long-polling when necessary)
                 The WebClient resource in this module will
                 handle these requests and act as a gateway
                 to sessions connected over the webclient.
"""
import json
import re

from time import time
from hashlib import md5
from twisted.web import server, resource
from twisted.internet.task import LoopingCall
from django.utils.functional import Promise
from django.utils.encoding import force_unicode
from django.conf import settings
from evennia.utils.ansi import parse_ansi
from evennia.utils import utils
from evennia.utils.text2html import parse_html
from evennia.server import session

_RE_SCREENREADER_REGEX = re.compile(r"%s" % settings.SCREENREADER_REGEX_STRIP, re.DOTALL + re.MULTILINE)
_SERVERNAME = settings.SERVERNAME
_KEEPALIVE = 30 # how often to check keepalive

# defining a simple json encoder for returning
# django data to the client. Might need to
# extend this if one wants to send more
# complex database objects too.

class LazyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Promise):
            return force_unicode(obj)
        return super(LazyEncoder, self).default(obj)


def jsonify(obj):
    return utils.to_str(json.dumps(obj, ensure_ascii=False, cls=LazyEncoder))


#
# WebClient resource - this is called by the ajax client
# using POST requests to /webclientdata.
#

class WebClient(resource.Resource):
    """
    An ajax/comet long-polling transport

    """
    isLeaf = True
    allowedMethods = ('POST',)

    def __init__(self):
        self.requests = {}
        self.databuffer = {}

        self.last_alive = {}
        self.keep_alive = None

    def _responseFailed(self, failure, suid, request):
        "callback if a request is lost/timed out"
        try:
            del self.requests[suid]
        except KeyError:
            pass

    def _keepalive(self):
        """
        Callback for checking the connection is still alive.
        """
        now = time()
        to_remove = []
        keep_alives = ((suid, remove) for suid, (t, remove)
                        in self.last_alive.iteritems() if now - t > _KEEPALIVE)
        for suid, remove in keep_alives:
            if remove:
                # keepalive timeout. Line is dead.
                to_remove.append(suid)
            else:
                # normal timeout - send keepalive
                self.last_alive[suid] = (now, True)
                self.lineSend(suid, ["ajax_keepalive", [], {}])
        # remove timed-out sessions
        for suid in to_remove:
            sess = self.sessionhandler.session_from_suid(suid)
            if sess:
                sess[0].disconnect()
            self.last_alive.pop(suid, None)
            if not self.last_alive:
                # no more ajax clients. Stop the keepalive
                self.keep_alive.stop()
                self.keep_alive = None

    def lineSend(self, suid, data):
        """
        This adds the data to the buffer and/or sends it to the client
        as soon as possible.

        Args:
            suid (int): Session id.
            data (list): A send structure [cmdname, [args], {kwargs}].

        """
        request = self.requests.get(suid)
        if request:
            # we have a request waiting. Return immediately.
            request.write(jsonify(data))
            request.finish()
            del self.requests[suid]
        else:
            # no waiting request. Store data in buffer
            dataentries = self.databuffer.get(suid, [])
            dataentries.append(jsonify(data))
            self.databuffer[suid] = dataentries

    def client_disconnect(self, suid):
        """
        Disconnect session with given suid.

        Args:
            suid (int): Session id.

        """
        if suid in self.requests:
            self.requests[suid].finish()
            del self.requests[suid]
        if suid in self.databuffer:
            del self.databuffer[suid]

    def mode_init(self, request):
        """
        This is called by render_POST when the client requests an init
        mode operation (at startup)

        Args:
            request (Request): Incoming request.

        """
        suid = request.args.get('suid', ['0'])[0]

        remote_addr = request.getClientIP()
        host_string = "%s (%s:%s)" % (_SERVERNAME, request.getRequestHostname(), request.getHost().port)
        if suid == '0':
            # creating a unique id hash string
            suid = md5(str(time())).hexdigest()
            self.databuffer[suid] = []

            sess = WebClientSession()
            sess.client = self
            sess.init_session("ajax/comet", remote_addr, self.sessionhandler)
            sess.suid = suid
            sess.sessionhandler.connect(sess)

            self.last_alive[suid] = (time(), False)
            if not self.keep_alive:
                # the keepalive is not running; start it.
                self.keep_alive = LoopingCall(self._keepalive)
                self.keep_alive.start(_KEEPALIVE, now=False)

        return jsonify({'msg': host_string, 'suid': suid})

    def mode_keepalive(self, request):
        """
        This is called by render_POST when the
        client is replying to the keepalive.
        """
        suid = request.args.get('suid', ['0'])[0]
        if suid == '0':
            return '""'
        print "keepalive succeeded"
        self.last_alive[suid] = (time(), False)
        return '""'

    def mode_input(self, request):
        """
        This is called by render_POST when the client
        is sending data to the server.

        Args:
            request (Request): Incoming request.

        """
        suid = request.args.get('suid', ['0'])[0]
        if suid == '0':
            return '""'

        self.last_alive[suid] = (time(), False)
        sess = self.sessionhandler.session_from_suid(suid)
        if sess:
            sess = sess[0]
            cmdarray = json.loads(request.args.get('data')[0])
            sess.sessionhandler.data_in(sess, **{cmdarray[0]:[cmdarray[1], cmdarray[2]]})
        return '""'

    def mode_receive(self, request):
        """
        This is called by render_POST when the client is telling us
        that it is ready to receive data as soon as it is available.
        This is the basis of a long-polling (comet) mechanism: the
        server will wait to reply until data is available.

        Args:
            request (Request): Incoming request.

        """
        suid = request.args.get('suid', ['0'])[0]
        if suid == '0':
            return '""'
        self.last_alive[suid] = (time(), False)

        dataentries = self.databuffer.get(suid, [])
        if dataentries:
            return dataentries.pop(0)
        request.notifyFinish().addErrback(self._responseFailed, suid, request)
        if suid in self.requests:
            self.requests[suid].finish()  # Clear any stale request.
        self.requests[suid] = request
        return server.NOT_DONE_YET

    def mode_close(self, request):
        """
        This is called by render_POST when the client is signalling
        that it is about to be closed.

        Args:
            request (Request): Incoming request.

        """
        suid = request.args.get('suid', ['0'])[0]
        if suid == '0':
            self.client_disconnect(suid)
        else:
            try:
                sess = self.sessionhandler.session_from_suid(suid)[0]
                sess.sessionhandler.disconnect(sess)
            except IndexError:
                self.client_disconnect(suid)
                pass
        return '""'

    def render_POST(self, request):
        """
        This function is what Twisted calls with POST requests coming
        in from the ajax client. The requests should be tagged with
        different modes depending on what needs to be done, such as
        initializing or sending/receving data through the request. It
        uses a long-polling mechanism to avoid sending data unless
        there is actual data available.

        Args:
            request (Request): Incoming request.

        """
        dmode = request.args.get('mode', [None])[0]
        if dmode == 'init':
            # startup. Setup the server.
            return self.mode_init(request)
        elif dmode == 'input':
            # input from the client to the server
            return self.mode_input(request)
        elif dmode == 'receive':
            # the client is waiting to receive data.
            return self.mode_receive(request)
        elif dmode == 'close':
            # the client is closing
            return self.mode_close(request)
        elif dmode == 'keepalive':
            # A reply to our keepalive request - all is well
            return self.mode_keepalive(request)
        else:
            # This should not happen if client sends valid data.
            return '""'


#
# A session type handling communication over the
# web client interface.
#

class WebClientSession(session.Session):
    """
    This represents a session running in a webclient.
    """

    def __init__(self, *args, **kwargs):
        self.protocol_name = "ajax/comet"
        super(WebClientSession, self).__init__(*args, **kwargs)

    def disconnect(self, reason="Server disconnected."):
        """
        Disconnect from server.

        Args:
            reason (str): Motivation for the disconnect.
        """
        self.client.lineSend(self.suid, ["connection_close", [reason], {}])
        self.client.client_disconnect(self.suid)
        self.sessionhandler.disconnect(self)

    def data_out(self, **kwargs):
        """
        Data Evennia -> User

        Kwargs:
            kwargs (any): Options to the protocol
        """
        self.sessionhandler.data_out(self, **kwargs)

    def send_text(self, *args, **kwargs):
        """
        Send text data. This will pre-process the text for
        color-replacement, conversion to html etc.

        Args:
            text (str): Text to send.

        Kwargs:
            options (dict): Options-dict with the following keys understood:
                - raw (bool): No parsing at all (leave ansi-to-html markers unparsed).
                - nomarkup (bool): Clean out all ansi/html markers and tokens.
                - screenreader (bool): Use Screenreader mode.
                - send_prompt (bool): Send a prompt with parsed html

        """
        if args:
            args = list(args)
            text = args[0]
            if text is None:
                return
        else:
            return

        flags = self.protocol_flags
        text = utils.to_str(text, force_string=True)

        options = kwargs.pop("options", {})
        raw = options.get("raw", False)
        nomarkup = options.get("nomarkup", False)
        screenreader = options.get("screenreader", flags.get("SCREENREADER", False))
        prompt = options.get("send_prompt", False)

        if screenreader:
            # screenreader mode cleans up output
            text = parse_ansi(text, strip_ansi=True, xterm256=False, mxp=False)
            text = _RE_SCREENREADER_REGEX.sub("", text)
        cmd = "prompt" if prompt else "text"
        if raw:
            args[0] = text
        else:
            args[0] = parse_html(text, strip_ansi=nomarkup)

        # send to client on required form [cmdname, args, kwargs]
        self.client.lineSend(self.suid, [cmd, args, kwargs])

    def send_prompt(self, *args, **kwargs):
        kwargs["options"].update({"send_prompt": True})
        self.send_text(*args, **kwargs)

    def send_default(self, cmdname, *args, **kwargs):
        """
        Data Evennia -> User.

        Args:
            cmdname (str): The first argument will always be the oob cmd name.
            *args (any): Remaining args will be arguments for `cmd`.

        Kwargs:
            options (dict): These are ignored for oob commands. Use command
                arguments (which can hold dicts) to send instructions to the
                client instead.

        """
        if not cmdname == "options":
            #print "ajax.send_default", cmdname, args, kwargs
            self.client.lineSend(self.suid, [cmdname, args, kwargs])
