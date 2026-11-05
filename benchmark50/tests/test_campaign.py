from pathlib import Path
import sys
import json
import unittest
from benchmark50.scripts import run as run_agents
from benchmark50.support import CONFIG, HERE, load_inputs


class CampaignTests(unittest.TestCase):
    def test_schedule_150_unique_tasks_balanced_order(self):
        qs=[json.loads(line) for line in (HERE/'data/questions.jsonl').read_text().splitlines()];s=run_agents.schedule(qs)
        self.assertEqual(len(s),150)
        self.assertEqual(len({(q['query_id'],a) for q,a in s}),150)
        self.assertEqual({a for q,a in s},set(CONFIG['arms']))
        self.assertNotEqual(s[0][1],s[3][1])

    def test_default_connection_and_no_execute_cannot_launch(self):
        with self.assertRaises(ValueError):run_agents.launch_gate(HERE/'config/connection.example.json',False)
        with self.assertRaises(ValueError):run_agents.launch_gate(HERE/'config/connection.example.json',True)

    def test_run_name_cannot_escape_experiment(self):
        for name in ['../old-run','/tmp/run','a/b','..','']:
            with self.assertRaises(ValueError):run_agents.run_path(name)
        self.assertEqual(run_agents.run_path('model50-01').parent,Path('runs').resolve())

    def test_partial_episode_is_counted_on_campaign_interruption(self):
        import tempfile,json
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);case=p/'q--legacy-tools-agent';case.mkdir()
            row={'query_id':'q','arm':'legacy-tools-agent','answer':None,'stop_reason':'interrupted',
                 'public_question':dict(query_id='q',start='s',target='t',max_hops=4),
                 'observations':[],'counters':{'model_rounds':1},'usage':{'unknown_usage_responses':1}}
            (case/'result.json').write_text(json.dumps(row))
            refs={'q':{**row['public_question'],'expected_paths':[['s','t']],'graph_raw_sha256':'hash','instance_id':'test','cohort':'test'}}
            rows=run_agents.collect_rows(p,refs)
            self.assertEqual(len(rows),1);self.assertEqual(rows[0]['stop_reason'],'interrupted')
            self.assertFalse(rows[0]['success'])


if __name__=='__main__':unittest.main()
