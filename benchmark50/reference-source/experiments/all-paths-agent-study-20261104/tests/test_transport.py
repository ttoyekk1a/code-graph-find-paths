from pathlib import Path
import sys
import tempfile
import unittest
import httpx

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import transport


class TransportTests(unittest.TestCase):
    def connection(self,**kw):
        return dict(enabled=True,base_url='https://example.invalid/v1',model='provided-model',
                    api_key_env='GRAPH_STUDY_API_KEY',api_key_default='EMPTY',protocol='chat_completions',stream=False,**kw)

    def test_empty_disabled_or_wrong_protocol_blocks(self):
        for override in [{'enabled':False},{'base_url':''},{'model':''},{'stream':True},{'protocol':'responses'},
                         {'base_url':'https://user:secret@example.invalid/v1'}]:
            c=self.connection();c.update(override)
            with self.assertRaises(ValueError):transport.validate_connection(c)

    def test_one_attempt_no_retry_and_no_auth_in_disk_receipt(self):
        seen=[]
        def mock(req):
            seen.append(req)
            return httpx.Response(504,text='gateway failure')
        client=httpx.Client(transport=httpx.MockTransport(mock))
        with tempfile.TemporaryDirectory() as td:
            t=transport.ChatTransport(self.connection(),client=client)
            with self.assertRaises(transport.ModelServiceError):t({'messages':[]},Path(td),5)
            self.assertEqual(len(seen),1)
            self.assertNotIn('Authorization',(Path(td)/'http.json').read_text())

    def test_stream_false_and_exact_model(self):
        import json
        def mock(req):
            body=json.loads(req.content)
            self.assertIs(body['stream'],False)
            self.assertEqual(body['model'],'provided-model')
            return httpx.Response(200,json={'model':'provided-model','choices':[{'message':{'role':'assistant','content':'ok'}}]})
        with tempfile.TemporaryDirectory() as td:
            t=transport.ChatTransport(self.connection(),client=httpx.Client(transport=httpx.MockTransport(mock)))
            t({'messages':[]},Path(td),5)


if __name__=='__main__':unittest.main()
