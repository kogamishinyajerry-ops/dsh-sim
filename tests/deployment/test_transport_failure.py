"""No Docker/solver: full launcher entrypoints with a strict transport fault fixture.
Inner-shell checks additionally execute the actual query bodies with safe fixtures.
Run: python -m unittest discover -s tests/deployment -p 'test_transport_failure.py' -v
"""
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / 'deployment/local-acceptance/run-local.sh'
STUB = r'''#!/usr/bin/env python3
import os, sys, json, re
from pathlib import Path
args=sys.argv[1:]; mode=os.environ['MODE']
with open(os.environ['CALLS'],'a') as f: f.write(json.dumps(args)+'\n')
if args[0]=='ps':
 if mode=='container_failure': sys.exit(42)
 print('jerrydsh-sim-env'); sys.exit(0)
if args[0]=='cp': sys.exit(42 if mode=='copy_failure' else 0)
if args[0]!='exec': sys.exit(98)
cmd=args[2:]; text='\n'.join(cmd)
def done(s='',rc=0):
 if s: print(s)
 sys.exit(rc)
if '# dsh-query: targets' in text:
 if mode=='list_unavailable': done(rc=42)
 if mode=='partial_output': done('12345',42)
 if mode=='invalid_pid': done('-123')
 if mode=='pid_one': done('1')
 done('' if mode=='empty_success' else '12345')
if '# dsh-query: signal' in text: done(rc=42 if mode=='signal_unavailable' else 0)
if '# dsh-query: live-pids' in text:
 if mode=='probe_unavailable': done(rc=42)
 if mode=='never_exit': done('12345')
 if mode=='delayed_exit':
  p=Path(os.environ['COUNT']); n=int(p.read_text()) if p.exists() else 0
  p.write_text(str(n+1)); done('12345' if n==0 else '')
 if mode=='start_probe_failure': done(rc=42)
 done()
if '# dsh-query: cleanup' in text: done(rc=42 if mode=='cleanup_unavailable' else 0)
if '# dsh-query: pid-file' in text: done(rc=42 if mode=='pid_read_failure' else 0)
if '# dsh-query: log-size' in text: done('0')
if '# dsh-query: worker-ready' in text: done(rc=42)
if 'nohup' in text: done(rc=42)
if cmd and cmd[0] in ('mkdir','chmod'): done()
done(rc=98)
'''

class LauncherTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='dsh-transport-test-')
  self.root=Path(self.tmp.name)
  self.script=self.root/'run-local.sh'; shutil.copyfile(SCRIPT,self.script)
  (self.root/'launch-worker.sh').write_text('# Fixture; never executed by the transport stub\n')
  for name,content in [('docker',STUB),('curl','#!/bin/sh\nexit 7\n')]:
   f=self.root/name;f.write_text(content.replace('#!/usr/bin/env python3', '#!'+sys.executable+' -S'));f.chmod(0o755)
  self.env={k:v for k,v in os.environ.items() if not k.startswith('DSH_SIM_')}
  self.env.update(PATH=str(self.root)+os.pathsep+os.environ['PATH'],CALLS=str(self.root/'calls'),COUNT=str(self.root/'count'),DSH_SIM_STOP_TIMEOUT='1',DSH_SIM_START_TIMEOUT='1')
 def tearDown(self): self.tmp.cleanup()
 def run_case(self,mode,action='stop'):
  env={**self.env,'MODE':mode}
  if mode=='delayed_exit': env['DSH_SIM_STOP_TIMEOUT']='8'
  p=subprocess.run(['bash',str(self.script),action],env=env,capture_output=True,text=True,timeout=12)
  calls=[json.loads(x) for x in (self.root/'calls').read_text().splitlines()]
  return p,calls
 def failed(self,mode,action='stop',rc=42):
  p,calls=self.run_case(mode,action)
  self.assertEqual(p.returncode,rc,p.stdout+p.stderr)
  self.assertNotIn('[run-local] stopped',p.stdout)
  self.assertNotIn('already stopped',p.stdout)
  self.assertNotIn('confirmed exited',p.stdout)
  if rc==42: self.assertIn('rc=42',p.stderr)
  return calls
 def test_container_query_failure(self): self.failed('container_failure')
 def test_list_failure(self):
  c=self.failed('list_unavailable'); self.assertFalse(any('dsh-query: signal' in str(a) for a in c))
 def test_partial_list_is_not_trusted(self): self.failed('partial_output')
 def test_signal_failure(self):
  c=self.failed('signal_unavailable'); self.assertFalse(any('dsh-query: cleanup' in str(a) for a in c))
 def test_exit_probe_failure(self): self.failed('probe_unavailable')
 def test_cleanup_failure(self): self.failed('cleanup_unavailable')
 def test_delayed_exit(self):
  p,_=self.run_case('delayed_exit'); self.assertEqual(p.returncode,0,p.stderr)
  for t in ('已请求','正在退出','已退出','[run-local] stopped'): self.assertIn(t,p.stdout)
 def test_never_exit(self): self.failed('never_exit',rc=1)
 def test_empty_success(self):
  p,c=self.run_case('empty_success');self.assertEqual(p.returncode,0,p.stderr)
  self.assertIn('already stopped',p.stdout);self.assertFalse(any('dsh-query: signal' in str(a) for a in c))
 def test_invalid_pid(self): self.failed('invalid_pid',rc=1)
 def test_pid_one(self): self.failed('pid_one',rc=1)
 def test_restart_does_not_start_when_stop_unknown(self):
  self.script.chmod(0o755)
  p,c=self.run_case('list_unavailable','restart');self.assertNotEqual(p.returncode,0)
  self.assertFalse(any('nohup' in str(a) for a in c))
 def test_status_unavailable(self): self.failed('list_unavailable','status')
 def test_start_read_failure(self): self.failed('pid_read_failure','start-api')
 def test_start_probe_failure(self): self.failed('start_probe_failure','start-api')
 def test_start_command_preserves_rc(self): self.failed('start_command_failure','start-api')
 def test_worker_launch_failure(self): self.failed('start_command_failure','start-worker')
 def test_combined_start_failure(self): self.failed('start_command_failure','start')
 def test_copy_failure_nonzero(self): self.failed('copy_failure','start-worker',rc=1)
 def test_unsafe_configuration_before_docker(self):
  env={**self.env,'MODE':'empty_success','DSH_SIM_DATA_DIR':"/tmp/';echo INJECTED;#"}
  p=subprocess.run(['bash',str(self.script),'stop'],env=env,capture_output=True,text=True)
  self.assertEqual(p.returncode,64);self.assertFalse((self.root/'calls').exists())

