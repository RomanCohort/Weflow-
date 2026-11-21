"""Deliver the user's existing failed /krea image exactly once, without generation."""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8',errors='replace')
from wechat_oracle.db import get_conn
from wechat_oracle.replier import Wx4pyReplier
from wx4py import WeChatClient

path=Path('C:/Users/dev/WeChat-Oracle/data/agent_outputs/57789621333@chatroom/krea2-20261117-114054-3278cf6b9b32.png')
with get_conn() as conn:
    row=conn.execute('SELECT * FROM messages WHERE msg_id=46517 AND group_id=?',('57789621333@chatroom',)).fetchone()
    assert row and '/krea' in row['content_text'] and row['group_name']=='friends'
assert path.is_file() and path.stat().st_size==1203451
wx=WeChatClient();assert wx.connect()
replier=Wx4pyReplier(wx)
replier.send_file('friends',path,None)
print('ATTACHMENT_CONFIRMED; existing-image-retried-once; generation-jobs=0',flush=True)
