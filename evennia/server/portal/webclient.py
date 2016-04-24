"""
Webclient based on websockets.

This implements a webclient with WebSockets (http://en.wikipedia.org/wiki/WebSocket)
by use of the txws implementation (https://github.com/MostAwesomeDude/txWS). It is
used together with evennia/web/media/javascript/evennia_websocket_webclient.js.

Thanks to Ricard Pillosu whose Evennia plugin inspired this module.

Communication over the websocket interface is done with normal text
communication. A special case is OOB-style communication; to do this
the client must send data on the following form:

    OOB{"func1":[args], "func2":[args], ...}

where the dict is JSON encoded. The initial OOB-prefix is used to
identify this type of communication, all other data is considered
plain text (command input).

Example of call from a javascript client:

    var websocket = new WebSocket("ws://localhost:8021");
    var msg1 = "WebSocket Test";
    websocket.send(msg1);
    var msg2 = JSON.stringify({ testfunc: [[1, 2, 3], { kwarg: "val" }] });
    websocket.send("OOB" + msg2);
    websocket.close();

"""
import re
import json
from twisted.internet.protocol import Protocol
from django.conf import settings
from evennia.server.session import Session
from evennia.utils.utils import to_str
from evennia.utils.ansi import parse_ansi
from evennia.utils.text2html import parse_html

_RE_SCREENREADER_REGEX = re.compile(r"%s" % settings.SCREENREADER_REGEX_STRIP, re.DOTALL + re.MULTILINE)

class WebSocketClient(Protocol, Session):
    """
    Implements the server-side of the Websocket connection.
    """

    def connectionMade(self):
        """
        This is called when the connection is first established.

        """
        client_address = self.transport.client
        client_address = client_address[0] if client_address else None
        self.init_session("websocket", client_address, self.factory.sessionhandler)
        # watch for dead links
        self.transport.setTcpKeepAlive(1)
        self.sessionhandler.connect(self)

    def disconnect(self, reason=None):
        """
        Generic hook for the engine to call in order to
        disconnect this protocol.

        Args:
            reason (str): Motivation for the disconnection.

        """
        self.data_out(text=((reason or "",), {}))
        self.connectionLost(reason)

    def connectionLost(self, reason):
        """
        This is executed when the connection is lost for whatever
        reason. it can also be called directly, from the disconnect
        method.

        Args:
            reason (str): Motivation for the lost connection.

        """
        self.sessionhandler.disconnect(self)
        self.transport.close()

    def dataReceived(self, string):
        """
        Method called when data is coming in over the websocket
        connection. This is always a JSON object on the following
        form:
            [cmdname, [args], {kwargs}]


        """
        cmdarray = json.loads(string)
        if cmdarray:
            self.data_in(**{cmdarray[0]:[cmdarray[1], cmdarray[2]]})

    def sendLine(self, line):
        """
        Send data to client.

        Args:
            line (str): Text to send.

        """

        return self.transport.write(line)

    def data_in(self, **kwargs):
        """
        Data User > Evennia.

        Args::
            text (str): Incoming text.
            kwargs (any): Options from protocol.

        Notes:
            The websocket client can send the
            "websocket_close" command to report
            that the client has been closed and
            that the session should be disconnected.

        """
        if "websocket_close" in kwargs:
            self.disconnect()
            return
        self.sessionhandler.data_in(self, **kwargs)

    def data_out(self, **kwargs):
        """
        Data Evennia->User.

        Kwargs:
            kwargs (any): Options ot the protocol
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
        text = to_str(text, force_string=True)

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
        print "send_text:", cmd, args, kwargs

        # send to client on required form [cmdname, args, kwargs]
        self.sendLine(json.dumps([cmd, args, kwargs]))


    def send_prompt(self, *args, **kwargs):
        kwargs["options"].update({"send_prompt": True})
        self.send_text(*args, **kwargs)

    def send_default(session, cmdname, *args, **kwargs):
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
            print "websocket.send_default", cmdname, args, kwargs
            session.sendLine(json.dumps([cmdname, args, kwargs]))
