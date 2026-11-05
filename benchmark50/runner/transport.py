"""Opt-in nonstreaming Chat transport; no old endpoints, retries or live imports."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import signal
import threading
import time
from urllib.parse import urlsplit
import httpx
from benchmark50.support import save


class ModelServiceError(RuntimeError): pass


def validate_connection(config):
    if config.get('enabled') is not True:raise ValueError('Model execution disabled; awaiting user endpoint')
    url=config.get('base_url','');parts=urlsplit(url)
    if (parts.scheme not in {'http','https'} or not parts.hostname or parts.username or parts.password
        or parts.query or parts.fragment):raise ValueError('Need credential-free HTTP(S) base_url')
    if not isinstance(config.get('model'),str) or not config['model'].strip():raise ValueError('Exact served model name required')
    if config.get('protocol')!='chat_completions' or config.get('stream') is not False:
        raise ValueError('This runner requires nonstreaming chat_completions')
    if not re.fullmatch(r'[A-Z][A-Z0-9_]*',config.get('api_key_env','')):raise ValueError('API key environment variable required')
    allowed = {'enabled', 'base_url', 'model', 'api_key_env', 'protocol', 'stream', 'provider'}
    if set(config) - allowed: raise ValueError('Unknown connection fields; credentials belong in the named environment variable')
    return {**config,'base_url':url.rstrip('/')}


@contextmanager
def wall_limit(seconds):
    if seconds<=0:raise TimeoutError('Episode deadline reached')
    if threading.current_thread() is not threading.main_thread():raise RuntimeError('Serial main-thread transport required')
    old=signal.getsignal(signal.SIGALRM);timer=signal.getitimer(signal.ITIMER_REAL);start=time.monotonic()
    def expired(*_):raise TimeoutError('HTTP wall deadline reached; no retry')
    signal.signal(signal.SIGALRM,expired)
    signal.setitimer(signal.ITIMER_REAL,min(seconds,timer[0]) if timer[0] else seconds)
    try:yield
    finally:
        signal.setitimer(signal.ITIMER_REAL,0);signal.signal(signal.SIGALRM,old)
        if timer[0]:signal.setitimer(signal.ITIMER_REAL,max(.000001,timer[0]-(time.monotonic()-start)),timer[1])


class ChatTransport:
    def __init__(self,connection,client=None):
        self.config=validate_connection(connection)
        self.client=client or httpx.Client(trust_env=False,follow_redirects=False)

    def __call__(self,request,out,remaining_seconds):
        cfg=self.config;path=Path(out);body={**request,'model':cfg['model'],'stream':False}
        save(path/'wire-request.json',body)
        receipt={'method':'POST','endpoint':cfg['base_url']+'/chat/completions',
                 'requested_model':cfg['model'],'attempts':1,'usage':'unknown'}
        start=time.monotonic()
        try:
            key=os.environ.get(cfg['api_key_env'],'EMPTY')
            with wall_limit(remaining_seconds):
                response=self.client.post(receipt['endpoint'],json=body,
                    headers={'Authorization':'Bearer '+key},timeout=remaining_seconds)
                receipt['status_code']=response.status_code
                (path/'wire-response.txt').write_text(response.text)
                if response.status_code!=200:raise ModelServiceError(f'HTTP {response.status_code}; generation not retried')
                obj=response.json()
                if not isinstance(obj,dict):raise ModelServiceError('Nonobject model response')
                receipt['usage']=obj.get('usage','unknown');receipt['returned_model']=obj.get('model')
                return obj
        except BaseException as exc:
            receipt['error_type']=type(exc).__name__
            raise
        finally:
            receipt['elapsed_seconds']=time.monotonic()-start;save(path/'http.json',receipt)

    def close(self):self.client.close()
