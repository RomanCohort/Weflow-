"""Always-on polling safety net for WeFlow SSE omissions, owned by live ingest."""
import threading
import time

from loguru import logger

from ..db import get_conn


class PollingFallback:
    def __init__(self, names, members, *, watermarks=None, auto_discover=False):
        self.names=names
        self.members=members
        self.watermarks=watermarks if watermarks is not None else {}
        self.auto_discover=auto_discover
        self.live_since=int(time.time())-120
        self.stop=threading.Event()
        self.thread=threading.Thread(target=self.run,name='weflow-poll-fallback',daemon=True)

    def initialize(self,conn):
        conn.execute('CREATE TABLE IF NOT EXISTS ingest_poll_state(group_id TEXT PRIMARY KEY,watermark INTEGER NOT NULL)')
        watermarks=self.watermarks
        for group in self.names:
            saved=conn.execute('SELECT watermark FROM ingest_poll_state WHERE group_id=?',(group,)).fetchone()
            last=conn.execute('SELECT max(t) FROM messages WHERE group_id=?',(group,)).fetchone()[0]
            watermarks[group]=int(saved[0] if saved else last if last is not None else time.time())
        return watermarks

    def refresh_sessions(self,client):
        from .live import _all_watched_sessions,_load_group_members
        sessions=_all_watched_sessions(client)
        now=int(time.time())
        missing=[
            (sid,name) for sid,name in sessions
            if sid not in self.names
        ]
        members={}
        for sid,_name in missing:
            members[sid]=_load_group_members(client,sid)
        renamed=0
        for sid,name in sessions:
            if sid in self.names and name and self.names[sid]!=name:
                renamed+=1
            self.names[sid]=name
            if sid in members:
                self.members[sid]=members[sid]
                self.watermarks.setdefault(sid,now-120)
        if missing:
            logger.info(
                'auto-discovered {} new WeFlow session(s)',
                len(missing),
            )
        if renamed:
            logger.info('updated names for {} WeFlow session(s)',renamed)
        return len(missing),renamed

    def poll_once(self,client,conn,watermarks):
        from .live import _poll_session
        total=0
        for group,name in list(self.names.items()):
            if self.stop.is_set():break
            try:
                watermark,count=_poll_session(client,conn,group,name,watermarks[group],self.members.get(group),
                                               live_since=self.live_since)
                watermarks[group]=watermark
                conn.execute('INSERT INTO ingest_poll_state(group_id,watermark) VALUES(?,?) '
                             'ON CONFLICT(group_id) DO UPDATE SET watermark=excluded.watermark',(group,watermark))
                total+=count
                if count:logger.info('poll fallback {}: archived {} message(s)',name or group,count)
            except Exception as exc:
                logger.warning('poll fallback {} failed: {}',name or group,type(exc).__name__)
        return total

    def run(self):
        from .live import _build_client
        with _build_client() as client,get_conn() as conn:
            watermarks=self.initialize(conn)
            logger.info('poll fallback started for {} approved group(s); interval=3s',len(self.names))
            next_refresh=0.0
            while not self.stop.is_set():
                if self.auto_discover and time.monotonic()>=next_refresh:
                    try:
                        self.refresh_sessions(client)
                    except Exception as exc:
                        logger.warning('session discovery failed: {}',type(exc).__name__)
                    next_refresh=time.monotonic()+30.0
                self.poll_once(client,conn,watermarks)
                self.stop.wait(3)

    def close(self):
        self.stop.set();self.thread.join(timeout=35)