class InnerQueryTests(unittest.TestCase):
 """Execute actual inner bash snippets, without a Docker daemon or real TERM."""
 def body(self,marker):
  s=SCRIPT.read_text(); start=s.index('# dsh-query: '+marker);end=s.index("\n  ' _",start)
  return s[start:end]
 def test_signal_permission_failure_is_not_exit(self):
  body='kill() { return 1; };\n'+self.body('signal')
  r=subprocess.run(['bash','-c',body,'_',str(os.getpid())],capture_output=True,text=True)
  self.assertEqual(r.returncode,77)
 def test_signal_disappearance_race_is_accepted(self):
  body='kill() { return 1; };\n'+self.body('signal')
  r=subprocess.run(['bash','-c',body,'_','99999999'],capture_output=True,text=True)
  self.assertEqual(r.returncode,0)
 def test_pgrep_no_match_is_success(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'pgrep';p.write_text('#!/bin/sh\nexit 1\n');p.chmod(0o755)
   r=subprocess.run(['bash','-c',self.body('targets'),'_',d],env={**os.environ,'PATH':d+':'+os.environ['PATH']},capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(r.stdout,'')
 def test_pgrep_error_is_failure(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'pgrep';p.write_text('#!/bin/sh\nexit 2\n');p.chmod(0o755)
   r=subprocess.run(['bash','-c',self.body('targets'),'_',d],env={**os.environ,'PATH':d+':'+os.environ['PATH']},capture_output=True,text=True)
   self.assertEqual(r.returncode,2)
 def test_unreadable_pid_not_empty(self):
  with tempfile.TemporaryDirectory() as d:
   (Path(d)/'api.pid').mkdir()
   r=subprocess.run(['bash','-c',self.body('targets'),'_',d],capture_output=True,text=True)
   self.assertNotEqual(r.returncode,0)
 def test_probe_actual_current_process(self):
  r=subprocess.run(['bash','-c',self.body('live-pids'),'_',str(os.getpid())],capture_output=True,text=True)
  self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(r.stdout.strip(),str(os.getpid()))
 def test_probe_nonexistent_process(self):
  r=subprocess.run(['bash','-c',self.body('live-pids'),'_','99999999'],capture_output=True,text=True)
  self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(r.stdout,'')
 def test_probe_unreadable_state_is_unknown(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'awk';p.write_text('#!/bin/sh\nexit 2\n');p.chmod(0o755)
   r=subprocess.run(['bash','-c',self.body('live-pids'),'_',str(os.getpid())],env={**os.environ,'PATH':d+':'+os.environ['PATH']},capture_output=True,text=True)
   self.assertEqual(r.returncode,74)

if __name__=='__main__': unittest.main(verbosity=2)
