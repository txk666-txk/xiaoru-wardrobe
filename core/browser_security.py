"""Local workbench sessions and single-use WebSocket capabilities.

This is a local operator boundary, not a public customer account system.
"""
from __future__ import annotations

import secrets
import threading
import time
import re
from urllib.parse import parse_qs, urlsplit


def valid_username(username):
    return (isinstance(username, str) and re.fullmatch(r'[\w.-]{1,64}', username) is not None
            and '..' not in username and not username.endswith('.')
            and username.split('.')[0].upper() not in {'CON', 'PRN', 'AUX', 'NUL',
                *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))})


class BrowserAuthority:
    SESSION_TTL = 8 * 60 * 60
    TICKET_TTL = 30

    def __init__(self, clock=time.time):
        self.clock = clock
        self.lock = threading.RLock()
        self.sessions = {}
        self.owners = {}
        self.tickets = {}

    def session(self, token=None):
        with self.lock:
            now = self.clock()
            expired = {key for key, value in self.sessions.items() if value['expires'] <= now}
            for key in expired:
                del self.sessions[key]
            self.owners = {name: owner for name, owner in self.owners.items() if owner not in expired}
            self.tickets = {key: value for key, value in self.tickets.items() if value['expires'] > now and value['session'] not in expired}
            if token not in self.sessions:
                token = secrets.token_urlsafe(32)
                self.sessions[token] = {'csrf': secrets.token_urlsafe(32), 'expires': now + self.SESSION_TTL}
            self.sessions[token]['expires'] = now + self.SESSION_TTL
            return token, dict(self.sessions[token])

    def claim(self, session, username):
        if not valid_username(username):
            return False
        with self.lock:
            if session not in self.sessions or self.sessions[session]['expires'] <= self.clock():
                return False
            if username in self.owners and self.owners[username] != session:
                return False
            self.owners[username] = session
            return True

    def issue(self, session, username, audience, origin):
        with self.lock:
            if audience not in ('web', 'human') or not self.claim(session, username):
                raise ValueError('聊天身份正由另一个浏览器使用，请选择其他用户')
            token = secrets.token_urlsafe(32)
            self.tickets[token] = {'session': session, 'username': username, 'audience': audience,
                                   'origin': origin, 'expires': self.clock() + self.TICKET_TTL}
            return token

    def consume(self, path, audience, origin):
        values = parse_qs(urlsplit(path).query).get('ticket', [])
        if len(values) != 1:
            return None
        with self.lock:
            ticket = self.tickets.pop(values[0], None)
            if not ticket or ticket['expires'] <= self.clock():
                return None
            if ticket['audience'] != audience or ticket['origin'] != origin:
                return None
            if not self.authorized(ticket['session'], ticket['username']):
                return None
            return ticket

    def authorized(self, session, username):
        with self.lock:
            return (session in self.sessions and self.sessions[session]['expires'] > self.clock()
                    and self.owners.get(username) == session)


authority = BrowserAuthority()
