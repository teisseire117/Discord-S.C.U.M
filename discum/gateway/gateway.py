import websocket
import _thread as thread
import json
import time
import random
import base64

from .sessionsettings import SessionSettings

class InvalidSession(Exception):
    pass

class GatewayServer:

    class LogLevel:
        SEND = '\033[94m'
        RECEIVE = '\033[92m'
        WARNING = '\033[93m'
        DEFAULT = '\033[m'

    class OPCODE: #https://discordapp.com/developers/docs/topics/opcodes-and-status-codes
        # Name                  Code    Client Action   Description
        DISPATCH =              0  #    Receive         dispatches an event
        HEARTBEAT =             1  #    Send/Receive    used for ping checking
        IDENTIFY =              2  #    Send            used for client handshake
        STATUS_UPDATE =         3  #    Send            used to update the client status
        VOICE_UPDATE =          4  #    Send            used to join/move/leave voice channels
        #                       5  #    ???             ???
        RESUME =                6  #    Send            used to resume a closed connection
        RECONNECT =             7  #    Receive         used to tell clients to reconnect to the gateway
        REQUEST_GUILD_MEMBERS = 8  #    Send            used to request guild members
        INVALID_SESSION =       9  #    Receive         used to notify client they have an invalid session id
        HELLO =                 10 #    Receive         sent immediately after connecting, contains heartbeat and server debug information
        HEARTBEAT_ACK =         11 #    Sent immediately following a client heartbeat that was received
        GUILD_SYNC =            12 #

    def __init__(self, websocketurl, token, ua_data, proxy_host, proxy_port, log):
        self.token = token
        self.ua_data = ua_data
        self.auth = {
                "token": self.token,
                "capabilities": 61,
                "properties": {
                    "os": self.ua_data["os"],
                    "browser": self.ua_data["browser"],
                    "device": self.ua_data["device"],
                    "browser_user_agent": self.ua_data["browser_user_agent"],
                    "browser_version": self.ua_data["browser_version"],
                    "os_version": self.ua_data["os_version"],
                    "referrer": "",
                    "referring_domain": "",
                    "referrer_current": "",
                    "referring_domain_current": "",
                    "release_channel": "stable",
                    "client_build_number": 71420,
                    "client_event_source": None
                },
                "presence": {
                    "status": "online",
                    "since": 0,
                    "activities": [],
                    "afk": False
                },
                "compress": False,
                "client_state": {
                    "guild_hashes": {},
                    "highest_last_message_id": "0",
                    "read_state_version": 0,
                    "user_guild_settings_version": -1
                }
            }
        if 'build_num' in self.ua_data and self.ua_data['build_num']!=71420:
            self.auth['properties']['client_build_number'] = self.ua_data['build_num']

        self.proxy_host = None if proxy_host in (None,False) else proxy_host
        self.proxy_port = None if proxy_port in (None,False) else proxy_port

        self.log = log

        self.interval = None
        self.session_id = None
        self.sequence = 0
        self.READY = False #becomes True once READY_SUPPLEMENTAL is received
        self.settings_ready = {}
        self.settings_ready_supp = {}

        #websocket.enableTrace(True)
        self.ws = self._get_ws_app(websocketurl)

        self._after_message_hooks = []
        self._last_err = None

        self.connected = False

        self.voice_data = {} #voice connections dependent on current (connected) session

    #WebSocketApp, more info here: https://github.com/websocket-client/websocket-client/blob/master/websocket/_app.py#L79
    def _get_ws_app(self, websocketurl):
        sec_websocket_key = base64.b64encode(bytes(random.getrandbits(8) for _ in range(16))).decode() #https://websockets.readthedocs.io/en/stable/_modules/websockets/handshake.html
        headers = {
            "Host": "gateway.discord.gg",
            "Connection": "Upgrade",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "User-Agent": self.ua_data["useragent"],
            "Upgrade": "websocket",
            "Origin": "https://discord.com",
            "Sec-WebSocket-Version": "13",
            "Accept-Language": "en-US",
            "Sec-WebSocket-Key": sec_websocket_key
        } #more info: https://stackoverflow.com/a/40675547

        ws = websocket.WebSocketApp(websocketurl,
                                    header = headers,
                                    on_open=lambda ws: self.on_open(ws),
                                    on_message=lambda ws, msg: self.on_message(ws, msg),
                                    on_error=lambda ws, msg: self.on_error(ws, msg),
                                    on_close=lambda ws: self.on_close(ws)
                                    )
        return ws

    def on_open(self, ws):
        self.connected = True
        if self.log: print("Connected to websocket.")
        self.send({"op": self.OPCODE.IDENTIFY, "d": self.auth})

    def on_message(self, ws, message):
        self.sequence += 1
        resp = json.loads(message)
        if self.log: print(f'{self.LogLevel.RECEIVE}< {resp}{self.LogLevel.DEFAULT}')
        if resp['op'] == self.OPCODE.HELLO: #only happens once, first message sent to client
            self.interval = (resp["d"]["heartbeat_interval"]-2000)/1000
            thread.start_new_thread(self._heartbeat, ())
        elif resp['op'] == self.OPCODE.INVALID_SESSION:
            if self.log: print("Invalid session.")
            self._last_err = InvalidSession()
            self.sequence = 0
            self.ws.keep_running = False
        if self.interval == None:
            if self.log: print("Identify failed.")
            self.ws.keep_running = False
        if resp['t'] == "READY":
            self.session_id = resp['d']['session_id']
            self.settings_ready = resp['d']
        elif resp['t'] == "READY_SUPPLEMENTAL":
            self.settings_ready_supp = resp['d']
            self.SessionSettings = SessionSettings(self.settings_ready, self.settings_ready_supp)
            self.READY = True
        elif resp['t'] in ("VOICE_SERVER_UPDATE", "VOICE_STATE_UPDATE"):
            self.voice_data.update(resp['d']) #called twice, resulting in a dictionary with 12 keys
        thread.start_new_thread(self._response_loop, (resp,))

    def on_error(self, ws, error):
        if self.log: print(f'{self.LogLevel.WARNING}{error}{self.LogLevel.DEFAULT}')
        self._last_err = error

    def on_close(self, ws):
        self.connected = False
        self.READY = False #reset self.READY
        if self.log: print('websocket closed')

    #Discord needs heartbeats, or else connection will sever
    def _heartbeat(self):
        if self.log: print("entering heartbeat")
        while self.connected:
            time.sleep(self.interval)
            if not self.connected:
                break
            self.send({"op": self.OPCODE.HEARTBEAT,"d": self.sequence-1 if self.sequence>0 else self.sequence})

    #just a wrapper for ws.send
    def send(self, payload):
        if self.log: print(f'{self.LogLevel.SEND}> {payload}{self.LogLevel.RECEIVE}')
        self.ws.send(json.dumps(payload))

    #close websocket
    def close(self):
        self.ws.keep_running = False

    #the next 2 functions come from https://github.com/scrubjay55/Reddit_ChatBot_Python/blob/master/Reddit_ChatBot_Python/Utils/WebSockClient.py (Apache License 2.0)
    def command(self, func):
        self._after_message_hooks.append(func)
        return func

    def _response_loop(self, resp):
        for func in self._after_message_hooks:
            if func(resp):
                break

    def removeCommand(self, func):
        try:
            self._after_message_hooks.remove(func)
        except ValueError:
            if self.log: print(f'{func} not found in _after_message_hooks.')
            pass

    def clearCommands(self):
        self._after_message_hooks = []

    #modified version of function run_4ever from https://github.com/scrubjay55/Reddit_ChatBot_Python/blob/master/Reddit_ChatBot_Python/Utils/WebSockClient.py (Apache License 2.0)
    def run(self, auto_reconnect=True):
        while auto_reconnect: #interestingly, web clients don't actually send resume packets so...
            self.ws.run_forever(ping_interval=10, ping_timeout=5, http_proxy_host=self.proxy_host, http_proxy_port=self.proxy_port)
            if isinstance(self._last_err, websocket._exceptions.WebSocketAddressException) or isinstance(self._last_err, websocket._exceptions.WebSocketTimeoutException) or isinstance(self._last_err, InvalidSession):
                if self.log: print("Connection Dropped. Retrying in 10 seconds.")
                time.sleep(10)
                self.ws.run_forever(ping_interval=10, ping_timeout=5, http_proxy_host=self.proxy_host, http_proxy_port=self.proxy_port)
                continue
            else:
                return 0
        if not auto_reconnect:
            self.ws.run_forever(ping_interval=10, ping_timeout=5, http_proxy_host=self.proxy_host, http_proxy_port=self.proxy_port)